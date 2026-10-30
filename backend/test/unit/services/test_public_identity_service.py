from datetime import timedelta
from contextlib import asynccontextmanager

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.utils.auth_middleware import _verify_api_key
from yuxi.services import public_identity_service as service
from yuxi.storage.postgres.models_business import (
    APIKey,
    Agent,
    Department,
    PublicApp,
    PublicAppKey,
    PublicEndUser,
    Tenant,
    TenantMembership,
    User,
    Base,
)
from yuxi.utils.datetime_utils import utc_now


@pytest.fixture
async def public_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync: Base.metadata.create_all(
                sync,
                tables=[
                    Tenant.__table__,
                    Department.__table__,
                    User.__table__,
                    TenantMembership.__table__,
                    PublicApp.__table__,
                    PublicAppKey.__table__,
                    PublicEndUser.__table__,
                    APIKey.__table__,
                    Agent.__table__,
                ],
            )
        )
        # This existing production index has a PostgreSQL-only partial predicate.
        await conn.execute(text("DROP INDEX uq_agents_default"))
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        db.add(Tenant(id=1, name="tenant"))
        db.add(Department(id=1, name="department", tenant_id=1))
        db.add(User(uid="owner", username="owner", role="admin", department_id=1, password_hash="unused"))
        db.add(TenantMembership(tenant_id=1, uid="owner", role="tenant_admin", status="active"))
        db.add_all(
            [
                PublicApp(id=name, tenant_id=1, owner_uid="owner", name=name, max_end_users=2)
                for name in ["app-a", "app-b"]
            ]
        )
        await db.commit()
        yield db
    await engine.dispose()


async def _key(db, app_id="app-a"):
    owner = await db.scalar(select(User).where(User.uid == "owner"))
    key, token = await service.create_app_key(db, user=owner, app_id=app_id, name="integration")
    await db.commit()
    return key, token


async def test_app_end_users_are_distinct_from_owner_and_other_app(public_db):
    db = public_db
    key, token = await _key(db)
    _, other = await _key(db, "app-b")
    first = await service.resolve_app_actor(db, token=token, external_id="customer")
    replay = await service.resolve_app_actor(db, token=token, external_id="customer")
    second = await service.resolve_app_actor(db, token=other, external_id="customer")
    assert first == replay and first.uid != second.uid and first.uid != "owner"
    user = await db.scalar(select(User).where(User.uid == first.uid))
    assert user.department_id is None and user.role == "user" and user.user_kind == "app_end_user"
    assert await _verify_api_key(token, db) == (None, None)
    assert key.last_used_at is not None
    assert await service.billing_owner_uid(db, first.uid) == "owner"
    billed = set(await db.scalars(service.billed_uids("owner")))
    assert billed == {"owner", first.uid, second.uid}


async def test_default_identity_cap_revocation_and_disabled_owner(public_db):
    db = public_db
    key, token = await _key(db)
    default = await service.resolve_app_actor(db, token=token)
    await service.resolve_app_actor(db, token=token, external_id="second")
    with pytest.raises(HTTPException) as limit:
        await service.resolve_app_actor(db, token=token, external_id="third")
    assert limit.value.status_code == 429
    key_id = key.id
    await db.rollback()
    key = await db.get(PublicAppKey, key_id)
    key.enabled = False
    await db.commit()
    with pytest.raises(HTTPException) as revoked:
        await service.resolve_app_actor(db, token=token)
    assert revoked.value.status_code == 401
    key.enabled = True
    key.expires_at = utc_now() - timedelta(seconds=1)
    await db.commit()
    with pytest.raises(HTTPException) as expired:
        await service.resolve_app_actor(db, token=token)
    assert expired.value.status_code == 401
    key.expires_at = utc_now() + timedelta(hours=1)
    (await db.scalar(select(User).where(User.uid == "owner"))).is_disabled = True
    await db.commit()
    with pytest.raises(HTTPException) as disabled:
        await service.resolve_app_actor(db, token=token)
    assert disabled.value.status_code == 403
    with pytest.raises(HTTPException):
        await service.app_principal(db, uid=default.uid)


@pytest.mark.parametrize("identity", ["", " trailing", "trailing ", "x" * 129, "control\n"])
async def test_invalid_end_user_ids_are_rejected_before_creation(public_db, identity):
    _, token = await _key(public_db)
    with pytest.raises(HTTPException) as invalid:
        await service.resolve_app_actor(public_db, token=token, external_id=identity)
    assert invalid.value.status_code == 422


async def test_worker_accepts_bound_end_user_and_rechecks_revocation(public_db, monkeypatch):
    from yuxi.services import run_worker

    db = public_db
    _, token = await _key(db)
    actor = await service.resolve_app_actor(db, token=token, external_id="worker")

    @asynccontextmanager
    async def context():
        yield db

    monkeypatch.setattr(run_worker.pg_manager, "get_async_session_context", context)
    user = await run_worker._load_user(actor.uid)
    assert user.uid == actor.uid and user.department_id is None
    membership = await db.scalar(select(TenantMembership).where(TenantMembership.uid == actor.uid))
    membership.status = "revoked"
    await db.commit()
    assert await run_worker._load_user(actor.uid) is None
    membership.status = "active"
    (await db.get(PublicApp, actor.app_id)).enabled = False
    await db.commit()
    assert await run_worker._load_user(actor.uid) is None
    assert (await run_worker._load_user("owner")).uid == "owner"
    (await db.scalar(select(User).where(User.uid == "owner"))).department_id = None
    await db.commit()
    assert await run_worker._load_user("owner") is None


async def test_app_agent_grants_intersect_owner_access_and_never_include_private(public_db):
    from yuxi.repositories.agent_repository import AgentRepository

    db = public_db
    _, token = await _key(db)
    actor = await service.resolve_app_actor(db, token=token)
    app = await db.get(PublicApp, actor.app_id)
    app.agent_slugs = ["shared", "private", "other-tenant"]
    for slug, tenant_id, access in [("shared", 1, "global"), ("private", 1, "user"), ("other-tenant", 2, "global")]:
        db.add(
            Agent(
                slug=slug,
                tenant_id=tenant_id,
                name=slug,
                backend_id="ChatbotAgent",
                created_by="owner",
                share_config={"access_level": access, "user_uids": ["owner"]},
            )
        )
    db.add(
        Agent(
            slug="not-granted",
            tenant_id=1,
            name="unlisted",
            backend_id="ChatbotAgent",
            created_by="owner",
            share_config={"access_level": "global"},
        )
    )
    await db.commit()
    user = await db.scalar(select(User).where(User.uid == actor.uid))
    repo = AgentRepository(db)
    assert (await repo.get_visible_by_slug(slug="shared", user=user, kind="main")).slug == "shared"
    for slug in ["private", "other-tenant", "not-granted"]:
        assert await repo.get_visible_by_slug(slug=slug, user=user, kind="main") is None
    app.agent_slugs = []
    await db.commit()
    assert await repo.get_visible_by_slug(slug="shared", user=user, kind="main") is None
    user.role = "superadmin"
    await db.commit()
    with pytest.raises(HTTPException) as promoted:
        await repo.list_visible(user=user)
    assert promoted.value.status_code == 403


async def test_model_credentials_require_explicit_app_delegation(public_db):
    db = public_db
    _, token = await _key(db)
    actor = await service.resolve_app_actor(db, token=token)
    assert await service.model_credential_uid(db, actor.uid) == actor.uid
    app = await db.get(PublicApp, actor.app_id)
    app.allow_owner_model_credentials = True
    await db.commit()
    assert await service.model_credential_uid(db, actor.uid) == "owner"
    app.enabled = False
    await db.commit()
    with pytest.raises(HTTPException):
        await service.model_credential_uid(db, actor.uid)


async def test_app_knowledge_grants_intersect_current_owner_acl_and_tenant(public_db, monkeypatch):
    from unittest.mock import AsyncMock

    from yuxi.knowledge.manager import KnowledgeBaseManager
    from yuxi.storage.postgres.manager import pg_manager

    db = public_db
    _, token = await _key(db)
    actor = await service.resolve_app_actor(db, token=token)
    app = await db.get(PublicApp, actor.app_id)
    app.kb_ids = ["shared", "cross-tenant", "other-department"]
    await db.commit()

    @asynccontextmanager
    async def context():
        yield db

    monkeypatch.setattr(pg_manager, "get_async_session_context", context)
    manager = object.__new__(KnowledgeBaseManager)
    databases = [
        {
            "kb_id": name,
            "tenant_id": tenant_id,
            "created_by": "another",
            "share_config": {"access_level": access, "department_ids": departments},
        }
        for name, tenant_id, access, departments in [
            ("shared", 1, "global", []),
            ("cross-tenant", 2, "global", []),
            ("other-department", 1, "department", [2]),
            ("not-granted", 1, "global", []),
        ]
    ]
    monkeypatch.setattr(manager, "get_databases", AsyncMock(return_value={"databases": databases}))
    user = await db.scalar(select(User).where(User.uid == actor.uid))
    assert [item["kb_id"] for item in (await manager.get_databases_by_user(user))["databases"]] == ["shared"]
    assert await manager.check_accessible({"uid": actor.uid, "role": "superadmin"}, "cross-tenant") is False
    # Even a platform admin owner cannot expand an APP across its tenant.
    owner = await db.scalar(select(User).where(User.uid == "owner"))
    owner.role = "superadmin"
    await db.commit()
    assert await manager.check_accessible({"uid": actor.uid}, "cross-tenant") is False
    app.kb_ids = []
    await db.commit()
    assert await manager.check_accessible({"uid": actor.uid}, "shared") is False


async def test_public_auth_uses_key_app_and_never_reinterprets_a_personal_key(public_db, monkeypatch):
    import hashlib
    from starlette.requests import Request

    from server.utils.public_auth import authenticate_public_request

    db = public_db
    _, token = await _key(db)

    def request(token, **headers):
        return Request(
            {
                "type": "http",
                "headers": [
                    (key.encode(), value.encode())
                    for key, value in {
                        "authorization": "Bearer " + token,
                        **headers,
                    }.items()
                ],
            }
        )

    actor = await authenticate_public_request(request(token, **{"x-end-user-id": "customer", "x-app-id": "forged"}), db)
    assert actor.app_id == "app-a" and actor.uid != "owner"
    owner = await db.scalar(select(User).where(User.uid == "owner"))
    for purpose in ["full", "desktop_legacy", "external_agent"]:
        value = "yxkey_" + purpose
        db.add(
            APIKey(
                user_id=owner.id,
                tenant_id=1,
                department_id=1,
                name=purpose,
                key_prefix=value[:14],
                key_hash=hashlib.sha256(value.encode()).hexdigest(),
                purpose=purpose,
                scopes=["*"],
                created_by="owner",
            )
        )
    await db.commit()
    for purpose in ["full", "desktop_legacy"]:
        personal = await authenticate_public_request(request("yxkey_" + purpose), db)
        assert personal.uid == "owner" and personal.app_id is None
        with pytest.raises(HTTPException) as invalid_header:
            await authenticate_public_request(request("yxkey_" + purpose, **{"x-end-user-id": "customer"}), db)
        assert invalid_header.value.status_code == 422
    with pytest.raises(HTTPException) as limited:
        await authenticate_public_request(request("yxkey_external_agent"), db)
    assert limited.value.status_code == 403
    monkeypatch.setattr("yuxi.utils.auth_utils.AuthUtils.verify_access_token", lambda _: {"sub": str(owner.id)})
    assert (await authenticate_public_request(request("jwt-test"), db)).uid == "owner"
    with pytest.raises(HTTPException):
        await authenticate_public_request(request("jwt-test", **{"x-end-user-id": "customer"}), db)
    (await db.get(PublicApp, actor.app_id)).enabled = False
    await db.commit()
    with pytest.raises(HTTPException) as revoked:
        await authenticate_public_request(request(token, **{"x-end-user-id": "customer"}), db)
    assert revoked.value.status_code == 403
