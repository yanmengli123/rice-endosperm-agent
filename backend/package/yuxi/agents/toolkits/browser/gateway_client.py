"""浏览器网关的 worker 侧客户端：执行上下文 + 内部 dispatch HTTP 调用。

工具在 ARQ worker 进程执行；扩展 WS 连接持有在 api 进程。本模块把命令经
`POST /api/browser/internal/dispatch`（共享密钥）转发到 api 进程中继给扩展。
"""

from __future__ import annotations

import os
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

import httpx

from yuxi.services.browser_gateway_service import BrowserGatewayError, resolve_cluster_secret
from yuxi.utils.logging_config import logger

DEFAULT_INTERNAL_BASE_URL = os.getenv("BROWSER_GATEWAY_INTERNAL_URL", "http://api:5050")
_INTERNAL_DISPATCH_PATH = "/api/browser/internal/dispatch"


@dataclass
class BrowserExecutionContext:
    """一次 AgentRun 内浏览器工具的执行身份（worker 进程内 contextvar）。"""

    tenant_id: int
    uid: str
    thread_id: str
    run_id: str | None = None
    agent_slug: str | None = None
    used: bool = False
    # 会话回收幂等标志：worker 边界与 chat_service 生成器 finally 都可能触发
    # best-effort 清理，同一实例上先到者负责 end_task。
    ended: bool = False


_browser_execution_context: ContextVar[BrowserExecutionContext | None] = ContextVar(
    "browser_execution_context", default=None
)


def set_browser_execution_context(context: BrowserExecutionContext):
    return _browser_execution_context.set(context)


def reset_browser_execution_context(token) -> None:
    if token is None:
        return
    try:
        _browser_execution_context.reset(token)
    except (ValueError, RuntimeError):
        # Streaming generators can be finalized by a different asyncio Context.
        # Match MCP cleanup semantics: cleanup must never replace the run result.
        return


def get_browser_execution_context() -> BrowserExecutionContext | None:
    return _browser_execution_context.get()


async def dispatch_browser_op(op: str, payload: dict[str, Any], *, timeout_s: float | None = None) -> dict:
    """调用网关内部 dispatch；网关业务错误抛 BrowserGatewayError（code 已结构化）。"""
    context = get_browser_execution_context()
    if context is None:
        raise BrowserGatewayError(
            "BROWSER_CONTEXT_MISSING",
            "浏览器执行上下文缺失（仅在开启「本机浏览器」的对话轮可用）",
            http_status=409,
        )
    if op != "end_task":
        context.used = True
    secret = await resolve_cluster_secret()
    if not secret:
        raise BrowserGatewayError("BROWSER_DISABLED", "浏览器网关未就绪（缺少内部共享密钥）", http_status=503)

    body = {
        "tenant_id": context.tenant_id,
        "uid": context.uid,
        "run_id": context.run_id,
        "op": op,
        "payload": payload,
        "timeout_s": timeout_s,
    }
    base_url = DEFAULT_INTERNAL_BASE_URL.rstrip("/")
    request_timeout = (timeout_s or 30) + 10
    try:
        async with httpx.AsyncClient(timeout=request_timeout) as client:
            response = await client.post(
                f"{base_url}{_INTERNAL_DISPATCH_PATH}",
                json=body,
                headers={"X-Browser-Cluster-Secret": secret},
            )
    except httpx.HTTPError as e:
        logger.warning(f"browser gateway unreachable: {type(e).__name__}")
        raise BrowserGatewayError(
            "BROWSER_GATEWAY_UNREACHABLE",
            "浏览器网关不可达，请稍后重试",
            http_status=503,
        ) from e

    if response.status_code != 200:
        detail = _safe_json(response.text)
        error = detail.get("detail") if isinstance(detail, dict) else None
        code = str((error or {}).get("code") or "BROWSER_GATEWAY_ERROR")
        message = str((error or {}).get("message") or f"浏览器网关错误（HTTP {response.status_code}）")
        raise BrowserGatewayError(code, message, http_status=response.status_code)
    data = _safe_json(response.text)
    if not isinstance(data, dict):
        raise BrowserGatewayError("BROWSER_GATEWAY_ERROR", "浏览器网关返回了无法解析的结果")
    if data.get("ok"):
        result = data.get("result")
        return result if isinstance(result, dict) else {}
    error = data.get("error") or {}
    raise BrowserGatewayError(
        str(error.get("code") or "BROWSER_EXTENSION_ERROR"), str(error.get("message") or "浏览器操作失败")
    )


async def end_browser_task_best_effort() -> None:
    """Close the BrowserSkill session created by this run without masking run completion."""
    context = get_browser_execution_context()
    if context is None or not context.used or context.ended:
        return
    context.ended = True
    try:
        await dispatch_browser_op("end_task", {}, timeout_s=15)
    except Exception as error:  # noqa: BLE001 -- cleanup must never replace the run result
        logger.warning(f"browser task cleanup skipped: {type(error).__name__}")


def _safe_json(text: str) -> Any:
    import json

    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None
