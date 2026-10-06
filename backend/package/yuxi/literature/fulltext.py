"""PostgreSQL GIN fulltext navigation; no query-time object-store corpus scan.

The projection is expendable, never evidence. Its URI must still equal the
current successful L1 job and source/read/card identities are checked in SQL.
Chinese bigrams are indexed as lexemes; no PostgreSQL language plugin required.
"""

from __future__ import annotations

import hashlib
import logging
import re
import asyncio
import time
from datetime import timedelta

from sqlalchemy import Integer, and_, cast, exists, func, literal_column, or_, select
from sqlalchemy.orm import aliased

from yuxi.literature.access import paper_visibility
from yuxi.literature.provenance import current_job
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import LiteratureCompileJob, LiteratureNoteSearch, LiteraturePaper
from yuxi.utils.datetime_utils import utc_now

logger = logging.getLogger(__name__)
MAX_NOTE_BYTES = 5 * 1024 * 1024


def latest_current_note():
    """Only the same note selected by the editor/reader can contribute hits."""
    newer = aliased(LiteratureCompileJob)
    return ~exists(
        select(newer.id)
        .where(
            newer.paper_id == LiteratureCompileJob.paper_id,
            newer.tenant_id == LiteratureCompileJob.tenant_id,
            newer.level == LiteratureCompileJob.level,
            newer.status == "SUCCEEDED",
            newer.note_uri.isnot(None),
            current_job(newer),
            or_(
                newer.updated_at > LiteratureCompileJob.updated_at,
                and_(newer.updated_at == LiteratureCompileJob.updated_at, newer.id > LiteratureCompileJob.id),
            ),
        )
        .correlate(LiteratureCompileJob, LiteraturePaper)
    )


def lexical_text(body: str) -> str:
    english = re.findall(r"[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*", body.lower())
    chinese = [a + b for run in re.findall(r"[\u4e00-\u9fff]+", body) for a, b in zip(run, run[1:])]
    return " ".join(english + chinese)


async def project_note(db, job, content: str) -> None:
    """Caller owns the transaction and current job/paper lock."""
    if len(content.encode()) > MAX_NOTE_BYTES:
        raise ValueError("全文索引单篇笔记超过 5 MiB")
    row = await db.get(LiteratureNoteSearch, job.id)
    if row is None:
        row = LiteratureNoteSearch(job_id=job.id, tenant_id=job.tenant_id)
        db.add(row)
    row.note_uri, row.body = job.note_uri, content
    row.content_digest = hashlib.sha256(content.encode()).hexdigest()
    row.lexical_text, row.updated_at = lexical_text(content), utc_now()
    row.attempt, row.next_retry_at, row.error_code = 0, None, None


async def search_notes(tenant_id: int, terms: list[str], *, kind: str = "", limit: int = 100) -> list[dict]:
    terms = list(dict.fromkeys(terms))[:64]
    if not terms:
        return []
    async with pg_manager.get_async_session_context() as db:
        conditions = [
            paper_visibility(tenant_id),
            LiteratureNoteSearch.tenant_id == tenant_id,
            LiteratureCompileJob.tenant_id == tenant_id,
            LiteratureCompileJob.level.in_(("L1", "L2", "L3")),
            LiteratureCompileJob.status == "SUCCEEDED",
            current_job(LiteratureCompileJob),
            latest_current_note(),
            LiteratureNoteSearch.note_uri == LiteratureCompileJob.note_uri,
            LiteratureNoteSearch.error_code.is_(None),
        ]
        if kind:
            conditions.append(LiteraturePaper.kind == kind)
        if db.get_bind().dialect.name == "postgresql":
            # regconfig literal keeps this expression identical to the GIN index.
            config = literal_column("'simple'::regconfig")
            vector = func.to_tsvector(config, LiteratureNoteSearch.lexical_text)
            queries = [func.plainto_tsquery(config, term) for term in terms]
            match = or_(*(vector.op("@@")(query) for query in queries))
            rank = sum(func.ts_rank_cd(vector, query) for query in queries)
        else:  # SQLite contract tests; production always uses the GIN expression.
            matches = [
                LiteratureNoteSearch.body.ilike(
                    "%" + term.replace("/", "//").replace("%", "/%").replace("_", "/_") + "%", escape="/"
                )
                for term in terms
            ]
            match, rank = or_(*matches), sum(cast(item, Integer) for item in matches)
        positions = [
            func.nullif(
                func.strpos(func.lower(LiteratureNoteSearch.body), term)
                if db.get_bind().dialect.name == "postgresql"
                else func.instr(func.lower(LiteratureNoteSearch.body), term),
                0,
            )
            for term in terms
        ]
        start = func.coalesce(*positions, 121) - 120
        start = func.greatest(1, start) if db.get_bind().dialect.name == "postgresql" else func.max(1, start)
        excerpt = func.substr(LiteratureNoteSearch.body, start, 400)
        rows = (
            await db.execute(
                select(LiteraturePaper.rid, excerpt, rank.label("rank"), LiteratureCompileJob.level)
                .select_from(LiteratureNoteSearch)
                .join(LiteratureCompileJob, LiteratureCompileJob.id == LiteratureNoteSearch.job_id)
                .join(LiteraturePaper, LiteraturePaper.id == LiteratureCompileJob.paper_id)
                .where(*conditions, match)
                .order_by(rank.desc(), LiteraturePaper.rid, LiteratureCompileJob.updated_at.desc())
                .limit(max(1, min(limit, 500)))
            )
        ).all()
    seen, hits = set(), []
    for rid, body, score, level in rows:
        if rid not in seen:
            seen.add(rid)
            hits.append({"rid": rid, "body": body, "score": float(score), "level": level})
    return hits


async def sync_note(job_id: int) -> bool:
    """Read a changed artifact once, then CAS its projection against the job."""
    async with pg_manager.get_async_session_context() as db:
        job = (
            await db.execute(
                select(LiteratureCompileJob)
                .join(LiteraturePaper, LiteraturePaper.id == LiteratureCompileJob.paper_id)
                .where(
                    LiteratureCompileJob.id == job_id,
                    current_job(LiteratureCompileJob),
                    LiteratureCompileJob.level.in_(("L1", "L2", "L3")),
                    LiteratureCompileJob.status == "SUCCEEDED",
                    LiteratureCompileJob.note_uri.isnot(None),
                    LiteraturePaper.trashed_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if job is None:
            return False
        uri, tenant_id = job.note_uri, job.tenant_id
    from yuxi.storage.minio import get_minio_client
    from yuxi.literature.reading.service import RENDER_BUCKET

    data = await get_minio_client().adownload_file(RENDER_BUCKET, uri.removeprefix("kbasset://"))
    if len(data) > MAX_NOTE_BYTES:
        raise ValueError("全文索引单篇笔记超过 5 MiB")
    async with pg_manager.get_async_session_context() as db:
        job = (
            await db.execute(
                select(LiteratureCompileJob)
                .join(LiteraturePaper, LiteraturePaper.id == LiteratureCompileJob.paper_id)
                .where(
                    LiteratureCompileJob.id == job_id,
                    LiteratureCompileJob.tenant_id == tenant_id,
                    LiteratureCompileJob.note_uri == uri,
                    LiteratureCompileJob.status == "SUCCEEDED",
                    current_job(LiteratureCompileJob),
                    LiteraturePaper.trashed_at.is_(None),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if job is None:
            return False
        await project_note(db, job, data.decode("utf-8"))
        await db.commit()
    return True


async def reconcile_literature_fulltext(ctx=None) -> int:
    """Bounded backfill/repair; a broken artifact does not hide other papers."""
    async with pg_manager.get_async_session_context() as db:
        ids = list(
            (
                await db.execute(
                    select(LiteratureCompileJob.id)
                    .join(LiteraturePaper, LiteraturePaper.id == LiteratureCompileJob.paper_id)
                    .outerjoin(LiteratureNoteSearch, LiteratureNoteSearch.job_id == LiteratureCompileJob.id)
                    .where(
                        current_job(LiteratureCompileJob),
                        LiteratureCompileJob.level.in_(("L1", "L2", "L3")),
                        LiteratureCompileJob.status == "SUCCEEDED",
                        LiteratureCompileJob.note_uri.isnot(None),
                        LiteraturePaper.trashed_at.is_(None),
                        or_(
                            LiteratureNoteSearch.job_id.is_(None),
                            LiteratureNoteSearch.note_uri != LiteratureCompileJob.note_uri,
                        ),
                        or_(
                            LiteratureNoteSearch.next_retry_at.is_(None),
                            LiteratureNoteSearch.next_retry_at <= utc_now(),
                        ),
                    )
                    .order_by(LiteratureCompileJob.updated_at.desc())
                    .limit(100)
                )
            ).scalars()
        )
    completed = 0
    deadline = time.monotonic() + 50
    for job_id in ids:
        if time.monotonic() >= deadline:
            break
        try:
            completed += bool(await asyncio.wait_for(sync_note(job_id), timeout=min(10, deadline - time.monotonic())))
        except Exception as exc:  # noqa: BLE001 - persisted backoff prevents starvation by corrupt artifacts
            async with pg_manager.get_async_session_context() as db:
                job = await db.get(LiteratureCompileJob, job_id)
                if job is not None:
                    row = await db.get(LiteratureNoteSearch, job_id, with_for_update=True)
                    # A concurrent successful projection wins; never erase it.
                    if row is None or row.note_uri != job.note_uri:
                        if row is None:
                            row = LiteratureNoteSearch(
                                job_id=job_id,
                                tenant_id=job.tenant_id,
                                note_uri="",
                                content_digest="",
                                body="",
                                lexical_text="",
                            )
                            db.add(row)
                        row.attempt = int(row.attempt or 0) + 1
                        row.next_retry_at = utc_now() + timedelta(minutes=min(60, 2 ** min(row.attempt, 6)))
                        row.error_code = type(exc).__name__[:64]
                        await db.commit()
            logger.warning("文献全文导航投影退避重试: job=%s error=%s", job_id, type(exc).__name__)
    return completed
