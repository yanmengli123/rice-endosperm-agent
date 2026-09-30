"""本机浏览器接入路由：配对/设备管理 REST、扩展 WS 端点与内部 dispatch。

路径规划（与 docker/apisix/apisix.yaml 的白名单保持同步）：
- /api/browser/pairing-links|devices|status|policy|runs/:id/preview|control — 用户 JWT（get_required_user）
- /api/browser/extension/authorize          — 匿名（一次性码即凭证），网关白名单
- /api/browser/extension/ws                 — 设备令牌经 Sec-WebSocket-Protocol 承载
- /api/browser/internal/dispatch|node-relay — 仅 compose 内网（worker→api / 节点互连），绝不经网关暴露
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.utils.auth_middleware import get_admin_user, get_db, get_required_user
from yuxi.repositories.agent_run_repository import AgentRunRepository
from yuxi.services.browser_gateway_service import (
    BROWSER_GATEWAY_PUBLIC_URL,
    BROWSER_HEARTBEAT_TIMEOUT_S,
    BROWSER_PROTOCOL_VERSION,
    SUPPORTED_OPS,
    BrowserGatewayError,
    authenticate_extension,
    authorize_pairing,
    cluster_secret_matches,
    create_pairing_link,
    delete_connection_lease,
    dispatch_browser_command,
    get_browser_connection_registry,
    get_browser_status,
    get_domain_policy,
    list_devices,
    renew_connection_lease,
    resolve_cluster_secret,
    revoke_device,
    set_domain_policy,
    set_run_paused,
    upsert_connection_lease,
)
from yuxi.services.principal import resolve_tenant_id
from yuxi.storage.postgres.models_business import User
from yuxi.utils.logging_config import logger

router = APIRouter(prefix="/browser", tags=["browser"])

# Sec-WebSocket-Protocol 中承载令牌的约定：["bearer", <device_token>]
_WS_TOKEN_PROTOCOL = "bearer"
# 预览：单进程并发观看上限与单流时长/帧间隔上限（预览不计预算、不落审计）
_PREVIEW_MAX_VIEWERS = 2
_PREVIEW_FRAME_INTERVAL_S = 2.5
_PREVIEW_MAX_DURATION_S = 900
_preview_semaphore = asyncio.Semaphore(_PREVIEW_MAX_VIEWERS)


def _pairing_url_base(request: Request) -> str:
    if BROWSER_GATEWAY_PUBLIC_URL:
        return BROWSER_GATEWAY_PUBLIC_URL.rstrip("/")
    origin = str(request.base_url).rstrip("/")
    if request.headers.get("x-forwarded-proto"):
        scheme = request.headers["x-forwarded-proto"].split(",")[0].strip()
        host = request.headers.get("x-forwarded-host") or request.url.netloc
        origin = f"{scheme}://{host}"
    return origin


async def _require_tenant(db: AsyncSession, user: User) -> int:
    return await resolve_tenant_id(db, str(user.uid))


# ---------------------------------------------------------------------------
# 用户侧 REST（JWT）
# ---------------------------------------------------------------------------


@router.post("/pairing-links")
async def create_pairing_link_route(
    request: Request,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = await _require_tenant(db, current_user)
    result = await create_pairing_link(db, uid=str(current_user.uid), tenant_id=tenant_id)
    pairing_url = f"{_pairing_url_base(request)}/api/browser/pairing?p={result['code']}"
    return {
        "code": result["code"],
        "pairing_url": pairing_url,
        "expires_at": result["expires_at"],
        "ttl_seconds": 300,
    }


@router.get("/status")
async def browser_status_route(
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = await _require_tenant(db, current_user)
    return await get_browser_status(db, uid=str(current_user.uid), tenant_id=tenant_id)


@router.get("/devices")
async def list_devices_route(
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = await _require_tenant(db, current_user)
    return {"devices": await list_devices(db, uid=str(current_user.uid), tenant_id=tenant_id)}


@router.delete("/devices/{device_id}")
async def revoke_device_route(
    device_id: str,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = await _require_tenant(db, current_user)
    revoked = await revoke_device(db, uid=str(current_user.uid), tenant_id=tenant_id, device_id=device_id)
    if not revoked:
        raise HTTPException(status_code=404, detail="设备不存在")
    return {"ok": True}


# ---------------------------------------------------------------------------
# 扩展侧（一次性码 / 设备令牌，无用户 JWT）
# ---------------------------------------------------------------------------


@router.post("/extension/authorize")
async def extension_authorize_route(
    payload: dict[str, Any],
    db: AsyncSession = Depends(get_db),
):
    try:
        return await authorize_pairing(
            db,
            code=str(payload.get("code") or ""),
            device_name=str(payload.get("device_name") or "")[:128],
            extension_version=str(payload.get("extension_version") or "")[:32] or None,
        )
    except BrowserGatewayError as e:
        raise HTTPException(status_code=e.http_status, detail={"code": e.code, "message": e.message}) from e


@router.websocket("/extension/ws")
async def extension_ws_route(
    websocket: WebSocket,
    db: AsyncSession = Depends(get_db),
):
    """扩展连接：令牌经 Sec-WebSocket-Protocol（避免代理记录 Authorization）。"""
    registry = get_browser_connection_registry()
    device_token = _extract_ws_token(websocket)
    if len(registry.connection_snapshot()) >= registry.max_connections:
        await websocket.close(code=4429)
        return
    if device_token is None:
        await websocket.close(code=4401)
        return
    try:
        auth, rotated = await authenticate_extension(db, device_token=device_token)
    except BrowserGatewayError as e:
        await websocket.close(code=_ws_close_code(e))
        return
    try:
        await websocket.accept(subprotocol=_WS_TOKEN_PROTOCOL)
    except Exception as e:
        logger.warning(f"browser ws accept failed: {e}")
        return

    try:
        # 握手：扩展先发 hello，服务端回 hello_ok；超时未握手按未就绪关闭
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=10)
        hello = _safe_json(raw)
        if (
            not isinstance(hello, dict)
            or str(hello.get("type") or "") != "hello"
            or hello.get("protocol") != BROWSER_PROTOCOL_VERSION
            or hello.get("v") != BROWSER_PROTOCOL_VERSION
        ):
            await websocket.close(code=4400)
            return
        await websocket.send_text(
            json.dumps(
                {
                    "v": BROWSER_PROTOCOL_VERSION,
                    "type": "hello_ok",
                    "protocol": BROWSER_PROTOCOL_VERSION,
                    "heartbeat_timeout_s": BROWSER_HEARTBEAT_TIMEOUT_S,
                },
                ensure_ascii=False,
            )
        )
        if rotated:
            await websocket.send_text(json.dumps({"v": BROWSER_PROTOCOL_VERSION, **rotated}, ensure_ascii=False))

        # 只有握手完成的连接才进入注册表，避免 Agent 在 hello_ok 前抢先下发命令。
        handle_old = registry.replace_connection(_build_handle(websocket, auth))
        if handle_old is not None and handle_old.websocket is not websocket:
            try:
                await handle_old.websocket.close(code=4441)
            except Exception:
                pass
        # Phase 3：登记连接租约（多副本时其他节点可经租约路由到本节点）
        try:
            await upsert_connection_lease(db, tenant_id=int(auth.tenant_id or 0), device_id=auth.device_id)
        except Exception as lease_error:  # noqa: BLE001 —— 租约是增强通道，失败不阻断连接
            logger.warning(f"browser lease upsert skipped: {type(lease_error).__name__}")
        logger.info(f"browser extension connected device_id={auth.device_id} uid={auth.uid}")

        while True:
            try:
                raw = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=BROWSER_HEARTBEAT_TIMEOUT_S,
                )
            except TimeoutError:
                await websocket.close(code=4408)
                return
            frame = _safe_json(raw)
            if not isinstance(frame, dict):
                continue
            await _handle_extension_frame(websocket, auth, frame, db)
    except WebSocketDisconnect:
        pass
    except TimeoutError:
        await websocket.close(code=4400)
    except Exception as e:
        logger.info(f"browser extension disconnected device_id={auth.device_id}: {type(e).__name__}")
    finally:
        registry.unregister(auth.device_id, websocket)
        try:
            await delete_connection_lease(db, device_id=auth.device_id)
        except Exception as lease_error:  # noqa: BLE001
            logger.warning(f"browser lease delete skipped: {type(lease_error).__name__}")
        logger.info(f"browser extension disconnected device_id={auth.device_id}")


def _build_handle(websocket: WebSocket, auth) -> Any:
    from yuxi.services.browser_gateway_service import BrowserConnectionHandle

    return BrowserConnectionHandle(
        websocket=websocket,
        device_id=auth.device_id,
        tenant_id=int(auth.tenant_id or 0),
        uid=auth.uid,
        device_name=auth.device_name or "",
        extension_version=auth.extension_version,
    )


def _extract_ws_token(websocket: WebSocket) -> str | None:
    protocols = websocket.headers.get("sec-websocket-protocol", "")
    parts = [p.strip() for p in protocols.split(",") if p.strip()]
    for index, part in enumerate(parts):
        if part.lower() == _WS_TOKEN_PROTOCOL and index + 1 < len(parts):
            token = parts[index + 1]
            if token.startswith("brt_"):
                return token
    return None


def _ws_close_code(error: BrowserGatewayError) -> int:
    if error.http_status in (401,):
        return 4401
    if error.http_status == 403:
        return 4403
    return 4400


async def _handle_extension_frame(websocket: WebSocket, auth, frame: dict, db: AsyncSession) -> None:
    registry = get_browser_connection_registry()
    frame_type = str(frame.get("type") or "")
    if frame_type == "heartbeat":
        registry.mark_heartbeat(auth.device_id)
        # 扩展以“收到服务端帧”为死链判断依据；必须回 ack，不能只在内存中记心跳。
        await websocket.send_text(
            json.dumps(
                {
                    "v": BROWSER_PROTOCOL_VERSION,
                    "type": "heartbeat_ack",
                    "ts": frame.get("ts"),
                },
                ensure_ascii=False,
            )
        )
        try:
            await renew_connection_lease(db, device_id=auth.device_id)
        except Exception as lease_error:  # noqa: BLE001 —— 续租失败不影响命令通道
            logger.warning(f"browser lease renew skipped: {type(lease_error).__name__}")
        return
    if frame_type == "result":
        cmd_id = str(frame.get("id") or "")
        registry.resolve(cmd_id, frame if isinstance(frame, dict) else {})
        return
    if frame_type == "hello":
        # 服务端不接受连接后重复 hello（握手由 hello_ok 完成），忽略即可
        return
    logger.debug(f"browser ws unknown frame type={frame_type} device_id={auth.device_id}")


# ---------------------------------------------------------------------------
# 内部 dispatch（worker→api，共享密钥；不经网关暴露）
# ---------------------------------------------------------------------------


@router.post("/internal/dispatch")
async def internal_dispatch_route(
    payload: dict[str, Any],
    x_browser_cluster_secret: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
):
    expected = await resolve_cluster_secret()
    if not cluster_secret_matches(x_browser_cluster_secret, expected):
        raise HTTPException(
            status_code=503, detail={"code": "BROWSER_INTERNAL_UNAUTHORIZED", "message": "内部调用未授权"}
        )
    op = str(payload.get("op") or "")
    if op not in SUPPORTED_OPS:
        raise HTTPException(status_code=400, detail={"code": "BROWSER_INVALID_OP", "message": f"不支持的操作: {op}"})
    try:
        result = await dispatch_browser_command(
            db,
            tenant_id=int(payload.get("tenant_id") or 0),
            uid=str(payload.get("uid") or ""),
            run_id=payload.get("run_id") or None,
            op=op,
            payload=payload.get("payload") if isinstance(payload.get("payload"), dict) else {},
            timeout_s=payload.get("timeout_s"),
        )
        return {"ok": True, "result": result}
    except BrowserGatewayError as e:
        return {"ok": False, "error": {"code": e.code, "message": e.message}}


@router.post("/internal/node-relay")
async def internal_node_relay_route(
    payload: dict[str, Any],
    x_browser_cluster_secret: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
):
    """跨节点中继（Phase 3）：发起节点经租约直连本节点，本节点只中继到本地连接、
    不落审计（审计权威在发起节点）。与本进程 dispatch 唯一区别是跳过审计与租约再路由。"""
    expected = await resolve_cluster_secret()
    if not cluster_secret_matches(x_browser_cluster_secret, expected):
        raise HTTPException(
            status_code=503, detail={"code": "BROWSER_INTERNAL_UNAUTHORIZED", "message": "内部调用未授权"}
        )
    op = str(payload.get("op") or "")
    device_id = str(payload.get("device_id") or "")
    if op not in SUPPORTED_OPS or not device_id:
        raise HTTPException(status_code=400, detail={"code": "BROWSER_INVALID_OP", "message": "参数缺失"})
    registry = get_browser_connection_registry()
    if registry.get(device_id) is None:
        return {"ok": False, "error": {"code": "BROWSER_OFFLINE", "message": "连接不在本节点"}}
    try:
        result = await registry.dispatch(
            device_id,
            op=op,
            payload=payload.get("payload") if isinstance(payload.get("payload"), dict) else {},
            timeout_s=float(payload.get("timeout_s") or 20),
        )
        return {"ok": True, "result": result}
    except BrowserGatewayError as e:
        return {"ok": False, "error": {"code": e.code, "message": e.message}}


# ---------------------------------------------------------------------------
# 域名策略（admin）
# ---------------------------------------------------------------------------


@router.get("/policy")
async def get_policy_route(
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = await _require_tenant(db, current_user)
    policy = await get_domain_policy(db, tenant_id=tenant_id)
    if policy is None:
        return {"mode": "off", "domains": [], "updated_at": None}
    return {
        "mode": policy.mode,
        "domains": policy.domains or [],
        "updated_at": policy.updated_at.isoformat() if policy.updated_at else None,
    }


@router.put("/policy")
async def put_policy_route(
    payload: dict[str, Any],
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = await _require_tenant(db, current_user)
    try:
        policy = await set_domain_policy(
            db,
            tenant_id=tenant_id,
            mode=str(payload.get("mode") or "off"),
            domains=payload.get("domains") if isinstance(payload.get("domains"), list) else [],
            updated_by=str(current_user.uid),
        )
    except BrowserGatewayError as e:
        raise HTTPException(status_code=e.http_status, detail={"code": e.code, "message": e.message}) from e
    return {"mode": policy.mode, "domains": policy.domains or [], "updated_at": policy.updated_at.isoformat()}


# ---------------------------------------------------------------------------
# 运行控制（暂停/继续/结束）
# ---------------------------------------------------------------------------


@router.post("/runs/{run_id}/control")
async def control_run_route(
    run_id: str,
    payload: dict[str, Any],
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    run = await AgentRunRepository(db).get_run_for_user(run_id, str(current_user.uid))
    if not run:
        raise HTTPException(status_code=404, detail="运行任务不存在")
    action = str(payload.get("action") or "")
    if action not in ("pause", "resume", "end"):
        raise HTTPException(
            status_code=400, detail={"code": "BROWSER_INVALID_ACTION", "message": "action 仅支持 pause/resume/end"}
        )
    tenant_id = await _require_tenant(db, current_user)
    if action == "pause":
        await set_run_paused(run_id, True)
    elif action == "resume":
        await set_run_paused(run_id, False)
    else:  # end：尽力结束浏览器会话，离线/已结束不视为错误
        await set_run_paused(run_id, False)
        try:
            await dispatch_browser_command(
                db,
                tenant_id=tenant_id,
                uid=str(current_user.uid),
                run_id=run_id,
                op="end_task",
                payload={},
                timeout_s=15,
            )
        except BrowserGatewayError as e:
            logger.info(f"browser run end_task skipped run={run_id}: {e.code}")
    return {"ok": True, "action": action, "run_status": run.status}


# ---------------------------------------------------------------------------
# 实时预览（低帧率截图 SSE；不计预算、不落审计）
# ---------------------------------------------------------------------------


@router.get("/runs/{run_id}/preview")
async def preview_run_route(
    run_id: str,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    run = await AgentRunRepository(db).get_run_for_user(run_id, str(current_user.uid))
    if not run:
        raise HTTPException(status_code=404, detail="运行任务不存在")
    tenant_id = await _require_tenant(db, current_user)
    if _preview_semaphore.locked():
        return _preview_end_response("viewer_limit")
    return StreamingResponse(
        _preview_stream(run_id=run_id, tenant_id=tenant_id, uid=str(current_user.uid)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _preview_end_response(reason: str) -> StreamingResponse:
    async def _end():
        yield f"data: {json.dumps({'type': 'end', 'reason': reason})}\n\n"

    return StreamingResponse(_end(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


async def _preview_stream(*, run_id: str, tenant_id: int, uid: str):
    """低帧率截图流：桥模式走 dispatch（audit/预算豁免、忽略暂停）；每帧间隔固定。"""
    from yuxi.storage.postgres.models_business import AgentRun

    async def _emit(event: dict) -> str:
        return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    async with _preview_semaphore:
        deadline = asyncio.get_event_loop().time() + _PREVIEW_MAX_DURATION_S
        terminal_statuses = {"completed", "failed", "cancelled", "interrupted"}
        while asyncio.get_event_loop().time() < deadline:
            try:
                async with pg_session() as session:
                    row = (
                        await session.execute(select(AgentRun.status).filter(AgentRun.id == run_id))
                    ).scalar_one_or_none()
                if row is None or str(row) in terminal_statuses:
                    yield await _emit({"type": "end", "reason": "run_terminal"})
                    return
                result = await dispatch_browser_command(
                    session,
                    tenant_id=tenant_id,
                    uid=uid,
                    run_id=run_id,
                    op="screenshot",
                    payload={},
                    timeout_s=20,
                    count_budget=False,
                    audit=False,
                    ignore_pause=True,
                )
                data_url = str((result or {}).get("data_url") or "")
                if data_url.startswith("data:image/"):
                    yield await _emit({"type": "frame", "data_url": data_url})
                else:
                    yield await _emit({"type": "error", "code": "BROWSER_PREVIEW_NO_FRAME", "message": "暂无可用画面"})
            except BrowserGatewayError as e:
                yield await _emit({"type": "error", "code": e.code, "message": e.message})
                # 桥/扩展离线类错误:预览流终止,避免每 2.5s 重复报错
                offline_codes = (
                    "BROWSER_OFFLINE",
                    "BROWSER_SKILL_OFFLINE",
                    "BROWSER_NOT_PAIRED",
                    "BROWSER_SKILL_DISABLED",
                )
                if e.code in offline_codes:
                    yield await _emit({"type": "end", "reason": "bridge_offline"})
                    return
            except asyncio.CancelledError:
                return
            except Exception as error:  # noqa: BLE001 —— 单帧失败不断流
                logger.warning(f"browser preview frame skipped: {type(error).__name__}")
            await asyncio.sleep(_PREVIEW_FRAME_INTERVAL_S)
    yield await _emit({"type": "end", "reason": "max_duration"})


def pg_session():
    from yuxi.storage.postgres.manager import pg_manager

    return pg_manager.get_async_session_context()


def _safe_json(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None
