"""Per-run MCP identity and append-only scientific call provenance."""

from __future__ import annotations

import contextvars
import hashlib
import json
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
    _MCP_EXECUTION_CONTEXT.reset(token)


def get_mcp_execution_context() -> McpExecutionContext | None:
    return _MCP_EXECUTION_CONTEXT.get()


def stable_digest(value: Any) -> str:
    try:
        payload = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))
    except Exception:
        payload = str(value)
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
                    arguments_digest=stable_digest(arguments),
                    result_digest=stable_digest(result),
                    status=status,
                    duration_ms=duration_ms,
                    data_access_level=context.data_access_level,
                    provenance=dict(provenance or {}),
                )
            )
            await session.commit()
    except Exception as exc:  # auditing must be visible but must not duplicate the scientific operation
        logger.error(f"Failed to persist MCP call audit server={server_slug} capability={capability_name}: {exc}")


__all__ = [
    "McpExecutionContext",
    "set_mcp_execution_context",
    "reset_mcp_execution_context",
    "get_mcp_execution_context",
    "stable_digest",
    "record_mcp_call",
]
