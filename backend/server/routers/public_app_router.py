"""Owner-scoped application grants and revocable public credentials."""

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import select, text

from server.utils.auth_middleware import get_db, get_required_user
from yuxi.services.principal import resolve_tenant_id
from yuxi.services.public_identity_service import create_app_key
from yuxi.services.public_query_service import unix_time
from yuxi.storage.postgres.models_business import PublicApp, PublicAppKey, PublicEndUser

apps = APIRouter(prefix="/user/apps", tags=["Public Applications"])


class AppSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    enabled: StrictBool = True
    agent_slugs: list[str] = Field(default_factory=list, max_length=100)
    kb_ids: list[str] = Field(default_factory=list, max_length=100)
    allow_owner_model_credentials: StrictBool = False
    max_end_users: int = Field(default=1000, ge=1, le=100000)


class KeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    expires_days: int = Field(default=90, ge=1, le=365)


class Enabled(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool


async def owner(request: Request, user=Depends(get_required_user)):
    if getattr(request.state, "api_key_purpose", None) is not None or user.user_kind != "human":
        raise HTTPException(403, "请使用个人登录会话管理应用")
    return user


async def owned_app(db, user, app_id, *, lock=False):
    stmt = select(PublicApp).where(
        PublicApp.id == app_id,
        PublicApp.owner_uid == user.uid,
        PublicApp.tenant_id == await resolve_tenant_id(db, user.uid),
    )
    if lock:
        stmt = stmt.with_for_update()
    app = await db.scalar(stmt)
    if app is None:
        raise HTTPException(404, "应用不存在")
    return app


def app_resource(app):
    return {
        "id": app.id,
        "name": app.name,
        "enabled": app.enabled,
        "agent_slugs": app.agent_slugs,
        "kb_ids": app.kb_ids,
        "allow_owner_model_credentials": app.allow_owner_model_credentials,
        "max_end_users": app.max_end_users,
        "created_at": unix_time(app.created_at),
    }


async def validate_grants(db, user, payload):
    from yuxi.repositories.agent_repository import AgentRepository

    visible = {
        agent.slug
        for agent in await AgentRepository(db).list_visible(user=user)
        if (agent.share_config or {}).get("access_level") != "user"
    }
    if not set(payload.agent_slugs) <= visible:
        raise HTTPException(422, "Agent 授权须来自当前可见且可共享的主智能体")
    if payload.kb_ids:
        from yuxi.knowledge.runtime import knowledge_base

        visible_kbs = {kb["kb_id"] for kb in (await knowledge_base.get_databases_by_user(user))["databases"]}
        if not set(payload.kb_ids) <= visible_kbs:
            raise HTTPException(422, "知识库授权须来自当前可见知识库")


@apps.get("")
async def list_apps(user=Depends(owner), db=Depends(get_db)):
    tenant_id = await resolve_tenant_id(db, user.uid)
    rows = list(
        await db.scalars(
            select(PublicApp)
            .where(PublicApp.owner_uid == user.uid, PublicApp.tenant_id == tenant_id)
            .order_by(PublicApp.created_at, PublicApp.id)
            .limit(100)
        )
    )
    return {"data": [app_resource(app) for app in rows]}


@apps.get("/grant-options")
async def grant_options(user=Depends(owner), db=Depends(get_db)):
    import os
    from yuxi.repositories.agent_repository import AgentRepository

    agents = [
        agent
        for agent in await AgentRepository(db).list_visible(user=user)
        if (agent.share_config or {}).get("access_level") != "user"
    ]
    kbs = []
    if os.getenv("LITE_MODE", "").lower() not in {"true", "1"}:
        from yuxi.knowledge.runtime import knowledge_base

        kbs = (await knowledge_base.get_databases_by_user(user))["databases"]
    return {
        "agents": [{"id": item.slug, "name": item.name} for item in agents],
        "knowledge_bases": [{"id": item["kb_id"], "name": item.get("name", item["kb_id"])} for item in kbs],
    }


@apps.get('/usage')
async def usage(days: int = Query(30, ge=1, le=365), user=Depends(owner), db=Depends(get_db)):
    from datetime import timedelta
    from yuxi.utils.datetime_utils import utc_now

    tenant = await resolve_tenant_id(db, user.uid)
    rows = (await db.execute(
        text('SELECT * FROM yuxi_public_app_usage(:uid,:tenant,:since)'),
        {'uid': user.uid, 'tenant': tenant, 'since': utc_now() - timedelta(days=days)},
    )).mappings().all()
    apps_by_id = {app.id: app for app in await db.scalars(select(PublicApp).where(
        PublicApp.owner_uid == user.uid, PublicApp.tenant_id == tenant,
    ))}
    data = [dict(row, name=apps_by_id[row['app_id']].name) for row in rows]
    counters = ('key_count', 'active_key_count', 'end_user_count', 'session_count',
                'input_count', 'run_count', 'total_tokens', 'estimated_runs')
    totals = {'app_count': len(data), **{name: sum(row[name] for row in data) for name in counters}}
    return {'days': days, 'apps': data, 'totals': totals}


@apps.post("", status_code=201)
async def create(payload: AppSettings, user=Depends(owner), db=Depends(get_db)):
    await validate_grants(db, user, payload)
    app = PublicApp(
        id=uuid4().hex, owner_uid=user.uid, tenant_id=await resolve_tenant_id(db, user.uid), **payload.model_dump()
    )
    db.add(app)
    await db.commit()
    return app_resource(app)


@apps.put("/{app_id}")
async def update(app_id: str, payload: AppSettings, user=Depends(owner), db=Depends(get_db)):
    app = await owned_app(db, user, app_id, lock=True)
    await validate_grants(db, user, payload)
    for name, value in payload.model_dump().items():
        setattr(app, name, value)
    await db.commit()
    return app_resource(app)


def key_resource(key):
    return {
        "id": key.id,
        "name": key.name,
        "key_prefix": key.key_prefix,
        "enabled": key.enabled,
        "expires_at": unix_time(key.expires_at),
        "created_at": unix_time(key.created_at),
        "last_used_at": unix_time(key.last_used_at),
    }


@apps.get("/{app_id}/keys")
async def keys(app_id: str, user=Depends(owner), db=Depends(get_db)):
    await owned_app(db, user, app_id)
    rows = list(
        await db.scalars(
            select(PublicAppKey)
            .where(PublicAppKey.app_id == app_id)
            .order_by(PublicAppKey.created_at, PublicAppKey.id)
            .limit(100)
        )
    )
    return {"data": [key_resource(key) for key in rows]}


@apps.post("/{app_id}/keys", status_code=201)
async def issue_key(app_id: str, payload: KeyCreate, user=Depends(owner), db=Depends(get_db)):
    await owned_app(db, user, app_id, lock=True)
    key, token = await create_app_key(db, user=user, app_id=app_id, **payload.model_dump())
    await db.commit()
    return {**key_resource(key), "key": token}


@apps.put("/{app_id}/keys/{key_id}")
async def key_enabled(app_id: str, key_id: str, payload: Enabled, user=Depends(owner), db=Depends(get_db)):
    await owned_app(db, user, app_id, lock=True)
    key = await db.scalar(select(PublicAppKey).where(PublicAppKey.id == key_id, PublicAppKey.app_id == app_id))
    if key is None:
        raise HTTPException(404, "凭据不存在")
    key.enabled = payload.enabled
    await db.commit()
    return key_resource(key)


@apps.get("/{app_id}/end-users")
async def end_users(
    app_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    user=Depends(owner),
    db=Depends(get_db),
):
    await owned_app(db, user, app_id)
    rows = list(
        await db.scalars(
            select(PublicEndUser)
            .where(PublicEndUser.app_id == app_id)
            .order_by(PublicEndUser.created_at, PublicEndUser.uid)
            .offset(offset)
            .limit(limit)
        )
    )
    return {
        "data": [
            {
                "uid": row.uid,
                "external_id": row.external_id,
                "enabled": row.enabled,
                "created_at": unix_time(row.created_at),
            }
            for row in rows
        ]
    }


@apps.put("/{app_id}/end-users/{uid}")
async def end_user_enabled(app_id: str, uid: str, payload: Enabled, user=Depends(owner), db=Depends(get_db)):
    await owned_app(db, user, app_id, lock=True)
    subject = await db.scalar(select(PublicEndUser).where(PublicEndUser.app_id == app_id, PublicEndUser.uid == uid))
    if subject is None:
        raise HTTPException(404, "终端用户不存在")
    subject.enabled = payload.enabled
    await db.commit()
    return {"uid": subject.uid, "enabled": subject.enabled}
