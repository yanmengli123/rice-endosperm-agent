"""Checksum-bound attachment navigation, locally extracted and GIN indexed.

Default recall includes attachments, not source PDFs. Never evidence. Legacy
backfill verifies the original bytes instead of trusting an unhashed text URI.
"""

import asyncio
import hashlib
import logging
import time
from datetime import timedelta

from sqlalchemy import Integer, cast, func, literal_column, or_, select

from yuxi.literature.access import paper_visibility
from yuxi.literature.attachments import extract_content
from yuxi.literature.fulltext import lexical_text
from yuxi.literature.reading.service import RENDER_BUCKET
from yuxi.storage.minio import get_minio_client
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import LiteratureAttachment, LiteraturePaper
from yuxi.utils.datetime_utils import utc_now

logger = logging.getLogger(__name__)


async def search_attachments(tenant_id: int, terms: list[str], *, kind: str = "", limit: int = 100) -> list[dict]:
    terms = list(dict.fromkeys(terms))[:64]
    if not terms:
        return []
    async with pg_manager.get_async_session_context() as db:
        conditions = [
            paper_visibility(tenant_id),
            LiteratureAttachment.tenant_id == tenant_id,
            LiteratureAttachment.text_digest.isnot(None),
            LiteratureAttachment.text_error.is_(None),
            LiteratureAttachment.text_uri.isnot(None),
        ]
        if kind:
            conditions.append(LiteraturePaper.kind == kind)
        postgres = db.get_bind().dialect.name == "postgresql"
        if postgres:
            config = literal_column("'simple'::regconfig")
            vector = func.to_tsvector(config, func.coalesce(LiteratureAttachment.text_lexical, ""))
            queries = [func.plainto_tsquery(config, term) for term in terms]
            match = or_(*(vector.op("@@")(query) for query in queries))
            rank = sum(func.ts_rank_cd(vector, query) for query in queries)
        else:
            matches = [
                LiteratureAttachment.text_body.ilike(
                    "%" + term.replace("/", "//").replace("%", "/%").replace("_", "/_") + "%", escape="/"
                )
                for term in terms
            ]
            match, rank = or_(*matches), sum(cast(item, Integer) for item in matches)
        positions = [
            func.nullif(
                func.strpos(func.lower(LiteratureAttachment.text_body), term)
                if postgres
                else func.instr(func.lower(LiteratureAttachment.text_body), term),
                0,
            )
            for term in terms
        ]
        start = func.coalesce(*positions, 121) - 120
        start = func.greatest(1, start) if postgres else func.max(1, start)
        rows = (
            await db.execute(
                select(
                    LiteraturePaper.rid,
                    LiteratureAttachment.id,
                    LiteratureAttachment.filename,
                    func.substr(LiteratureAttachment.text_body, start, 400),
                    rank.label("rank"),
                )
                .join(LiteraturePaper, LiteraturePaper.id == LiteratureAttachment.paper_id)
                .where(*conditions, match)
                .order_by(rank.desc(), LiteratureAttachment.id)
                .limit(max(1, min(limit, 500)))
            )
        ).all()
    seen, hits = set(), []
    for rid, attachment_id, filename, body, score in rows:
        if rid not in seen:
            seen.add(rid)
            hits.append(
                {
                    "rid": rid,
                    "attachment_id": attachment_id,
                    "filename": filename,
                    "excerpt": body,
                    "score": float(score),
                }
            )
    return hits


async def sync_attachment(attachment_id: int) -> bool:
    async with pg_manager.get_async_session_context() as db:
        row = await db.get(LiteratureAttachment, attachment_id)
        if row is None or row.text_digest is not None:
            return False
        identity = (row.tenant_id, row.content_digest, row.uri, row.paper_id)
        filename = row.filename
    data = await get_minio_client().adownload_file(RENDER_BUCKET, identity[2].removeprefix("kbasset://"))
    if hashlib.sha256(data).hexdigest() != identity[1]:
        raise ValueError("附件原始内容校验失败")
    text = await asyncio.to_thread(extract_content, data, filename)
    digest = hashlib.sha256(text.encode()).hexdigest()
    name = (
        f"tenants/{identity[0]}/literature/attachments/{identity[3]}/_text/{attachment_id}/{identity[1]}/{digest}.txt"
    )
    if text.strip():
        await get_minio_client().aupload_file(
            bucket_name=RENDER_BUCKET, object_name=name, data=text.encode(), content_type="text/plain"
        )
    async with pg_manager.get_async_session_context() as db:
        row = await db.get(LiteratureAttachment, attachment_id, with_for_update=True)
        if (
            row is None
            or (row.tenant_id, row.content_digest, row.uri, row.paper_id) != identity
            or row.text_digest is not None
        ):
            return False
        row.text_digest, row.text_body, row.text_lexical = digest, text, lexical_text(text)
        row.text_uri = f"kbasset://{name}" if text.strip() else None
        row.text_error, row.text_retry_at, row.text_attempt = None, None, 0
        await db.commit()
    return True


async def reconcile_literature_attachments(ctx=None) -> int:
    async with pg_manager.get_async_session_context() as db:
        ids = list(
            (
                await db.execute(
                    select(LiteratureAttachment.id)
                    .join(LiteraturePaper, LiteraturePaper.id == LiteratureAttachment.paper_id)
                    .where(
                        LiteraturePaper.trashed_at.is_(None),
                        LiteratureAttachment.text_digest.is_(None),
                        or_(
                            LiteratureAttachment.text_retry_at.is_(None),
                            LiteratureAttachment.text_retry_at <= utc_now(),
                        ),
                        or_(
                            *(
                                LiteratureAttachment.filename.ilike(f"%.{ext}")
                                for ext in ("pdf", "docx", "doc", "txt", "md")
                            )
                        ),
                    )
                    .order_by(LiteratureAttachment.id)
                    .limit(100)
                )
            ).scalars()
        )
    completed, deadline = 0, time.monotonic() + 50
    for attachment_id in ids:
        if time.monotonic() >= deadline:
            break
        try:
            completed += bool(
                await asyncio.wait_for(sync_attachment(attachment_id), min(10, deadline - time.monotonic()))
            )
        except Exception as exc:  # noqa: BLE001 - durable bounded backoff, no provider error body
            async with pg_manager.get_async_session_context() as db:
                row = await db.get(LiteratureAttachment, attachment_id, with_for_update=True)
                if row is not None and row.text_digest is None:
                    row.text_attempt += 1
                    row.text_retry_at = utc_now() + timedelta(minutes=min(60, 2 ** min(row.text_attempt, 6)))
                    row.text_error = type(exc).__name__[:64]
                    await db.commit()
            logger.warning("附件全文投影重试: attachment=%s error=%s", attachment_id, type(exc).__name__)
    return completed
