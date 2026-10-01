"""本机浏览器网关单测：配对生命周期、设备替换/轮换、连接注册表中继、门控与预算。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.datastructures import Headers

from yuxi.services.browser_gateway_service import (
    BROWSER_PROTOCOL_VERSION,
    BrowserConnectionHandle,
    BrowserConnectionRegistry,
    BrowserGatewayError,
    authenticate_extension,
    authorize_pairing,
    browser_not_paired,
    browser_offline,
    browser_timeout,
    count_run_commands,
    create_pairing_link,
    dispatch_browser_command,
    get_browser_connection_registry,
    record_command,
    validate_navigate_url,
)
from yuxi.storage.postgres.models_business import (
    Base,
    BrowserDeviceAuthorization,
    BrowserPairingLink,
    Department,
    Tenant,
    TenantMembership,
    User,
)

pytestmark = [pytest.mark.unit]


@pytest_asyncio.fixture(autouse=True)
async def _reset_redis_loop_binding():
    """全局异步 Redis 客户端绑定首次使用时的 event loop；测试各自新 loop，
    用后必须关闭，否则下一个测试复用旧连接报 attached to a different loop。"""
    yield
    from yuxi.storage.redis import close_async_redis_client

    try:
        await close_async_redis_client()
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture(autouse=True)
def disable_browser_skill_bridge(monkeypatch):
    """Unit tests use the in-process extension transport unless explicitly enabled."""
    from yuxi.services import browser_skill_bridge

    monkeypatch.setattr(browser_skill_bridge, "BROWSER_SKILL_BRIDGE_URL", "")
    monkeypatch.setattr(browser_skill_bridge, "BROWSER_SKILL_BRIDGE_SECRET_FILE", "")


@pytest_asyncio.fixture()
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        dept = Department(name="默认部门", tenant_id=1)
        user = User(
            username="Admin",
            uid="admin",
            password_hash="$argon2id$placeholder",
            role="superadmin",
            department=dept,
        )
        tenant = Tenant(id=1, name="默认企业", status="active")
        membership = TenantMembership(tenant_id=1, uid=user.uid, role="platform_admin", status="active")
        db.add_all([tenant, membership, dept, user])
        await db.commit()
        yield db, user
    await engine.dispose()


class FakeWebSocket:
    """记录发送帧并在 send_text 时按脚本自动回帧的假 WS。

    回帧解析依赖 registry 引用：使用独立 BrowserConnectionRegistry 的用例
    必须显式传入，避免解析到模块级单例。
    """

    def __init__(self, script=None, registry=None):
        self.sent: list[str] = []
        self.closed: int | None = None
        self.script = script or {}
        self.registry = registry

    async def send_text(self, text: str) -> None:
        self.sent.append(text)
        frame = json.loads(text)
        responder = self.script.get(frame.get("op"))
        if responder is not None:
            payload = responder
            (self.registry or get_browser_connection_registry()).resolve(frame["id"], payload)

    async def close(self, code: int = 1000) -> None:
        self.closed = code


def _handle(ws, device_id="dev1", tenant_id=1, uid="admin"):
    return BrowserConnectionHandle(
        websocket=ws,
        device_id=device_id,
        tenant_id=tenant_id,
        uid=uid,
        device_name="Chrome on Windows",
    )


# ---------------------------------------------------------------------------
# 配对生命周期
# ---------------------------------------------------------------------------


async def test_pairing_lifecycle_authorize_once(session):
    db, _user = session
    link = await create_pairing_link(db, uid="admin", tenant_id=1)
    assert link["code"].startswith("brp_")

    result = await authorize_pairing(db, code=link["code"], device_name="Chrome on Windows", extension_version="0.1.0")
    assert result["device_token"].startswith("brt_")
    assert result["protocol_version"] == BROWSER_PROTOCOL_VERSION

    # 同码二次使用必须失败（一次性）
    with pytest.raises(BrowserGatewayError) as consumed:
        await authorize_pairing(db, code=link["code"], device_name="again", extension_version=None)
    assert consumed.value.code == "pairing_link_consumed"


async def test_pairing_unknown_code_rejected(session):
    db, _user = session
    with pytest.raises(BrowserGatewayError) as missing:
        await authorize_pairing(db, code="brp_does_not_exist", device_name="x", extension_version=None)
    assert missing.value.code == "pairing_link_invalid"


async def test_pairing_link_expired(session):
    db, _user = session
    link = await create_pairing_link(db, uid="admin", tenant_id=1)
    row = (await db.execute(select(BrowserPairingLink).filter(BrowserPairingLink.code_hash.is_not(None)))).scalar_one()
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db.commit()

    with pytest.raises(BrowserGatewayError) as expired:
        await authorize_pairing(db, code=link["code"], device_name="x", extension_version=None)
    assert expired.value.code == "pairing_link_expired"


async def test_new_authorization_replaces_old_device(session):
    db, _user = session
    first = await create_pairing_link(db, uid="admin", tenant_id=1)
    token_a = (await authorize_pairing(db, code=first["code"], device_name="A", extension_version=None))["device_token"]

    second = await create_pairing_link(db, uid="admin", tenant_id=1)
    result_b = await authorize_pairing(db, code=second["code"], device_name="B", extension_version=None)

    # 旧设备令牌立即失效（替换语义），新设备可用
    with pytest.raises(BrowserGatewayError) as revoked:
        await authenticate_extension(db, device_token=token_a)
    assert revoked.value.http_status == 403

    auth, rotated = await authenticate_extension(db, device_token=result_b["device_token"])
    assert auth.device_id == result_b["device_id"]
    assert rotated is None

    actives = (
        (
            await db.execute(
                select(BrowserDeviceAuthorization).filter(
                    BrowserDeviceAuthorization.status == BrowserDeviceAuthorization.STATUS_ACTIVE
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(actives) == 1
    assert actives[0].device_id == result_b["device_id"]


async def test_token_rotation_when_near_expiry(session):
    db, _user = session
    link = await create_pairing_link(db, uid="admin", tenant_id=1)
    authed = await authorize_pairing(db, code=link["code"], device_name="A", extension_version=None)

    row = (
        await db.execute(
            select(BrowserDeviceAuthorization).filter(BrowserDeviceAuthorization.device_id == authed["device_id"])
        )
    ).scalar_one()
    row.token_expires_at = datetime.now(UTC) + timedelta(days=10)  # 余量 < 60 天 → 触发轮换
    await db.commit()

    auth, rotated = await authenticate_extension(db, device_token=authed["device_token"])
    assert rotated is not None
    assert rotated["device_token"].startswith("brt_")
    # 旧令牌哈希已被替换
    with pytest.raises(BrowserGatewayError):
        await authenticate_extension(db, device_token=authed["device_token"])
    new_auth, rotated_again = await authenticate_extension(db, device_token=rotated["device_token"])
    assert new_auth.device_id == auth.device_id
    assert rotated_again is None


# ---------------------------------------------------------------------------
# 连接注册表与命令中继
# ---------------------------------------------------------------------------


async def test_registry_dispatch_roundtrip():
    registry = BrowserConnectionRegistry()
    ws = FakeWebSocket(script={"get_status": {"ok": True, "result": {"tabs": []}}}, registry=registry)
    registry.replace_connection(_handle(ws, device_id="dev1"))

    result = await registry.dispatch("dev1", op="get_status", payload={}, timeout_s=5)
    assert result == {"tabs": []}
    assert ws.sent and json.loads(ws.sent[0])["type"] == "cmd"


async def test_registry_dispatch_timeout():
    registry = BrowserConnectionRegistry()
    ws = FakeWebSocket(registry=registry)  # 不回帧
    registry.replace_connection(_handle(ws, device_id="dev1"))

    with pytest.raises(BrowserGatewayError) as timeout:
        await registry.dispatch("dev1", op="get_status", payload={}, timeout_s=0.05)
    assert timeout.value.code == "BROWSER_TIMEOUT"


async def test_registry_dispatch_offline():
    registry = BrowserConnectionRegistry()
    with pytest.raises(BrowserGatewayError) as offline:
        await registry.dispatch("missing", op="get_status", payload={}, timeout_s=1)
    assert offline.value.code == "BROWSER_OFFLINE"


def test_get_for_user_scopes_by_tenant_and_uid():
    registry = BrowserConnectionRegistry()
    registry.replace_connection(_handle(FakeWebSocket(), device_id="dev1", tenant_id=1, uid="admin"))
    assert registry.get_for_user(1, "admin") is not None
    assert registry.get_for_user(1, "other") is None
    assert registry.get_for_user(2, "admin") is None


# ---------------------------------------------------------------------------
# dispatch_browser_command 门控 / 预算 / 策略
# ---------------------------------------------------------------------------


async def test_dispatch_requires_pairing(session):
    db, _user = session
    with pytest.raises(BrowserGatewayError) as not_paired:
        await dispatch_browser_command(db, tenant_id=1, uid="admin", run_id=None, op="get_status", payload={})
    assert not_paired.value.code == "BROWSER_NOT_PAIRED"


async def test_dispatch_requires_online_extension(session):
    db, _user = session
    link = await create_pairing_link(db, uid="admin", tenant_id=1)
    await authorize_pairing(db, code=link["code"], device_name="A", extension_version=None)

    with pytest.raises(BrowserGatewayError) as offline:
        await dispatch_browser_command(db, tenant_id=1, uid="admin", run_id=None, op="get_status", payload={})
    assert offline.value.code == "BROWSER_OFFLINE"


async def test_dispatch_happy_path_records_audit(session):
    db, _user = session
    link = await create_pairing_link(db, uid="admin", tenant_id=1)
    authed = await authorize_pairing(db, code=link["code"], device_name="A", extension_version=None)
    ws = FakeWebSocket(script={"get_status": {"ok": True, "result": {"tabs": [{"tab_id": 1}]}}})
    get_browser_connection_registry().replace_connection(_handle(ws, device_id=authed["device_id"]))

    result = await dispatch_browser_command(db, tenant_id=1, uid="admin", run_id="run-1", op="get_status", payload={})
    assert result == {"tabs": [{"tab_id": 1}]}
    assert await count_run_commands(db, run_id="run-1") == 1


async def test_browser_skill_bridge_status_is_managed_externally(session, monkeypatch):
    from yuxi.services import browser_skill_bridge
    from yuxi.services.browser_gateway_service import get_browser_status

    monkeypatch.setattr(browser_skill_bridge, "BROWSER_SKILL_BRIDGE_URL", "http://host.docker.internal:52801")
    monkeypatch.setattr(browser_skill_bridge, "BROWSER_SKILL_BRIDGE_SECRET_FILE", "/tmp/secret")

    async def fake_health():
        return {
            "ok": True,
            "browsers": [
                {
                    "browser_name": "chrome",
                    "browser_version": "154.0.0.0",
                    "extension_version": "0.3.2",
                }
            ],
        }

    monkeypatch.setattr(browser_skill_bridge, "browser_skill_health", fake_health)
    db, _user = session
    status = await get_browser_status(db, uid="admin", tenant_id=1)

    assert status["paired"] is True
    assert status["online"] is True
    assert status["managed_externally"] is True
    assert status["backend"] == "browser_skill"
    assert status["device"]["device_id"] == "bsk-local"


async def test_browser_skill_bridge_dispatch_records_audit_without_pairing(session, monkeypatch):
    from yuxi.services import browser_skill_bridge

    monkeypatch.setattr(browser_skill_bridge, "BROWSER_SKILL_BRIDGE_URL", "http://host.docker.internal:52801")
    monkeypatch.setattr(browser_skill_bridge, "BROWSER_SKILL_BRIDGE_SECRET_FILE", "/tmp/secret")

    async def fake_dispatch(**kwargs):
        assert kwargs["uid"] == "admin"
        assert kwargs["op"] == "get_status"
        return {"tabs": [{"tab_id": 9}]}

    monkeypatch.setattr(browser_skill_bridge, "dispatch_browser_skill", fake_dispatch)
    db, _user = session
    result = await dispatch_browser_command(
        db,
        tenant_id=1,
        uid="admin",
        run_id="bridge-run",
        op="get_status",
        payload={},
    )

    assert result == {"tabs": [{"tab_id": 9}]}
    assert await count_run_commands(db, run_id="bridge-run") == 1


async def test_run_command_budget_enforced(session):
    db, _user = session
    for _ in range(40):
        await record_command(
            db,
            tenant_id=1,
            uid="admin",
            run_id="run-budget",
            device_id="dev1",
            op="get_status",
            ok=True,
        )
    # 门控顺序：配对 → 在线 → 预算；预算前置于连接语义之上依然要求设备可用
    link = await create_pairing_link(db, uid="admin", tenant_id=1)
    authed = await authorize_pairing(db, code=link["code"], device_name="A", extension_version=None)
    get_browser_connection_registry().replace_connection(_handle(FakeWebSocket(), device_id=authed["device_id"]))
    with pytest.raises(BrowserGatewayError) as budget:
        await dispatch_browser_command(db, tenant_id=1, uid="admin", run_id="run-budget", op="get_status", payload={})
    assert budget.value.code == "BROWSER_BUDGET_EXCEEDED"


async def test_dispatch_records_extension_error(session):
    db, _user = session
    link = await create_pairing_link(db, uid="admin", tenant_id=1)
    authed = await authorize_pairing(db, code=link["code"], device_name="A", extension_version=None)
    ws = FakeWebSocket(
        script={"click": {"ok": False, "error": {"code": "EXT_SELECTOR_NOT_FOUND", "message": "找不到元素"}}}
    )
    get_browser_connection_registry().replace_connection(_handle(ws, device_id=authed["device_id"]))

    with pytest.raises(BrowserGatewayError) as failed:
        await dispatch_browser_command(
            db, tenant_id=1, uid="admin", run_id=None, op="click", payload={"selector": "#x"}
        )
    assert failed.value.code == "EXT_SELECTOR_NOT_FOUND"
    # 失败同样落审计（append-only），run_id 为空时按 IS NULL 统计
    assert await count_run_commands(db, run_id=None) == 1


async def test_navigate_url_policy():
    assert validate_navigate_url("https://example.com") == "https://example.com"
    for bad in ("javascript:alert(1)", "file:///etc/passwd", "chrome://settings", "not a url", ""):
        with pytest.raises(BrowserGatewayError) as denied:
            validate_navigate_url(bad)
        assert denied.value.code == "BROWSER_POLICY_DENIED"


# ---------------------------------------------------------------------------
# 便捷构造器与 WS 令牌提取
# ---------------------------------------------------------------------------


async def test_error_factories():
    assert browser_not_paired().code == "BROWSER_NOT_PAIRED"
    assert browser_offline().code == "BROWSER_OFFLINE"
    assert browser_timeout("navigate").code == "BROWSER_TIMEOUT"


async def test_extract_ws_token_from_subprotocol():
    from server.routers.browser_router import _extract_ws_token

    class _WS:
        headers = Headers({"sec-websocket-protocol": "bearer, brt_abc123, chat"})

    assert _extract_ws_token(_WS()) == "brt_abc123"

    class _WSNone:
        headers = Headers({"sec-websocket-protocol": "chat, superchat"})

    assert _extract_ws_token(_WSNone()) is None


async def test_heartbeat_is_acknowledged():
    from types import SimpleNamespace

    from server.routers.browser_router import _handle_extension_frame

    ws = FakeWebSocket()
    registry = get_browser_connection_registry()
    registry.replace_connection(_handle(ws, device_id="heartbeat-device"))
    auth = SimpleNamespace(device_id="heartbeat-device")

    # db=None 时租约续租走异常降级路径（不影响 ack 语义）
    await _handle_extension_frame(ws, auth, {"v": 1, "type": "heartbeat", "ts": 123}, None)

    assert json.loads(ws.sent[-1]) == {"v": 1, "type": "heartbeat_ack", "ts": 123}


# ---------------------------------------------------------------------------
# Phase 2/3：域名策略、暂停控制、连接租约、request_help 注册
# ---------------------------------------------------------------------------


async def test_domain_policy_allowlist_and_denylist(session):
    db, _user = session
    from yuxi.services.browser_gateway_service import enforce_domain_policy, set_domain_policy

    await set_domain_policy(db, tenant_id=1, mode="allowlist", domains=["Example.com"], updated_by="admin")
    await enforce_domain_policy(db, tenant_id=1, url="https://www.example.com/page")  # 子域命中
    with pytest.raises(BrowserGatewayError) as denied:
        await enforce_domain_policy(db, tenant_id=1, url="https://other.org")
    assert denied.value.code == "BROWSER_POLICY_DENIED"

    await set_domain_policy(db, tenant_id=1, mode="denylist", domains=["evil.example"], updated_by="admin")
    await enforce_domain_policy(db, tenant_id=1, url="https://example.com")
    with pytest.raises(BrowserGatewayError) as blocked:
        await enforce_domain_policy(db, tenant_id=1, url="https://sub.evil.example")
    assert blocked.value.code == "BROWSER_POLICY_DENIED"

    await set_domain_policy(db, tenant_id=1, mode="off", domains=[], updated_by="admin")
    await enforce_domain_policy(db, tenant_id=1, url="https://anything.org")


async def test_run_pause_blocks_and_resumes(session):
    db, _user = session
    from yuxi.services.browser_gateway_service import browser_paused, set_run_paused

    link = await create_pairing_link(db, uid="admin", tenant_id=1)
    authed = await authorize_pairing(db, code=link["code"], device_name="A", extension_version=None)
    ws = FakeWebSocket(script={"get_status": {"ok": True, "result": {"tabs": []}}})
    get_browser_connection_registry().replace_connection(_handle(ws, device_id=authed["device_id"]))

    await set_run_paused("run-paused-test", True)
    with pytest.raises(BrowserGatewayError) as paused:
        await dispatch_browser_command(
            db, tenant_id=1, uid="admin", run_id="run-paused-test", op="get_status", payload={}
        )
    assert paused.value.code == browser_paused().code

    await set_run_paused("run-paused-test", False)
    result = await dispatch_browser_command(
        db, tenant_id=1, uid="admin", run_id="run-paused-test", op="get_status", payload={}
    )
    assert result == {"tabs": []}


async def test_connection_lease_lifecycle(session):
    db, _user = session
    from yuxi.services.browser_gateway_service import (
        _dispatch_via_lease,
        delete_connection_lease,
        find_active_lease,
        upsert_connection_lease,
    )

    await upsert_connection_lease(db, tenant_id=1, device_id="dev-lease")
    lease = await find_active_lease(db, device_id="dev-lease")
    assert lease is not None

    # 本节点租约 → 不跨节点中继（返回 None，由调用方走本地离线语义）
    outcome = await _dispatch_via_lease(
        db,
        device_id="dev-lease",
        tenant_id=1,
        uid="admin",
        run_id=None,
        op="get_status",
        payload={},
        timeout_s=5,
        audit=False,
    )
    assert outcome is None

    await delete_connection_lease(db, device_id="dev-lease")
    assert await find_active_lease(db, device_id="dev-lease") is None


async def test_request_help_tool_registered():
    from yuxi.agents.toolkits.browser.tools import get_browser_runtime_tools

    names = {tool.name for tool in get_browser_runtime_tools()}
    assert "browser_request_help" in names
