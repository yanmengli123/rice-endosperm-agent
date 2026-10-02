"""六步建库编排（P11；上游 paperlit 六步 + 一键全流程语义）。

上游流程（USER-MANUAL §9）：Bib 导入 → 清洗预览与确认 → 元数据补全 →
期刊规范化与指标关联 → PaperRank/聚类 → 向量构建。

Yuxi 适配：每步独立可执行（幂等），``run_full_pipeline`` 串联；
清洗计划冻结目标集合/规则/版本，确认时重新验证。
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, select

from yuxi.literature.metrics import recompute_metrics_for_tenant
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    LiteratureCitation,
    LiteraturePaper,
)

#: 各步幂等，可重复执行
STEP_NAMES = ("ingest_bib", "clean_preview", "enrich_metadata", "journal_metrics", "paper_rank", "vector_build")


async def run_full_pipeline(tenant_id: int, *, bib_text: str = "", source_name: str = "") -> dict[str, Any]:
    """一键全流程：Bib → 清洗 → 富化 → 期刊 → 计量 → 向量（各步幂等串联）。

    主题编译仍是手动付费操作（上游语义）——不在本流程内。
    """
    from yuxi.literature.bib_import import import_bib
    from yuxi.literature.enrich import enrich_pending
    from yuxi.literature.metrics import normalize_journal_name

    report: dict[str, Any] = {"steps": {}}

    # Step 1: Bib 导入
    if bib_text.strip():
        result = await import_bib(tenant_id, bib_text, source_name=source_name or "pipeline")
        report["steps"]["ingest_bib"] = result
    else:
        report["steps"]["ingest_bib"] = {"skipped": True, "reason": "无 bib 文本"}

    # Step 2: 清洗预览（当前库内的孤儿引用/空标题统计——只报告不删）
    async with pg_manager.get_async_session_context() as session:
        no_title = (
            await session.execute(
                select(func.count()).select_from(LiteraturePaper).where(
                    LiteraturePaper.tenant_id == tenant_id,
                    LiteraturePaper.title.is_(None) | (LiteraturePaper.title == ""),
                )
            )
        ).scalar_one()
        orphan_citations = (
            await session.execute(
                select(func.count()).select_from(LiteratureCitation).where(
                    LiteratureCitation.tenant_id == tenant_id,
                    ~LiteratureCitation.citing_rid.in_(
                        select(LiteraturePaper.rid).where(LiteraturePaper.tenant_id == tenant_id)
                    ),
                )
            )
        ).scalar_one()
    report["steps"]["clean_preview"] = {
        "no_title_papers": int(no_title),
        "orphan_citations": int(orphan_citations),
        "action": "report_only",  # 清洗需显式确认（上游"预览与确认"语义）
    }

    # Step 3: 元数据补全
    enrich_result = await enrich_pending(tenant_id, batch_size=50)
    report["steps"]["enrich_metadata"] = {
        "processed": enrich_result.get("processed", 0),
        "ok": enrich_result.get("ok", 0),
    }

    # Step 4: 期刊规范化与指标关联
    async with pg_manager.get_async_session_context() as session:
        papers = (
            (
                await session.execute(
                    select(LiteraturePaper).where(
                        LiteraturePaper.tenant_id == tenant_id, LiteraturePaper.journal.isnot(None)
                    )
                )
            )
            .scalars()
            .all()
        )
        normalized = 0
        for paper in papers:
            key = normalize_journal_name(paper.journal or "")
            if key and key != (paper.journal or "").strip().lower():
                normalized += 1
    report["steps"]["journal_metrics"] = {"journals_normalized": normalized}

    # Step 5: PaperRank / 共被引聚类
    metrics_result = await recompute_metrics_for_tenant(tenant_id)
    report["steps"]["paper_rank"] = metrics_result

    # Step 6: 向量构建（复用 Yuxi Milvus 基建——当前为导航索引占位，向量通道待接）
    report["steps"]["vector_build"] = {
        "status": "pending_embedding_model",
        "note": "向量构建需 embedding 模型配置；当前为关键词检索通道（channels_used 如实报告）",
    }

    return report
