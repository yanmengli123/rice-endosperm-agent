from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
import weakref
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from yuxi.knowledge.chunking.ragflow_like.parsers.academic import ACADEMIC_CHUNKER_VERSION
from yuxi.knowledge.pdf_evidence.contracts import ParserArtifact, PipelineResult, UnifiedArticle
from yuxi.knowledge.pdf_evidence.pipeline import PIPELINE_VERSION, ScientificPdfPipeline, build_parser_fingerprint
from yuxi.knowledge.runtime import knowledge_base
from yuxi.knowledge.utils import is_minio_url, parse_minio_url
from yuxi.knowledge.utils.kb_utils import sanitize_processing_params
from yuxi.services.knowledge_asset_service import (
    build_kbasset_url,
    materialize_reused_revision_assets,
    rewrite_kbasset_uri,
)
from yuxi.services.run_queue_service import get_arq_pool
from yuxi.storage.minio import get_minio_client
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    ArticleReference,
    CitationMention,
    EvidenceAnchorRecord,
    KnowledgeBase,
    KnowledgeDocumentIdentityCache,
    KnowledgeFile,
    KnowledgeIndexRevision,
    KnowledgeParseArtifact,
    KnowledgeParseRevision,
    KnowledgeParseStage,
)
from yuxi.utils.datetime_utils import utc_now
from yuxi.utils.logging_config import logger

TERMINAL_PARSE_STATUSES = {"INDEXED_FULL", "INDEXED_CONTENT_ONLY", "INDEXED_TEXT_ONLY", "REJECTED"}
LEASE_MINUTES = 30
LEASE_HEARTBEAT_SECONDS = 60
PARSE_STAGE_NAMES = ("NATIVE", "MINERU", "GROBID", "UNIFIED", "QUALITY", "INDEX", "ACTIVATE")
_PDF_INGEST_SEMAPHORES: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


def _workflow_now():
    """Return an aware UTC timestamp for every TIMESTAMPTZ workflow field.

    Using a naive UTC value against PostgreSQL running in a non-UTC session
    shifts lease comparisons by the session offset and can prevent stale jobs
    from ever being recovered.
    """
    return utc_now()


def _pdf_ingest_concurrency_limit() -> int:
    raw = os.getenv("SCIENTIFIC_PDF_MAX_CONCURRENCY", "2")
    try:
        return min(max(int(raw), 1), 8)
    except ValueError:
        logger.warning("Invalid SCIENTIFIC_PDF_MAX_CONCURRENCY=%r; using 2", raw)
        return 2


def _pdf_ingest_semaphore() -> asyncio.Semaphore:
    """Return a loop-local gate so tests and worker reloads never share loop-bound state."""
    loop = asyncio.get_running_loop()
    semaphore = _PDF_INGEST_SEMAPHORES.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(_pdf_ingest_concurrency_limit())
        _PDF_INGEST_SEMAPHORES[loop] = semaphore
    return semaphore


def _digest(value: str | bytes) -> str:
    payload = value if isinstance(value, bytes) else value.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _revision_id(tenant_id: int, file_id: str, fingerprint: str) -> str:
    return f"spr_{_digest(f'{tenant_id}|{file_id}|{fingerprint}')[:40]}"


def _index_revision_id(parse_revision_id: str, chunker_fingerprint: str) -> str:
    return f"sir_{_digest(f'{parse_revision_id}|{chunker_fingerprint}')[:40]}"


def _scientific_chunking_contract() -> dict[str, Any]:
    return {
        "chunk_preset_id": "academic",
        "chunker_version": ACADEMIC_CHUNKER_VERSION,
        "chunk_parser_config": {
            "chunk_token_num": 600,
            "hard_token_limit": 900,
            "overlap_token_num": 64,
            "include_references": False,
        },
    }


def _persistent_processing_params(
    runtime_params: dict[str, Any], chunking: dict[str, Any]
) -> dict[str, Any]:
    """Return the JSON-safe configuration persisted on ``KnowledgeFile``.

    ``asset_uri_builder`` and ``image_prefix`` are execution capabilities, not
    document configuration.  They must stay inside the worker invocation: the
    callback cannot be encoded as JSON and the prefix exposes private storage
    layout.  Keeping this boundary explicit also prevents future parser hooks
    from accidentally entering PostgreSQL JSONB.
    """
    persistent = sanitize_processing_params(runtime_params) or {}
    # Other document parsers may deliberately persist their own image prefix,
    # so this PDF-private key is removed at the feature boundary rather than
    # from the shared sanitizer.
    persistent.pop("image_prefix", None)
    return {
        **persistent,
        **chunking,
        "pdf_evidence_pipeline": True,
    }


def _chunker_fingerprint(parse_revision_id: str, chunking: dict[str, Any]) -> str:
    return _digest(json.dumps({"parse_revision_id": parse_revision_id, **chunking}, sort_keys=True, ensure_ascii=False))


def _ingest_job_id(revision_id: str, attempt: int) -> str:
    """Keep concurrent submissions idempotent while allowing a failed attempt to be retried."""
    return f"scientific-pdf:{revision_id}:{max(int(attempt or 0), 0)}"


def _stage_id(revision_id: str, stage_name: str) -> str:
    return f"sps_{_digest(f'{revision_id}|{stage_name}')[:40]}"


def _safe_name(value: str) -> str:
    name = Path(value).name
    cleaned = "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in name)
    return cleaned[:180] or "artifact.bin"


async def _load_source(file_record: KnowledgeFile) -> bytes:
    source = str(file_record.path or file_record.minio_url or "")
    if not is_minio_url(source):
        raise ValueError("科研 PDF 原文必须位于受管 MinIO 存储中")
    bucket, object_name = parse_minio_url(source)
    return await get_minio_client().adownload_file(bucket, object_name)


async def create_or_reuse_scientific_pdf_ingest(
    *, kb_id: str, file_id: str, operator_id: str, enqueue: bool = True
) -> dict[str, Any]:
    async with pg_manager.get_async_session_context() as session:
        result = await session.execute(
            select(KnowledgeFile, KnowledgeBase)
            .join(KnowledgeBase, KnowledgeBase.kb_id == KnowledgeFile.kb_id)
            .where(KnowledgeFile.file_id == file_id, KnowledgeFile.kb_id == kb_id)
        )
        row = result.one_or_none()
        if row is None:
            raise ValueError("文档不存在")
        file_record, kb_record = row
        if str(file_record.file_type or "").lower() != "pdf":
            raise ValueError("PDF 文献证据库只接受 PDF 文件")
        tenant_id = int(kb_record.tenant_id or 1)
        source_sha256 = str(file_record.content_hash or "")
        processing_params = dict(file_record.processing_params or {})

    if len(source_sha256) != 64:
        source_sha256 = _digest(await _load_source(file_record))
    fingerprint = build_parser_fingerprint(source_sha256, processing_params)
    revision_id = _revision_id(tenant_id, file_id, fingerprint)

    async with pg_manager.get_async_session_context() as session:
        await session.execute(
            pg_insert(KnowledgeParseRevision)
            .values(
                revision_id=revision_id,
                tenant_id=tenant_id,
                kb_id=kb_id,
                file_id=file_id,
                source_sha256=source_sha256,
                parser_fingerprint=fingerprint,
                pipeline_version=PIPELINE_VERSION,
                status="PENDING",
                created_by=operator_id,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    KnowledgeParseRevision.tenant_id,
                    KnowledgeParseRevision.file_id,
                    KnowledgeParseRevision.parser_fingerprint,
                ]
            )
        )
        revision = (
            await session.execute(
                select(KnowledgeParseRevision).where(KnowledgeParseRevision.revision_id == revision_id)
            )
        ).scalar_one()
        file_row = (
            await session.execute(select(KnowledgeFile).where(KnowledgeFile.file_id == file_id))
        ).scalar_one()
        if revision.status in TERMINAL_PARSE_STATUSES - {"REJECTED"}:
            desired_fingerprint = _chunker_fingerprint(revision_id, _scientific_chunking_contract())
            active_index = None
            if file_row.active_index_revision_id:
                active_index = (
                    await session.execute(
                        select(KnowledgeIndexRevision).where(
                            KnowledgeIndexRevision.revision_id == file_row.active_index_revision_id
                        )
                    )
                ).scalar_one_or_none()
            if active_index is None or active_index.chunker_fingerprint != desired_fingerprint:
                # Parser artifacts are immutable and reusable. Re-open only the
                # durable index stages when chunker code/config changes.
                revision.status = "PENDING"
                revision.completed_at = None
                revision.error_message = None
                revision.reused_from_revision_id = revision_id
        for stage_name in PARSE_STAGE_NAMES:
            await session.execute(
                pg_insert(KnowledgeParseStage)
                .values(
                    stage_id=_stage_id(revision_id, stage_name),
                    revision_id=revision_id,
                    stage_name=stage_name,
                    status="PENDING",
                    input_fingerprint=fingerprint,
                )
                .on_conflict_do_nothing(
                    index_elements=[KnowledgeParseStage.revision_id, KnowledgeParseStage.stage_name]
                )
            )
        if revision.status == "FAILED":
            revision.status = "PENDING"
            revision.error_message = None
        await session.execute(
            pg_insert(KnowledgeDocumentIdentityCache)
            .values(
                tenant_id=tenant_id,
                source_sha256=source_sha256,
                parser_fingerprint=fingerprint,
                canonical_revision_id=revision_id,
                status="BUILDING",
            )
            .on_conflict_do_nothing(
                index_elements=[
                    KnowledgeDocumentIdentityCache.tenant_id,
                    KnowledgeDocumentIdentityCache.source_sha256,
                    KnowledgeDocumentIdentityCache.parser_fingerprint,
                ]
            )
        )
        cache = (
            await session.execute(
                select(KnowledgeDocumentIdentityCache)
                .where(
                    KnowledgeDocumentIdentityCache.tenant_id == tenant_id,
                    KnowledgeDocumentIdentityCache.source_sha256 == source_sha256,
                    KnowledgeDocumentIdentityCache.parser_fingerprint == fingerprint,
                )
                .with_for_update()
            )
        ).scalar_one()
        canonical_revision_id = str(cache.canonical_revision_id or "")
        if cache.status == "READY" and canonical_revision_id:
            revision.reused_from_revision_id = canonical_revision_id
        elif cache.status == "BUILDING" and canonical_revision_id and canonical_revision_id != revision_id:
            revision.status = "WAITING_CACHE"
            revision.reused_from_revision_id = canonical_revision_id
        elif cache.status == "FAILED" or not canonical_revision_id:
            cache.status = "BUILDING"
            cache.canonical_revision_id = revision_id
            revision.reused_from_revision_id = None
        if revision.status not in TERMINAL_PARSE_STATUSES:
            file_row.status = "parsing"
            file_row.evidence_status = revision.status
            file_row.error_message = None

    enqueue_deferred = False
    if enqueue and revision.status not in TERMINAL_PARSE_STATUSES | {"WAITING_CACHE"}:
        try:
            queue = await get_arq_pool()
            await queue.enqueue_job(
                "process_scientific_pdf_ingest",
                revision_id,
                _job_id=_ingest_job_id(revision_id, revision.attempt),
            )
        except Exception:  # noqa: BLE001
            # The durable PENDING revision is the source of truth. The periodic
            # reconciler will enqueue it after Redis/worker connectivity recovers.
            enqueue_deferred = True
            logger.exception("科研 PDF 任务已持久化，但即时入队失败，将由恢复任务补偿: %s", revision_id)
    return {
        "revision_id": revision_id,
        "status": revision.status,
        "reused": bool(revision.reused_from_revision_id)
        or revision.attempt > 0
        or revision.status in TERMINAL_PARSE_STATUSES,
        "enqueue_deferred": enqueue_deferred,
    }


async def _set_stage_status(
    revision_id: str,
    stage_name: str,
    status: str,
    *,
    worker_id: str | None = None,
    output_artifact_id: str | None = None,
    error_code: str | None = None,
    error_detail: str | None = None,
) -> None:
    now = _workflow_now()
    async with pg_manager.get_async_session_context() as session:
        stage = (
            await session.execute(
                select(KnowledgeParseStage).where(
                    KnowledgeParseStage.revision_id == revision_id,
                    KnowledgeParseStage.stage_name == stage_name,
                )
            )
        ).scalar_one_or_none()
        if stage is None:
            return
        stage.status = status
        stage.updated_at = now
        if status == "RUNNING":
            stage.attempt = int(stage.attempt or 0) + 1
            stage.started_at = now
            stage.finished_at = None
            stage.lease_owner = worker_id
            stage.lease_expires_at = now + timedelta(minutes=LEASE_MINUTES)
            stage.error_code = None
            stage.error_detail = None
        else:
            stage.finished_at = now
            stage.lease_owner = None
            stage.lease_expires_at = None
            stage.output_artifact_id = output_artifact_id
            stage.error_code = error_code
            stage.error_detail = error_detail


async def _fail_running_stages(revision_id: str, worker_id: str, exc: Exception) -> None:
    now = _workflow_now()
    async with pg_manager.get_async_session_context() as session:
        stages = list(
            (
                await session.execute(
                    select(KnowledgeParseStage).where(
                        KnowledgeParseStage.revision_id == revision_id,
                        KnowledgeParseStage.status == "RUNNING",
                        KnowledgeParseStage.lease_owner == worker_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        for stage in stages:
            stage.status = "FAILED"
            stage.finished_at = now
            stage.lease_owner = None
            stage.lease_expires_at = None
            stage.error_code = type(exc).__name__
            stage.error_detail = str(exc)


async def _load_parse_revision(revision_id: str) -> KnowledgeParseRevision | None:
    async with pg_manager.get_async_session_context() as session:
        return (
            (
                await session.execute(
                    select(KnowledgeParseRevision).where(KnowledgeParseRevision.revision_id == revision_id)
                )
            )
            .scalars()
            .one_or_none()
        )


def _rewrite_reused_markdown_identity(
    pipeline_result: PipelineResult,
    source_revision: KnowledgeParseRevision,
    target_revision: KnowledgeParseRevision,
) -> PipelineResult:
    """Point reused kbasset URIs at the reusing file/revision identity.

    The canonical Markdown is immutable; the rewrite happens only on the
    freshly-materialized copy for the reusing revision, keeping the asset
    endpoint's file->revision ownership check valid for every tenant.
    """
    rewritten_markdown = rewrite_kbasset_uri(
        pipeline_result.annotated_markdown,
        source_file_id=source_revision.file_id,
        source_revision_id=source_revision.revision_id,
        target_file_id=target_revision.file_id,
        target_revision_id=target_revision.revision_id,
    )
    article = pipeline_result.unified_article
    if article is not None:
        article.markdown = rewrite_kbasset_uri(
            article.markdown,
            source_file_id=source_revision.file_id,
            source_revision_id=source_revision.revision_id,
            target_file_id=target_revision.file_id,
            target_revision_id=target_revision.revision_id,
        )
    return PipelineResult(
        pipeline_result.capability,
        article,
        pipeline_result.qa_report,
        pipeline_result.artifacts,
        rewritten_markdown,
        pipeline_result.parser_fingerprint,
    )


async def _load_cached_pipeline_result(source_revision_id: str, parser_fingerprint: str) -> PipelineResult | None:
    async with pg_manager.get_async_session_context() as session:
        source = (
            await session.execute(
                select(KnowledgeParseRevision).where(KnowledgeParseRevision.revision_id == source_revision_id)
            )
        ).scalar_one_or_none()
        artifacts = list(
            (
                await session.execute(
                    select(KnowledgeParseArtifact).where(KnowledgeParseArtifact.revision_id == source_revision_id)
                )
            )
            .scalars()
            .all()
        )
    if source is None or source.parser_fingerprint != parser_fingerprint:
        return None
    if source.status != "REJECTED" and not source.article_uri:
        return None

    downloaded: dict[str, bytes] = {}
    copied_artifacts: list[ParserArtifact] = []
    minio = get_minio_client()
    for artifact in artifacts:
        if not is_minio_url(artifact.object_uri):
            return None
        bucket, object_name = parse_minio_url(artifact.object_uri)
        content = await minio.adownload_file(bucket, object_name)
        downloaded[artifact.kind] = content
        if artifact.kind not in {"original_pdf", "indexed_markdown"}:
            filename = str((artifact.metadata_json or {}).get("filename") or f"{artifact.kind}.bin")
            copied_artifacts.append(
                ParserArtifact(
                    kind=artifact.kind,
                    filename=filename,
                    content=content,
                    content_type=artifact.content_type or "application/octet-stream",
                )
            )

    if source.status == "REJECTED":
        return PipelineResult(
            "REJECTED",
            None,
            dict(source.qa_report or {}),
            copied_artifacts,
            "",
            parser_fingerprint,
        )
    article_payload = downloaded.get("unified_article")
    markdown_payload = downloaded.get("indexed_markdown")
    if not article_payload or markdown_payload is None:
        return None
    article_data = json.loads(article_payload.decode("utf-8"))
    article = UnifiedArticle(**article_data)
    annotated_markdown = markdown_payload.decode("utf-8")
    qa_report = dict(source.qa_report or {})
    qa_report["capabilities"] = {
        **dict(qa_report.get("capabilities") or {}),
        # Capability derivation can evolve independently from immutable parser
        # artifacts; refresh deterministic flags during a chunker-only rebuild.
        "figure_retrieval": bool(re.search(r"!\[[^\]]*\]\([^)]+\)", article.markdown)),
    }
    capability = str((source.capabilities or {}).get("state") or (source.qa_report or {}).get("capability") or "")
    if capability not in TERMINAL_PARSE_STATUSES - {"REJECTED"}:
        return None
    return PipelineResult(
        capability,
        article,
        qa_report,
        copied_artifacts,
        annotated_markdown,
        parser_fingerprint,
    )


async def _publish_identity_cache(revision: KnowledgeParseRevision) -> None:
    async with pg_manager.get_async_session_context() as session:
        cache = (
            await session.execute(
                select(KnowledgeDocumentIdentityCache)
                .where(
                    KnowledgeDocumentIdentityCache.tenant_id == revision.tenant_id,
                    KnowledgeDocumentIdentityCache.source_sha256 == revision.source_sha256,
                    KnowledgeDocumentIdentityCache.parser_fingerprint == revision.parser_fingerprint,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if cache is None or cache.canonical_revision_id != revision.revision_id:
            return
        cache.status = "READY"
        waiters = list(
            (
                await session.execute(
                    select(KnowledgeParseRevision).where(
                        KnowledgeParseRevision.tenant_id == revision.tenant_id,
                        KnowledgeParseRevision.source_sha256 == revision.source_sha256,
                        KnowledgeParseRevision.parser_fingerprint == revision.parser_fingerprint,
                        KnowledgeParseRevision.status == "WAITING_CACHE",
                    )
                )
            )
            .scalars()
            .all()
        )
        for waiter in waiters:
            waiter.status = "PENDING"
            waiter.reused_from_revision_id = revision.revision_id
            file_row = (
                await session.execute(select(KnowledgeFile).where(KnowledgeFile.file_id == waiter.file_id))
            ).scalar_one_or_none()
            if file_row:
                file_row.evidence_status = "PENDING"
    if waiters:
        queue = await get_arq_pool()
        for waiter in waiters:
            await queue.enqueue_job(
                "process_scientific_pdf_ingest",
                waiter.revision_id,
                _job_id=_ingest_job_id(waiter.revision_id, waiter.attempt),
            )


async def _release_identity_cache(revision: KnowledgeParseRevision) -> None:
    async with pg_manager.get_async_session_context() as session:
        cache = (
            await session.execute(
                select(KnowledgeDocumentIdentityCache)
                .where(
                    KnowledgeDocumentIdentityCache.tenant_id == revision.tenant_id,
                    KnowledgeDocumentIdentityCache.source_sha256 == revision.source_sha256,
                    KnowledgeDocumentIdentityCache.parser_fingerprint == revision.parser_fingerprint,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if cache is None or cache.canonical_revision_id != revision.revision_id or cache.status == "READY":
            return
        cache.status = "FAILED"
        cache.canonical_revision_id = None
        waiters = list(
            (
                await session.execute(
                    select(KnowledgeParseRevision).where(
                        KnowledgeParseRevision.reused_from_revision_id == revision.revision_id,
                        KnowledgeParseRevision.status == "WAITING_CACHE",
                    )
                )
            )
            .scalars()
            .all()
        )
        for waiter in waiters:
            waiter.status = "PENDING"
            waiter.reused_from_revision_id = None
            file_row = (
                await session.execute(select(KnowledgeFile).where(KnowledgeFile.file_id == waiter.file_id))
            ).scalar_one_or_none()
            if file_row:
                file_row.evidence_status = "PENDING"
    if waiters:
        queue = await get_arq_pool()
        for waiter in waiters:
            await queue.enqueue_job(
                "process_scientific_pdf_ingest",
                waiter.revision_id,
                _job_id=_ingest_job_id(waiter.revision_id, waiter.attempt),
            )


async def _take_identity_cache_ownership(revision: KnowledgeParseRevision) -> None:
    async with pg_manager.get_async_session_context() as session:
        cache = (
            await session.execute(
                select(KnowledgeDocumentIdentityCache)
                .where(
                    KnowledgeDocumentIdentityCache.tenant_id == revision.tenant_id,
                    KnowledgeDocumentIdentityCache.source_sha256 == revision.source_sha256,
                    KnowledgeDocumentIdentityCache.parser_fingerprint == revision.parser_fingerprint,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if cache:
            cache.status = "BUILDING"
            cache.canonical_revision_id = revision.revision_id
        current = (
            await session.execute(
                select(KnowledgeParseRevision).where(KnowledgeParseRevision.revision_id == revision.revision_id)
            )
        ).scalar_one()
        current.reused_from_revision_id = None


async def _claim_revision(revision_id: str, worker_id: str) -> KnowledgeParseRevision | None:
    now = _workflow_now()
    async with pg_manager.get_async_session_context() as session:
        revision = (
            await session.execute(
                select(KnowledgeParseRevision)
                .where(KnowledgeParseRevision.revision_id == revision_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if revision is None or revision.status in TERMINAL_PARSE_STATUSES | {"WAITING_CACHE"}:
            return None
        if revision.status == "RUNNING" and revision.lease_expires_at and revision.lease_expires_at > now:
            return None
        revision.status = "RUNNING"
        revision.attempt = int(revision.attempt or 0) + 1
        revision.lease_owner = worker_id
        revision.lease_expires_at = now + timedelta(minutes=LEASE_MINUTES)
        revision.started_at = revision.started_at or now
        revision.updated_at = now
        file_row = (
            await session.execute(select(KnowledgeFile).where(KnowledgeFile.file_id == revision.file_id))
        ).scalar_one_or_none()
        if file_row:
            file_row.evidence_status = "RUNNING"
        return revision


async def _renew_revision_lease(revision_id: str, worker_id: str, stop: asyncio.Event) -> None:
    """Renew long MinerU/indexing jobs without hiding a lost lease from the worker."""
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=LEASE_HEARTBEAT_SECONDS)
            return
        except TimeoutError:
            pass

        try:
            now = _workflow_now()
            async with pg_manager.get_async_session_context() as session:
                revision = (
                    await session.execute(
                        select(KnowledgeParseRevision).where(
                            KnowledgeParseRevision.revision_id == revision_id,
                            KnowledgeParseRevision.status == "RUNNING",
                            KnowledgeParseRevision.lease_owner == worker_id,
                        )
                    )
                ).scalar_one_or_none()
                if revision is None:
                    return
                revision.lease_expires_at = now + timedelta(minutes=LEASE_MINUTES)
                revision.updated_at = now
                stages = list(
                    (
                        await session.execute(
                            select(KnowledgeParseStage).where(
                                KnowledgeParseStage.revision_id == revision_id,
                                KnowledgeParseStage.status == "RUNNING",
                                KnowledgeParseStage.lease_owner == worker_id,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                for stage in stages:
                    stage.lease_expires_at = now + timedelta(minutes=LEASE_MINUTES)
                    stage.updated_at = now
        except Exception as exc:  # noqa: BLE001
            # A transient database outage must not kill the processing coroutine.  The
            # ownership checks before state transitions act as the fencing token.
            logger.warning(f"Scientific PDF lease heartbeat failed: revision={revision_id}: {exc}")


async def _owned_revision(session, revision_id: str, worker_id: str) -> KnowledgeParseRevision:
    revision = (
        await session.execute(
            select(KnowledgeParseRevision).where(
                KnowledgeParseRevision.revision_id == revision_id,
                KnowledgeParseRevision.status == "RUNNING",
                KnowledgeParseRevision.lease_owner == worker_id,
            )
        )
    ).scalar_one_or_none()
    if revision is None:
        raise RuntimeError("科研 PDF 任务租约已丢失，旧 Worker 已停止提交结果")
    return revision


async def _store_artifacts(revision: KnowledgeParseRevision, artifacts: list[ParserArtifact]) -> dict[str, str]:
    minio = get_minio_client()
    uris: dict[str, str] = {}
    artifact_rows: list[dict[str, Any]] = []
    for artifact in artifacts:
        sha256 = _digest(artifact.content)
        filename = _safe_name(artifact.filename)
        if artifact.kind == "original_pdf":
            object_name = f"tenants/{revision.tenant_id}/documents/{revision.source_sha256}/source/original.pdf"
        else:
            if artifact.kind == "pymupdf_native":
                namespace = "native"
            elif artifact.kind.startswith("mineru_"):
                namespace = "mineru"
            elif artifact.kind == "grobid_tei":
                namespace = "grobid"
            elif artifact.kind in {"unified_article", "indexed_markdown"}:
                namespace = "unified"
            else:
                namespace = "quality"
            object_name = (
                f"tenants/{revision.tenant_id}/documents/{revision.source_sha256}/"
                f"{namespace}/{revision.revision_id}/{sha256}-{filename}"
            )
        result = await minio.aupload_file(
            bucket_name="knowledgebases",
            object_name=object_name,
            data=artifact.content,
            content_type=artifact.content_type,
        )
        uris[artifact.kind] = result.url
        artifact_rows.append(
            {
                "artifact_id": f"spa_{_digest(f'{revision.revision_id}|{artifact.kind}|{sha256}')[:40]}",
                "revision_id": revision.revision_id,
                "kind": artifact.kind,
                "object_uri": result.url,
                "sha256": sha256,
                "content_type": artifact.content_type,
                "size_bytes": len(artifact.content),
                "metadata_json": {"filename": filename},
            }
        )
    async with pg_manager.get_async_session_context() as session:
        for row in artifact_rows:
            await session.execute(
                pg_insert(KnowledgeParseArtifact)
                .values(**row)
                .on_conflict_do_nothing(
                    index_elements=[
                        KnowledgeParseArtifact.revision_id,
                        KnowledgeParseArtifact.kind,
                        KnowledgeParseArtifact.sha256,
                    ]
                )
            )
    return uris


async def _persist_article_records(revision_id: str, article: dict[str, Any]) -> None:
    async with pg_manager.get_async_session_context() as session:
        # A retry rebuilds the exact same immutable revision; replace partial rows left by a process crash.
        await session.execute(delete(EvidenceAnchorRecord).where(EvidenceAnchorRecord.parse_revision_id == revision_id))
        await session.execute(delete(ArticleReference).where(ArticleReference.parse_revision_id == revision_id))
        await session.execute(delete(CitationMention).where(CitationMention.parse_revision_id == revision_id))
        session.add_all(
            [
                EvidenceAnchorRecord(
                    anchor_id=anchor["anchor_id"],
                    parse_revision_id=revision_id,
                    page=int(anchor["page"]),
                    bbox=list(anchor["bbox"]),
                    word_start=int(anchor["word_start"]),
                    word_end=int(anchor["word_end"]),
                    quote_hash=anchor["quote_hash"],
                    prefix_hash=anchor["prefix_hash"],
                    suffix_hash=anchor["suffix_hash"],
                    quote=anchor["quote"],
                )
                for anchor in article.get("anchors") or []
            ]
        )
        session.add_all(
            [
                ArticleReference(
                    parse_revision_id=revision_id,
                    reference_id=str(reference.get("reference_id") or f"b{index}"),
                    title=reference.get("title"),
                    doi=reference.get("doi"),
                    raw_text=reference.get("raw"),
                    metadata_json=reference,
                )
                for index, reference in enumerate(article.get("references") or [])
            ]
        )
        session.add_all(
            [
                CitationMention(
                    parse_revision_id=revision_id,
                    mention_id=str(mention.get("mention_id") or f"cm_{index}"),
                    reference_id=mention.get("reference_id"),
                    mention_text=mention.get("text"),
                    anchor_id=mention.get("anchor_id"),
                    metadata_json=mention,
                )
                for index, mention in enumerate(article.get("citation_mentions") or [])
            ]
        )


async def _link_stage_artifacts(revision_id: str) -> None:
    async with pg_manager.get_async_session_context() as session:
        artifacts = list(
            (
                await session.execute(
                    select(KnowledgeParseArtifact).where(KnowledgeParseArtifact.revision_id == revision_id)
                )
            )
            .scalars()
            .all()
        )
        stages = list(
            (await session.execute(select(KnowledgeParseStage).where(KnowledgeParseStage.revision_id == revision_id)))
            .scalars()
            .all()
        )
        by_name = {stage.stage_name: stage for stage in stages}
        mapping = {
            "NATIVE": next((item for item in artifacts if item.kind == "pymupdf_native"), None),
            "MINERU": next((item for item in artifacts if item.kind.startswith("mineru_")), None),
            "GROBID": next((item for item in artifacts if item.kind == "grobid_tei"), None),
            "UNIFIED": next((item for item in artifacts if item.kind == "unified_article"), None),
        }
        for stage_name, artifact in mapping.items():
            if artifact and stage_name in by_name:
                by_name[stage_name].output_artifact_id = artifact.artifact_id


async def _process_scientific_pdf_ingest(ctx: dict[str, Any], revision_id: str) -> dict[str, Any] | None:
    worker_id = str(ctx.get("job_id") or ctx.get("worker_name") or os.getpid())
    revision = await _claim_revision(revision_id, worker_id)
    if revision is None:
        return None

    temp_path = ""
    index_revision_id: str | None = None
    pipeline_result: PipelineResult | None = None
    lease_stop = asyncio.Event()
    lease_task = asyncio.create_task(
        _renew_revision_lease(revision_id, worker_id, lease_stop),
        name=f"scientific-pdf-lease-{revision_id}",
    )
    try:

        async def stage_callback(stage_name: str, status: str, detail: dict[str, Any] | None = None) -> None:
            detail = detail or {}
            await _set_stage_status(
                revision_id,
                stage_name,
                status,
                worker_id=worker_id,
                error_code=detail.get("error_code"),
                error_detail=detail.get("error_detail"),
            )

        async with pg_manager.get_async_session_context() as session:
            file_record = (
                await session.execute(select(KnowledgeFile).where(KnowledgeFile.file_id == revision.file_id))
            ).scalar_one()
            params = dict(file_record.processing_params or {})
            filename = str(file_record.original_filename or file_record.filename or "document.pdf")
        source_bytes = await _load_source(file_record)
        if _digest(source_bytes) != revision.source_sha256:
            raise ValueError("原始 PDF 内容哈希与解析版本不一致，已拒绝继续处理")

        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_file:
            temp_file.write(source_bytes)
            temp_path = temp_file.name
        params["image_prefix"] = (
            f"tenants/{revision.tenant_id}/documents/{revision.source_sha256}/mineru/{revision.revision_id}/images"
        )
        # Markdown 中的图片引用一律写 kbasset:// 逻辑 URI：渲染层经鉴权 Asset API
        # 拉取私有对象，MinIO 布局 / 预签名 URL 永不出现在知识数据里。
        params["asset_uri_builder"] = lambda object_name: build_kbasset_url(
            revision.file_id,
            revision.revision_id,
            Path(object_name).name,
        )
        if revision.reused_from_revision_id:
            pipeline_result = await _load_cached_pipeline_result(
                revision.reused_from_revision_id,
                revision.parser_fingerprint,
            )
            if pipeline_result is not None:
                source_revision = await _load_parse_revision(revision.reused_from_revision_id)
                canonical_copy_failed = source_revision is None
                if source_revision is not None:
                    try:
                        await materialize_reused_revision_assets(source_revision, revision)
                        pipeline_result = _rewrite_reused_markdown_identity(
                            pipeline_result, source_revision, revision
                        )
                    except Exception as materialize_error:  # noqa: BLE001
                        # Canonical images/identity cannot be reproduced; fall back
                        # to a full deterministic re-parse instead of serving broken
                        # kbasset references.
                        logger.warning(
                            "Scientific PDF reuse materialization failed; re-parsing: "
                            "revision=%s source=%s: %s",
                            revision_id,
                            revision.reused_from_revision_id,
                            materialize_error,
                        )
                        canonical_copy_failed = True
                if canonical_copy_failed:
                    pipeline_result = None
                    await _take_identity_cache_ownership(revision)
            if pipeline_result is not None:
                for stage_name in ("NATIVE", "MINERU", "GROBID", "UNIFIED", "QUALITY"):
                    await _set_stage_status(revision_id, stage_name, "REUSED", worker_id=worker_id)
        if pipeline_result is None:
            pipeline_result = await ScientificPdfPipeline().run(
                temp_path,
                params,
                stage_callback=stage_callback,
            )
        artifacts = [
            ParserArtifact("original_pdf", _safe_name(filename), source_bytes, "application/pdf"),
            *pipeline_result.artifacts,
        ]
        if pipeline_result.annotated_markdown:
            artifacts.append(
                ParserArtifact(
                    "indexed_markdown",
                    "evidence.md",
                    pipeline_result.annotated_markdown.encode("utf-8"),
                    "text/markdown",
                )
            )
        artifacts.append(
            ParserArtifact(
                "quality_report",
                "quality-report.json",
                json.dumps(
                    pipeline_result.qa_report,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8"),
                "application/json",
            )
        )
        artifacts.append(
            ParserArtifact(
                "parse_manifest",
                "manifest.json",
                json.dumps(
                    {
                        "schema_version": "scientific_pdf_manifest_v1",
                        "source_sha256": revision.source_sha256,
                        "parser_fingerprint": revision.parser_fingerprint,
                        "pipeline_version": revision.pipeline_version,
                        "artifacts": [
                            {
                                "kind": artifact.kind,
                                "filename": artifact.filename,
                                "sha256": _digest(artifact.content),
                                "size_bytes": len(artifact.content),
                            }
                            for artifact in artifacts
                        ],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8"),
                "application/json",
            )
        )
        artifact_uris = await _store_artifacts(revision, artifacts)
        await _link_stage_artifacts(revision_id)

        if pipeline_result.capability == "REJECTED" or pipeline_result.unified_article is None:
            async with pg_manager.get_async_session_context() as session:
                current = await _owned_revision(session, revision_id, worker_id)
                current.status = "REJECTED"
                current.qa_report = pipeline_result.qa_report
                current.capabilities = {
                    "state": "REJECTED",
                    "fulltext_search": False,
                    "academic_structure": False,
                    "citation_navigation": False,
                    "pdf_highlight": False,
                    "figure_retrieval": False,
                    "table_retrieval": False,
                    "formula_retrieval": False,
                }
                current.completed_at = _workflow_now()
                current.lease_owner = None
                current.lease_expires_at = None
                file_row = (
                    await session.execute(select(KnowledgeFile).where(KnowledgeFile.file_id == revision.file_id))
                ).scalar_one()
                file_row.status = "error_parsing"
                file_row.evidence_status = "REJECTED"
                file_row.evidence_capabilities = {
                    "state": "REJECTED",
                    "fulltext_search": False,
                    "academic_structure": False,
                    "citation_navigation": False,
                    "pdf_highlight": False,
                    "figure_retrieval": False,
                    "table_retrieval": False,
                    "formula_retrieval": False,
                    "qa": pipeline_result.qa_report,
                }
                file_row.error_message = "; ".join(pipeline_result.qa_report.get("reasons") or ["PDF 质量门禁拒绝"])
            await _publish_identity_cache(revision)
            return {"revision_id": revision_id, "status": "REJECTED"}

        article = pipeline_result.unified_article.to_dict()
        await _persist_article_records(revision_id, article)
        markdown_uri = artifact_uris["indexed_markdown"]
        chunking = _scientific_chunking_contract()
        chunker_fingerprint = _chunker_fingerprint(revision_id, chunking)
        index_revision_id = _index_revision_id(revision_id, chunker_fingerprint)

        async with pg_manager.get_async_session_context() as session:
            current = await _owned_revision(session, revision_id, worker_id)
            current.article_uri = artifact_uris.get("unified_article")
            current.article_summary = {
                "title": article.get("title"),
                "counts": pipeline_result.qa_report.get("counts"),
            }
            current.qa_report = pipeline_result.qa_report
            capability_contract = {
                "state": pipeline_result.capability,
                **dict(pipeline_result.qa_report.get("capabilities") or {}),
            }
            current.capabilities = capability_contract
            current.lease_expires_at = _workflow_now() + timedelta(minutes=LEASE_MINUTES)
            await session.execute(
                pg_insert(KnowledgeIndexRevision)
                .values(
                    revision_id=index_revision_id,
                    tenant_id=revision.tenant_id,
                    kb_id=revision.kb_id,
                    file_id=revision.file_id,
                    parse_revision_id=revision_id,
                    chunker_fingerprint=chunker_fingerprint,
                    status="BUILDING",
                )
                .on_conflict_do_update(
                    index_elements=[
                        KnowledgeIndexRevision.parse_revision_id,
                        KnowledgeIndexRevision.chunker_fingerprint,
                    ],
                    set_={"status": "BUILDING", "error_message": None},
                )
            )
            file_row = (
                await session.execute(select(KnowledgeFile).where(KnowledgeFile.file_id == revision.file_id))
            ).scalar_one()
            file_row.markdown_file = markdown_uri
            file_row.status = "parsed"
            file_row.processing_params = _persistent_processing_params(params, chunking)
            file_row.evidence_status = "INDEXING"
            file_row.evidence_capabilities = {**capability_contract, "qa": pipeline_result.qa_report}
            file_row.error_message = None

        await _publish_identity_cache(revision)

        await _set_stage_status(revision_id, "INDEX", "RUNNING", worker_id=worker_id)
        index_result = await knowledge_base.index_file(
            revision.kb_id,
            revision.file_id,
            operator_id=revision.created_by,
            params={
                **chunking,
                "_index_revision_id": index_revision_id,
                "_parse_revision_id": revision_id,
            },
        )
        await _set_stage_status(revision_id, "INDEX", "SUCCEEDED", worker_id=worker_id)
        await _set_stage_status(revision_id, "ACTIVATE", "RUNNING", worker_id=worker_id)
        now = _workflow_now()
        async with pg_manager.get_async_session_context() as session:
            current = await _owned_revision(session, revision_id, worker_id)
            current.status = pipeline_result.capability
            current.completed_at = now
            current.lease_owner = None
            current.lease_expires_at = None
            index_revision = (
                await session.execute(
                    select(KnowledgeIndexRevision).where(KnowledgeIndexRevision.revision_id == index_revision_id)
                )
            ).scalar_one()
            await session.execute(
                update(KnowledgeIndexRevision)
                .where(
                    KnowledgeIndexRevision.file_id == revision.file_id,
                    KnowledgeIndexRevision.revision_id != index_revision_id,
                    KnowledgeIndexRevision.status == "ACTIVE",
                )
                .values(status="SUPERSEDED")
            )
            index_revision.status = "ACTIVE"
            index_revision.chunk_count = int(index_result.get("chunk_count") or 0)
            index_revision.token_count = int(index_result.get("token_count") or 0)
            index_revision.activated_at = now
            index_revision.completed_at = now
            file_row = (
                await session.execute(select(KnowledgeFile).where(KnowledgeFile.file_id == revision.file_id))
            ).scalar_one()
            file_row.active_parse_revision_id = revision_id
            file_row.active_index_revision_id = index_revision_id
            file_row.evidence_status = pipeline_result.capability
        await _set_stage_status(revision_id, "ACTIVATE", "SUCCEEDED", worker_id=worker_id)
        try:
            await knowledge_base.cleanup_inactive_file_index_revisions(
                revision.kb_id, revision.file_id, index_revision_id
            )
        except Exception as cleanup_error:  # noqa: BLE001
            logger.warning(f"Scientific PDF stale index cleanup deferred: revision={revision_id}: {cleanup_error}")
        return {
            "revision_id": revision_id,
            "index_revision_id": index_revision_id,
            "status": pipeline_result.capability,
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"Scientific PDF ingest failed: revision={revision_id}: {exc}")
        await _fail_running_stages(revision_id, worker_id, exc)
        async with pg_manager.get_async_session_context() as session:
            current = (
                await session.execute(
                    select(KnowledgeParseRevision).where(KnowledgeParseRevision.revision_id == revision_id)
                )
            ).scalar_one_or_none()
            owns_revision = bool(
                current and current.status not in TERMINAL_PARSE_STATUSES and current.lease_owner == worker_id
            )
            file_row = (
                await session.execute(select(KnowledgeFile).where(KnowledgeFile.file_id == revision.file_id))
            ).scalar_one_or_none()
            active_index_survived = bool(
                file_row
                and file_row.active_index_revision_id
                and file_row.active_index_revision_id != index_revision_id
            )
            if owns_revision and active_index_survived and pipeline_result is not None:
                # A shadow-index failure must never take a previously active
                # parse/index offline. The failed index revision keeps the
                # diagnostic and a later submission can retry deterministically.
                current.status = pipeline_result.capability
                current.error_message = None
                current.completed_at = _workflow_now()
                current.lease_owner = None
                current.lease_expires_at = None
                file_row.status = "parsed"
                file_row.evidence_status = pipeline_result.capability
                file_row.error_message = None
            elif owns_revision:
                current.status = "FAILED"
                current.error_message = str(exc)
                current.lease_owner = None
                current.lease_expires_at = None
            if index_revision_id and owns_revision:
                index_revision = (
                    await session.execute(
                        select(KnowledgeIndexRevision).where(KnowledgeIndexRevision.revision_id == index_revision_id)
                    )
                ).scalar_one_or_none()
                if index_revision and index_revision.status != "ACTIVE":
                    index_revision.status = "FAILED"
                    index_revision.error_message = str(exc)
                    index_revision.completed_at = _workflow_now()
            if file_row and owns_revision and not active_index_survived:
                file_row.status = "error_indexing" if current and current.article_uri else "error_parsing"
                file_row.evidence_status = "FAILED"
                file_row.error_message = str(exc)
        await _release_identity_cache(revision)
        raise
    finally:
        lease_stop.set()
        lease_task.cancel()
        await asyncio.gather(lease_task, return_exceptions=True)
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


async def process_scientific_pdf_ingest(ctx: dict[str, Any], revision_id: str) -> dict[str, Any] | None:
    """Run one durable ingest while protecting local parser capacity from ARQ's larger job pool."""
    async with _pdf_ingest_semaphore():
        return await _process_scientific_pdf_ingest(ctx, revision_id)


async def recover_stale_scientific_pdf_ingests(ctx: dict[str, Any] | None = None) -> int:
    del ctx
    now = _workflow_now()
    async with pg_manager.get_async_session_context() as session:
        orphan_files = list(
            (
                await session.execute(
                    select(KnowledgeFile)
                    .where(
                        KnowledgeFile.file_type == "pdf",
                        KnowledgeFile.active_parse_revision_id.is_(None),
                        KnowledgeFile.status.in_(("uploaded", "parsing")),
                    )
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )
        rows = list(
            (
                await session.execute(
                    select(KnowledgeParseRevision).where(
                        or_(
                            KnowledgeParseRevision.status == "PENDING",
                            (KnowledgeParseRevision.status == "RUNNING")
                            & or_(
                                KnowledgeParseRevision.lease_expires_at.is_(None),
                                KnowledgeParseRevision.lease_expires_at < now,
                            ),
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            if row.status == "RUNNING":
                row.status = "PENDING"
                row.lease_owner = None
                row.lease_expires_at = None
                stages = list(
                    (
                        await session.execute(
                            select(KnowledgeParseStage).where(
                                KnowledgeParseStage.revision_id == row.revision_id,
                                KnowledgeParseStage.status == "RUNNING",
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                for stage in stages:
                    stage.status = "PENDING"
                    stage.lease_owner = None
                    stage.lease_expires_at = None
        waiters = list(
            (
                await session.execute(
                    select(KnowledgeParseRevision).where(KnowledgeParseRevision.status == "WAITING_CACHE")
                )
            )
            .scalars()
            .all()
        )
        for waiter in waiters:
            source = (
                await session.execute(
                    select(KnowledgeParseRevision).where(
                        KnowledgeParseRevision.revision_id == waiter.reused_from_revision_id
                    )
                )
            ).scalar_one_or_none()
            source_ready = bool(source and (source.article_uri or source.status == "REJECTED"))
            source_active = bool(
                source
                and source.status in {"PENDING", "RUNNING"}
                and (source.status != "RUNNING" or bool(source.lease_expires_at and source.lease_expires_at >= now))
            )
            if source_active and not source_ready:
                continue
            waiter.status = "PENDING"
            if not source_ready:
                waiter.reused_from_revision_id = None
            file_row = (
                await session.execute(select(KnowledgeFile).where(KnowledgeFile.file_id == waiter.file_id))
            ).scalar_one_or_none()
            if file_row:
                file_row.evidence_status = "PENDING"
            rows.append(waiter)
    recovered_orphans = 0
    for file_row in orphan_files:
        processing_params = dict(file_row.processing_params or {})
        if not processing_params.get("pdf_evidence_pipeline"):
            continue
        await create_or_reuse_scientific_pdf_ingest(
            kb_id=file_row.kb_id,
            file_id=file_row.file_id,
            operator_id=file_row.created_by or "system-recovery",
        )
        recovered_orphans += 1

    if not rows:
        return recovered_orphans
    queue = await get_arq_pool()
    for row in rows:
        await queue.enqueue_job(
            "process_scientific_pdf_ingest",
            row.revision_id,
            _job_id=_ingest_job_id(row.revision_id, row.attempt),
        )
    return recovered_orphans + len(rows)


async def get_scientific_pdf_status(*, kb_id: str, file_id: str) -> dict[str, Any]:
    async with pg_manager.get_async_session_context() as session:
        rows = list(
            (
                await session.execute(
                    select(KnowledgeParseRevision)
                    .where(KnowledgeParseRevision.kb_id == kb_id, KnowledgeParseRevision.file_id == file_id)
                    .order_by(KnowledgeParseRevision.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        revision_ids = [row.revision_id for row in rows]
        stages = (
            list(
                (
                    await session.execute(
                        select(KnowledgeParseStage)
                        .where(KnowledgeParseStage.revision_id.in_(revision_ids))
                        .order_by(KnowledgeParseStage.id.asc())
                    )
                )
                .scalars()
                .all()
            )
            if revision_ids
            else []
        )
        anchors = (
            list(
                (
                    await session.execute(
                        select(EvidenceAnchorRecord)
                        .where(EvidenceAnchorRecord.parse_revision_id.in_(revision_ids))
                        .order_by(EvidenceAnchorRecord.parse_revision_id, EvidenceAnchorRecord.id.asc())
                        .limit(100)
                    )
                )
                .scalars()
                .all()
            )
            if revision_ids
            else []
        )
    stages_by_revision: dict[str, list[dict[str, Any]]] = {}
    for stage in stages:
        stages_by_revision.setdefault(stage.revision_id, []).append(
            {
                "stage": stage.stage_name,
                "status": stage.status,
                "attempt": stage.attempt,
                "output_artifact_id": stage.output_artifact_id,
                "error_code": stage.error_code,
                "error_detail": stage.error_detail,
                "started_at": stage.started_at.isoformat() if stage.started_at else None,
                "finished_at": stage.finished_at.isoformat() if stage.finished_at else None,
            }
        )
    anchors_by_revision: dict[str, list[dict[str, Any]]] = {}
    for anchor in anchors:
        samples = anchors_by_revision.setdefault(anchor.parse_revision_id, [])
        if len(samples) >= 5:
            continue
        samples.append(
            {
                "anchor_id": anchor.anchor_id,
                "page": anchor.page,
                "bbox": anchor.bbox,
                "quote": anchor.quote,
            }
        )
    return {
        "file_id": file_id,
        "revisions": [
            {
                "revision_id": row.revision_id,
                "status": row.status,
                "attempt": row.attempt,
                "parser_fingerprint": row.parser_fingerprint,
                "qa_report": row.qa_report,
                "capabilities": row.capabilities,
                "error": row.error_message,
                "article_summary": row.article_summary,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "completed_at": row.completed_at.isoformat() if row.completed_at else None,
                "reused_from_revision_id": row.reused_from_revision_id,
                "stages": stages_by_revision.get(row.revision_id, []),
                "anchor_samples": anchors_by_revision.get(row.revision_id, []),
            }
            for row in rows
        ],
    }
