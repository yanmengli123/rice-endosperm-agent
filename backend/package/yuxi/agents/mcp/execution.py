"""Per-run MCP identity and append-only scientific call provenance."""

from __future__ import annotations

import contextvars
import hashlib
import json
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

from yuxi.storage.postgres.models_business import MCPCallAudit
from yuxi.utils import logger


@dataclass(frozen=True)
class McpExecutionContext:
    tenant_id: int
    uid: str
    run_id: str | None = None
    agent_slug: str | None = None
    installation_id: int | None = None
    data_access_level: str = "PUBLIC"


_MCP_EXECUTION_CONTEXT: contextvars.ContextVar[McpExecutionContext | None] = contextvars.ContextVar(
    "yuxi_mcp_execution_context", default=None
)


def set_mcp_execution_context(context: McpExecutionContext):
    return _MCP_EXECUTION_CONTEXT.set(context)


def reset_mcp_execution_context(token) -> None:
    """跨异步上下文安全地清理执行上下文（清理失败绝不能污染已完成的结果流）。

    ContextVar 的 token 只允许在创建它的同一个 Context 中 reset。流式对话链路里
    set 与 finally 的 reset 可能落在不同 Task/Context（生成器被队列桥接任务消费、
    客户端断开时由另一个任务执行 aclose），此时原生 reset 会抛 ``ValueError``；
    对同一 token 的二次 reset 会抛 ``RuntimeError``。

    清理语义按兜底处理：
    - 同 Context 首次 reset：正常恢复旧值；
    - 跨 Context / 重复 reset：降级为 no-op。不能写回 ``None``——那会把当前
      Context 里由外层请求设置的执行上下文一并抹掉（嵌套请求会因此丢失归属）。
      一次性 asyncio 任务在销毁时其 Context 会被解释器回收，不存在跨请求残留；
      残留风险比误伤外层值更小，故采用 no-op。
    """
    if token is None:
        return
    try:
        _MCP_EXECUTION_CONTEXT.reset(token)
    except (ValueError, RuntimeError):
        # 跨 Context 或已使用的 token：no-op，绝不抛异常。
        # 两类异常消息都包含 Token repr；它们若沿生成器 finally 逸出，会污染
        # 已经产生的回答流，这正是桌面端“无法识别的数据”的根因之一。
        return


def get_mcp_execution_context() -> McpExecutionContext | None:
    return _MCP_EXECUTION_CONTEXT.get()


def _safe_json_value(value: Any) -> Any:
    """把审计字段转换成可规范序列化的 JSON 值，拒绝运行时对象。"""

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return {"__bytes__": value.hex()}
    if isinstance(value, (Token, ContextVar)):
        raise TypeError(f"refusing to serialize {type(value).__qualname__} into MCP call digest")
    if callable(value):
        raise TypeError(f"refusing to serialize callable into MCP call digest: {type(value).__qualname__}")
    if isinstance(value, BaseException):
        raise TypeError(f"refusing to serialize {type(value).__qualname__} into MCP call digest")
    if isinstance(value, (list, tuple)):
        return [_safe_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _safe_json_value(item) for key, item in value.items()}
    if hasattr(value, "__dict__"):
        fields = {name: _safe_json_value(item) for name, item in vars(value).items() if not name.startswith("_")}
        if fields:
            return {"__type__": type(value).__qualname__, **fields}
    raise TypeError(f"refusing to serialize unsupported type {type(value).__qualname__} into MCP call digest")


def stable_digest(value: Any) -> str:
    """生成与字典插入顺序无关的稳定摘要，拒绝用对象 repr 兜底。"""

    try:
        payload = json.dumps(
            _safe_json_value(value),
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except TypeError:
        raise
    except Exception as error:
        raise TypeError(f"failed to compute MCP call digest for {type(value).__qualname__}") from error
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def record_mcp_call(
    *,
    server_slug: str,
    capability_type: str,
    capability_name: str,
    arguments: Any,
    result: Any,
    status: str,
    duration_ms: int | None,
    provenance: dict[str, Any] | None = None,
) -> None:
    context = get_mcp_execution_context()
    if context is None:
        return
    from yuxi.storage.postgres.manager import pg_manager

    try:
        arguments_digest = stable_digest(arguments)
    except TypeError as error:
        logger.warning(f"Skipped MCP call audit for server={server_slug} capability={capability_name}: {error}")
        return
    try:
        result_digest = stable_digest(result)
    except TypeError as error:
        logger.warning(f"Skipped MCP call audit for server={server_slug} capability={capability_name}: {error}")
        return
    try:
        async with pg_manager.get_async_session_context() as session:
            session.add(
                MCPCallAudit(
                    tenant_id=context.tenant_id,
                    uid=context.uid,
                    run_id=context.run_id,
                    agent_slug=context.agent_slug,
                    installation_id=context.installation_id,
                    server_slug=server_slug,
                    capability_type=capability_type,
                    capability_name=capability_name,
                    arguments_digest=arguments_digest,
                    result_digest=result_digest,
                    status=status,
                    duration_ms=duration_ms,
                    data_access_level=context.data_access_level,
                    provenance=dict(provenance or {}),
                )
            )
            await session.commit()
    except Exception as exc:  # auditing must be visible but must not duplicate the scientific operation
        logger.error(f"Failed to persist MCP call audit server={server_slug} capability={capability_name}: {exc!r}")


__all__ = [
    "McpExecutionContext",
    "set_mcp_execution_context",
    "reset_mcp_execution_context",
    "get_mcp_execution_context",
    "stable_digest",
    "record_mcp_call",
]
