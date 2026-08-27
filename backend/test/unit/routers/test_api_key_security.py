from __future__ import annotations

import importlib
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.routers.auth_router import (
    DesktopLoginRequest,
    UserUpdate,
    delete_user,
    login_desktop_client,
    login_for_access_token,
    update_user,
)
from server.routers.user_router import APIKeyCreate, _admin_guard, create_api_key, disable_user, enable_user
from server.utils.auth_middleware import _verify_api_key, get_current_user
from yuxi.repositories import user_repository as user_repository_module
from yuxi.repositories.user_repository import UserRepository
from yuxi.storage.postgres.models_business import APIKey, AgentRun, Base, Department, Tenant, TenantMembership, User
from yuxi.utils.auth_utils import AuthUtils

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]
user_router_module = importlib.import_module("server.routers.user_router")


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _FakeApiKeySession:
    def __init__(self, api_key: APIKey):
        self.api_key = api_key
        self.execute_calls = 0

    async def execute(self, _statement):
        self.execute_calls += 1
        return _ScalarResult(self.api_key)


@pytest_asyncio.fixture()
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        dept_a = Department(name="Dept A", tenant_id=1)
        dept_b = Department(name="Dept B", tenant_id=1)
        superadmin = User(
            username="Super Admin",
            uid="superadmin",
            password_hash="$argon2id$placeholder",
            role="superadmin",
            department=dept_a,
        )
        dept_b_admin = User(
            username="Dept B Admin",
            uid="dept_b_admin",
            password_hash="$argon2id$placeholder",
            role="admin",
            department=dept_b,
        )
        regular_user = User(
            username="Regular",
            uid="regular",
            password_hash="$argon2id$placeholder",
            role="user",
            department=dept_a,
        )
        deleted_user = User(
            username="Deleted",
            uid="deleted",
            password_hash="$argon2id$placeholder",
            role="user",
            department=dept_a,
            is_deleted=1,
        )
        db.add_all([dept_a, dept_b, superadmin, dept_b_admin, regular_user, deleted_user])
        # P5 严格租户解析：种子默认租户与各用户成员关系
        tenant_row = Tenant(id=1, name="默认企业", status="active")
        memberships = [
            TenantMembership(
                tenant_id=1,
                uid=u.uid,
                role="platform_admin" if u.role == "superadmin" else "tenant_admin" if u.role == "admin" else "member",
                status="active",
            )
            for u in (superadmin, dept_b_admin, regular_user)
        ]
        db.add_all([tenant_row, *memberships])
        await db.commit()
        for item in [dept_a, dept_b, superadmin, dept_b_admin, regular_user, deleted_user]:
            await db.refresh(item)
        yield {
            "db": db,
            "dept_a": dept_a,
            "dept_b": dept_b,
            "superadmin": superadmin,
            "dept_b_admin": dept_b_admin,
            "regular_user": regular_user,
            "deleted_user": deleted_user,
        }
    await engine.dispose()


async def test_api_key_rejects_deleted_bound_user_without_department_or_superadmin_fallback(session):
    db = session["db"]
    secret, key_hash, key_prefix = AuthUtils.generate_api_key()
    api_key = APIKey(
        key_hash=key_hash,
        key_prefix=key_prefix,
        name="deleted user key",
        user_id=session["deleted_user"].id,
        department_id=session["dept_b"].id,
        created_by=str(session["deleted_user"].id),
    )
    db.add(api_key)
    await db.commit()

    user, verified_key = await _verify_api_key(secret, db)

    assert user is None
    assert verified_key is None


async def test_api_key_without_user_binding_is_rejected_before_department_mapping(session):
    secret, key_hash, key_prefix = AuthUtils.generate_api_key()
    api_key = APIKey(
        key_hash=key_hash,
        key_prefix=key_prefix,
        name="department key",
        user_id=None,
        department_id=session["dept_b"].id,
        created_by=str(session["superadmin"].id),
    )
    fake_db = _FakeApiKeySession(api_key)

    user, verified_key = await _verify_api_key(secret, fake_db)

    assert user is None
    assert verified_key is None
    assert fake_db.execute_calls == 1


async def test_create_api_key_rejects_mismatched_department(session):
    db = session["db"]

    with pytest.raises(HTTPException) as exc:
        await create_api_key(
            APIKeyCreate(name="wrong department", department_id=session["dept_b"].id),
            current_user=session["regular_user"],
            db=db,
        )

    assert exc.value.status_code == 403


async def test_create_api_key_allows_current_user_department(session):
    db = session["db"]

    response = await create_api_key(
        APIKeyCreate(name="own department", department_id=session["dept_a"].id),
        current_user=session["regular_user"],
        db=db,
    )

    assert response.api_key.user_id == session["regular_user"].id
    assert response.api_key.department_id == session["dept_a"].id
    assert response.secret.startswith(response.api_key.key_prefix)


async def test_api_key_rejects_stale_cross_department_binding(session):
    db = session["db"]
    secret, key_hash, key_prefix = AuthUtils.generate_api_key()
    api_key = APIKey(
        key_hash=key_hash,
        key_prefix=key_prefix,
        name="stale department binding",
        user_id=session["regular_user"].id,
        department_id=session["dept_b"].id,
        created_by=str(session["regular_user"].id),
    )
    db.add(api_key)
    await db.commit()

    user, verified_key = await _verify_api_key(secret, db)

    assert user is None
    assert verified_key is None


async def test_department_change_explicitly_disables_existing_api_keys(session):
    db = session["db"]
    user = session["regular_user"]
    _secret, key_hash, key_prefix = AuthUtils.generate_api_key()
    api_key = APIKey(
        key_hash=key_hash,
        key_prefix=key_prefix,
        name="old department key",
        user_id=user.id,
        department_id=user.department_id,
        created_by=str(user.id),
        is_enabled=True,
    )
    db.add(api_key)
    await db.commit()

    await update_user(
        user.id,
        UserUpdate(department_id=session["dept_b"].id),
        Request({"type": "http", "method": "PUT", "path": "/", "headers": []}),
        current_user=session["superadmin"],
        db=db,
    )
    await db.refresh(api_key)
    await db.refresh(user)

    assert user.department_id == session["dept_b"].id
    assert api_key.is_enabled is False


async def test_auth_version_invalidates_existing_jwt(session):
    db = session["db"]
    user = session["regular_user"]
    token = AuthUtils.create_access_token({"sub": str(user.id), "auth_version": user.auth_version})
    user.auth_version += 1
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await get_current_user(authorization=f"Bearer {token}", db=db)

    assert exc.value.status_code == 401


async def test_malformed_signed_jwt_subject_is_rejected_as_unauthorized(session):
    token = AuthUtils.create_access_token({"sub": "not-an-integer", "auth_version": 0})

    with pytest.raises(HTTPException) as exc:
        await get_current_user(authorization=f"Bearer {token}", db=session["db"])

    assert exc.value.status_code == 401


async def test_login_does_not_disclose_whether_identifier_exists(session):
    db = session["db"]
    user = session["regular_user"]
    user.password_hash = AuthUtils.hash_password("correct-password")
    await db.commit()

    class Form:
        password = "wrong-password"

    existing_form = Form()
    existing_form.username = user.uid
    missing_form = Form()
    missing_form.username = "missing-user"

    with pytest.raises(HTTPException) as existing_exc:
        await login_for_access_token(existing_form, db)
    with pytest.raises(HTTPException) as missing_exc:
        await login_for_access_token(missing_form, db)

    assert existing_exc.value.status_code == missing_exc.value.status_code == 401
    assert existing_exc.value.detail == missing_exc.value.detail == "登录标识或密码错误"


async def test_atomic_desktop_login_uses_server_account_scope_and_matching_key(session):
    db = session["db"]
    user = session["regular_user"]
    user.password_hash = AuthUtils.hash_password("desktop-password")
    secret, key_hash, key_prefix = AuthUtils.generate_api_key()
    api_key = APIKey(
        key_hash=key_hash,
        key_prefix=key_prefix,
        name="desktop login",
        user_id=user.id,
        department_id=user.department_id,
        tenant_id=1,
        purpose="desktop_legacy",
        created_by=str(session["superadmin"].id),
    )
    db.add(api_key)
    await db.commit()
    await db.refresh(api_key)

    response = await login_desktop_client(
        DesktopLoginRequest(login_id=user.username, password="desktop-password", api_key=secret),
        Request({"type": "http", "method": "POST", "path": "/api/auth/desktop/login", "headers": []}),
        db,
    )

    assert response.uid == user.uid
    assert response.username == user.username
    assert response.account_scope_id == user.account_scope_id
    assert response.api_key_id == api_key.id


async def test_atomic_desktop_login_rejects_key_owned_by_another_user(session):
    db = session["db"]
    user = session["regular_user"]
    user.password_hash = AuthUtils.hash_password("desktop-password")
    secret, key_hash, key_prefix = AuthUtils.generate_api_key()
    api_key = APIKey(
        key_hash=key_hash,
        key_prefix=key_prefix,
        name="other user key",
        user_id=session["superadmin"].id,
        department_id=session["superadmin"].department_id,
        tenant_id=1,
        purpose="desktop_legacy",
        created_by=str(session["superadmin"].id),
    )
    db.add(api_key)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await login_desktop_client(
            DesktopLoginRequest(login_id=user.uid, password="desktop-password", api_key=secret),
            Request({"type": "http", "method": "POST", "path": "/api/auth/desktop/login", "headers": []}),
            db,
        )

    assert exc.value.status_code == 401
    assert exc.value.detail == "登录信息或 API Key 错误"


async def test_department_admin_cannot_manage_user_from_another_department(session):
    with pytest.raises(HTTPException) as exc:
        _admin_guard(session["dept_b_admin"], session["regular_user"])

    assert exc.value.status_code == 403


async def test_enabling_user_does_not_restore_individually_revoked_key(session):
    db = session["db"]
    user = session["regular_user"]
    user.is_disabled = True
    _secret, key_hash, key_prefix = AuthUtils.generate_api_key()
    api_key = APIKey(
        key_hash=key_hash,
        key_prefix=key_prefix,
        name="revoked key",
        user_id=user.id,
        department_id=user.department_id,
        created_by=str(user.id),
        is_enabled=False,
    )
    db.add(api_key)
    await db.commit()

    await enable_user(user.uid, current_user=session["superadmin"], db=db)
    await db.refresh(api_key)

    assert user.is_disabled is False
    assert api_key.is_enabled is False


async def test_disabling_user_publishes_cancel_for_active_runs(session, monkeypatch):
    db = session["db"]
    user = session["regular_user"]
    run = AgentRun(
        id="run-disable-test",
        conversation_thread_id="thread-disable-test",
        agent_slug="default-chatbot",
        uid=user.uid,
        status="running",
        request_id="request-disable-test-0001",
        input_payload={},
    )
    db.add(run)
    await db.commit()
    published = []

    async def fake_publish(run_id):
        published.append(run_id)

    monkeypatch.setattr(user_router_module, "publish_cancel_signal", fake_publish)
    result = await disable_user(user.uid, current_user=session["superadmin"], db=db)
    await db.refresh(run)

    assert result["cancelled_runs"] == 1
    assert run.status == "cancel_requested"
    assert published == [run.id]


async def test_delete_user_disables_owned_api_keys(session):
    db = session["db"]
    _secret, key_hash, key_prefix = AuthUtils.generate_api_key()
    api_key = APIKey(
        key_hash=key_hash,
        key_prefix=key_prefix,
        name="owned key",
        user_id=session["regular_user"].id,
        created_by=str(session["regular_user"].id),
    )
    db.add(api_key)
    await db.commit()
    await db.refresh(api_key)

    result = await delete_user(session["regular_user"].id, None, session["superadmin"], db)
    await db.refresh(api_key)

    assert result["success"] is True
    assert api_key.is_enabled is False


async def test_user_repository_soft_delete_disables_owned_api_keys(session, monkeypatch):
    db = session["db"]
    _secret, key_hash, key_prefix = AuthUtils.generate_api_key()
    api_key = APIKey(
        key_hash=key_hash,
        key_prefix=key_prefix,
        name="repository owned key",
        user_id=session["regular_user"].id,
        created_by=str(session["regular_user"].id),
    )
    db.add(api_key)
    await db.commit()
    await db.refresh(api_key)

    @asynccontextmanager
    async def fake_session_context():
        yield db
        await db.commit()

    monkeypatch.setattr(user_repository_module.pg_manager, "get_async_session_context", fake_session_context)

    assert await UserRepository().soft_delete(session["regular_user"].id) is True
    await db.refresh(api_key)

    assert api_key.is_enabled is False
