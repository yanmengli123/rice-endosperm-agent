"""文献域质量指标聚合（观测缺口收口：拒绝率/降级率/修复成功率）。

口径（2026-10 投产验收 §观测）：
- 拒绝/降级：`answer.source_guard.completed` 轨迹事件的 guard_status 分布；
  fact_passed=false 计入事实门禁未过（拒绝面），DEGRADED 计入降级面。
- 修复成功率：repair_attempt_count>0 的事件中 guard_status=PASSED 的占比。
- 任务健康：编译/翻译/渲染任务按状态计数（DEAD/BLOCKED 即死信面）。
- 入库健康：pdf_evidence 解析版本按状态 + 重试预算消耗（attempt>=预算告警）。
- 登记修复：literature_source_events 状态分布（对账补偿的面）。

全部 SQL 聚合在库端完成，端点只投影；窗口 days 默认 7，上限 90。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import Integer, case, func, select

from yuxi.services.scientific_pdf_ingest_service import MAX_INGEST_ATTEMPTS
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeBase,
    KnowledgeParseRevision,
    LiteratureCompileJob,
    LiteratureReaderRender,
    LiteratureSourceEvent,
    LiteratureTranslationJob,
)
from yuxi.storage.postgres.models_trace import AgentRunTraceEvent
from yuxi.utils.datetime_utils import utc_now

_SOURCE_GUARD_EVENT = "answer.source_guard.completed"
_SOURCE_REFERENCE_EVENT = "knowledge.source_reference.completed"
_FIGURE_TABLE_PUBLICATION_EVENT = "knowledge.figure_table_publication.completed"
# 拒绝率/降级率/修复成功率按 days 窗口统计；任务/入库健康是当前状态快照，不设窗口。
# attributes 为 NULL 时 `->>` 返回 NULL，case 不命中即 else 0——无需 coalesce
# （且 dict 字面量会被当作绑定参数传给 asyncpg 而报 DataError）。
_ATTR = AgentRunTraceEvent.attributes


def _ratio(part: Any, whole: Any) -> float | None:
    whole_i = int(whole or 0)
    if whole_i <= 0:
        return None
    return round(float(part or 0) / whole_i, 4)


async def quality_metrics(tenant_id: int, *, days: int = 7) -> dict[str, Any]:
    days = max(1, min(int(days), 90))
    window_start = utc_now() - timedelta(days=days)

    async with pg_manager.get_async_session_context() as session:
        guard = (
            await session.execute(
                select(
                    func.count().label("total"),
                    func.sum(case((_ATTR.op("->>")("guard_status") == "PASSED", 1), else_=0)).label("passed"),
                    func.sum(case((_ATTR.op("->>")("guard_status") == "DEGRADED", 1), else_=0)).label("degraded"),
                    func.sum(case((_ATTR.op("->>")("fact_passed") == "false", 1), else_=0)).label("fact_failed"),
                    func.sum(
                        case(
                            (
                                (func.coalesce(_ATTR.op("->>")("repair_attempt_count"), "0").cast(Integer) > 0)
                                & (_ATTR.op("->>")("guard_status") == "PASSED"),
                                1,
                            ),
                            else_=0,
                        )
                    ).label("repair_succeeded"),
                    func.sum(
                        case(
                            (func.coalesce(_ATTR.op("->>")("repair_attempt_count"), "0").cast(Integer) > 0, 1), else_=0
                        )
                    ).label("repair_attempted"),
                )
                .select_from(AgentRunTraceEvent)
                .where(
                    AgentRunTraceEvent.tenant_id == tenant_id,
                    AgentRunTraceEvent.event_type == _SOURCE_GUARD_EVENT,
                    AgentRunTraceEvent.created_at >= window_start,
                )
            )
        ).one()

        source_references = (
            await session.execute(
                select(
                    func.count().label("events"),
                    func.coalesce(func.sum(func.coalesce(_ATTR.op("->>")("source_count"), "0").cast(Integer)), 0).label(
                        "sources"
                    ),
                    func.coalesce(func.sum(func.coalesce(_ATTR.op("->>")("target_count"), "0").cast(Integer)), 0).label(
                        "targets"
                    ),
                    func.coalesce(
                        func.sum(func.coalesce(_ATTR.op("->>")("resolved_count"), "0").cast(Integer)), 0
                    ).label("resolved"),
                    func.coalesce(
                        func.sum(func.coalesce(_ATTR.op("->>")("missing_count"), "0").cast(Integer)), 0
                    ).label("missing"),
                    func.coalesce(
                        func.sum(func.coalesce(_ATTR.op("->>")("ambiguous_count"), "0").cast(Integer)), 0
                    ).label("ambiguous"),
                    func.coalesce(
                        func.sum(func.coalesce(_ATTR.op("->>")("budget_exceeded_count"), "0").cast(Integer)), 0
                    ).label("budget_exceeded"),
                    func.sum(case((_ATTR.op("->>")("status") == "UNAVAILABLE", 1), else_=0)).label(
                        "unavailable_events"
                    ),
                )
                .select_from(AgentRunTraceEvent)
                .where(
                    AgentRunTraceEvent.tenant_id == tenant_id,
                    AgentRunTraceEvent.event_type == _SOURCE_REFERENCE_EVENT,
                    AgentRunTraceEvent.created_at >= window_start,
                )
            )
        ).one()

        publication = (
            await session.execute(
                select(
                    func.count().label("events"),
                    *[
                        func.coalesce(func.sum(func.coalesce(_ATTR.op("->>")(name), "0").cast(Integer)), 0).label(name)
                        for name in (
                            "source_quote_count",
                            "figure_ref_count",
                            "figure_asset_count",
                            "table_count",
                            "visual_verified_count",
                            "caption_only_count",
                            "reference_only_count",
                            "panel_verified_count",
                            "whole_figure_fallback_count",
                            "caption_fallback_count",
                        )
                    ],
                )
                .select_from(AgentRunTraceEvent)
                .where(
                    AgentRunTraceEvent.tenant_id == tenant_id,
                    AgentRunTraceEvent.event_type == _FIGURE_TABLE_PUBLICATION_EVENT,
                    AgentRunTraceEvent.created_at >= window_start,
                )
            )
        ).one()

        compile_rows = (
            await session.execute(
                select(LiteratureCompileJob.status, func.count())
                .where(LiteratureCompileJob.tenant_id == tenant_id)
                .group_by(LiteratureCompileJob.status)
            )
        ).all()
        translate_rows = (
            await session.execute(
                select(LiteratureTranslationJob.status, func.count())
                .where(LiteratureTranslationJob.tenant_id == tenant_id)
                .group_by(LiteratureTranslationJob.status)
            )
        ).all()
        render_rows = (
            await session.execute(
                select(LiteratureReaderRender.status, func.count())
                .join(KnowledgeParseRevision, KnowledgeParseRevision.revision_id == LiteratureReaderRender.revision_id)
                .join(KnowledgeBase, KnowledgeBase.kb_id == KnowledgeParseRevision.kb_id)
                .where(KnowledgeBase.tenant_id == tenant_id)
                .group_by(LiteratureReaderRender.status)
            )
        ).all()
        ingest_rows = (
            await session.execute(
                select(
                    KnowledgeParseRevision.status,
                    func.count(),
                    func.sum(case((KnowledgeParseRevision.attempt >= MAX_INGEST_ATTEMPTS, 1), else_=0)),
                )
                .join(KnowledgeBase, KnowledgeBase.kb_id == KnowledgeParseRevision.kb_id)
                .where(KnowledgeBase.tenant_id == tenant_id, KnowledgeBase.contract_key == "pdf_evidence")
                .group_by(KnowledgeParseRevision.status)
            )
        ).all()
        repair_rows = (
            await session.execute(
                select(
                    LiteratureSourceEvent.status,
                    func.count(),
                    func.coalesce(func.sum(LiteratureSourceEvent.attempt), 0),
                )
                .where(LiteratureSourceEvent.tenant_id == tenant_id)
                .group_by(LiteratureSourceEvent.status)
            )
        ).all()

    guard_total = int(guard.total or 0)
    return {
        "window_days": days,
        "source_guard": {
            "total": guard_total,
            "passed": int(guard.passed or 0),
            "degraded": int(guard.degraded or 0),
            "fact_failed": int(guard.fact_failed or 0),
            "degradation_rate": _ratio(guard.degraded, guard_total),
            "refusal_rate": _ratio(guard.fact_failed, guard_total),
            "repair_attempted": int(guard.repair_attempted or 0),
            "repair_success_rate": _ratio(guard.repair_succeeded, guard.repair_attempted),
        },
        "source_references": {
            "events": int(source_references.events or 0),
            "sources": int(source_references.sources or 0),
            "targets": int(source_references.targets or 0),
            "resolved": int(source_references.resolved or 0),
            "missing": int(source_references.missing or 0),
            "ambiguous": int(source_references.ambiguous or 0),
            "budget_exceeded": int(source_references.budget_exceeded or 0),
            "unavailable_events": int(source_references.unavailable_events or 0),
            "resolution_rate": _ratio(source_references.resolved, source_references.targets),
        },
        "figure_table_publication": {
            key: int(getattr(publication, key) or 0)
            for key in (
                "events",
                "source_quote_count",
                "figure_ref_count",
                "figure_asset_count",
                "table_count",
                "visual_verified_count",
                "caption_only_count",
                "reference_only_count",
                "panel_verified_count",
                "whole_figure_fallback_count",
                "caption_fallback_count",
            )
        },
        "jobs": {
            "compile": {status: int(n) for status, n in compile_rows},
            "translation": {status: int(n) for status, n in translate_rows},
            "render": {status: int(n) for status, n in render_rows},
        },
        "ingest": {
            "by_status": {status: int(n) for status, n, _ in ingest_rows},
            "attempt_budget_exhausted": int(sum(int(exh or 0) for _, _, exh in ingest_rows)),
            "attempt_budget": MAX_INGEST_ATTEMPTS,
        },
        "registration_repair": {
            "by_status": {status: int(n) for status, n, _ in repair_rows},
            "total_attempts": int(sum(int(a or 0) for _, _, a in repair_rows)),
        },
    }
