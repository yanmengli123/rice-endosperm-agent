"""桥模式多用户隔离：BROWSER_SKILL_ALLOWED_UIDS 配置后,非白名单用户 fail-closed。"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.services import browser_gateway_service as svc
from yuxi.storage.postgres.models_business import Base, Department, Tenant, TenantMembership, User

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


@pytest_asyncio.fixture()
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        dept = Department(name="默认部门", tenant_id=1)
        user = User(username="u", uid="u", password_hash="$argon2id$x", role="user", department=dept)
        db.add_all([Tenant(id=1, name="t", status="active"), dept, user, TenantMembership(tenant_id=1, uid="u", role="member", status="active")])
        await db.commit()
        yield db
    await engine.dispose()


async def _dispatch(db):
    return await svc.dispatch_browser_command(
        db, tenant_id=1, uid="u", run_id=None, op="get_status", payload={}, count_budget=False, audit=False
    )


async def test_bridge_uid_not_in_allowlist_rejected(session, monkeypatch):
    from yuxi.services import browser_skill_bridge

    monkeypatch.setattr(browser_skill_bridge, "BROWSER_SKILL_BRIDGE_URL", "http://x")
    monkeypatch.setattr(browser_skill_bridge, "BROWSER_SKILL_BRIDGE_SECRET_FILE", "/dev/null")
    monkeypatch.setattr(svc, "BROWSER_SKILL_ALLOWED_UIDS", frozenset({"someone-else"}))
    with pytest.raises(svc.BrowserGatewayError) as denied:
        await _dispatch(session)
    assert denied.value.code == "BROWSER_FORBIDDEN_FOR_USER"
    assert denied.value.http_status == 403


async def test_bridge_uid_in_allowlist_passes_gate(session, monkeypatch):
    from yuxi.services import browser_skill_bridge

    monkeypatch.setattr(browser_skill_bridge, "BROWSER_SKILL_BRIDGE_URL", "http://x")
    monkeypatch.setattr(browser_skill_bridge, "BROWSER_SKILL_BRIDGE_SECRET_FILE", "/dev/null")
    monkeypatch.setattr(svc, "BROWSER_SKILL_ALLOWED_UIDS", frozenset({"u"}))

    async def _fake_dispatch(**kwargs):
        return {"tabs": []}

    monkeypatch.setattr(browser_skill_bridge, "dispatch_browser_skill", _fake_dispatch)
    result = await _dispatch(session)
    assert result == {"tabs": []}


async def test_empty_allowlist_keeps_single_workstation_trust(session, monkeypatch):
    from yuxi.services import browser_skill_bridge

    monkeypatch.setattr(browser_skill_bridge, "BROWSER_SKILL_BRIDGE_URL", "http://x")
    monkeypatch.setattr(browser_skill_bridge, "BROWSER_SKILL_BRIDGE_SECRET_FILE", "/dev/null")
    monkeypatch.setattr(svc, "BROWSER_SKILL_ALLOWED_UIDS", frozenset())

    async def _fake_dispatch(**kwargs):
        return {"ok": True}

    monkeypatch.setattr(browser_skill_bridge, "dispatch_browser_skill", _fake_dispatch)
    assert await _dispatch(session) == {"ok": True}
