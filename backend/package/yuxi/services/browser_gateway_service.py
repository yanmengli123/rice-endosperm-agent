"""本机浏览器网关：配对、设备授权、扩展连接注册表与命令中继。

架构定位（对应 docs/local-browser.md）：
- 浏览器扩展（用户本机 Chromium 125+）主动外连本服务 WSS 端点，用户机器无入站端口；
- 本服务只持有连接与命令中继，数据权威（配对/设备/审计）全部落 PG；
- Agent 工具（toolkits/browser）经内部 dispatch 端点调用本服务，共享密钥鉴权；
- 单节点内嵌于 api 进程；多副本演进路径见文档 Phase 3（连接租约 + 节点直连 RPC）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import socket
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.models_business import (
    BrowserCommandAudit,
    BrowserConnectionLease,
    BrowserDeviceAuthorization,
    BrowserDomainPolicy,
    BrowserPairingLink,
    User,
)
from yuxi.storage.redis import get_async_redis_client
from yuxi.utils.datetime_utils import utc_now
from yuxi.utils.logging_config import logger

# ---------------------------------------------------------------------------
# 常量与环境
# ---------------------------------------------------------------------------

BROWSER_PROTOCOL_VERSION = 1
EXTENSION_WS_PATH = "/api/browser/extension/ws"

BROWSER_PAIRING_TTL_SECONDS = int(os.getenv("BROWSER_PAIRING_TTL_SECONDS", "300"))
BROWSER_DEVICE_TOKEN_TTL_DAYS = int(os.getenv("BROWSER_DEVICE_TOKEN_TTL_DAYS", "90"))
# 令牌有效期不足 2×轮换窗（即年龄超过 30 天）时在续连时轮换
BROWSER_DEVICE_TOKEN_ROTATE_THRESHOLD_DAYS = int(os.getenv("BROWSER_DEVICE_TOKEN_ROTATE_THRESHOLD_DAYS", "30"))
BROWSER_GATEWAY_MAX_CONNECTIONS = int(os.getenv("BROWSER_GATEWAY_MAX_CONNECTIONS", "32"))
BROWSER_MAX_COMMANDS_PER_RUN = int(os.getenv("BROWSER_MAX_COMMANDS_PER_RUN", "40"))
BROWSER_HEARTBEAT_INTERVAL_S = int(os.getenv("BROWSER_HEARTBEAT_INTERVAL_S", "10"))
BROWSER_HEARTBEAT_TIMEOUT_S = max(45, BROWSER_HEARTBEAT_INTERVAL_S * 4)
BROWSER_GATEWAY_PUBLIC_URL = os.getenv("BROWSER_GATEWAY_PUBLIC_URL", "").strip()
# Phase 3 多副本路由：本节点 ID（缺省容器主机名）与节点间直连地址
BROWSER_GATEWAY_NODE_ID = os.getenv("BROWSER_GATEWAY_NODE_ID", "").strip() or socket.gethostname()
BROWSER_GATEWAY_NODE_URL = os.getenv("BROWSER_GATEWAY_NODE_URL", "").strip().rstrip("/")
BROWSER_CONNECTION_LEASE_SECONDS = int(os.getenv("BROWSER_CONNECTION_LEASE_SECONDS", "45"))

CLUSTER_SECRET_ENV = "BROWSER_GATEWAY_CLUSTER_SECRET"
_CLUSTER_SECRET_REDIS_KEY = "yuxi:browser:cluster_secret"

# 暂停/继续控制（run 粒度，Redis 标记；1h TTL 兜底防悬挂）
_RUN_PAUSE_TTL_SECONDS = 3600

SUPPORTED_OPS = frozenset({"navigate", "read_page", "click", "type", "screenshot", "get_status", "end_task"})
OP_TIMEOUTS_S = {
    "navigate": 30,
    "read_page": 20,
    "click": 15,
    "type": 20,
    "screenshot": 20,
    "get_status": 10,
    "end_task": 15,
}
DEFAULT_OP_TIMEOUT_S = 20

_READ_PAGE_TEXT_LIMIT = 20_000


class BrowserGatewayError(Exception):
    """浏览器网关结构化错误；code 会进入工具结果与审计。"""

    def __init__(self, code: str, message: str, *, http_status: int = 502):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def pairing_link_invalid(message: str = "配对链接无效") -> BrowserGatewayError:
    return BrowserGatewayError("pairing_link_invalid", message, http_status=400)


def pairing_link_expired() -> BrowserGatewayError:
    return BrowserGatewayError("pairing_link_expired", "配对链接已过期，请重新生成", http_status=400)


def pairing_link_consumed() -> BrowserGatewayError:
    return BrowserGatewayError("pairing_link_consumed", "配对链接已被使用，请重新生成", http_status=400)


def browser_not_paired() -> BrowserGatewayError:
    return BrowserGatewayError(
        "BROWSER_NOT_PAIRED", "本机浏览器尚未配对，请在工具箱「浏览器连接」生成配对链接", http_status=409
    )


def browser_offline() -> BrowserGatewayError:
    return BrowserGatewayError(
        "BROWSER_OFFLINE",
        "本机浏览器扩展不在线，请打开浏览器并确认扩展已连接",
        http_status=409,
    )


def browser_timeout(op: str) -> BrowserGatewayError:
    return BrowserGatewayError("BROWSER_TIMEOUT", f"浏览器操作 {op} 超时", http_status=504)


def browser_paused() -> BrowserGatewayError:
    return BrowserGatewayError(
        "BROWSER_PAUSED",
        "本轮浏览器任务已被用户暂停；在对话预览中点击「继续」后可恢复",
        http_status=409,
    )


# ---------------------------------------------------------------------------
# 哈希与摘要
# ---------------------------------------------------------------------------


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _args_digest(payload: dict | None) -> str:
    try:
        canonical = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        canonical = repr(payload)
    return _sha256_hex(canonical)[:80]


# ---------------------------------------------------------------------------
# 配对与设备授权（数据权威在 PG）
# ---------------------------------------------------------------------------


async def create_pairing_link(db: AsyncSession, *, uid: str, tenant_id: int) -> dict:
    """生成一次性配对码；同用户旧 pending 链接标记 superseded（不吊销已授权设备）。"""
    code = "brp_" + secrets.token_urlsafe(24)
    now = utc_now()
    pending = (
        (
            await db.execute(
                select(BrowserPairingLink).filter(
                    BrowserPairingLink.uid == uid,
                    BrowserPairingLink.status == BrowserPairingLink.STATUS_PENDING,
                )
            )
        )
        .scalars()
        .all()
    )
    for row in pending:
        row.status = BrowserPairingLink.STATUS_SUPERSEDED
    link = BrowserPairingLink(
        tenant_id=tenant_id,
        uid=uid,
        code_hash=_sha256_hex(code),
        status=BrowserPairingLink.STATUS_PENDING,
        expires_at=now + timedelta(seconds=BROWSER_PAIRING_TTL_SECONDS),
    )
    db.add(link)
    await db.commit()
    return {"code": code, "expires_at": link.expires_at.isoformat()}


def _is_expired(value: datetime, now: datetime) -> bool:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value <= now


async def _require_active_user(db: AsyncSession, uid: str) -> User:
    user = (await db.execute(select(User).filter(User.uid == uid))).scalar_one_or_none()
    if user is None or getattr(user, "is_disabled", False):
        raise BrowserGatewayError("user_unavailable", "用户不可用或已停用", http_status=403)
    return user


async def authorize_pairing(
    db: AsyncSession,
    *,
    code: str,
    device_name: str,
    extension_version: str | None,
) -> dict:
    """一次性码换设备令牌；新激活替换 (tenant_id, uid) 下的旧 active 设备。"""
    code = (code or "").strip()
    if not code:
        raise pairing_link_invalid()
    link = (
        await db.execute(select(BrowserPairingLink).filter(BrowserPairingLink.code_hash == _sha256_hex(code)))
    ).scalar_one_or_none()
    if link is None or link.status in (BrowserPairingLink.STATUS_SUPERSEDED,):
        raise pairing_link_invalid()
    if link.status == BrowserPairingLink.STATUS_CONSUMED:
        raise pairing_link_consumed()
    now = utc_now()
    if _is_expired(link.expires_at, now):
        link.status = BrowserPairingLink.STATUS_EXPIRED
        await db.commit()
        raise pairing_link_expired()

    user = await _require_active_user(db, link.uid)
    tenant_id = int(link.tenant_id or 0)

    device_token = "brt_" + secrets.token_urlsafe(32)
    device_id = uuid.uuid4().hex
    token_expires_at = now + timedelta(days=BROWSER_DEVICE_TOKEN_TTL_DAYS)

    existing = (
        (
            await db.execute(
                select(BrowserDeviceAuthorization).filter(
                    BrowserDeviceAuthorization.tenant_id == tenant_id,
                    BrowserDeviceAuthorization.uid == link.uid,
                    BrowserDeviceAuthorization.status == BrowserDeviceAuthorization.STATUS_ACTIVE,
                )
            )
        )
        .scalars()
        .all()
    )
    for old in existing:
        old.status = BrowserDeviceAuthorization.STATUS_REVOKED
        old.revoked_at = now

    link.status = BrowserPairingLink.STATUS_CONSUMED
    link.consumed_at = now
    link.device_name = (device_name or "")[:128]
    link.extension_version = extension_version or None

    auth = BrowserDeviceAuthorization(
        tenant_id=tenant_id,
        uid=link.uid,
        device_id=device_id,
        device_name=(device_name or "")[:128],
        extension_version=(extension_version or "")[:32] or None,
        token_hash=_sha256_hex(device_token),
        token_expires_at=token_expires_at,
        status=BrowserDeviceAuthorization.STATUS_ACTIVE,
        last_seen_at=now,
    )
    db.add(auth)
    await db.commit()
    logger.info(f"browser device authorized uid={user.uid} device_id={device_id}")
    return {
        "device_token": device_token,
        "device_id": device_id,
        "device_name": auth.device_name,
        "protocol_version": BROWSER_PROTOCOL_VERSION,
        "heartbeat_interval_s": BROWSER_HEARTBEAT_INTERVAL_S,
        "token_expires_at": token_expires_at.isoformat(),
    }


async def authenticate_extension(
    db: AsyncSession,
    *,
    device_token: str,
) -> tuple[BrowserDeviceAuthorization, dict | None]:
    """扩展 WS 握手鉴权；余量不足时轮换令牌，返回 (设备授权, 轮换载荷|None)。

    轮换载荷形如 {"type": "token_rotated", "device_token": ..., "token_expires_at": ...}。
    失败时抛 BrowserGatewayError（http_status 映射 WS 关闭码）。
    """
    if not device_token:
        raise browser_not_paired()
    auth = (
        await db.execute(
            select(BrowserDeviceAuthorization).filter(
                BrowserDeviceAuthorization.token_hash == _sha256_hex(device_token)
            )
        )
    ).scalar_one_or_none()
    if auth is None:
        raise browser_not_paired()
    if auth.status != BrowserDeviceAuthorization.STATUS_ACTIVE:
        raise BrowserGatewayError("BROWSER_DEVICE_REVOKED", "设备授权已被撤销或替换", http_status=403)
    now = utc_now()
    if _is_expired(auth.token_expires_at, now):
        auth.status = BrowserDeviceAuthorization.STATUS_REVOKED
        auth.revoked_at = now
        await db.commit()
        raise BrowserGatewayError("BROWSER_DEVICE_EXPIRED", "设备令牌已过期，请重新配对", http_status=403)
    await _require_active_user(db, auth.uid)

    rotated: dict | None = None
    expires_at = auth.token_expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    remaining = expires_at - now
    if remaining < timedelta(days=2 * BROWSER_DEVICE_TOKEN_ROTATE_THRESHOLD_DAYS):
        new_token = "brt_" + secrets.token_urlsafe(32)
        auth.token_hash = _sha256_hex(new_token)
        auth.token_expires_at = now + timedelta(days=BROWSER_DEVICE_TOKEN_TTL_DAYS)
        rotated = {
            "type": "token_rotated",
            "device_token": new_token,
            "token_expires_at": auth.token_expires_at.isoformat(),
        }
        logger.info(f"browser device token rotated device_id={auth.device_id}")
    auth.last_seen_at = now
    await db.commit()
    return auth, rotated


async def list_devices(db: AsyncSession, *, uid: str, tenant_id: int) -> list[dict]:
    from yuxi.services.browser_skill_bridge import (
        browser_skill_health,
        is_browser_skill_bridge_enabled,
    )

    if is_browser_skill_bridge_enabled():
        try:
            health = await browser_skill_health()
            browsers = health.get("browsers") if isinstance(health.get("browsers"), list) else []
            browser = browsers[0] if browsers else {}
            return [_browser_skill_device(browser, online=bool(browsers))]
        except BrowserGatewayError:
            return [_browser_skill_device({}, online=False)]

    rows = (
        (
            await db.execute(
                select(BrowserDeviceAuthorization)
                .filter(
                    BrowserDeviceAuthorization.tenant_id == tenant_id,
                    BrowserDeviceAuthorization.uid == uid,
                )
                .order_by(BrowserDeviceAuthorization.created_at.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    registry = get_browser_connection_registry()
    devices = []
    for row in rows:
        devices.append(
            {
                "device_id": row.device_id,
                "device_name": row.device_name,
                "extension_version": row.extension_version,
                "status": row.status,
                "online": registry.is_online(row.device_id),
                "authorized_at": row.created_at.isoformat() if row.created_at else None,
                "token_expires_at": row.token_expires_at.isoformat() if row.token_expires_at else None,
                "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
            }
        )
    return devices


async def revoke_device(db: AsyncSession, *, uid: str, tenant_id: int, device_id: str) -> bool:
    auth = (
        await db.execute(
            select(BrowserDeviceAuthorization).filter(
                BrowserDeviceAuthorization.tenant_id == tenant_id,
                BrowserDeviceAuthorization.uid == uid,
                BrowserDeviceAuthorization.device_id == device_id,
            )
        )
    ).scalar_one_or_none()
    if auth is None:
        return False
    if auth.status == BrowserDeviceAuthorization.STATUS_ACTIVE:
        auth.status = BrowserDeviceAuthorization.STATUS_REVOKED
        auth.revoked_at = utc_now()
        await db.commit()
    get_browser_connection_registry().close_connection(device_id)
    return True


async def get_browser_status(db: AsyncSession, *, uid: str, tenant_id: int) -> dict:
    from yuxi.services.browser_skill_bridge import (
        browser_skill_health,
        is_browser_skill_bridge_enabled,
    )

    if is_browser_skill_bridge_enabled():
        try:
            health = await browser_skill_health()
            browsers = health.get("browsers") if isinstance(health.get("browsers"), list) else []
            browser = browsers[0] if browsers else {}
            return {
                "paired": True,
                "online": bool(browsers),
                "managed_externally": True,
                "backend": "browser_skill",
                "diagnostic": None,
                "bridge": {
                    "daemon_version": health.get("daemon_version"),
                    "protocol_version": health.get("protocol_version"),
                    "active_sessions": health.get("active_bridge_sessions", 0),
                },
                "device": _browser_skill_device(browser, online=bool(browsers)),
            }
        except BrowserGatewayError as error:
            return {
                "paired": True,
                "online": False,
                "managed_externally": True,
                "backend": "browser_skill",
                "diagnostic": {"code": error.code, "message": error.message},
                "bridge": None,
                "device": _browser_skill_device({}, online=False),
            }

    auth = (
        await db.execute(
            select(BrowserDeviceAuthorization)
            .filter(
                BrowserDeviceAuthorization.tenant_id == tenant_id,
                BrowserDeviceAuthorization.uid == uid,
                BrowserDeviceAuthorization.status == BrowserDeviceAuthorization.STATUS_ACTIVE,
            )
            .order_by(BrowserDeviceAuthorization.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    registry = get_browser_connection_registry()
    if auth is None:
        return {"paired": False, "online": False, "device": None}
    return {
        "paired": True,
        "online": registry.is_online(auth.device_id),
        "device": {
            "device_id": auth.device_id,
            "device_name": auth.device_name,
            "extension_version": auth.extension_version,
            "authorized_at": auth.created_at.isoformat() if auth.created_at else None,
            "token_expires_at": auth.token_expires_at.isoformat() if auth.token_expires_at else None,
            "last_seen_at": auth.last_seen_at.isoformat() if auth.last_seen_at else None,
        },
    }


def _browser_skill_device(browser: dict, *, online: bool) -> dict:
    browser_name = str(browser.get("browser_name") or "Chrome").capitalize()
    browser_version = str(browser.get("browser_version") or "").strip()
    device_name = str(browser.get("label") or "").strip() or f"BrowserSkill · {browser_name}"
    if browser_version:
        device_name += f" {browser_version.split('.')[0]}"
    connected_at = None
    try:
        connected_at_ms = int(browser.get("connected_at_ms") or 0)
        if connected_at_ms > 0:
            connected_at = datetime.fromtimestamp(connected_at_ms / 1000, tz=UTC).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        connected_at = None
    return {
        "device_id": "bsk-local",
        "device_name": device_name,
        "extension_version": browser.get("extension_version"),
        "status": "active",
        "online": online,
        "authorized_at": connected_at,
        "token_expires_at": None,
        "last_seen_at": utc_now().isoformat() if online else None,
        "managed_externally": True,
    }


# ---------------------------------------------------------------------------
# 审计（append-only）
# ---------------------------------------------------------------------------


async def record_command(
    db: AsyncSession,
    *,
    tenant_id: int,
    uid: str,
    run_id: str | None,
    device_id: str,
    op: str,
    ok: bool,
    error_code: str | None = None,
    args_digest: str | None = None,
    result_digest: str | None = None,
    duration_ms: int | None = None,
) -> None:
    db.add(
        BrowserCommandAudit(
            tenant_id=tenant_id,
            uid=uid,
            run_id=run_id,
            device_id=device_id,
            op=op,
            status="ok" if ok else "error",
            error_code=error_code,
            args_digest=args_digest,
            result_digest=result_digest,
            duration_ms=duration_ms,
        )
    )
    await db.commit()


async def count_run_commands(db: AsyncSession, *, run_id: str) -> int:
    return int(
        (
            await db.execute(
                select(func.count()).select_from(BrowserCommandAudit).filter(BrowserCommandAudit.run_id == run_id)
            )
        ).scalar_one()
    )


# ---------------------------------------------------------------------------
# 连接注册表（进程内；多副本演进见文档 Phase 3）
# ---------------------------------------------------------------------------


@dataclass
class BrowserConnectionHandle:
    websocket: object
    device_id: str
    tenant_id: int
    uid: str
    device_name: str = ""
    extension_version: str | None = None
    connected_at: datetime = field(default_factory=utc_now)
    last_heartbeat: float = field(default_factory=time.monotonic)


class BrowserConnectionRegistry:
    """扩展 WS 连接注册表 + 命令中继（请求/响应按 cmd_id 关联，超时断言）。

    连接容量是单节点活动设备保护上限（对齐 WeKnora BrowserSkill 语义），不是吞吐承诺。
    同一设备的新连接替换旧连接（旧 WS 由调用方关闭）。
    """

    def __init__(self, max_connections: int = BROWSER_GATEWAY_MAX_CONNECTIONS):
        self._max_connections = max_connections
        self._connections: dict[str, BrowserConnectionHandle] = {}
        self._pending: dict[str, asyncio.Future] = {}

    @property
    def max_connections(self) -> int:
        return self._max_connections

    def replace_connection(self, handle: BrowserConnectionHandle) -> BrowserConnectionHandle | None:
        """注册新连接，返回被替换的旧连接（调用方负责关闭旧 WS）。"""
        old = self._connections.get(handle.device_id)
        self._connections[handle.device_id] = handle
        return old

    def unregister(self, device_id: str, websocket: object | None = None) -> None:
        current = self._connections.get(device_id)
        if current is None:
            return
        if websocket is not None and current.websocket is not websocket:
            return
        self._connections.pop(device_id, None)

    def get_for_user(self, tenant_id: int, uid: str) -> BrowserConnectionHandle | None:
        for handle in self._connections.values():
            if handle.tenant_id == tenant_id and handle.uid == uid:
                return handle
        return None

    def get(self, device_id: str) -> BrowserConnectionHandle | None:
        return self._connections.get(device_id)

    def is_online(self, device_id: str) -> bool:
        return device_id in self._connections

    def mark_heartbeat(self, device_id: str) -> None:
        handle = self._connections.get(device_id)
        if handle is not None:
            handle.last_heartbeat = time.monotonic()

    def stale_device_ids(self, timeout_s: int = BROWSER_HEARTBEAT_TIMEOUT_S) -> list[str]:
        threshold = time.monotonic() - timeout_s
        return [d for d, h in self._connections.items() if h.last_heartbeat < threshold]

    def close_connection(self, device_id: str) -> None:
        handle = self._connections.pop(device_id, None)
        if handle is not None:
            _schedule_ws_close(handle.websocket, code=4403)

    def connection_snapshot(self) -> list[dict]:
        return [
            {
                "device_id": h.device_id,
                "tenant_id": h.tenant_id,
                "uid": h.uid,
                "device_name": h.device_name,
                "extension_version": h.extension_version,
                "connected_at": h.connected_at.isoformat(),
            }
            for h in self._connections.values()
        ]

    # ---- 命令中继 ----

    async def dispatch(self, device_id: str, *, op: str, payload: dict | None, timeout_s: float) -> dict:
        handle = self._connections.get(device_id)
        if handle is None:
            raise browser_offline()
        cmd_id = f"cmd_{uuid.uuid4().hex}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[cmd_id] = future
        frame = {"v": BROWSER_PROTOCOL_VERSION, "id": cmd_id, "type": "cmd", "op": op, "payload": payload or {}}
        try:
            try:
                await handle.websocket.send_text(json.dumps(frame, ensure_ascii=False))
            except Exception as e:
                raise browser_offline() from e
            try:
                reply = await asyncio.wait_for(future, timeout=timeout_s)
            except TimeoutError as e:
                raise browser_timeout(op) from e
        finally:
            self._pending.pop(cmd_id, None)
        if not isinstance(reply, dict):
            raise BrowserGatewayError("BROWSER_EXTENSION_ERROR", "扩展返回了无法解析的结果")
        if reply.get("ok"):
            result = reply.get("result")
            return result if isinstance(result, dict) else {}
        error = reply.get("error") or {}
        raise BrowserGatewayError(
            str(error.get("code") or "BROWSER_EXTENSION_ERROR"),
            str(error.get("message") or "浏览器扩展操作失败"),
        )

    def resolve(self, cmd_id: str, reply: dict) -> bool:
        future = self._pending.get(cmd_id)
        if future is None or future.done():
            return False
        future.set_result(reply)
        return True

    def pending_count(self) -> int:
        return len(self._pending)


def _schedule_ws_close(websocket: object, *, code: int) -> None:
    async def _close() -> None:
        try:
            await websocket.close(code=code)
        except Exception:
            pass

    try:
        asyncio.get_running_loop().create_task(_close())
    except RuntimeError:
        pass


_registry: BrowserConnectionRegistry | None = None


def get_browser_connection_registry() -> BrowserConnectionRegistry:
    global _registry
    if _registry is None:
        _registry = BrowserConnectionRegistry()
    return _registry


# ---------------------------------------------------------------------------
# 高层 dispatch（工具层入口）：门控 + 预算 + 审计
# ---------------------------------------------------------------------------


def validate_navigate_url(url: str) -> str:
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise BrowserGatewayError(
            "BROWSER_POLICY_DENIED",
            "仅允许导航到 http/https 页面",
            http_status=400,
        )
    return url


async def get_active_authorization(db: AsyncSession, *, uid: str, tenant_id: int) -> BrowserDeviceAuthorization | None:
    return (
        await db.execute(
            select(BrowserDeviceAuthorization)
            .filter(
                BrowserDeviceAuthorization.tenant_id == tenant_id,
                BrowserDeviceAuthorization.uid == uid,
                BrowserDeviceAuthorization.status == BrowserDeviceAuthorization.STATUS_ACTIVE,
            )
            .order_by(BrowserDeviceAuthorization.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def dispatch_browser_command(
    db: AsyncSession,
    *,
    tenant_id: int,
    uid: str,
    run_id: str | None,
    op: str,
    payload: dict | None,
    timeout_s: float | None = None,
    count_budget: bool = True,
    audit: bool = True,
    ignore_pause: bool = False,
) -> dict:
    """门控（未配对/离线）→ 暂停 → 预算 → 服务端策略 → 中继 → 审计。失败抛 BrowserGatewayError。

    count_budget/audit/ignore_pause 供预览等非模型通道关闭预算、审计与暂停拦截
    （暂停只挡模型命令，用户仍需预览画面确认现场）。
    """
    if op not in SUPPORTED_OPS:
        raise BrowserGatewayError("BROWSER_INVALID_OP", f"不支持的浏览器操作: {op}", http_status=400)
    if op == "navigate":
        payload = dict(payload or {})
        payload["url"] = validate_navigate_url(str(payload.get("url") or ""))
        await enforce_domain_policy(db, tenant_id=tenant_id, url=payload["url"])

    from yuxi.services.browser_skill_bridge import (
        dispatch_browser_skill,
        is_browser_skill_bridge_enabled,
    )

    bridge_enabled = is_browser_skill_bridge_enabled()
    auth = None
    registry = None
    device_id = "bsk-local"
    if not bridge_enabled:
        auth = await get_active_authorization(db, uid=uid, tenant_id=tenant_id)
        if auth is None:
            raise browser_not_paired()
        registry = get_browser_connection_registry()
        handle = registry.get(auth.device_id)
        if handle is None:
            # 本节点无连接：查连接租约（Phase 3），未过期且属其他节点则直连中继
            result = await _dispatch_via_lease(
                db,
                device_id=auth.device_id,
                tenant_id=tenant_id,
                uid=uid,
                run_id=run_id,
                op=op,
                payload=payload,
                timeout_s=float(timeout_s or OP_TIMEOUTS_S.get(op, DEFAULT_OP_TIMEOUT_S)),
                audit=audit,
            )
            if result is not None:
                return result
            raise browser_offline()
        device_id = auth.device_id
    if op != "end_task" and not ignore_pause:
        if await is_run_paused(run_id):
            raise browser_paused()
        if count_budget and run_id and BROWSER_MAX_COMMANDS_PER_RUN > 0:
            used = await count_run_commands(db, run_id=run_id)
            if used >= BROWSER_MAX_COMMANDS_PER_RUN:
                raise BrowserGatewayError(
                    "BROWSER_BUDGET_EXCEEDED",
                    f"本轮浏览器操作步数已达上限（{BROWSER_MAX_COMMANDS_PER_RUN}）",
                    http_status=429,
                )

    effective_timeout = float(timeout_s or OP_TIMEOUTS_S.get(op, DEFAULT_OP_TIMEOUT_S))
    started = time.monotonic()
    try:
        if bridge_enabled:
            result = await dispatch_browser_skill(
                uid=uid,
                run_id=run_id,
                op=op,
                payload=payload or {},
                timeout_s=effective_timeout,
            )
        else:
            result = await registry.dispatch(
                device_id,
                op=op,
                payload=payload,
                timeout_s=effective_timeout,
            )
    except BrowserGatewayError as e:
        if audit:
            await record_command(
                db,
                tenant_id=tenant_id,
                uid=uid,
                run_id=run_id,
                device_id=device_id,
                op=op,
                ok=False,
                error_code=e.code,
                args_digest=_args_digest(payload),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        raise
    if audit:
        await record_command(
            db,
            tenant_id=tenant_id,
            uid=uid,
            run_id=run_id,
            device_id=device_id,
            op=op,
            ok=True,
            args_digest=_args_digest(payload),
            result_digest=_args_digest(result) if op != "screenshot" else None,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    return result


# ---------------------------------------------------------------------------
# Phase 3：连接租约与跨节点中继
# ---------------------------------------------------------------------------


async def upsert_connection_lease(db: AsyncSession, *, tenant_id: int, device_id: str) -> None:
    """连接注册时登记租约；node_url 未配置时仅登记节点 ID（跨节点路由退化为离线）。"""
    now = utc_now()
    lease_until = now + timedelta(seconds=BROWSER_CONNECTION_LEASE_SECONDS)
    existing = (
        await db.execute(select(BrowserConnectionLease).filter(BrowserConnectionLease.device_id == device_id))
    ).scalar_one_or_none()
    if existing is None:
        db.add(
            BrowserConnectionLease(
                tenant_id=tenant_id,
                device_id=device_id,
                node_id=BROWSER_GATEWAY_NODE_ID,
                node_url=BROWSER_GATEWAY_NODE_URL,
                lease_until=lease_until,
                renewed_at=now,
            )
        )
    else:
        existing.node_id = BROWSER_GATEWAY_NODE_ID
        existing.node_url = BROWSER_GATEWAY_NODE_URL
        existing.lease_until = lease_until
        existing.renewed_at = now
    await db.commit()


async def renew_connection_lease(db: AsyncSession, *, device_id: str) -> None:
    lease = (
        await db.execute(select(BrowserConnectionLease).filter(BrowserConnectionLease.device_id == device_id))
    ).scalar_one_or_none()
    if lease is None:
        return
    now = utc_now()
    lease.lease_until = now + timedelta(seconds=BROWSER_CONNECTION_LEASE_SECONDS)
    lease.renewed_at = now
    await db.commit()


async def delete_connection_lease(db: AsyncSession, *, device_id: str) -> None:
    lease = (
        await db.execute(select(BrowserConnectionLease).filter(BrowserConnectionLease.device_id == device_id))
    ).scalar_one_or_none()
    if lease is not None:
        await db.delete(lease)
        await db.commit()


async def find_active_lease(db: AsyncSession, *, device_id: str) -> BrowserConnectionLease | None:
    lease = (
        await db.execute(select(BrowserConnectionLease).filter(BrowserConnectionLease.device_id == device_id))
    ).scalar_one_or_none()
    if lease is None:
        return None
    lease_until = lease.lease_until
    if lease_until.tzinfo is None:
        lease_until = lease_until.replace(tzinfo=UTC)
    if lease_until <= utc_now():
        return None
    return lease


async def _dispatch_via_lease(
    db: AsyncSession,
    *,
    device_id: str,
    tenant_id: int,
    uid: str,
    run_id: str | None,
    op: str,
    payload: dict | None,
    timeout_s: float,
    audit: bool,
) -> dict | None:
    """本节点无连接时的租约路由：未过期租约指向其他节点 → 节点直连中继。

    中继节点不落审计（审计权威在发起节点），命中返回结果、未命中返回 None。
    """
    lease = await find_active_lease(db, device_id=device_id)
    if lease is None or lease.node_id == BROWSER_GATEWAY_NODE_ID or not lease.node_url:
        return None
    secret = await resolve_cluster_secret()
    if not secret:
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout_s + 10, trust_env=False) as client:
            response = await client.post(
                f"{lease.node_url}/api/browser/internal/node-relay",
                json={
                    "tenant_id": tenant_id,
                    "uid": uid,
                    "run_id": run_id,
                    "op": op,
                    "payload": payload or {},
                    "timeout_s": timeout_s,
                    "device_id": device_id,
                },
                headers={"X-Browser-Cluster-Secret": secret},
            )
    except httpx.HTTPError as error:
        logger.warning(f"browser node relay unreachable node={lease.node_id}: {type(error).__name__}")
        return None
    data = _safe_json(response.text)
    if response.status_code != 200 or not isinstance(data, dict):
        return None
    if audit:
        started = time.monotonic()
        if data.get("ok"):
            result = data.get("result")
            await record_command(
                db,
                tenant_id=tenant_id,
                uid=uid,
                run_id=run_id,
                device_id=device_id,
                op=op,
                ok=True,
                args_digest=_args_digest(payload),
                result_digest=_args_digest(result) if op != "screenshot" else None,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        else:
            error = data.get("error") or {}
            await record_command(
                db,
                tenant_id=tenant_id,
                uid=uid,
                run_id=run_id,
                device_id=device_id,
                op=op,
                ok=False,
                error_code=str(error.get("code") or "BROWSER_EXTENSION_ERROR"),
                args_digest=_args_digest(payload),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
    if data.get("ok"):
        result = data.get("result")
        return result if isinstance(result, dict) else {}
    error = data.get("error") or {}
    raise BrowserGatewayError(
        str(error.get("code") or "BROWSER_EXTENSION_ERROR"),
        str(error.get("message") or "浏览器扩展操作失败"),
    )


# ---------------------------------------------------------------------------
# 域名策略（租户级，作用于 navigate）
# ---------------------------------------------------------------------------


def _host_matches_domain(host: str, entry: str) -> bool:
    return host == entry or host.endswith("." + entry)


async def get_domain_policy(db: AsyncSession, *, tenant_id: int) -> BrowserDomainPolicy | None:
    return (
        await db.execute(select(BrowserDomainPolicy).filter(BrowserDomainPolicy.tenant_id == tenant_id))
    ).scalar_one_or_none()


async def set_domain_policy(
    db: AsyncSession,
    *,
    tenant_id: int,
    mode: str,
    domains: list[str],
    updated_by: str | None,
) -> BrowserDomainPolicy:
    valid_modes = (BrowserDomainPolicy.MODE_OFF, BrowserDomainPolicy.MODE_ALLOWLIST, BrowserDomainPolicy.MODE_DENYLIST)
    if mode not in valid_modes:
        raise BrowserGatewayError(
            "BROWSER_POLICY_MODE_INVALID", "策略模式仅支持 off/allowlist/denylist", http_status=400
        )
    normalized: list[str] = []
    for raw in domains or []:
        entry = str(raw).strip().lower().lstrip(".")
        if not entry:
            continue
        if "/" in entry or " " in entry:
            raise BrowserGatewayError("BROWSER_POLICY_DOMAIN_INVALID", f"非法域名条目: {entry}", http_status=400)
        if entry not in normalized:
            normalized.append(entry)
    if len(normalized) > 200:
        raise BrowserGatewayError("BROWSER_POLICY_DOMAIN_INVALID", "域名条目最多 200 条", http_status=400)
    policy = await get_domain_policy(db, tenant_id=tenant_id)
    if policy is None:
        policy = BrowserDomainPolicy(tenant_id=tenant_id, mode=mode, domains=normalized, updated_by=updated_by)
        db.add(policy)
    else:
        policy.mode = mode
        policy.domains = normalized
        policy.updated_by = updated_by
    await db.commit()
    return policy


async def enforce_domain_policy(db: AsyncSession, *, tenant_id: int, url: str) -> None:
    """navigate 目标 host 对照租户策略；off/无策略放行，allowlist 未命中或 denylist 命中拒绝。"""
    policy = await get_domain_policy(db, tenant_id=tenant_id)
    if policy is None or policy.mode == BrowserDomainPolicy.MODE_OFF:
        return
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return
    entries = [str(entry).strip().lower() for entry in (policy.domains or []) if str(entry).strip()]
    matched = any(_host_matches_domain(host, entry) for entry in entries)
    if policy.mode == BrowserDomainPolicy.MODE_ALLOWLIST and not matched:
        raise BrowserGatewayError(
            "BROWSER_POLICY_DENIED",
            f"域名 {host} 不在租户白名单内，已拒绝导航",
            http_status=403,
        )
    if policy.mode == BrowserDomainPolicy.MODE_DENYLIST and matched:
        raise BrowserGatewayError(
            "BROWSER_POLICY_DENIED",
            f"域名 {host} 在租户黑名单内，已拒绝导航",
            http_status=403,
        )


# ---------------------------------------------------------------------------
# 暂停/继续（run 粒度 Redis 标记）
# ---------------------------------------------------------------------------


def _run_pause_key(run_id: str | None) -> str:
    return f"browser:paused:{run_id}"


async def is_run_paused(run_id: str | None) -> bool:
    if not run_id:
        return False
    try:
        redis = await get_async_redis_client()
        return bool(await redis.get(_run_pause_key(run_id)))
    except Exception as error:  # noqa: BLE001 —— Redis 不可用时放行（可用性优先，预算/审计仍在）
        logger.warning(f"browser pause check skipped: {type(error).__name__}")
        return False


async def set_run_paused(run_id: str | None, paused: bool) -> None:
    if not run_id:
        return
    redis = await get_async_redis_client()
    if paused:
        await redis.set(_run_pause_key(run_id), "1", ex=_RUN_PAUSE_TTL_SECONDS)
    else:
        await redis.delete(_run_pause_key(run_id))


# ---------------------------------------------------------------------------
# 内部 dispatch 共享密钥：env 优先，缺省经 Redis 零配置分发（仅 compose 内网可达）
# ---------------------------------------------------------------------------


async def resolve_cluster_secret() -> str | None:
    env_value = os.getenv(CLUSTER_SECRET_ENV, "").strip()
    if env_value:
        return env_value
    try:
        redis = await get_async_redis_client()
        value = await redis.get(_CLUSTER_SECRET_REDIS_KEY)
        if value:
            return value.decode("utf-8") if isinstance(value, bytes) else str(value)
        secret = "brsec_" + secrets.token_urlsafe(32)
        await redis.set(_CLUSTER_SECRET_REDIS_KEY, secret)
        return secret
    except Exception as e:
        logger.warning(f"browser cluster secret unavailable: {e}")
        return None


def _safe_json(text: str):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def cluster_secret_matches(provided: str | None, expected: str | None) -> bool:
    if not expected or not provided:
        return False
    return secrets.compare_digest(provided, expected)
