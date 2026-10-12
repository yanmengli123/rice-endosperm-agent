from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
import weakref
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from yuxi.knowledge.chunking.ragflow_like.parsers.academic import ACADEMIC_CHUNKER_VERSION
from yuxi.knowledge.pdf_evidence.asset_paths import revision_image_prefix
from yuxi.knowledge.pdf_evidence.contracts import ParserArtifact, PipelineResult, UnifiedArticle
from yuxi.knowledge.pdf_evidence.pipeline import (
    PIPELINE_VERSION,
    ScientificPdfPipeline,
    build_parser_fingerprint,
    build_mineru_stage_fingerprint,
)
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
    EvidenceSpanRecord,
    KnowledgeBase,
    KnowledgeDocumentIdentityCache,
    KnowledgeFile,
    KnowledgeIndexRevision,
    KnowledgeParseArtifact,
    KnowledgeParseRevision,
    KnowledgeParseStage,
    PdfSourceRegionRecord,
    PdfTableRecord,
    PdfTableCellRecord,
)
from yuxi.utils.datetime_utils import utc_now
from yuxi.utils.logging_config import logger

TERMINAL_PARSE_STATUSES = {
    "INDEXED_FULL",
    "INDEXED_CONTENT_ONLY",
    "INDEXED_TEXT_ONLY",
    "REJECTED",
    "PARSED_ONLY",
    "FAILED",
}
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


def _extract_article_doi(article: dict[str, Any] | None) -> str:
    """解析器元数据优先，其次正文首页抽取；失败返回 ""（身份回退指纹 RID）。

    登记桥（reconcile.register_scientific_revision）据此把 DOI 带进
    literature_papers；没有它，PDF 导入文献永远无法进入元数据富化链。
    """
    if not isinstance(article, dict):
        return ""
    metadata = article.get("metadata") or {}
    if isinstance(metadata, dict):
        candidate = str(metadata.get("doi") or "").strip()
        if candidate:
            from yuxi.literature.identity.rid import is_doi, normalize_doi

            if is_doi(candidate):
                return normalize_doi(candidate)
    from yuxi.literature.identity.rid import extract_doi_from_text

    return extract_doi_from_text(str(article.get("markdown") or ""))


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


def _persistent_processing_params(runtime_params: dict[str, Any], chunking: dict[str, Any]) -> dict[str, Any]:
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


async def inspect_file_index_lineage(
    *,
    file_id: str,
    index_revision_id: str,
    expected_chunk_count: int | None = None,
) -> dict[str, Any]:
    """对一个索引版本在 PostgreSQL 落库的 chunk 做血缘对账。

    matched = chunk_id 携带该索引版本的 rev token；stale = 同文件下不属于该
    版本的 chunk（legacy 无版本或其它 token）。stale chunk 在激活成功后会由
    cleanup_inactive_file_index_revisions 物理清理，因此不算闸门失败，但审计
    报告必须暴露（存量悬空 stale 正是激活指针与 chunk 血缘不一致的现场）。
    """
    from yuxi.knowledge.implementations.milvus import MilvusKB
    from yuxi.repositories.knowledge_chunk_repository import KnowledgeChunkRepository

    token = MilvusKB._index_revision_token(index_revision_id)
    marker = f"_rev_{token}_"
    chunks = await KnowledgeChunkRepository().list_by_file_id(file_id)
    matched = 0
    stale_chunk_ids: list[str] = []
    provenance_mismatch_chunk_ids: list[str] = []
    for chunk in chunks:
        chunk_id = str(chunk.chunk_id or "")
        if marker not in chunk_id:
            stale_chunk_ids.append(chunk_id)
            continue
        matched += 1
        provenance = dict(chunk.source_provenance or {})
        if str(provenance.get("index_revision_id") or "") != str(index_revision_id):
            provenance_mismatch_chunk_ids.append(chunk_id)

    problems: list[str] = []
    if not chunks:
        problems.append("文件在 PostgreSQL 中没有任何 chunk")
    elif matched == 0:
        problems.append(f"没有任何 chunk 携带索引版本标记 {marker}")
    if expected_chunk_count is not None and expected_chunk_count > 0 and matched != expected_chunk_count:
        problems.append(f"版本化 chunk 数 {matched} 与索引结果 chunk_count={expected_chunk_count} 不一致")
    if provenance_mismatch_chunk_ids:
        problems.append(f"{len(provenance_mismatch_chunk_ids)} 个版本化 chunk 的 source_provenance 未指向该索引版本")
    return {
        "file_id": file_id,
        "index_revision_id": index_revision_id,
        "rev_token": token,
        "total_chunks": len(chunks),
        "matched_chunks": matched,
        "stale_chunks": len(stale_chunk_ids),
        "stale_chunk_ids_sample": stale_chunk_ids[:10],
        "provenance_mismatch_chunk_ids": provenance_mismatch_chunk_ids[:10],
        "expected_chunk_count": expected_chunk_count,
        "problems": problems,
    }


async def get_scientific_pdf_index_lineage(*, kb_id: str, file_id: str) -> dict[str, Any]:
    """索引血缘对账探针：报告文件激活指针与 PG 实际 chunk 的一致性。

    审计口径比激活闸更严：只要激活版本下存在不属于它的 chunk（悬空血缘）
    就判 inconsistent——这正是 legacy 手动重建索引留下的现场。
    """
    async with pg_manager.get_async_session_context() as session:
        file_row = (
            await session.execute(
                select(KnowledgeFile).where(KnowledgeFile.file_id == file_id, KnowledgeFile.kb_id == kb_id)
            )
        ).scalar_one_or_none()
        if file_row is None:
            raise ValueError("文档不存在")
        active_index_revision_id = str(file_row.active_index_revision_id or "")
        if not active_index_revision_id:
            return {
                "kb_id": kb_id,
                "file_id": file_id,
                "managed": False,
                "consistent": False,
                "problems": ["文件没有激活索引版本指针（非科研 PDF 证据托管文件）"],
            }
    report = await inspect_file_index_lineage(
        file_id=file_id,
        index_revision_id=active_index_revision_id,
        expected_chunk_count=None,
    )
    problems = list(report["problems"])
    if report["stale_chunks"] > 0:
        problems.append(f"{report['stale_chunks']} 个 chunk 不属于激活索引版本（悬空血缘）")
    return {**report, "kb_id": kb_id, "managed": True, "consistent": not problems, "problems": problems}


def _ingest_job_id(revision_id: str, attempt: int, generation: int = 0) -> str:
    """Keep concurrent submissions idempotent while allowing a failed attempt to be retried."""
    base = f"scientific-pdf:{revision_id}:{max(int(attempt or 0), 0)}"
    return f"{base}:r{generation}" if generation else base


INGEST_RETRYABLE_STATUSES = ("FAILED", "REJECTED")

#: 失败自动重试预算（含首发，对标文献域编译 MAX_COMPILE_ATTEMPTS）：异常失败在
#: 预算内先回 PENDING，由 5 分钟对账 cron 以新 attempt 重排（新 job id）；预算
#: 耗尽才置 FAILED 死信（管理员经 /ingest/{rid}/retry 人工恢复兜底）。确定性
#: 拒绝（REJECTED）走正常返回路径，不进异常分支、不消耗预算。
MAX_INGEST_ATTEMPTS = 3


def _retry_budget_spent(revision: Any) -> bool:
    return int(revision.attempt or 0) - int(getattr(revision, "retry_budget_start", 0) or 0) >= MAX_INGEST_ATTEMPTS


async def request_scientific_pdf_retry(*, revision_id: str, tenant_id: int, operator_id: str) -> dict[str, Any]:
    """死信人工恢复：FAILED/REJECTED 终态解析版本复位并重新入队（管理员）。

    与 reconcile（只捞 PENDING/滞留 RUNNING）互补：这是终端死信的唯一
    人工恢复路径。attempt 先自增再入队，保证 ARQ job id 与已死任务不撞。
    重复 PDF 等确定性拒绝重跑仍可能再次 REJECTED——这是诚实语义，不是缺陷。
    """
    async with pg_manager.get_async_session_context() as session:
        row = (
            await session.execute(
                select(KnowledgeParseRevision, KnowledgeBase.tenant_id.label("kb_tenant"))
                .join(KnowledgeBase, KnowledgeBase.kb_id == KnowledgeParseRevision.kb_id)
                .where(
                    KnowledgeParseRevision.revision_id == revision_id,
                    KnowledgeBase.tenant_id == int(tenant_id),
                )
                .with_for_update(of=KnowledgeParseRevision)
            )
        ).first()
        if row is None:
            raise ValueError("解析版本不存在或不属于当前租户")
        revision, _ = row
        if revision.status not in INGEST_RETRYABLE_STATUSES:
            raise ValueError(f"仅终态 FAILED/REJECTED 可人工恢复，当前状态: {revision.status}")
        revision.retry_budget_start = int(revision.attempt or 0)
        revision.retry_generation = int(getattr(revision, "retry_generation", 0) or 0) + 1
        new_attempt = revision.attempt
        generation = revision.retry_generation
        revision.status = "PENDING"
        revision.lease_owner = None
        revision.lease_expires_at = None
        revision.error_message = None
        stages = (
            (
                await session.execute(
                    select(KnowledgeParseStage).where(
                        KnowledgeParseStage.revision_id == revision_id,
                        KnowledgeParseStage.status.in_(("RUNNING", "FAILED")),
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
        file_row = (
            await session.execute(
                select(KnowledgeFile).where(KnowledgeFile.file_id == revision.file_id).with_for_update()
            )
        ).scalar_one_or_none()
        if file_row is not None and file_row.evidence_status in ("FAILED", "REJECTED"):
            file_row.evidence_status = "PENDING"
        await session.commit()
    queue = await get_arq_pool()
    await queue.enqueue_job(
        "process_scientific_pdf_ingest",
        revision_id,
        _job_id=_ingest_job_id(revision_id, new_attempt, generation),
    )
    logger.info("死信人工恢复已入队: revision=%s attempt=%s operator=%s", revision_id, new_attempt, operator_id)
    return {"revision_id": revision_id, "status": "PENDING", "attempt": new_attempt, "retried": True}


def _stage_id(revision_id: str, stage_name: str) -> str:
    return f"sps_{_digest(f'{revision_id}|{stage_name}')[:40]}"


def _safe_name(value: str) -> str:
    name = Path(value).name
    cleaned = "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in name)
    return cleaned[:180] or "artifact.bin"


_CAPABILITY_RANK = {
    "REJECTED": 0,
    "FAILED": 0,
    "INDEXED_TEXT_ONLY": 1,
    "INDEXED_CONTENT_ONLY": 2,
    "INDEXED_FULL": 3,
}


async def _load_source(file_record: KnowledgeFile) -> bytes:
    source = str(file_record.path or file_record.minio_url or "")
    if not is_minio_url(source):
        raise ValueError("科研 PDF 原文必须位于受管 MinIO 存储中")
    bucket, object_name = parse_minio_url(source)
    return await get_minio_client().adownload_file(bucket, object_name)


async def create_or_reuse_scientific_pdf_ingest(
    *,
    kb_id: str,
    file_id: str,
    operator_id: str,
    enqueue: bool = True,
    processing_mode: str | None = None,
    defer_by_seconds: float = 0,
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

    mode = processing_mode or processing_params.get("scientific_processing_mode", "activate")
    if mode not in {"activate", "parse_only"}:
        raise ValueError("无效的科研 PDF 处理模式")

    if len(source_sha256) != 64:
        source_sha256 = _digest(await _load_source(file_record))
    fingerprint = build_parser_fingerprint(source_sha256, processing_params)
    if mode == "parse_only":
        # Separate immutable run identity: a parse-only retry cannot reopen or
        # inherit the activation intent of an existing full ingestion.
        fingerprint = _digest(f"parse_only:{fingerprint}".encode())
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
                parser_params=sanitize_processing_params(processing_params),
                pipeline_version=PIPELINE_VERSION,
                processing_mode=mode,
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
        file_row = (await session.execute(select(KnowledgeFile).where(KnowledgeFile.file_id == file_id))).scalar_one()
        if revision.status in TERMINAL_PARSE_STATUSES - {"REJECTED", "PARSED_ONLY"}:
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
            lineage_broken_reasons: list[str] = []
            if active_index is not None and active_index.chunker_fingerprint == desired_fingerprint:
                # 指纹一致还不够：激活索引的 chunk 血缘必须真实存在（防 legacy
                # 手动重建索引写入无版本 chunk 后留下悬空激活指针的存量态）。
                lineage_report = await inspect_file_index_lineage(
                    file_id=file_id,
                    index_revision_id=active_index.revision_id,
                    expected_chunk_count=int(active_index.chunk_count or 0) or None,
                )
                lineage_broken_reasons = list(lineage_report["problems"])
                if lineage_report["stale_chunks"] > 0:
                    lineage_broken_reasons.append(
                        f"{lineage_report['stale_chunks']} 个 chunk 不属于激活索引版本（悬空血缘）"
                    )
            if (
                active_index is None
                or active_index.chunker_fingerprint != desired_fingerprint
                or lineage_broken_reasons
            ):
                # Parser artifacts are immutable and reusable. Re-open only the
                # durable index stages when chunker code/config changes — or when
                # the active index lineage is broken and must be rebuilt in shadow.
                if lineage_broken_reasons:
                    logger.warning(
                        "激活索引血缘损坏，重开持久化索引阶段: "
                        f"file={file_id}, index_revision={active_index.revision_id if active_index else 'none'}, "
                        f"problems={'; '.join(lineage_broken_reasons)}"
                    )
                revision.status = "PENDING"
                revision.retry_budget_start = int(revision.attempt or 0)
                revision.retry_generation = int(revision.retry_generation or 0) + 1
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
                _job_id=_ingest_job_id(revision_id, revision.attempt, int(revision.retry_generation or 0)),
                # 篇间间隔（对标 PaperAgent 批量节流）：批量导入时按序号错峰入队
                **({"_defer_by": timedelta(seconds=defer_by_seconds)} if defer_by_seconds > 0 else {}),
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
        if worker_id is not None:
            await _owned_revision(session, revision_id, worker_id)
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
                    select(KnowledgeParseStage)
                    .join(KnowledgeParseRevision, KnowledgeParseRevision.revision_id == KnowledgeParseStage.revision_id)
                    .where(
                        KnowledgeParseStage.revision_id == revision_id,
                        KnowledgeParseStage.status == "RUNNING",
                        KnowledgeParseStage.lease_owner == worker_id,
                        KnowledgeParseRevision.status == "RUNNING",
                        KnowledgeParseRevision.lease_owner == worker_id,
                        KnowledgeParseRevision.lease_expires_at > now,
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
            stage.error_code = getattr(exc, "code", type(exc).__name__)
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
                    select(KnowledgeParseArtifact)
                    .where(KnowledgeParseArtifact.revision_id == source_revision_id)
                    .order_by(KnowledgeParseArtifact.kind.asc(), KnowledgeParseArtifact.id.desc())
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
        if _digest(content) != artifact.sha256:
            logger.warning(f"Scientific PDF cache hash mismatch: artifact={artifact.artifact_id}")
            return None
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
    capability = str(
        (source.capabilities or {}).get("parse_capability")
        or (source.capabilities or {}).get("state")
        or (source.qa_report or {}).get("capability")
        or ""
    )
    if capability not in TERMINAL_PARSE_STATUSES - {"REJECTED", "PARSED_ONLY"}:
        return None
    return PipelineResult(
        capability,
        article,
        qa_report,
        copied_artifacts,
        annotated_markdown,
        parser_fingerprint,
    )


async def _load_compatible_mineru_cache(
    target_revision: KnowledgeParseRevision,
) -> tuple[str, list[ParserArtifact]] | None:
    """Reuse immutable MinerU output when only downstream contracts changed.

    The source SHA is the hard boundary. GROBID, alignment, quality and
    chunking are deliberately rerun so a V2 revision cannot inherit V1
    locator decisions.
    """
    async with pg_manager.get_async_session_context() as session:
        candidates = list(
            (
                await session.execute(
                    select(KnowledgeParseRevision)
                    .where(
                        KnowledgeParseRevision.tenant_id == target_revision.tenant_id,
                        KnowledgeParseRevision.source_sha256 == target_revision.source_sha256,
                        KnowledgeParseRevision.revision_id != target_revision.revision_id,
                        KnowledgeParseRevision.status.in_(("INDEXED_FULL", "INDEXED_CONTENT_ONLY")),
                    )
                    .order_by(KnowledgeParseRevision.completed_at.desc().nullslast())
                    .limit(5)
                )
            )
            .scalars()
            .all()
        )
        candidate_ids = [candidate.revision_id for candidate in candidates]
        rows = (
            list(
                (
                    await session.execute(
                        select(KnowledgeParseArtifact)
                        .where(KnowledgeParseArtifact.revision_id.in_(candidate_ids))
                        .order_by(KnowledgeParseArtifact.id.desc())
                    )
                )
                .scalars()
                .all()
            )
            if candidate_ids
            else []
        )
    artifacts_by_revision: dict[str, list[KnowledgeParseArtifact]] = {}
    for row in rows:
        artifacts_by_revision.setdefault(row.revision_id, []).append(row)
    minio = get_minio_client()
    for source_revision in candidates:
        source_rows = artifacts_by_revision.get(source_revision.revision_id, [])
        article_row = next((row for row in source_rows if row.kind == "unified_article"), None)
        mineru_rows = [row for row in source_rows if row.kind.startswith("mineru_")]
        layout_rows = [
            row
            for row in mineru_rows
            if str((row.metadata_json or {}).get("filename") or "")
            .casefold()
            .endswith(("content_list.json", "content_list_v2.json"))
        ]
        if article_row is None or not layout_rows:
            continue
        try:
            bucket, object_name = parse_minio_url(article_row.object_uri)
            article_payload = await minio.adownload_file(bucket, object_name)
            if _digest(article_payload) != article_row.sha256:
                continue
            article = json.loads(article_payload.decode("utf-8"))
            if article.get("source_sha256") != target_revision.source_sha256:
                continue
            source_stage = (article.get("parser_provenance") or {}).get("mineru", {}).get("stage_fingerprint")
            if source_stage != build_mineru_stage_fingerprint(dict(target_revision.parser_params or {})):
                continue
            await materialize_reused_revision_assets(source_revision, target_revision)
            markdown = rewrite_kbasset_uri(
                str(article.get("markdown") or ""),
                source_file_id=source_revision.file_id,
                source_revision_id=source_revision.revision_id,
                target_file_id=target_revision.file_id,
                target_revision_id=target_revision.revision_id,
            )
            copied: list[ParserArtifact] = []
            for row in mineru_rows:
                bucket, object_name = parse_minio_url(row.object_uri)
                content = await minio.adownload_file(bucket, object_name)
                if _digest(content) != row.sha256:
                    raise ValueError("PDF_CACHE_ARTIFACT_HASH_MISMATCH")
                filename = str((row.metadata_json or {}).get("filename") or f"{row.kind}.bin")
                lowered = filename.casefold()
                if lowered.endswith("content_list_v2.json"):
                    kind = "mineru_content_list_v2"
                elif lowered.endswith("content_list.json"):
                    kind = "mineru_content_list"
                elif lowered == "full.md":
                    kind = "mineru_markdown"
                else:
                    kind = row.kind
                copied.append(
                    ParserArtifact(
                        kind=kind,
                        filename=filename,
                        content=content,
                        content_type=row.content_type or "application/octet-stream",
                    )
                )
            raw_markdown = next((a.content.decode("utf-8") for a in copied if a.kind == "mineru_markdown"), None)
            if raw_markdown is not None:
                markdown = raw_markdown
            if markdown.strip():
                logger.info(
                    "Reusing immutable MinerU artifacts for downstream PDF pipeline upgrade: "
                    f"source={source_revision.revision_id}, target={target_revision.revision_id}"
                )
                return markdown, copied
        except Exception as cache_error:  # noqa: BLE001
            logger.warning(
                "Compatible MinerU artifact reuse failed; trying another candidate: "
                f"source={source_revision.revision_id}: {cache_error}"
            )
    return None


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
                _job_id=_ingest_job_id(waiter.revision_id, waiter.attempt, int(waiter.retry_generation or 0)),
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
                _job_id=_ingest_job_id(waiter.revision_id, waiter.attempt, int(waiter.retry_generation or 0)),
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
        if _retry_budget_spent(revision):
            revision.status = "FAILED"
            revision.error_message = "PDF_RETRY_BUDGET_EXHAUSTED"
            revision.completed_at = now
            revision.lease_owner = None
            revision.lease_expires_at = None
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
            select(KnowledgeParseRevision)
            .where(
                KnowledgeParseRevision.revision_id == revision_id,
                KnowledgeParseRevision.status == "RUNNING",
                KnowledgeParseRevision.lease_owner == worker_id,
                KnowledgeParseRevision.lease_expires_at > _workflow_now(),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if revision is None:
        raise RuntimeError("科研 PDF 任务租约已丢失，旧 Worker 已停止提交结果")
    return revision


async def _store_artifacts(
    revision: KnowledgeParseRevision, artifacts: list[ParserArtifact], *, worker_id: str | None = None
) -> dict[str, str]:
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
        if worker_id:
            await _owned_revision(session, revision.revision_id, worker_id)
        if revision.sealed_at is not None or revision.status in {
            "INDEXED_FULL",
            "INDEXED_CONTENT_ONLY",
            "INDEXED_TEXT_ONLY",
            "PARSED_ONLY",
        }:
            raise ValueError("PUBLISHED_PDF_EVIDENCE_IMMUTABLE")
        for row in artifact_rows:
            await session.execute(
                pg_insert(KnowledgeParseArtifact)
                .values(**row)
                .on_conflict_do_update(
                    index_elements=[KnowledgeParseArtifact.revision_id, KnowledgeParseArtifact.kind],
                    set_={
                        "artifact_id": row["artifact_id"],
                        "object_uri": row["object_uri"],
                        "sha256": row["sha256"],
                        "content_type": row["content_type"],
                        "size_bytes": row["size_bytes"],
                        "metadata_json": row["metadata_json"],
                    },
                )
            )
    return uris


async def _persist_article_records(
    revision_id: str,
    article: dict[str, Any],
    *,
    markdown_body: str = "",
    worker_id: str | None = None,
    prepared_figures: bool = False,
) -> dict[str, Any]:
    summaries: dict[str, Any] = {}
    async with pg_manager.get_async_session_context() as session:
        # A retry rebuilds the exact same immutable revision; replace partial rows left by a process crash.
        if worker_id is not None:
            await _owned_revision(session, revision_id, worker_id)
        revision = (
            await session.execute(
                select(KnowledgeParseRevision).where(KnowledgeParseRevision.revision_id == revision_id)
            )
        ).scalar_one_or_none()
        if revision is not None and (revision.sealed_at is not None or revision.status in TERMINAL_PARSE_STATUSES):
            raise ValueError("PUBLISHED_PDF_EVIDENCE_IMMUTABLE")
        await session.execute(delete(PdfTableRecord).where(PdfTableRecord.parse_revision_id == revision_id))
        await session.execute(
            delete(PdfSourceRegionRecord).where(PdfSourceRegionRecord.parse_revision_id == revision_id)
        )
        region_rows = {}
        if revision is not None:
            for region in article.get("regions") or []:
                row = PdfSourceRegionRecord(tenant_id=revision.tenant_id, parse_revision_id=revision_id, **region)
                session.add(row)
                region_rows[region["region_id"]] = row
            await session.flush()
            for table in article.get("tables") or []:
                table_row = PdfTableRecord(
                    tenant_id=revision.tenant_id,
                    parse_revision_id=revision_id,
                    table_id=table["table_id"],
                    caption=table["caption"],
                    region_ids=table["region_ids"],
                    metadata_json=table["metadata"],
                )
                session.add(table_row)
                await session.flush()
                for cell in table["cells"]:
                    region = region_rows.get(cell.get("region_id"))
                    session.add(
                        PdfTableCellRecord(
                            table_record_id=table_row.id,
                            region_record_id=region.id if region else None,
                            row_index=cell["row_index"],
                            column_index=cell["column_index"],
                            row_span=cell["row_span"],
                            column_span=cell["column_span"],
                            raw_value=cell["raw_value"],
                            is_header=cell["is_header"],
                            metadata_json=cell["metadata"],
                        )
                    )
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
                    fragments=anchor.get("fragments")
                    or [
                        {
                            "page_index": int(anchor["page"]) - 1,
                            "bbox": list(anchor["bbox"]),
                            "coordinate_space": "pdf_points",
                        }
                    ],
                    anchor_type=str(anchor.get("anchor_type") or "paragraph"),
                    locator_quality=str(anchor.get("locator_quality") or "MEDIUM"),
                    confidence=float(anchor.get("confidence") or 0.0),
                    locatable=bool(anchor.get("locatable", False)),
                    source=str(anchor.get("source") or "pymupdf"),
                    document_partition=str(anchor.get("document_partition") or "UNKNOWN"),
                    partition_confidence=float(anchor.get("partition_confidence") or 0.0),
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
        # P2-10/P2-11：在同事务内确定性生成证据单元 span 与多字段词法索引。
        # 挂 parse_revision_id 级联；解析器升级重建 revision 后按 evidence_id 回归。
        anchor_rows = (
            (
                await session.execute(
                    select(EvidenceAnchorRecord)
                    .where(EvidenceAnchorRecord.parse_revision_id == revision_id)
                    .order_by(EvidenceAnchorRecord.page, EvidenceAnchorRecord.anchor_id)
                )
            )
            .scalars()
            .all()
        )
        revision = (
            await session.execute(
                select(KnowledgeParseRevision).where(KnowledgeParseRevision.revision_id == revision_id)
            )
        ).scalar_one_or_none()
        if revision is not None and anchor_rows:
            from yuxi.knowledge.evidence.span_builder import (
                build_evidence_spans,
                build_lexical_index,
            )

            spans_summary = await build_evidence_spans(
                session,
                revision=revision,
                anchors=list(anchor_rows),
                markdown_body=markdown_body,
                prepared_units=article.get("evidence_units") or None,
                regions=region_rows,
            )
            spans = (
                (
                    await session.execute(
                        select(EvidenceSpanRecord).where(EvidenceSpanRecord.parse_revision_id == revision_id)
                    )
                )
                .scalars()
                .all()
            )
            lexical_summary = await build_lexical_index(
                session,
                revision=revision,
                anchors=list(anchor_rows),
                spans=list(spans),
            )
            # R-P2：图表资产指纹索引（实体 + 资产：sha256/pHash/尺寸/panel 变体）。
            # 失败不阻断入库（summary 如实计数），重解析随 revision 级联重建。
            # v4：多 panel 图组需要原 PDF 渲染合成整图——按需惰性下载（身份缓存复用路径同样可用）
            from yuxi.knowledge.vision.figure_ingestor import persist_figure_index

            source_file = (
                None
                if prepared_figures
                else (
                    (await session.execute(select(KnowledgeFile).where(KnowledgeFile.file_id == str(revision.file_id))))
                    .scalars()
                    .first()
                )
            )

            async def _load_pdf_for_figures() -> bytes | None:
                return await _load_source(source_file) if source_file is not None else None

            figure_summary = await persist_figure_index(
                session,
                revision=revision,
                article_assets=list(article.get("assets") or []),
                spans=list(spans),
                anchors=list(anchor_rows),
                pdf_bytes_loader=_load_pdf_for_figures,
                prepared=prepared_figures,
            )
            summaries = {
                "evidence_spans": spans_summary,
                "lexical_index": lexical_summary,
                "figure_index": figure_summary,
            }
    return summaries


async def _link_stage_artifacts(revision_id: str, *, worker_id: str) -> None:
    async with pg_manager.get_async_session_context() as session:
        await _owned_revision(session, revision_id, worker_id)
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


def _snapshot_active_file_state(
    file_row: KnowledgeFile,
    active_revision: KnowledgeParseRevision | None,
) -> dict[str, Any]:
    """Capture the serving activation independently from transient ingest state.

    Submitting a replacement revision marks the file as ``parsing`` before the
    worker starts.  When an older index remains active, its revision—not those
    transient fields—is authoritative for the evidence capability restored after
    a shadow-build failure or quality downgrade.
    """
    evidence_status = file_row.evidence_status
    evidence_capabilities = file_row.evidence_capabilities
    file_status = file_row.status
    if active_revision is not None and file_row.active_index_revision_id:
        evidence_status = active_revision.status
        evidence_capabilities = {
            "state": active_revision.status,
            **dict(active_revision.capabilities or {}),
            "qa": active_revision.qa_report,
        }
        file_status = "parsed"
    return {
        "active_parse_revision_id": file_row.active_parse_revision_id,
        "active_index_revision_id": file_row.active_index_revision_id,
        "markdown_file": file_row.markdown_file,
        "status": file_status,
        "processing_params": file_row.processing_params,
        "evidence_status": evidence_status,
        "evidence_capabilities": evidence_capabilities,
    }


def _restore_active_file_state(file_row: KnowledgeFile, snapshot: dict[str, Any]) -> None:
    """Restore only the previously serving file projection after a shadow failure."""
    for field in (
        "active_parse_revision_id",
        "active_index_revision_id",
        "markdown_file",
        "status",
        "processing_params",
        "evidence_status",
        "evidence_capabilities",
    ):
        setattr(file_row, field, snapshot.get(field))


async def _complete_parse_only(
    revision: KnowledgeParseRevision,
    worker_id: str,
    article: dict[str, Any],
    result: PipelineResult,
    artifact_uris: dict[str, str],
    previous_activation: dict[str, Any],
) -> None:
    """Finish a read-only draft without creating evidence, index or source events."""
    async with pg_manager.get_async_session_context() as session:
        current = await _owned_revision(session, revision.revision_id, worker_id)
        await session.execute(
            update(KnowledgeParseStage)
            .where(
                KnowledgeParseStage.revision_id == revision.revision_id,
                KnowledgeParseStage.stage_name.in_(("INDEX", "ACTIVATE")),
            )
            .values(status="SKIPPED", finished_at=_workflow_now(), lease_owner=None, lease_expires_at=None)
        )
        current.article_uri = artifact_uris.get("unified_article")
        current.article_summary = {
            "title": article.get("title"),
            "counts": result.qa_report.get("counts"),
            "doi": _extract_article_doi(article),
        }
        current.qa_report = result.qa_report
        current.capabilities = {
            "state": "PARSED_ONLY",
            "parse_capability": result.capability,
            "reading": True,
            "fulltext_search": False,
            "evidence": False,
        }
        current.status = "PARSED_ONLY"
        current.completed_at = _workflow_now()
        current.sealed_at = current.sealed_at or current.completed_at
        current.lease_owner = None
        current.lease_expires_at = None
        file_row = (
            await session.execute(
                select(KnowledgeFile).where(KnowledgeFile.file_id == revision.file_id).with_for_update()
            )
        ).scalar_one()
        # A concurrent full run may have activated while this draft parsed.
        # Never restore a stale snapshot over a newer serving revision.
        if file_row.active_parse_revision_id:
            if file_row.active_parse_revision_id == previous_activation.get("active_parse_revision_id"):
                _restore_active_file_state(file_row, previous_activation)
        else:
            file_row.status = "parsed"
            file_row.evidence_status = "PARSED_ONLY"
            file_row.evidence_capabilities = dict(current.capabilities)
            file_row.error_message = None
            # markdown_file is an evidence-serving pointer, not a draft pointer.
            file_row.markdown_file = None


async def _process_scientific_pdf_ingest(ctx: dict[str, Any], revision_id: str) -> dict[str, Any] | None:
    worker_id = str(ctx.get("job_id") or ctx.get("worker_name") or os.getpid())
    revision = await _claim_revision(revision_id, worker_id)
    if revision is None:
        return None

    temp_path = ""
    index_revision_id: str | None = None
    pipeline_result: PipelineResult | None = None
    previous_activation: dict[str, Any] = {}
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
            active_revision = None
            if file_record.active_parse_revision_id:
                active_revision = (
                    await session.execute(
                        select(KnowledgeParseRevision).where(
                            KnowledgeParseRevision.revision_id == file_record.active_parse_revision_id
                        )
                    )
                ).scalar_one_or_none()
            previous_activation = _snapshot_active_file_state(file_record, active_revision)
            params = dict(
                revision.parser_params if revision.parser_params is not None else file_record.processing_params or {}
            )
            filename = str(file_record.original_filename or file_record.filename or "document.pdf")
        if revision.sealed_at is not None:
            from yuxi.services.pdf_evidence_service import download_pdf_original

            original = await download_pdf_original(
                kb_id=revision.kb_id,
                file_id=revision.file_id,
                parse_revision_id=revision.revision_id,
                source_sha256=revision.source_sha256,
            )
            source_bytes = original["content"]
            pipeline_result = await _load_cached_pipeline_result(revision.revision_id, revision.parser_fingerprint)
            if pipeline_result is not None:
                for stage_name in ("NATIVE", "MINERU", "GROBID", "UNIFIED", "QUALITY"):
                    await _set_stage_status(revision_id, stage_name, "REUSED", worker_id=worker_id)
        else:
            source_bytes = await _load_source(file_record)
        if _digest(source_bytes) != revision.source_sha256:
            raise ValueError("原始 PDF 内容哈希与解析版本不一致，已拒绝继续处理")

        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_file:
            temp_file.write(source_bytes)
            temp_path = temp_file.name
        params["image_prefix"] = revision_image_prefix(
            tenant_id=int(revision.tenant_id),
            source_sha256=str(revision.source_sha256),
            revision_id=str(revision.revision_id),
        )
        # Markdown 中的图片引用一律写 kbasset:// 逻辑 URI：渲染层经鉴权 Asset API
        # 拉取私有对象，MinIO 布局 / 预签名 URL 永不出现在知识数据里。
        params["asset_uri_builder"] = lambda object_name: build_kbasset_url(
            revision.file_id,
            revision.revision_id,
            Path(object_name).name,
        )
        if revision.reused_from_revision_id and pipeline_result is None and revision.sealed_at is None:
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
                        pipeline_result = _rewrite_reused_markdown_identity(pipeline_result, source_revision, revision)
                    except Exception as materialize_error:  # noqa: BLE001
                        # Canonical images/identity cannot be reproduced; fall back
                        # to a full deterministic re-parse instead of serving broken
                        # kbasset references.
                        logger.warning(
                            "Scientific PDF reuse materialization failed; re-parsing: revision=%s source=%s: %s",
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
            if revision.sealed_at is not None:
                raise ValueError("PUBLISHED_PDF_CACHE_UNAVAILABLE_CREATE_NEW_REVISION")
            mineru_cache = await _load_compatible_mineru_cache(revision)
            pipeline_result = await ScientificPdfPipeline().run(
                temp_path,
                params,
                stage_callback=stage_callback,
                mineru_cache=mineru_cache,
            )
        article: dict[str, Any] | None = None
        if pipeline_result.capability != "REJECTED" and pipeline_result.unified_article is not None:
            article = pipeline_result.unified_article.to_dict()
            if revision.processing_mode != "parse_only" and revision.sealed_at is None:
                from yuxi.knowledge.vision.figure_ingestor import prepare_figure_assets

                await prepare_figure_assets(revision, article, pipeline_result.artifacts, source_bytes)
            projection_summaries = (
                {}
                if revision.processing_mode == "parse_only"
                else dict(revision.qa_report or {})
                if revision.sealed_at is not None
                else await _persist_article_records(
                    revision_id,
                    article,
                    markdown_body=pipeline_result.annotated_markdown or "",
                    worker_id=worker_id,
                    prepared_figures=True,
                )
            )
            pipeline_result.qa_report.update(projection_summaries)
            pipeline_result.artifacts[:] = [a for a in pipeline_result.artifacts if a.kind != "unified_article"] + [
                ParserArtifact(
                    "unified_article",
                    "unified-article.json",
                    json.dumps(article, ensure_ascii=False, sort_keys=True).encode(),
                    "application/json",
                )
            ]
            figure_summary = dict(projection_summaries.get("figure_index") or {})
            pipeline_result.qa_report["capabilities"] = {
                **dict(pipeline_result.qa_report.get("capabilities") or {}),
                "figure_image_locator": bool(figure_summary.get("locator_ready_assets")),
                "panel_locator": any(a.get("panel_label") for a in article.get("assets", [])),
            }
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
        if revision.sealed_at is not None:
            async with pg_manager.get_async_session_context() as session:
                artifact_rows = (
                    (
                        await session.execute(
                            select(KnowledgeParseArtifact).where(KnowledgeParseArtifact.revision_id == revision_id)
                        )
                    )
                    .scalars()
                    .all()
                )
                artifact_uris = {a.kind: a.object_uri for a in artifact_rows}
        else:
            artifact_uris = await _store_artifacts(revision, artifacts, worker_id=worker_id)
        await _link_stage_artifacts(revision_id, worker_id=worker_id)

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
                if file_row.active_parse_revision_id:
                    if file_row.active_parse_revision_id == previous_activation.get("active_parse_revision_id"):
                        _restore_active_file_state(file_row, previous_activation)
                else:
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
            return {"revision_id": revision_id, "status": "REJECTED", "activated": False}

        assert article is not None
        if revision.processing_mode == "parse_only":
            await _complete_parse_only(
                revision, worker_id, article, pipeline_result, artifact_uris, previous_activation
            )
            await _publish_identity_cache(revision)
            return {"revision_id": revision_id, "status": "PARSED_ONLY", "activated": False}
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
                "doi": _extract_article_doi(article),
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
            if not file_row.active_index_revision_id:
                file_row.status = "indexing"
                file_row.evidence_status = "INDEXING"

        await _publish_identity_cache(revision)

        await _set_stage_status(revision_id, "INDEX", "RUNNING", worker_id=worker_id)
        index_result = await knowledge_base.index_file(
            revision.kb_id,
            revision.file_id,
            operator_id=revision.created_by,
            params={
                **_persistent_processing_params(params, chunking),
                "_index_revision_id": index_revision_id,
                "_parse_revision_id": revision_id,
            },
        )
        # 激活闸：候选索引版本必须先证明自己在 PG 中完整落库（版本化 chunk
        # 数量与血缘一致），才允许切换激活指针；失败走统一异常路径，旧激活
        # 态由 previous_activation 恢复逻辑保护。
        lineage_report = await inspect_file_index_lineage(
            file_id=revision.file_id,
            index_revision_id=index_revision_id,
            expected_chunk_count=int(index_result.get("chunk_count") or 0),
        )
        if lineage_report["problems"]:
            raise RuntimeError(
                f"激活前索引血缘校验失败（index_revision={index_revision_id}）: "
                + "; ".join(lineage_report["problems"])
            )
        await _set_stage_status(revision_id, "INDEX", "SUCCEEDED", worker_id=worker_id)
        await _set_stage_status(revision_id, "ACTIVATE", "RUNNING", worker_id=worker_id)
        now = _workflow_now()
        async with pg_manager.get_async_session_context() as session:
            current = await _owned_revision(session, revision_id, worker_id)
            current.status = pipeline_result.capability
            current.sealed_at = current.sealed_at or now
            current.completed_at = now
            current.lease_owner = None
            current.lease_expires_at = None
            index_revision = (
                await session.execute(
                    select(KnowledgeIndexRevision).where(KnowledgeIndexRevision.revision_id == index_revision_id)
                )
            ).scalar_one()
            index_revision.chunk_count = int(index_result.get("chunk_count") or 0)
            index_revision.token_count = int(index_result.get("token_count") or 0)
            index_revision.completed_at = now
            file_row = (
                await session.execute(
                    select(KnowledgeFile).where(KnowledgeFile.file_id == revision.file_id).with_for_update()
                )
            ).scalar_one()
            if file_row.active_parse_revision_id != previous_activation.get("active_parse_revision_id"):
                serving = (
                    await session.execute(
                        select(KnowledgeParseRevision).where(
                            KnowledgeParseRevision.revision_id == file_row.active_parse_revision_id
                        )
                    )
                ).scalar_one_or_none()
                previous_activation = _snapshot_active_file_state(file_row, serving)
            previous_status = str(previous_activation.get("evidence_status") or "")
            quality_downgrade = bool(
                previous_activation.get("active_index_revision_id")
                and _CAPABILITY_RANK.get(previous_status, 0) > _CAPABILITY_RANK.get(pipeline_result.capability, 0)
            )
            from yuxi.knowledge.pdf_evidence.quality import quality_regressions

            regressions = quality_regressions(
                dict((previous_activation.get("evidence_capabilities") or {}).get("qa") or {}),
                pipeline_result.qa_report,
            )
            quality_downgrade = quality_downgrade or bool(
                previous_activation.get("active_index_revision_id") and regressions
            )
            if regressions:
                current.qa_report = {
                    **pipeline_result.qa_report,
                    "activation_rejected": {"reason": "QUALITY_REGRESSION", "metrics": regressions},
                }
            if quality_downgrade:
                index_revision.status = "SUPERSEDED"
                file_row.active_parse_revision_id = previous_activation.get("active_parse_revision_id")
                file_row.active_index_revision_id = previous_activation.get("active_index_revision_id")
                file_row.markdown_file = previous_activation.get("markdown_file")
                file_row.status = previous_activation.get("status")
                file_row.processing_params = previous_activation.get("processing_params")
                file_row.evidence_status = previous_status
                file_row.evidence_capabilities = previous_activation.get("evidence_capabilities")
                logger.warning(
                    "Scientific PDF candidate was not activated because it would downgrade evidence quality: "
                    f"file={revision.file_id}, active={previous_status}, candidate={pipeline_result.capability}"
                )
            else:
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
                index_revision.activated_at = now
                file_row.active_parse_revision_id = revision_id
                file_row.active_index_revision_id = index_revision_id
                file_row.markdown_file = markdown_uri
                file_row.status = "indexed"
                file_row.processing_params = _persistent_processing_params(params, chunking)
                file_row.evidence_status = pipeline_result.capability
                file_row.evidence_capabilities = {**capability_contract, "qa": current.qa_report}
                file_row.error_message = None
                file_row.chunk_count = int(index_result.get("chunk_count") or 0)
                file_row.token_count = int(index_result.get("token_count") or 0)
                from yuxi.literature.provenance import invalidate_note_publications
                from yuxi.literature.reconcile import record_source_event

                previous_parse = previous_activation.get("active_parse_revision_id")
                if previous_parse and previous_parse != revision_id:
                    await invalidate_note_publications(session, str(previous_parse))
                await record_source_event(session, revision_id, int(revision.tenant_id))
            # Complete the stage in the same owned transaction that releases
            # the revision lease, avoiding an unfenced post-activation write.
            await session.execute(
                update(KnowledgeParseStage)
                .where(
                    KnowledgeParseStage.revision_id == revision_id,
                    KnowledgeParseStage.stage_name == "ACTIVATE",
                    KnowledgeParseStage.lease_owner == worker_id,
                )
                .values(status="SUCCEEDED", finished_at=now, updated_at=now, lease_owner=None, lease_expires_at=None)
            )
        active_index_revision_id = (
            str(previous_activation.get("active_index_revision_id")) if quality_downgrade else index_revision_id
        )
        try:
            await knowledge_base.cleanup_inactive_file_index_revisions(
                revision.kb_id, revision.file_id, active_index_revision_id
            )
        except Exception as cleanup_error:  # noqa: BLE001
            logger.warning(f"Scientific PDF stale index cleanup deferred: revision={revision_id}: {cleanup_error}")
        # R6b：doclex 词典预热（尽力而为——Phase 0 兜底正确性，预热只优化首次构建时延）
        try:
            from yuxi.knowledge.graphs.doclex.service import doclex_prewarm_job_id

            prewarm_queue = await get_arq_pool()
            await prewarm_queue.enqueue_job(
                "prewarm_doclex_for_kb",
                revision.kb_id,
                _job_id=doclex_prewarm_job_id(revision.kb_id, revision_id),
            )
        except Exception as prewarm_error:  # noqa: BLE001
            logger.warning(f"doclex prewarm enqueue deferred: revision={revision_id}: {prewarm_error}")
        try:
            # 应用层挂接：权威解析完成后登记派生文献工作区。失败不回滚已经
            # 激活的证据版本，reconcile_scientific_literature_papers 会补偿。
            from yuxi.literature.reconcile import register_scientific_revision

            await register_scientific_revision(revision_id, enqueue_render=True)
        except Exception as literature_error:  # noqa: BLE001
            logger.exception(
                f"Literature registration deferred to reconciler: revision={revision_id}: {literature_error}"
            )
        return {
            "revision_id": revision_id,
            "index_revision_id": index_revision_id,
            "status": pipeline_result.capability,
            "activated": not quality_downgrade,
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"Scientific PDF ingest failed: revision={revision_id}: {exc}")
        await _fail_running_stages(revision_id, worker_id, exc)
        async with pg_manager.get_async_session_context() as session:
            current = (
                await session.execute(
                    select(KnowledgeParseRevision)
                    .where(
                        KnowledgeParseRevision.revision_id == revision_id,
                        KnowledgeParseRevision.status == "RUNNING",
                        KnowledgeParseRevision.lease_owner == worker_id,
                        KnowledgeParseRevision.lease_expires_at > _workflow_now(),
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            owns_revision = current is not None
            file_row = (
                await session.execute(select(KnowledgeFile).where(KnowledgeFile.file_id == revision.file_id))
            ).scalar_one_or_none()
            active_index_survived = bool(
                file_row
                and file_row.active_index_revision_id
                and file_row.active_index_revision_id != index_revision_id
            )
            retry_scheduled = False
            if owns_revision:
                # 重试预算：预算内异常先回 PENDING 交由对账 cron 重排；预算耗尽
                # 才置 FAILED 死信（人工恢复端点兜底）。
                from yuxi.knowledge.pdf_evidence.native import PdfInputError

                retry_scheduled = not isinstance(exc, PdfInputError) and not _retry_budget_spent(current)
                if isinstance(exc, PdfInputError):
                    current.qa_report = {"failure": {"code": exc.code, "retryable": False}}
                current.status = "PENDING" if retry_scheduled else "FAILED"
                current.error_message = str(exc)
                current.lease_owner = None
                current.lease_expires_at = None
                if not retry_scheduled:
                    current.completed_at = _workflow_now()
                if active_index_survived:
                    # A shadow-index failure must never take a previously active
                    # parse/index offline. The candidate itself stays retryable
                    # (budget exhausted → FAILED with its diagnostic); reporting
                    # the pipeline capability here creates a false-success
                    # terminal revision with zero evidence rows.
                    if file_row.active_parse_revision_id == previous_activation.get("active_parse_revision_id"):
                        _restore_active_file_state(file_row, previous_activation)
                    file_row.error_message = None
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
            if file_row and owns_revision:
                if retry_scheduled and not active_index_survived:
                    # 预算内重试：文件回到解析中口径，错误详情留在 revision 上
                    file_row.status = "parsing"
                    file_row.evidence_status = "PENDING"
                    file_row.error_message = None
                elif not active_index_survived:
                    file_row.status = "error_indexing" if current and current.article_uri else "error_parsing"
                    file_row.evidence_status = "FAILED"
                    file_row.error_message = str(exc)
        if owns_revision:
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
            if _retry_budget_spent(row):
                row.status = "FAILED"
                row.error_message = "PDF_RETRY_BUDGET_EXHAUSTED"
                row.completed_at = now
                row.lease_owner = None
                row.lease_expires_at = None
                continue
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
        if row.status != "PENDING":
            continue
        await queue.enqueue_job(
            "process_scientific_pdf_ingest",
            row.revision_id,
            _job_id=_ingest_job_id(row.revision_id, row.attempt, int(getattr(row, "retry_generation", 0) or 0)),
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
        if not anchor.locatable or anchor.locator_quality != "HIGH":
            continue
        samples = anchors_by_revision.setdefault(anchor.parse_revision_id, [])
        if len(samples) >= 5:
            continue
        samples.append(
            {
                "anchor_id": anchor.anchor_id,
                "page": anchor.page,
                "bbox": anchor.bbox,
                "quote": anchor.quote,
                "fragments": anchor.fragments,
                "anchor_type": anchor.anchor_type,
                "locator_quality": anchor.locator_quality,
                "confidence": anchor.confidence,
                "locatable": anchor.locatable,
                "source": anchor.source,
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


def classify_pdf_evidence_upgrade(
    file_record: KnowledgeFile, revision: KnowledgeParseRevision | None
) -> dict[str, Any]:
    """Classify one active PDF without mutating or downloading its source."""
    reasons: list[str] = []
    source_sha256 = str(file_record.content_hash or "")
    expected_fingerprint = None
    if not re.fullmatch(r"[0-9a-fA-F]{64}", source_sha256):
        reasons.append("SOURCE_HASH_REQUIRES_BACKFILL")
    else:
        expected_fingerprint = build_parser_fingerprint(
            source_sha256.lower(), dict(file_record.processing_params or {})
        )
    if not file_record.active_parse_revision_id:
        reasons.append("NO_ACTIVE_PARSE_REVISION")
    elif revision is None:
        reasons.append("ACTIVE_PARSE_REVISION_MISSING")
    else:
        if str(revision.pipeline_version or "") != PIPELINE_VERSION:
            reasons.append("PIPELINE_VERSION_OUTDATED")
        if expected_fingerprint and str(revision.parser_fingerprint or "") != expected_fingerprint:
            reasons.append("PARSER_FINGERPRINT_OUTDATED")
        if revision.sealed_at is None:
            reasons.append("ACTIVE_REVISION_NOT_SEALED")
        if str(revision.status or "") not in {
            "INDEXED_FULL",
            "INDEXED_CONTENT_ONLY",
            "INDEXED_TEXT_ONLY",
        }:
            reasons.append("ACTIVE_REVISION_NOT_INDEXED")
    # 升级前后质量对比的"前"基线：当前活跃能力面的质量向量摘要（只读投影，
    # 操作员判断"这次升级值不值"的依据；升级失败/质量回退时激活守卫保持现状）。
    current_quality = None
    capabilities = getattr(file_record, "evidence_capabilities", None)
    if isinstance(capabilities, dict):
        vector = (capabilities.get("qa") or {}).get("quality_vector")
        if isinstance(vector, dict):
            current_quality = {
                key: vector.get(key)
                for key in (
                    "version",
                    "source_binding_coverage",
                    "text_precise_locator_coverage",
                    "figure_region_locator_coverage",
                    "figure_ocr_ready_coverage",
                    "table_cell_locator_coverage",
                    "raw_block_acceptance",
                )
                if vector.get(key) is not None
            } or None
    return {
        "file_id": str(file_record.file_id),
        "kb_id": str(file_record.kb_id),
        "filename": str(file_record.original_filename or file_record.filename or file_record.file_id),
        "active_parse_revision_id": str(file_record.active_parse_revision_id or "") or None,
        "active_pipeline_version": str(revision.pipeline_version or "") if revision else None,
        "active_status": str(revision.status or "") if revision else None,
        "expected_pipeline_version": PIPELINE_VERSION,
        "expected_parser_fingerprint": expected_fingerprint,
        "current_quality_vector": current_quality,
        "upgrade_required": bool(reasons),
        "reasons": reasons,
    }


async def list_pdf_evidence_upgrade_candidates(
    *, tenant_id: int, kb_id: str, limit: int = 100, after_file_id: str | None = None
) -> dict[str, Any]:
    """Return a tenant-scoped, stable page of PDF evidence upgrade inventory."""
    bounded_limit = max(1, min(int(limit), 500))
    async with pg_manager.get_async_session_context() as session:
        query = (
            select(KnowledgeFile, KnowledgeParseRevision)
            .join(KnowledgeBase, KnowledgeBase.kb_id == KnowledgeFile.kb_id)
            .outerjoin(
                KnowledgeParseRevision,
                KnowledgeParseRevision.revision_id == KnowledgeFile.active_parse_revision_id,
            )
            .where(
                KnowledgeBase.tenant_id == tenant_id,
                KnowledgeBase.kb_id == kb_id,
                KnowledgeBase.contract_key == "pdf_evidence",
                KnowledgeFile.is_folder.is_not(True),
                or_(
                    KnowledgeFile.file_type == "pdf",
                    KnowledgeFile.filename.ilike("%.pdf"),
                    KnowledgeFile.original_filename.ilike("%.pdf"),
                ),
            )
            .order_by(KnowledgeFile.file_id)
            .limit(bounded_limit + 1)
        )
        if after_file_id:
            query = query.where(KnowledgeFile.file_id > after_file_id)
        rows = list((await session.execute(query)).all())
    has_more = len(rows) > bounded_limit
    page = rows[:bounded_limit]
    items = [classify_pdf_evidence_upgrade(file_record, revision) for file_record, revision in page]
    return {
        "schema": "pdf-evidence-upgrade-inventory.v1",
        "kb_id": kb_id,
        "expected_pipeline_version": PIPELINE_VERSION,
        "items": items,
        "page_count": len(items),
        "upgrade_required_count": sum(int(item["upgrade_required"]) for item in items),
        "next_cursor": str(page[-1][0].file_id) if has_more and page else None,
    }


async def schedule_pdf_evidence_upgrades(
    *, tenant_id: int, kb_id: str, file_ids: list[str], operator_id: str, dry_run: bool = True
) -> dict[str, Any]:
    """Plan or enqueue explicitly selected upgrades; never schedules an implicit whole KB."""
    selected = list(dict.fromkeys(str(item).strip() for item in file_ids if str(item).strip()))
    if not selected or len(selected) > 100:
        raise ValueError("file_ids 必须包含 1-100 个唯一文件")
    async with pg_manager.get_async_session_context() as session:
        rows = list(
            (
                await session.execute(
                    select(KnowledgeFile, KnowledgeParseRevision)
                    .join(KnowledgeBase, KnowledgeBase.kb_id == KnowledgeFile.kb_id)
                    .outerjoin(
                        KnowledgeParseRevision,
                        KnowledgeParseRevision.revision_id == KnowledgeFile.active_parse_revision_id,
                    )
                    .where(
                        KnowledgeBase.tenant_id == tenant_id,
                        KnowledgeBase.kb_id == kb_id,
                        KnowledgeBase.contract_key == "pdf_evidence",
                        KnowledgeFile.file_id.in_(selected),
                        KnowledgeFile.is_folder.is_not(True),
                        or_(
                            KnowledgeFile.file_type == "pdf",
                            KnowledgeFile.filename.ilike("%.pdf"),
                            KnowledgeFile.original_filename.ilike("%.pdf"),
                        ),
                    )
                    .order_by(KnowledgeFile.file_id)
                )
            ).all()
        )
    by_file = {str(file_record.file_id): (file_record, revision) for file_record, revision in rows}
    missing = [file_id for file_id in selected if file_id not in by_file]
    if missing:
        raise ValueError(f"文件不存在、越权或不属于 pdf_evidence：{', '.join(missing[:5])}")
    plan = [classify_pdf_evidence_upgrade(*by_file[file_id]) for file_id in selected]
    if dry_run:
        return {
            "schema": "pdf-evidence-upgrade-batch.v1",
            "dry_run": True,
            "kb_id": kb_id,
            "requested": len(selected),
            "upgrade_required": sum(int(item["upgrade_required"]) for item in plan),
            "items": plan,
        }
    results: list[dict[str, Any]] = []
    for index, item in enumerate(plan):
        if not item["upgrade_required"]:
            results.append({**item, "enqueue_status": "SKIPPED_CURRENT"})
            continue
        try:
            queued = await create_or_reuse_scientific_pdf_ingest(
                kb_id=kb_id,
                file_id=item["file_id"],
                operator_id=operator_id,
                defer_by_seconds=min(index * 0.25, 10.0),
            )
            results.append(
                {
                    **item,
                    "enqueue_status": "QUEUED" if not queued.get("enqueue_deferred") else "DEFERRED",
                    "revision_id": queued.get("revision_id"),
                    "revision_status": queued.get("status"),
                }
            )
        except Exception as exc:  # one bad file must not hide the rest of the explicit batch
            logger.exception("Failed to schedule PDF evidence upgrade for %s", item["file_id"])
            results.append({**item, "enqueue_status": "FAILED", "error": str(exc)[:500]})
    return {
        "schema": "pdf-evidence-upgrade-batch.v1",
        "dry_run": False,
        "kb_id": kb_id,
        "requested": len(selected),
        "queued": sum(int(item.get("enqueue_status") in {"QUEUED", "DEFERRED"}) for item in results),
        "failed": sum(int(item.get("enqueue_status") == "FAILED") for item in results),
        "skipped_current": sum(int(item.get("enqueue_status") == "SKIPPED_CURRENT") for item in results),
        "items": results,
    }


_SCI_PDF_TASK_TYPE = "pdf_ingest"
_TASK_STATUS_BY_REVISION = {
    "PARSED_ONLY": "success",
    "PENDING": "queued",
    "WAITING_CACHE": "queued",
    "RUNNING": "running",
    "INDEXED_FULL": "success",
    "INDEXED_CONTENT_ONLY": "success",
    "INDEXED_TEXT_ONLY": "success",
    "REJECTED": "failed",
    "FAILED": "failed",
}
_TASK_REVISION_TERMINAL_STATUSES = TERMINAL_PARSE_STATUSES | {"FAILED"}
_PIPELINE_STAGE_LABELS = {
    "NATIVE": "原生解析",
    "MINERU": "MinerU 解析",
    "GROBID": "GROBID 结构化解析",
    "UNIFIED": "统一文章构建",
    "QUALITY": "质量门禁",
    "INDEX": "向量入库",
    "ACTIVATE": "激活解析版本",
}
_PIPELINE_STAGE_DONE_STATUSES = {"SUCCEEDED", "REUSED", "SKIPPED"}
_PIPELINE_CAPABILITY_LABELS = {
    "PARSED_ONLY": "仅解析完成（未索引、未激活）",
    "INDEXED_FULL": "全文证据索引完成",
    "INDEXED_CONTENT_ONLY": "内容证据索引完成",
    "INDEXED_TEXT_ONLY": "纯文本索引完成",
    "REJECTED": "质量门禁拒绝该文档",
}


def _pipeline_stage_progress(stage_rows: list[Any]) -> tuple[float, str | None]:
    """Derive task-center progress from durable stage rows (7 stages total)."""
    status_by_name = {row.stage_name: str(row.status or "") for row in stage_rows}
    done = 0.0
    running_stage = None
    for stage_name in PARSE_STAGE_NAMES:
        stage_status = status_by_name.get(stage_name)
        if not stage_status:
            continue
        if stage_status in _PIPELINE_STAGE_DONE_STATUSES:
            done += 1.0
        elif stage_status == "RUNNING" and running_stage is None:
            running_stage = stage_name
            done += 0.5
    progress = round(done / len(PARSE_STAGE_NAMES) * 100.0, 1)
    message = f"正在{_PIPELINE_STAGE_LABELS[running_stage]}" if running_stage else None
    return progress, message


async def list_scientific_pdf_pipeline_tasks(*, status: str | None = None, limit: int = 100) -> dict[str, Any]:
    """Project durable scientific PDF ingest state into task-center entries.

    The pipeline executes in the ARQ worker process, so its live state lives in
    PostgreSQL (revisions + stages), not in the API process's in-memory Tasker.
    The task center reads this read-only projection so PDF upload/parse/index
    progress shows up there and survives API restarts.
    """
    bounded_limit = max(1, min(int(limit or 100), 100))
    retention_floor = _workflow_now() - timedelta(hours=24)
    async with pg_manager.get_async_session_context() as session:
        active_rows = list(
            (
                await session.execute(
                    select(KnowledgeParseRevision, KnowledgeFile.original_filename, KnowledgeFile.filename)
                    .join(KnowledgeFile, KnowledgeFile.file_id == KnowledgeParseRevision.file_id)
                    .where(KnowledgeParseRevision.status.in_(("PENDING", "RUNNING", "WAITING_CACHE")))
                    .order_by(KnowledgeParseRevision.created_at.desc())
                    .limit(bounded_limit)
                )
            ).all()
        )
        terminal_rows = list(
            (
                await session.execute(
                    select(KnowledgeParseRevision, KnowledgeFile.original_filename, KnowledgeFile.filename)
                    .join(KnowledgeFile, KnowledgeFile.file_id == KnowledgeParseRevision.file_id)
                    .where(
                        KnowledgeParseRevision.status.in_(tuple(_TASK_REVISION_TERMINAL_STATUSES)),
                        KnowledgeParseRevision.completed_at >= retention_floor,
                    )
                    .order_by(KnowledgeParseRevision.completed_at.desc())
                    .limit(bounded_limit)
                )
            ).all()
        )
        revision_rows = sorted(
            [*active_rows, *terminal_rows],
            key=lambda row: row[0].created_at or row[0].completed_at or row[0].updated_at,
            reverse=True,
        )[:bounded_limit]
        revision_ids = [row[0].revision_id for row in revision_rows]
        stage_rows = (
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

    stages_by_revision: dict[str, list[Any]] = defaultdict(list)
    for stage in stage_rows:
        stages_by_revision[stage.revision_id].append(stage)

    tasks: list[dict[str, Any]] = []
    for revision, original_filename, filename in revision_rows:
        mapped_status = _TASK_STATUS_BY_REVISION.get(str(revision.status or ""), "running")
        if status and mapped_status != status:
            continue
        progress, running_message = _pipeline_stage_progress(stages_by_revision.get(revision.revision_id, []))
        if mapped_status == "success":
            progress = 100.0
            message = _PIPELINE_CAPABILITY_LABELS.get(str(revision.status or ""), "解析入库完成")
        elif mapped_status == "running":
            message = running_message or "正在解析文档"
        elif mapped_status == "queued":
            message = "排队等待解析" if revision.status == "PENDING" else "等待相同文档的解析结果复用"
        elif mapped_status == "failed":
            message = str(revision.error_message or "解析入库失败")
            progress = min(progress, 95.0) if revision.status != "REJECTED" else 100.0
        else:
            message = "解析入库进行中"
        tasks.append(
            {
                "id": f"scipdf:{revision.revision_id}",
                "name": f"PDF 解析入库：{original_filename or filename or 'PDF 文档'}",
                "type": _SCI_PDF_TASK_TYPE,
                "status": mapped_status,
                "progress": progress,
                "message": message,
                # Timestamps use the driver-returned isoformat (+08:00 offset) so all
                # four fields stay on the same wall clock in the task center; the
                # columns mix naive-UTC defaults with aware writes, so re-normalizing
                # through ensure_utc would skew them 8h apart.
                "created_at": revision.created_at.isoformat() if revision.created_at else None,
                "updated_at": revision.updated_at.isoformat() if revision.updated_at else None,
                "started_at": revision.started_at.isoformat() if revision.started_at else None,
                "completed_at": revision.completed_at.isoformat() if revision.completed_at else None,
                "error": revision.error_message if mapped_status == "failed" else None,
                "cancel_requested": False,
                "cancelable": False,
                "deletable": False,
                "created_by": revision.created_by,
                "tenant_id": int(revision.tenant_id or 0),
                "payload": {
                    "kb_id": revision.kb_id,
                    "file_id": revision.file_id,
                    "revision_id": revision.revision_id,
                    "attempt": revision.attempt,
                },
            }
        )

    status_counter = Counter(task["status"] for task in tasks)
    type_counter = Counter(task["type"] for task in tasks)
    return {
        "tasks": tasks,
        "summary": {
            "total": len(tasks),
            "filtered_total": len(tasks),
            "status_counts": dict(status_counter),
            "type_counts": dict(type_counter),
        },
    }


async def rebuild_figure_index_for_file(*, kb_id: str, file_id: str, render_synthetic: bool = True) -> dict[str, Any]:
    """Legacy repair path; published figure facts require a new parse revision."""
    async with pg_manager.get_async_session_context() as session:
        file_record = (
            (
                await session.execute(
                    select(KnowledgeFile).where(KnowledgeFile.file_id == file_id, KnowledgeFile.kb_id == kb_id)
                )
            )
            .scalars()
            .first()
        )
        if file_record is None or not file_record.active_parse_revision_id:
            raise ValueError("文件不存在或没有已激活的科研 PDF 解析版本")
        revision = (
            (
                await session.execute(
                    select(KnowledgeParseRevision).where(
                        KnowledgeParseRevision.revision_id == file_record.active_parse_revision_id
                    )
                )
            )
            .scalars()
            .first()
        )
        if revision is None:
            raise ValueError("激活的解析版本不存在")
        if revision.sealed_at is not None:
            raise ValueError("PUBLISHED_PDF_EVIDENCE_IMMUTABLE_CREATE_NEW_REVISION")
        artifact = (
            (
                await session.execute(
                    select(KnowledgeParseArtifact).where(
                        KnowledgeParseArtifact.revision_id == revision.revision_id,
                        KnowledgeParseArtifact.kind == "unified_article",
                    )
                )
            )
            .scalars()
            .first()
        )
        if artifact is None or not is_minio_url(artifact.object_uri):
            raise ValueError("该解析版本没有 unified_article 产物，无法重建图表索引")
        bucket, object_name = parse_minio_url(artifact.object_uri)
        article = json.loads((await get_minio_client().adownload_file(bucket, object_name)).decode("utf-8"))
        spans = list(
            (
                await session.execute(
                    select(EvidenceSpanRecord).where(EvidenceSpanRecord.parse_revision_id == revision.revision_id)
                )
            )
            .scalars()
            .all()
        )
        anchors = list(
            (
                await session.execute(
                    select(EvidenceAnchorRecord).where(EvidenceAnchorRecord.parse_revision_id == revision.revision_id)
                )
            )
            .scalars()
            .all()
        )
        from yuxi.knowledge.vision.figure_ingestor import persist_figure_index

        async def _load_pdf() -> bytes | None:
            return await _load_source(file_record)

        summary = await persist_figure_index(
            session,
            revision=revision,
            article_assets=list(article.get("assets") or []),
            spans=spans,
            anchors=anchors,
            pdf_bytes_loader=_load_pdf if render_synthetic else None,
        )
        qa_report = dict(revision.qa_report or {})
        qa_report["figure_index"] = summary
        capabilities = dict(qa_report.get("capabilities") or {})
        capabilities["figure_image_locator"] = bool(summary.get("locator_ready_assets"))
        qa_report["capabilities"] = capabilities
        revision.qa_report = qa_report
        await session.commit()
        return {"revision_id": str(revision.revision_id), "figure_index": summary}
