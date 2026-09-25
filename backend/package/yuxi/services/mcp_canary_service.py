"""每日 MCP live canary：离线契约证明不了远程持续可用，本服务补位。

定位（企业级观测闭环）：
- default-chatbot 的七个科研 MCP 跑两级探针——**连通级**（host.discover：
  可达性与工具清单）与**数据级**（真实调用 + 黄金标志校验）；通用 UI 工具
  扩展不混入科研来源 SLO；
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
import uuid
from dataclasses import dataclass
from typing import Any

from yuxi.utils import logger


@dataclass(frozen=True)
class CanaryDataProbe:
    tool_name: str
    arguments: dict[str, Any]
    expected_markers: tuple[str, ...]


#: 本 canary 的健康域是 default-chatbot 绑定的七个科研 MCP；memory/filesystem/
#: fetch/chart 等通用扩展依赖临时 npm/PyPI 安装，不属于科研来源 SLO，混入会把
#: 包镜像网络抖动错误计算为权威数据源故障。
CANARY_SERVER_SLUGS = frozenset(
    {"ricekb", "bio-mcp", "ricekb-profile", "gramene", "plant-genomics", "data-aggregator", "gene-authority"}
)

#: 数据级黄金探针：除工具成功外，还要求返回稳定的已知标志，防止 HTTP 200/
#: 空对象被误报健康。参数均经过 live 验证，并保持有界结果集。
CANARY_DATA_PROBES: dict[str, CanaryDataProbe] = {
    "ricekb": CanaryDataProbe("ricekb_resolve", {"query": "Wx", "limit": 3}, ("Os06g0133000",)),
    "bio-mcp": CanaryDataProbe(
        "plant_gene_lookup",
        {"symbol": "Os06g0133000", "species": "oryza_sativa"},
        ("Os06g0133000", "chr6"),
    ),
    "ricekb-profile": CanaryDataProbe(
        "ricekb_gene_profile", {"identifier": "Wx"}, ('"status": "FOUND"', "Os06g0133000")
    ),
    "gramene": CanaryDataProbe(
        "genes_in_region",
        {"region": "6", "start": 1_750_000, "end": 1_780_000, "taxon_id": 4530, "rows": 10},
        ("Os06g0133000",),
    ),
    "plant-genomics": CanaryDataProbe(
        "ensembl_plants_lookup_locus",
        {"locus": "AT1G01010", "organism": "arabidopsis_thaliana"},
        ("AT1G01010", "NAC001"),
    ),
    "data-aggregator": CanaryDataProbe(
        "search",
        {"query": "WAXY rice endosperm", "size": 1, "sources": ["literature"]},
        ('"count": 1', "pubmed:"),
    ),
    "gene-authority": CanaryDataProbe(
        "europe_pmc_search_rest",
        {"query": "WAXY rice endosperm", "page_size": 1},
        ("WAXY", "resultList"),
    ),
}

# Contract probes exercise high-value operation shapes that a single generic
# server probe cannot cover.  They are deliberately bounded and read-only.
CANARY_CONTRACT_PROBES: dict[str, tuple[CanaryDataProbe, ...]] = {
    "ricekb": (
        CanaryDataProbe(
            "ricekb_sequence",
            {"source": "RAP_DB", "sequence_type": "cds", "sequence_id": "Os06t0133000-01"},
            ("Os06t0133000-01", "cds", "1830"),
        ),
    ),
    "gene-authority": (
        CanaryDataProbe(
            "ncbi_datasets_gene_report_rest",
            {"identifiers": ["Wx"], "identifier_type": "symbol", "taxon": "Oryza sativa", "page_size": 5},
            ("4340018", "Oryza sativa"),
        ),
        CanaryDataProbe(
            "ncbi_datasets_gene_summary_cli",
            {"identifiers": ["4340018"], "identifier_type": "gene-id", "taxon": None},
            ("4340018", "NCBI_DATASETS_CLI"),
        ),
        CanaryDataProbe(
            "uniprot_search_rest",
            {"query": "gene:Wx AND organism_id:4530", "size": 3, "reviewed_only": True},
            ("primaryAccession", "Oryza sativa"),
        ),
    ),
}

_EMPTY_RESULT_STATUSES = frozenset({"NOT_FOUND", "NO_EVIDENCE", "EMPTY", "CONTRACT_MISMATCH"})


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
    """数据级 + 关键参数契约探针，返回 (ok, provider_status, elapsed_ms)。"""
    from yuxi.agents.mcp.host import get_host

    started = time.monotonic()
    status = "OK"
    probes = (CANARY_DATA_PROBES[slug], *CANARY_CONTRACT_PROBES.get(slug, ()))
    for probe in probes:
        result = await get_host().call_tool(slug, runtime_config, probe.tool_name, dict(probe.arguments))
        elapsed = int((time.monotonic() - started) * 1000)
        status = str((result.provenance or {}).get("provider_status") or ("ERROR" if result.is_error else "OK"))
        if result.is_error or status.upper() in _EMPTY_RESULT_STATUSES:
            return False, status, elapsed
        text = str(result.text or "")
        if not text.strip():
            return False, "EMPTY", elapsed
        folded = text.casefold()
        if not all(marker.casefold() in folded for marker in probe.expected_markers):
            return False, "CONTRACT_MISMATCH", elapsed
    return True, status, int((time.monotonic() - started) * 1000)


async def run_mcp_live_canary() -> dict[str, Any]:
    """跑一轮全部服务器的 live canary，返回汇总（同时落 trace 事件）。"""
    from yuxi.agents.mcp.execution import McpExecutionContext, reset_mcp_execution_context, set_mcp_execution_context
    from yuxi.agents.mcp.service import build_runtime_config, get_enabled_mcp_server_config
    from yuxi.storage.postgres.manager import pg_manager
    from yuxi.storage.postgres.models_business import AgentRun
    from yuxi.trace import emit_trace
    from yuxi.trace.recorder import TraceRecorder

    # cron 不在聊天 run 上下文中；裸 emit_trace 会直接 no-op。为每轮 canary
    # 创建一个终态 system run，使用 ADMIN 可见轨迹持久化探针与汇总指标，且
    # 不向用户聊天流推送这些运维事件。
    canary_run_id = f"mcp-canary-{uuid.uuid4().hex}"
    recorder = TraceRecorder(
        run_id=canary_run_id,
        thread_id=canary_run_id,
        tenant_id=1,
        uid="system:canary",
        agent_slug="system-mcp-canary",
        run_type="mcp_canary",
        request_id=canary_run_id,
    )
    recorder.activate()
    try:
        token = set_mcp_execution_context(McpExecutionContext(tenant_id=1, uid="system:canary", run_id=canary_run_id))
        probe_latencies: list[float] = []
        ok_count = 0
        server_count = 0
        empty_result_count = 0
        try:
            # 遍历 SLO 期望集合，而不是只 SELECT enabled+READY：未安装、被禁用
            # 或生命周期退化都必须作为失败计入，不能从分母消失后形成“6/6 健康”。
            for slug in sorted(CANARY_SERVER_SLUGS):
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
                        visibility="ADMIN",
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
                visibility="ADMIN",
            )
        except Exception:  # noqa: BLE001
            pass

        # Trace 投影/Outbox 对 agent_runs 有外键：flush 前登记终态 system run。
        async with pg_manager.get_async_session_context() as session:
            session.add(
                AgentRun(
                    tenant_id=1,
                    id=canary_run_id,
                    conversation_thread_id=canary_run_id,
                    agent_slug="system-mcp-canary",
                    uid="system:canary",
                    status="completed",
                    request_id=canary_run_id,
                    run_type="mcp_canary",
                    input_payload={"canary_summary": summary},
                )
            )
        logger.info(f"MCP live canary completed: {summary}")
        return summary
    finally:
        await recorder.finalize()


__all__ = [
    "CANARY_CONTRACT_PROBES",
    "CANARY_DATA_PROBES",
    "CANARY_SERVER_SLUGS",
    "CanaryDataProbe",
    "run_mcp_live_canary",
]
