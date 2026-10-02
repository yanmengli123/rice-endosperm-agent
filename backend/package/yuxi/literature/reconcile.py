"""Scientific PDF 权威解析到文献域的幂等登记桥。

该模块位于 literature 域，由 services 编排层调用；knowledge 不反向依赖
literature。即时挂接失败时，cron 以 PostgreSQL 活跃解析版本为事实源补偿。
"""

from __future__ import annotations

import logging

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from yuxi.literature.notes.service import register_literature_paper
from yuxi.literature.reading.service import enqueue_reader_render
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeFile,
    KnowledgeParseRevision,
    LiteraturePaper,
    LiteratureReaderRender,
    LiteratureSourceEvent,
)
from yuxi.utils.datetime_utils import utc_now

logger = logging.getLogger(__name__)


async def record_source_event(session, revision_id: str, tenant_id: int) -> None:
    """Called inside the authority activation transaction, with no separate commit."""
    await session.execute(
        pg_insert(LiteratureSourceEvent)
        .values(
            revision_id=revision_id,
            tenant_id=tenant_id,
            status="PENDING",
            attempt=0,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        .on_conflict_do_nothing(index_elements=("revision_id",))
    )


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
                    KnowledgeParseRevision.pipeline_version.like("scientific_pdf_%"),
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
        render = await enqueue_reader_render(revision_id=revision_id)
    async with pg_manager.get_async_session_context() as session:
        await record_source_event(session, revision_id, int(revision.tenant_id))
        event = (
            await session.execute(select(LiteratureSourceEvent).where(LiteratureSourceEvent.revision_id == revision_id))
        ).scalar_one()
        event.status = "COMPLETED" if enqueue_render else "PENDING"
        event.error = None
        event.updated_at = utc_now()
    return {
        "revision_id": revision_id,
        "paper_id": paper.id,
        "rid": paper.rid,
        "status": "REGISTERED",
        "render": render,
    }


async def reconcile_scientific_literature_papers(ctx: dict | None = None, *, limit: int = 100) -> int:
    """Repair missing registration, missing render rows and pending activation events."""
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
                    .outerjoin(
                        LiteratureReaderRender, LiteratureReaderRender.revision_id == KnowledgeParseRevision.revision_id
                    )
                    .outerjoin(
                        LiteratureSourceEvent, LiteratureSourceEvent.revision_id == KnowledgeParseRevision.revision_id
                    )
                    .where(
                        KnowledgeParseRevision.pipeline_version.like("scientific_pdf_%"),
                        or_(
                            LiteraturePaper.id.is_(None),
                            LiteratureReaderRender.id.is_(None),
                            LiteratureSourceEvent.status == "PENDING",
                        ),
                    )
                    .group_by(KnowledgeParseRevision.revision_id, KnowledgeParseRevision.completed_at)
                    .order_by(KnowledgeParseRevision.completed_at.asc())
                    .limit(limit)
                )
            ).scalars()
        )
    completed = 0
    for revision_id in revisions:
        try:
            result = await register_scientific_revision(str(revision_id), enqueue_render=True)
            if result["status"] == "REGISTERED":
                completed += 1
        except Exception as exc:  # one failed delivery must not starve other active sources
            logger.exception("文献登记补偿失败: revision=%s", revision_id)
            async with pg_manager.get_async_session_context() as session:
                revision = (
                    await session.execute(
                        select(KnowledgeParseRevision).where(KnowledgeParseRevision.revision_id == revision_id)
                    )
                ).scalar_one()
                await record_source_event(session, str(revision_id), int(revision.tenant_id))
                event = (
                    await session.execute(
                        select(LiteratureSourceEvent).where(LiteratureSourceEvent.revision_id == revision_id)
                    )
                ).scalar_one()
                event.attempt += 1
                event.error = f"{type(exc).__name__}: {exc}"[:2000]
                event.updated_at = utc_now()
    return completed
