"""APP credentials and end-user identities stay outside product credentials."""

import hashlib
import secrets
from dataclasses import dataclass
from contextlib import asynccontextmanager
from datetime import UTC, timedelta
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import func, select

from yuxi.services.principal import resolve_tenant_id
from yuxi.storage.postgres.models_business import (
    PublicApp,
    PublicAppKey,
    PublicEndUser,
    TenantMembership,
    User,
)
from yuxi.utils.datetime_utils import utc_now


@dataclass(frozen=True)
class PublicActor:
    uid: str
    tenant_id: int
    app_id: str | None = None
    owner_uid: str | None = None
    key_id: str | None = None


async def app_principal(db, *, uid):
    """Recheck the binding, APP, owner and tenant before each new execution."""
    row = (
        await db.execute(
            select(PublicEndUser, PublicApp, User)
            .join(PublicApp, PublicEndUser.app_id == PublicApp.id)
            .join(User, PublicApp.owner_uid == User.uid)
            .where(PublicEndUser.uid == uid)
            .execution_options(populate_existing=True)
        )
    ).first()
    if row is None:
        raise HTTPException(403, "终端用户绑定不存在")
    end_user, app, owner = row
    subject = await db.scalar(select(User).where(User.uid == uid).execution_options(populate_existing=True))
    if (
        subject is None
        or subject.is_deleted
        or subject.is_disabled
        or subject.role != "user"
        or subject.department_id is not None
        or subject.user_kind != "app_end_user"
        or owner.user_kind != "human"
        or not end_user.enabled
        or not app.enabled
        or owner.is_deleted
        or owner.is_disabled
        or end_user.tenant_id != app.tenant_id
    ):
        raise HTTPException(403, "应用或终端用户已停用")
    from yuxi.services.principal import _resolve_active_membership

    if (await _resolve_active_membership(db, owner.uid)).tenant_id != app.tenant_id:
        raise HTTPException(403, "应用所有者的租户归属已变化")
    if (await _resolve_active_membership(db, uid)).tenant_id != app.tenant_id:
        raise HTTPException(403, "终端用户的租户归属已变化")
    return app, owner


async def resolve_app_actor(db, *, token, external_id=None):
    identity = "__default__" if external_id is None else external_id
    if not identity or identity != identity.strip() or len(identity) > 128 or any(ord(char) < 32 for char in identity):
        raise HTTPException(422, "X-End-User-Id 须为 1–128 个无首尾空白或控制字符的字符")
    key = await db.scalar(
        select(PublicAppKey)
        .where(PublicAppKey.key_hash == hashlib.sha256(token.encode()).hexdigest())
        .execution_options(populate_existing=True)
    )
    if key is None:
        return None
    expires = key.expires_at
    if expires and expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)  # SQLite drops the timezone of this TIMESTAMPTZ field.
    if not key.enabled or (expires and expires <= utc_now()):
        raise HTTPException(401, "APP 凭据已失效")
    # This lock serializes first use of an external identity and the account cap.
    app = await db.scalar(
        select(PublicApp).where(PublicApp.id == key.app_id).with_for_update().execution_options(populate_existing=True)
    )
    if not app or not app.enabled:
        raise HTTPException(403, "应用已停用")
    owner = await db.scalar(select(User).where(User.uid == app.owner_uid))
    if not owner or owner.is_deleted or owner.is_disabled or (owner.user_kind or "human") != "human":
        raise HTTPException(403, "应用所有者已不可用")
    if await resolve_tenant_id(db, owner.uid) != app.tenant_id:
        raise HTTPException(403, "应用所有者的租户归属已变化")
    end_user = await db.scalar(
        select(PublicEndUser).where(PublicEndUser.app_id == app.id, PublicEndUser.external_id == identity)
    )
    if end_user is None:
        count = await db.scalar(select(func.count()).select_from(PublicEndUser).where(PublicEndUser.app_id == app.id))
        if count >= app.max_end_users:
            raise HTTPException(429, "应用终端用户数量已达上限")
        uid = "app_" + uuid4().hex
        # No product password or department membership is inherited from the owner.
        user = User(
            uid=uid,
            username=uid,
            password_hash="!external-account:" + secrets.token_hex(32),
            user_kind="app_end_user",
            role="user",
        )
        db.add(user)
        await db.flush()
        db.add(TenantMembership(tenant_id=app.tenant_id, uid=uid, role="member", status="active"))
        end_user = PublicEndUser(uid=uid, app_id=app.id, tenant_id=app.tenant_id, external_id=identity)
        db.add(end_user)
        await db.flush()
    user = await db.scalar(select(User).where(User.uid == end_user.uid))
    if (
        not end_user.enabled
        or end_user.tenant_id != app.tenant_id
        or not user
        or user.is_deleted
        or user.is_disabled
        or user.role != "user"
        or user.department_id is not None
        or user.user_kind != "app_end_user"
    ):
        raise HTTPException(403, "终端用户已停用")
    key.last_used_at = utc_now()
    actor = PublicActor(
        uid=end_user.uid, tenant_id=app.tenant_id, app_id=app.id, owner_uid=app.owner_uid, key_id=key.id
    )
    # Owner validation must not leave its RLS identity on the end user's request.
    if await resolve_tenant_id(db, end_user.uid) != app.tenant_id:
        raise HTTPException(403, "终端用户的租户归属已变化")
    await db.commit()
    return actor


async def create_app_key(db, *, user, app_id, name, expires_days=90):
    app = await db.scalar(select(PublicApp).where(PublicApp.id == app_id, PublicApp.owner_uid == str(user.uid)))
    if not app or app.tenant_id != await resolve_tenant_id(db, str(user.uid)):
        raise HTTPException(404, "应用不存在")
    if not app.enabled:
        raise HTTPException(409, "应用已停用")
    token = "yxkey_" + secrets.token_hex(24)
    key = PublicAppKey(
        id=uuid4().hex,
        app_id=app.id,
        name=name,
        key_hash=hashlib.sha256(token.encode()).hexdigest(),
        key_prefix=token[:14],
        expires_at=utc_now() + timedelta(days=expires_days),
    )
    db.add(key)
    await db.flush()
    return key, token


async def billing_owner_uid(db, uid):
    """All APP end users share their owner's entitlement and accounting budget."""
    owner = await db.scalar(
        select(PublicApp.owner_uid)
        .join(PublicEndUser, PublicEndUser.app_id == PublicApp.id)
        .where(PublicEndUser.uid == uid)
    )
    return owner or uid


def billed_uids(owner_uid):
    return (
        select(PublicEndUser.uid)
        .join(PublicApp, PublicEndUser.app_id == PublicApp.id)
        .where(PublicApp.owner_uid == owner_uid)
        .union(select(User.uid).where(User.uid == owner_uid))
    )


async def model_credential_uid(db, uid):
    if not uid.startswith(("app_", "app:")):
        return uid
    if not await is_app_end_user(db, uid):
        return uid
    app, owner = await app_principal(db, uid=uid)
    return owner.uid if app.allow_owner_model_credentials else uid


async def is_app_end_user(db, uid):
    return await db.scalar(select(User.user_kind).where(User.uid == uid)) == "app_end_user"


@asynccontextmanager
async def model_credential_reader(db, uid):
    owner_uid = await model_credential_uid(db, uid)
    if owner_uid == uid or db.get_bind().dialect.name != "postgresql":
        yield db, owner_uid
        return
    from yuxi.storage.postgres.manager import pg_manager
    from yuxi.storage.postgres.runtime_security import actor

    app, _owner = await app_principal(db, uid=uid)
    # This narrow read uses a separate transaction: the caller's APP transaction
    # and its RLS identity remain unchanged, including while opening a frozen key.
    with actor(app.tenant_id, owner_uid):
        async with pg_manager.get_async_session_context() as reader:
            yield reader, owner_uid
