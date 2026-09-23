"""每日 MCP live canary：离线契约证明不了远程持续可用，本服务补位。

定位（企业级观测闭环）：
- 每台启用且 READY 的服务器跑两级探针——**连通级**（host.discover：可达性
  与工具清单）与**数据级**（已知参数形态的真实调用，当前覆盖 gene-authority
  的 Europe PMC 检索；探针表按服务器扩展，参数形态确证一个加一个）；
- 指标走 trace 事件（`mcp.canary.probe` / `mcp.canary.completed`）：
  成功率、空结果率、p95 延迟——复用现有轨迹查询即可观测，不引入新指标面；
- canary 失败绝不告警风暴：结果只落轨迹与日志，由监控侧按 success_rate
  阈值决定升级。

运行时上下文：canary 在 worker 进程执行，无用户会话——`McpExecutionContext`
以系统身份（tenant=默认租户）设置，探针审计照常落 ``mcp_call_audit``（数据级
探针本就是真实调用，审计链完整）。
"""

from __future__ import annotations

import time
from typing import Any

from yuxi.utils import logger

#: 数据级探针表：slug → (tool_name, args)。只登记参数形态已确证的工具；
#: 未登记的服务只跑连通级探针。
CANARY_DATA_PROBES: dict[str, tuple[str, dict[str, Any]]] = {
    "gene-authority": ("europe_pmc_search_rest", {"query": "WAXY rice endosperm", "page_size": 1}),
}

_EMPTY_RESULT_STATUSES = frozenset({"NOT_FOUND", "NO_EVIDENCE"})


def _p95(samples: list[float]) -> int:
    if not samples:
        return 0
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, round(0.95 * (len(ordered) + 1)) - 1))
    return int(ordered[index])


async def _probe_discovery(slug: str, runtime_config: dict[str, Any]) -> tuple[bool, int, str]:
    """连通级探针：host.discover 可达性 + 工具数量。"""
    from yuxi.agents.mcp.host import get_host

    descriptors, _info = await get_host().discover(slug, runtime_config, force_refresh=True, update_cache=False)
    return True, len(descriptors), ""


async def _probe_data(slug: str, runtime_config: dict[str, Any]) -> tuple[bool, str, int]:
    """数据级探针：真实调用一个已知参数形态的工具，返回 (ok, provider_status, elapsed_ms)。"""
    from yuxi.agents.mcp.host import get_host

    tool_name, args = CANARY_DATA_PROBES[slug]
    started = time.monotonic()
    result = await get_host().call_tool(slug, runtime_config, tool_name, dict(args))
    elapsed = int((time.monotonic() - started) * 1000)
    status = str((result.provenance or {}).get("provider_status") or ("ERROR" if result.is_error else "OK"))
    return (not result.is_error), status, elapsed


async def run_mcp_live_canary() -> dict[str, Any]:
    """跑一轮全部服务器的 live canary，返回汇总（同时落 trace 事件）。"""
    from sqlalchemy import select
    from yuxi.agents.mcp.domain import McpLifecycleStatus
    from yuxi.agents.mcp.execution import McpExecutionContext, reset_mcp_execution_context, set_mcp_execution_context
    from yuxi.agents.mcp.service import build_runtime_config, get_enabled_mcp_server_config
    from yuxi.storage.postgres.manager import pg_manager
    from yuxi.storage.postgres.models_business import MCPServer
    from yuxi.trace import emit_trace

    token = set_mcp_execution_context(McpExecutionContext(tenant_id=1, uid="system:canary", run_id=None))
    probe_latencies: list[float] = []
    ok_count = 0
    server_count = 0
    empty_result_count = 0
    try:
        async with pg_manager.get_async_session_context() as session:
            servers = (
                (
                    await session.execute(
                        select(MCPServer.slug).where(
                            MCPServer.enabled == 1,
                            MCPServer.lifecycle_status == McpLifecycleStatus.READY.value,
                        )
                    )
                )
                .scalars()
                .all()
            )
        for slug in [slug for slug in servers if isinstance(slug, str) and slug]:
            server_count += 1
            started = time.monotonic()
            ok = False
            provider_status = ""
            tool_count = 0
            error = ""
            try:
                config = await get_enabled_mcp_server_config(slug)
                if config is None:
                    error = "server config unavailable"
                else:
                    runtime_config = build_runtime_config(slug, config)
                    ok, tool_count, error = await _probe_discovery(slug, runtime_config)
                    if ok and slug in CANARY_DATA_PROBES:
                        data_ok, provider_status, data_elapsed = await _probe_data(slug, runtime_config)
                        ok = data_ok
                        probe_latencies.append(float(data_elapsed))
                        if provider_status in _EMPTY_RESULT_STATUSES:
                            empty_result_count += 1
                    elif ok:
                        probe_latencies.append((time.monotonic() - started) * 1000)
            except Exception as exc:  # noqa: BLE001 —— canary 探针失败是数据点，不是任务失败
                ok = False
                error = f"{type(exc).__name__}: {exc}"[:200]
                provider_status = provider_status or "UNAVAILABLE"
            if ok:
                ok_count += 1
            try:
                emit_trace(
                    category="MCP",
                    operation="canary",
                    event_type="mcp.canary.probe",
                    attributes={
                        "mcp_server": slug,
                        "probe": "discovery+data" if slug in CANARY_DATA_PROBES else "discovery",
                        "ok": ok,
                        "provider_status": provider_status or "OK",
                        "elapsed_ms": int((time.monotonic() - started) * 1000),
                        "tool_count": tool_count,
                        "error": error,
                    },
                    visibility="USER",
                )
            except Exception:  # noqa: BLE001 —— 轨迹绝不影响 canary
                pass
    finally:
        reset_mcp_execution_context(token)

    summary = {
        "server_count": server_count,
        "ok_count": ok_count,
        "success_rate": round(ok_count / server_count, 4) if server_count else 0.0,
        "p95_latency_ms": _p95(probe_latencies),
        "empty_result_count": empty_result_count,
    }
    try:
        emit_trace(
            category="MCP",
            operation="canary",
            event_type="mcp.canary.completed",
            attributes=summary,
            visibility="USER",
        )
    except Exception:  # noqa: BLE001
        pass
    logger.info(f"MCP live canary completed: {summary}")
    return summary


__all__ = ["CANARY_DATA_PROBES", "run_mcp_live_canary"]
