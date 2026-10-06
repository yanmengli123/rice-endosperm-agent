"""Optional original-text navigation, projected once, never an EvidenceEnvelope."""

import asyncio
import hashlib
import json
import time
from datetime import UTC, timedelta

from sqlalchemy import Integer, cast, exists, func, literal_column, or_, select

from yuxi.literature.access import paper_visibility
from yuxi.literature.fulltext import lexical_text, MAX_NOTE_BYTES
from yuxi.literature.reader_bundle import get_reader_bundle
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import LiteraturePaper, LiteratureOriginalSearch, LiteratureReaderRender
from yuxi.utils.datetime_utils import utc_now, ensure_utc


def current_projection():
    return (
        (LiteratureOriginalSearch.updated_at >= LiteraturePaper.updated_at)
        & LiteratureOriginalSearch.error_code.is_(None)
        & ~exists(
            select(LiteratureReaderRender.id).where(
                LiteratureReaderRender.revision_id == LiteraturePaper.source_revision_id,
                LiteratureReaderRender.updated_at > LiteratureOriginalSearch.updated_at,
            )
        )
    )


async def _identity(db, paper):
    def timestamp(value):
        # SQLite strips tzinfo from these TIMESTAMPTZ test columns. Production
        # uses aware values; hash canonical UTC rather than connection timezone.
        if db.get_bind().dialect.name == "sqlite" and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return ensure_utc(value).isoformat()

    renders = (
        (
            await db.execute(
                select(LiteratureReaderRender.id, LiteratureReaderRender.updated_at)
                .where(LiteratureReaderRender.revision_id == paper.source_revision_id)
                .order_by(LiteratureReaderRender.id)
            )
        ).all()
        if paper.source_revision_id
        else []
    )
    return hashlib.sha256(
        json.dumps(
            [
                paper.source_revision_id,
                paper.reading_version,
                timestamp(paper.updated_at),
                paper.artifact_pointers,
                [(identifier, timestamp(updated)) for identifier, updated in renders],
            ],
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()


async def sync_original(paper_id: int) -> bool:
    async with pg_manager.get_async_session_context() as db:
        paper = await db.get(LiteraturePaper, paper_id)
        if paper is None or paper.trashed_at is not None:
            return False
        identity, tenant_id, rid = await _identity(db, paper), paper.tenant_id, paper.rid
    bundle = await get_reader_bundle(tenant_id, rid)
    if not bundle or not bundle.get("ready"):
        raise ValueError("当前原文未就绪")
    body = bundle["markdown"]
    if len(body.encode()) > MAX_NOTE_BYTES:
        raise ValueError("原文投影超过 5 MiB")
    async with pg_manager.get_async_session_context() as db:
        paper = (
            await db.execute(
                select(LiteraturePaper)
                .where(LiteraturePaper.id == paper_id, paper_visibility(tenant_id))
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if paper is None or await _identity(db, paper) != identity:
            raise ValueError("原文投影期间版本变更")
        row = await db.get(LiteratureOriginalSearch, paper_id)
        if row is None:
            row = LiteratureOriginalSearch(paper_id=paper_id, tenant_id=tenant_id)
            db.add(row)
        row.input_digest, row.body = identity, body
        row.content_digest = hashlib.sha256(body.encode()).hexdigest()
        row.lexical_text, row.updated_at = lexical_text(body), utc_now()
        row.attempt, row.next_retry_at, row.error_code = 0, None, None
        await db.commit()
    return True


async def search_originals(tenant_id: int, terms: list[str], *, kind="", limit=100) -> list[dict]:
    terms = list(dict.fromkeys(terms))[:64]
    if not terms:
        return []
    async with pg_manager.get_async_session_context() as db:
        if db.get_bind().dialect.name == "postgresql":
            config = literal_column("'simple'::regconfig")
            vector = func.to_tsvector(config, LiteratureOriginalSearch.lexical_text)
            matches = [vector.op("@@")(func.plainto_tsquery(config, term)) for term in terms]
            rank = sum(func.ts_rank_cd(vector, func.plainto_tsquery(config, term)) for term in terms)
            positions = [func.nullif(func.strpos(func.lower(LiteratureOriginalSearch.body), term), 0) for term in terms]
            start = func.greatest(1, func.coalesce(*positions, 121) - 120)
        else:
            from yuxi.literature.retrieval import escape_like

            matches = [LiteratureOriginalSearch.body.ilike(f"%{escape_like(term)}%", escape="\\") for term in terms]
            rank = sum(cast(match, Integer) for match in matches)
            positions = [func.nullif(func.instr(func.lower(LiteratureOriginalSearch.body), term), 0) for term in terms]
            start = func.max(1, func.coalesce(*positions, 121) - 120)
        rows = (
            await db.execute(
                select(LiteraturePaper.rid, func.substr(LiteratureOriginalSearch.body, start, 400))
                .join(LiteratureOriginalSearch, LiteratureOriginalSearch.paper_id == LiteraturePaper.id)
                .where(
                    paper_visibility(tenant_id),
                    LiteratureOriginalSearch.tenant_id == tenant_id,
                    current_projection(),
                    or_(*matches),
                    LiteraturePaper.kind == kind if kind else True,
                )
                .order_by(rank.desc(), LiteraturePaper.rid)
                .limit(max(1, min(limit, 500)))
            )
        ).all()
    return [{"rid": rid, "excerpt": excerpt} for rid, excerpt in rows]


async def reconcile_original_search(ctx=None) -> int:
    async with pg_manager.get_async_session_context() as db:
        ids = (
            (
                await db.execute(
                    select(LiteraturePaper.id)
                    .outerjoin(LiteratureOriginalSearch, LiteratureOriginalSearch.paper_id == LiteraturePaper.id)
                    .where(
                        LiteraturePaper.trashed_at.is_(None),
                        or_(LiteratureOriginalSearch.paper_id.is_(None), ~current_projection()),
                        or_(
                            LiteratureOriginalSearch.next_retry_at.is_(None),
                            LiteratureOriginalSearch.next_retry_at <= utc_now(),
                        ),
                    )
                    .order_by(LiteraturePaper.id)
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )
    completed, deadline = 0, time.monotonic() + 50
    for paper_id in ids:
        if time.monotonic() >= deadline:
            break
        try:
            completed += await asyncio.wait_for(sync_original(paper_id), timeout=min(10, deadline - time.monotonic()))
        except Exception as exc:  # noqa: BLE001 - bounded backoff, never publish broken text
            async with pg_manager.get_async_session_context() as db:
                paper = await db.get(LiteraturePaper, paper_id)
                row = await db.get(LiteratureOriginalSearch, paper_id, with_for_update=True)
                if paper is None or (row and row.error_code is None and row.input_digest == await _identity(db, paper)):
                    continue
                if row is None:
                    row = LiteratureOriginalSearch(
                        paper_id=paper_id,
                        tenant_id=paper.tenant_id,
                        input_digest="",
                        content_digest="",
                        body="",
                        lexical_text="",
                        attempt=0,
                    )
                    db.add(row)
                row.attempt += 1
                row.error_code = type(exc).__name__[:64]
                row.next_retry_at = utc_now() + timedelta(minutes=min(60, 2 ** min(row.attempt, 6)))
                row.updated_at = utc_now()
                await db.commit()
    return completed
