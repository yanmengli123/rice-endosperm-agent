"""Scientific PDF 权威解析到文献域的幂等登记桥。

该模块位于 literature 域，由 services 编排层调用；knowledge 不反向依赖
literature。即时挂接失败时，cron 以 PostgreSQL 活跃解析版本为事实源补偿。
"""

from __future__ import annotations

from sqlalchemy import select

from yuxi.literature.notes.service import register_literature_paper
from yuxi.literature.reading.service import enqueue_reader_render
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import KnowledgeFile, KnowledgeParseRevision, LiteraturePaper


async def register_scientific_revision(revision_id: str, *, enqueue_render: bool = True) -> dict:
    """把一个已激活解析版本登记为文献；重复调用无副作用。"""
    async with pg_manager.get_async_session_context() as session:
        row = (
            await session.execute(
                select(KnowledgeParseRevision, KnowledgeFile)
                .join(KnowledgeFile, KnowledgeFile.file_id == KnowledgeParseRevision.file_id)
                .where(
                    KnowledgeParseRevision.revision_id == revision_id,
                    KnowledgeFile.active_parse_revision_id == revision_id,
                )
            )
        ).one_or_none()
    if row is None:
        return {"revision_id": revision_id, "status": "SKIPPED", "reason": "revision_not_active"}

    revision, file_row = row
    summary = dict(revision.article_summary or {})
    doi = str(summary.get("doi") or "").strip()
    title = str(summary.get("title") or file_row.original_filename or file_row.filename or "").strip()
    paper = await register_literature_paper(
        tenant_id=int(revision.tenant_id),
        doi=doi,
        fingerprint=str(revision.source_sha256),
        title=title,
        source_revision_id=revision_id,
    )
    render = None
    if enqueue_render:
        render = await enqueue_reader_render(revision_id)
    return {
        "revision_id": revision_id,
        "paper_id": paper.id,
        "rid": paper.rid,
        "status": "REGISTERED",
        "render": render,
    }


async def reconcile_scientific_literature_papers(ctx: dict | None = None, *, limit: int = 100) -> int:
    """补偿所有尚未登记的活跃科研 PDF 解析版本。"""
    del ctx
    async with pg_manager.get_async_session_context() as session:
        revisions = list(
            (
                await session.execute(
                    select(KnowledgeParseRevision.revision_id)
                    .join(KnowledgeFile, KnowledgeFile.active_parse_revision_id == KnowledgeParseRevision.revision_id)
                    .outerjoin(
                        LiteraturePaper,
                        (LiteraturePaper.tenant_id == KnowledgeParseRevision.tenant_id)
                        & (LiteraturePaper.source_revision_id == KnowledgeParseRevision.revision_id),
                    )
                    .where(LiteraturePaper.id.is_(None))
                    .order_by(KnowledgeParseRevision.completed_at.asc())
                    .limit(limit)
                )
            ).scalars()
        )
    completed = 0
    for revision_id in revisions:
        result = await register_scientific_revision(str(revision_id), enqueue_render=True)
        if result["status"] == "REGISTERED":
            completed += 1
    return completed
