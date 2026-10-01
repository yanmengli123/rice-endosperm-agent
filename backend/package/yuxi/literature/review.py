"""识别复核与人工仲裁（G4，ADR-0010 决议 12）。

- 双通道渲染是**可选增强**：用户显式触发，子进程跑 vendor P14 完整链
  （paddle=True, ai_review=False——本地词典规则 + PDF 文本层第三信号，
  0 LLM 成本产出 review.json）；AI 综合仲裁（上游 dual_ai_review）后续以
  Yuxi 模型链替代接入。
- PaddleOCR 凭据经环境变量读取（YUXI_PADDLE_TOKEN），不落库。
- 四选一裁决（A=保留 MinerU / B=采用 Paddle / C=两者合并 / D=均不取）
  持久化 `literature_review_decisions`；修正文本的渲染回放属已知限制。
"""

from __future__ import annotations

import json
import os
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from yuxi.literature.reading.service import RENDER_BUCKET, enqueue_reader_render
from yuxi.storage.minio import get_minio_client
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeParseRevision,
    LiteratureReaderRender,
    LiteratureReviewDecision,
)

VALID_CHOICES = ("A", "B", "C", "D")


async def enqueue_dual_channel_render(revision_id: str) -> dict[str, Any]:
    """触发双通道渲染（可选增强；Paddle 凭据未配置时 fail-fast 明确报因）。"""
    token = os.getenv("YUXI_PADDLE_TOKEN", "")
    if not token:
        raise ValueError("未配置 PaddleOCR 凭据（YUXI_PADDLE_TOKEN），双通道复核不可用")
    return await enqueue_reader_render(revision_id=revision_id, force=True)


async def get_review_items(tenant_id: int, revision_id: str) -> dict[str, Any]:
    """读取 review.json 复核清单（MinIO work/ 前缀）+ 已裁决映射。"""
    async with pg_manager.get_async_session_context() as session:
        render = (
            await session.execute(
                select(LiteratureReaderRender).where(
                    LiteratureReaderRender.revision_id == revision_id,
                    LiteratureReaderRender.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if render is None:
            raise ValueError(f"解析版本无渲染记录: {revision_id}")
        revision_exists = (
            await session.execute(
                select(KnowledgeParseRevision.id).where(KnowledgeParseRevision.revision_id == revision_id)
            )
        ).scalar_one_or_none()
    if revision_exists is None:
        raise ValueError(f"解析版本不存在: {revision_id}")
    base = render.markdown_uri.removeprefix("kbasset://").removesuffix("render.md")
    review_object = f"{base}work/review.json"
    items: list[dict[str, Any]] = []
    try:
        data = await get_minio_client().adownload_file(RENDER_BUCKET, review_object)
        review = json.loads(data.decode("utf-8"))
        items = list(review.get("items") or [])
    except Exception:  # noqa: BLE001 - 无双通道产物（纯离线渲染）→ 空清单
        items = []
    decisions = await list_decisions(tenant_id, revision_id)
    decided = {d["report_idx"]: d for d in decisions}
    for item in items:
        idx = int(item.get("report_idx", -1))
        item["decision"] = decided.get(idx)
    blocking = sum(1 for item in items if item.get("blocking") and not item.get("decision"))
    return {
        "revision_id": revision_id,
        "total": len(items),
        "blocking_pending": blocking,
        "items": items,
        "decisions": decisions,
    }


async def decide(
    tenant_id: int,
    revision_id: str,
    *,
    report_idx: int,
    choice: str,
    resolved_text: str | None = None,
    para_id: str = "",
    decided_by: str = "",
) -> dict[str, Any]:
    """四选一仲裁（幂等 upsert：同 (tenant, revision, report_idx) 覆盖）。"""
    if choice not in VALID_CHOICES:
        raise ValueError(f"非法裁决: {choice}（可选 {VALID_CHOICES}）")
    if choice in ("B", "C", "D") and not (resolved_text or "").strip():
        # B/C/D 需要结果文本（B=采用 Paddle、C=合并、D=均不取即删除标记文本）
        raise ValueError(f"裁决 {choice} 需提供 resolved_text")
    async with pg_manager.get_async_session_context() as session:
        stmt = (
            pg_insert(LiteratureReviewDecision)
            .values(
                tenant_id=tenant_id,
                revision_id=revision_id,
                report_idx=report_idx,
                para_id=para_id,
                choice=choice,
                resolved_text=resolved_text,
                decided_by=decided_by,
            )
            .on_conflict_do_update(
                index_elements=("tenant_id", "revision_id", "report_idx"),
                set_={
                    "choice": choice,
                    "resolved_text": resolved_text,
                    "decided_by": decided_by,
                },
            )
        )
        await session.execute(stmt)
        await session.commit()
    return {"revision_id": revision_id, "report_idx": report_idx, "choice": choice}


async def list_decisions(tenant_id: int, revision_id: str) -> list[dict[str, Any]]:
    async with pg_manager.get_async_session_context() as session:
        rows = (
            (
                await session.execute(
                    select(LiteratureReviewDecision).where(
                        LiteratureReviewDecision.tenant_id == tenant_id,
                        LiteratureReviewDecision.revision_id == revision_id,
                    )
                )
            )
            .scalars()
            .all()
        )
    return [
        {
            "report_idx": row.report_idx,
            "para_id": row.para_id,
            "choice": row.choice,
            "resolved_text": row.resolved_text,
            "decided_by": row.decided_by,
        }
        for row in rows
    ]
