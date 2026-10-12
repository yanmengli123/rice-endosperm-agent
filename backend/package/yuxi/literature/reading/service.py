"""文献域阅读视图渲染服务（P1，ADR-0010 决议 7/10）。

编排形态对齐 ``scientific_pdf_ingest_service``：PG 行是持久真相（PENDING →
RUNNING → SUCCEEDED/FAILED），ARQ 只是调度器；入列失败交给对账 cron 补偿；
渲染执行经 :mod:`yuxi.literature.reading.process` 在子进程完成（不阻塞 loop）。

不变量 L0-2（由 test_render_fingerprint_isolation 钉死）：渲染只读
``mineru_markdown``（缺失时使用已入库 ``indexed_markdown``）/ ``original_pdf`` 工件，只写 MinIO 派生物与
``literature_reader_renders`` 行——不写 evidence_span、不进 Milvus、
不改 ``parser_fingerprint``。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select, update

from yuxi.knowledge.utils import is_minio_url, parse_minio_url
from yuxi.knowledge.utils.text_utils import validate_markdown_quality
from yuxi.literature.contracts import READER_RENDER_DUAL_VERSION, READER_RENDER_VERSION
from yuxi.literature.reading.process import (
    DEFAULT_RENDER_TIMEOUT_S,
    render_in_subprocess,
)
from yuxi.services.run_queue_service import get_arq_pool
from yuxi.storage.minio import get_minio_client
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeParseArtifact,
    KnowledgeParseRevision,
    LiteratureArtifactRevision,
    LiteraturePaper,
    LiteratureReaderRender,
)
from yuxi.utils.datetime_utils import utc_now

logger = logging.getLogger(__name__)

RENDER_JOB_NAME = "process_literature_reader_render"
RENDER_BUCKET = "knowledgebases"
MAX_RENDER_ATTEMPTS = 3
#: RUNNING 超过该时长视为孤儿（对账 cron 回收重排）。
RUNNING_LEASE_SECONDS = 60 * 30
#: PENDING 超过该时长视为入列丢失（Redis 断连期间的补偿场景）。
PENDING_REQUEUE_SECONDS = 60 * 10


def _render_job_id(revision_id: str, attempt: int, render_version: str, generation: int) -> str:
    return f"lit-render:{revision_id}:{render_version}:g{generation}:a{attempt}"


async def _dispatch_render(revision_id: str, render_version: str, generation: int, attempt: int) -> bool:
    """DB 是调度真相；Redis 拒绝/断连留待 cron 补偿，不冒充成功入列。"""
    try:
        queue = await get_arq_pool()
        job = await queue.enqueue_job(
            RENDER_JOB_NAME,
            revision_id,
            render_version,
            generation,
            attempt,
            _job_id=_render_job_id(revision_id, attempt, render_version, generation),
        )
        return job is not None
    except Exception:  # noqa: BLE001
        logger.exception("阅读渲染已持久化，入队失败将由对账任务补偿: %s", revision_id)
        return False


def _resolve_render_input(uri: str) -> tuple[str, str]:
    """把工件 ``object_uri`` 解析为 (bucket, object)。

    工件行存的是完整 MinIO URL（如 ``http://minio:9000/knowledgebases/...``），
    与 :mod:`scientific_pdf_ingest_service` 同一形态。直接把它当对象名传给
    MinIO 客户端会让每一次渲染都取不到对象，进而级联拖垮编译与翻译。
    非本系统 URL 一律拒绝，不做猜测回退。
    """
    if not is_minio_url(uri):
        raise ValueError(f"渲染输入不是本系统 MinIO URL: {uri}")
    bucket, object_name = parse_minio_url(uri)
    if not object_name:
        raise ValueError(f"渲染输入 URL 缺少对象名: {uri}")
    return bucket, object_name


def _render_object_prefix(*, tenant_id: int, source_sha256: str, revision_id: str) -> str:
    """阅读派生物在 MinIO 的规范前缀（与解析工件同树、独立 reader_render 命名空间）。"""
    return f"tenants/{tenant_id}/documents/{source_sha256}/reader_render/{revision_id}"


def render_version_for(dual: bool) -> str:
    """渲染模式编码进 render_version（决议 12）：离线 v1 与双通道 v1-dual 并存。"""
    return READER_RENDER_DUAL_VERSION if dual else READER_RENDER_VERSION


async def enqueue_reader_render(*, revision_id: str, force: bool = False, dual: bool = False) -> dict[str, Any]:
    """为解析版本排一个阅读渲染任务（幂等：成功且同版本则跳过，RUNNING 则跳过）。

    ``dual=True`` 走双通道路径（PaddleOCR 辅通道 + 第三信号，决议 12）：
    凭据经 YUXI_PADDLE_TOKEN 环境变量，缺失时 fail-fast（由 review 层预检）。
    """
    render_version = render_version_for(dual)
    async with pg_manager.get_async_session_context() as session:
        revision = (
            await session.execute(
                select(KnowledgeParseRevision)
                .where(KnowledgeParseRevision.revision_id == revision_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if revision is None:
            raise ValueError(f"解析版本不存在: {revision_id}")
        row = (
            await session.execute(
                select(LiteratureReaderRender)
                .where(LiteratureReaderRender.revision_id == revision_id)
                .where(LiteratureReaderRender.render_version == render_version)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is not None and not force:
            if row.status == "SUCCEEDED" and row.render_version == render_version:
                return {"revision_id": revision_id, "status": "SUCCEEDED", "reused": True}
            if row.status in {"RUNNING", "PENDING"}:
                return {"revision_id": revision_id, "status": row.status, "reused": True}
        if row is not None and row.status in {"RUNNING", "PENDING"}:
            return {"revision_id": revision_id, "status": row.status, "reused": True}
        if row is None:
            row = LiteratureReaderRender(
                revision_id=revision_id,
                tenant_id=revision.tenant_id,
                render_version=render_version,
                status="PENDING",
                attempt=0,
                generation=0,
            )
            session.add(row)
            await session.flush()
        else:
            row.status = "PENDING"
            row.error = None
            row.render_version = render_version
            row.attempt = 0
            row.generation += 1
        row.updated_at = utc_now()
        revision_id_value = revision_id
        attempt = int(row.attempt or 0)
        generation = row.generation
        await session.commit()

    enqueued = await _dispatch_render(revision_id_value, render_version, generation, attempt)
    return {
        "revision_id": revision_id_value,
        "status": "PENDING",
        "reused": False,
        "enqueue_deferred": not enqueued,
    }


async def _claim_render(
    revision_id: str,
    render_version: str | None = None,
    expected_generation: int | None = None,
    expected_attempt: int | None = None,
) -> LiteratureReaderRender | None:
    """PENDING → RUNNING；旧无代际消息只能认领迁移后的初始代际/初次执行。"""
    async with pg_manager.get_async_session_context() as session:
        query = select(LiteratureReaderRender).where(
            LiteratureReaderRender.revision_id == revision_id, LiteratureReaderRender.status == "PENDING"
        )
        query = query.where(
            LiteratureReaderRender.generation == (expected_generation if expected_generation is not None else 0),
            LiteratureReaderRender.attempt == (expected_attempt if expected_attempt is not None else 0),
        )
        if render_version:
            query = query.where(LiteratureReaderRender.render_version == render_version)
        row = (
            await session.execute(query.order_by(LiteratureReaderRender.id).limit(1).with_for_update())
        ).scalar_one_or_none()
        if row is None or row.status != "PENDING":
            return None
        row.status = "RUNNING"
        row.updated_at = utc_now()
        await session.commit()
        return row


async def _load_render_inputs(revision_id: str, *, dual: bool = False) -> tuple[KnowledgeParseRevision, dict[str, str]]:
    """单路排版可使用已入库正文；双 OCR 复核仍要求 MinerU 工件。"""
    async with pg_manager.get_async_session_context() as session:
        revision = (
            await session.execute(
                select(KnowledgeParseRevision).where(KnowledgeParseRevision.revision_id == revision_id)
            )
        ).scalar_one_or_none()
        if revision is None:
            raise ValueError(f"解析版本不存在: {revision_id}")
        artifacts = (
            (
                await session.execute(
                    select(KnowledgeParseArtifact).where(
                        KnowledgeParseArtifact.revision_id == revision_id,
                        KnowledgeParseArtifact.kind.in_(("mineru_markdown", "indexed_markdown", "original_pdf")),
                    )
                )
            )
            .scalars()
            .all()
        )
    by_kind = {artifact.kind: artifact.object_uri for artifact in artifacts}
    markdown_kind = "mineru_markdown" if dual or "mineru_markdown" in by_kind else "indexed_markdown"
    missing = [kind for kind in (markdown_kind, "original_pdf") if kind not in by_kind]
    if missing:
        raise ValueError(f"解析版本缺少渲染输入工件 {missing}: {revision_id}")
    by_kind["reading_markdown"] = by_kind[markdown_kind]
    return revision, by_kind


async def _finish_success(
    row_id: int,
    *,
    markdown_uri: str,
    result: dict[str, Any],
    document_content: bytes | None = None,
    expected_attempt: int | None = None,
    expected_generation: int | None = None,
) -> None:
    async with pg_manager.get_async_session_context() as session:
        row = (
            await session.execute(
                select(LiteratureReaderRender).where(LiteratureReaderRender.id == row_id).with_for_update()
            )
        ).scalar_one()
        if (
            row.status != "RUNNING"
            or row.attempt != (expected_attempt if expected_attempt is not None else 0)
            or row.generation != (expected_generation if expected_generation is not None else 0)
        ):
            raise ValueError("阅读渲染租约已失效；未切换正文")
        await session.execute(
            update(LiteratureReaderRender)
            .where(LiteratureReaderRender.id == row_id)
            .values(
                status="SUCCEEDED",
                markdown_uri=markdown_uri,
                review_uri=None,
                review_digest=None,
                image_count=int(result.get("image_count") or 0),
                latex_count=int(result.get("latex_count") or 0),
                source_digest=str(result.get("source_digest") or ""),
                error=None,
                updated_at=utc_now(),
            )
        )
        papers = list(
            (
                await session.execute(
                    select(LiteraturePaper)
                    .where(
                        LiteraturePaper.source_revision_id == row.revision_id,
                        LiteraturePaper.tenant_id == row.tenant_id,
                        LiteraturePaper.trashed_at.is_(None),
                    )
                    .with_for_update()
                )
            ).scalars()
        )
        changed = False
        for paper in papers:
            frozen = "document" in (paper.frozen_artifacts or [])
            if document_content:
                revision_id = f"lar_{uuid.uuid4().hex[:20]}"
                session.add(
                    LiteratureArtifactRevision(
                        revision_id=revision_id,
                        tenant_id=paper.tenant_id,
                        paper_id=paper.id,
                        kind="document",
                        source="rendered",
                        frozen=False,
                        object_uri=markdown_uri.removesuffix("render.md") + "document.json",
                        content_digest=hashlib.sha256(document_content).hexdigest(),
                        source_revision_id=row.revision_id,
                        input_reading_version=paper.reading_version,
                        created_by="system:literature-render",
                    )
                )
                if not frozen:
                    paper.artifact_pointers = {**(paper.artifact_pointers or {}), "document": revision_id}
            if not frozen:
                paper.reading_version += 1
                paper.ai_value_score, paper.topic_score, paper.value_score = None, None, 0.0
                changed = True
        from yuxi.literature.provenance import invalidate_note_publications

        if changed:
            await invalidate_note_publications(session, row.revision_id)
        await session.commit()


async def _finish_failure(
    revision_id: str,
    row_id: int,
    reason: str,
    *,
    expected_attempt: int | None = None,
    expected_generation: int | None = None,
) -> bool:
    """失败处置：attempt 内回 PENDING 并重排，超限置 FAILED。返回是否已重排。"""
    async with pg_manager.get_async_session_context() as session:
        row = (
            await session.execute(
                select(LiteratureReaderRender).where(LiteratureReaderRender.id == row_id).with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        if (
            row.status != "RUNNING"
            or row.attempt != (expected_attempt if expected_attempt is not None else 0)
            or row.generation != (expected_generation if expected_generation is not None else 0)
        ):
            return False
        row.attempt = int(row.attempt or 0) + 1
        row.generation += 1
        row.error = reason[:2000]
        row.updated_at = utc_now()
        requeued = False
        if row.attempt < MAX_RENDER_ATTEMPTS:
            row.status = "PENDING"
            requeued = True
        else:
            row.status = "FAILED"
        attempt = row.attempt
        render_version = row.render_version
        generation = row.generation
        await session.commit()
    if requeued:
        await _dispatch_render(revision_id, render_version, generation, attempt)
    return requeued


async def process_literature_reader_render(
    ctx: dict[str, Any],
    revision_id: str,
    render_version: str | None = None,
    expected_generation: int | None = None,
    expected_attempt: int | None = None,
) -> dict[str, Any] | None:
    """ARQ 任务：渲染一个解析版本的阅读视图（子进程执行，见决议 7）。"""
    row = await _claim_render(revision_id, render_version, expected_generation, expected_attempt)
    if row is None:
        return None
    claim_attempt = int(row.attempt or 0)
    claim_generation = row.generation
    try:
        dual_mode = row.render_version == READER_RENDER_DUAL_VERSION
        revision, inputs = await _load_render_inputs(revision_id, dual=dual_mode)
        minio = get_minio_client()
        # ParserArtifact.object_uri stores a full MinIO URL, not a bare object name
        # (same shape scientific_pdf_ingest_service parses). Resolve it through the
        # URL contract before touching MinIO, otherwise every render 404s.
        markdown_bucket, markdown_object = _resolve_render_input(inputs["reading_markdown"])
        pdf_bucket, pdf_object = _resolve_render_input(inputs["original_pdf"])
        markdown_bytes = await minio.adownload_file(markdown_bucket, markdown_object)
        pdf_bytes = await minio.adownload_file(pdf_bucket, pdf_object)

        prefix = _render_object_prefix(
            tenant_id=revision.tenant_id, source_sha256=revision.source_sha256, revision_id=revision_id
        )
        # Publish the DB pointer only after all artifacts are complete. A rerun
        # must never overwrite the currently served document or prior decisions.
        prefix += f"/{row.render_version}/{uuid.uuid4().hex}"
        markdown_uri = f"kbasset://{prefix}/render.md"

        with tempfile.TemporaryDirectory(prefix="lit-render-") as tmp:
            tmp_dir = Path(tmp)
            pdf_path = tmp_dir / "source.pdf"
            pdf_path.write_bytes(pdf_bytes)
            md_path = tmp_dir / "full.md"
            md_path.write_bytes(markdown_bytes)
            extra_env = {}
            if dual_mode:
                paddle_token = os.getenv("YUXI_PADDLE_TOKEN", "").strip()
                if not paddle_token:
                    raise ValueError("双通道渲染缺少凭据（YUXI_PADDLE_TOKEN 未配置）")
                extra_env["PADDLEOCR_ACCESS_TOKEN"] = paddle_token
            result = await asyncio.to_thread(
                render_in_subprocess,
                pdf_path=str(pdf_path),
                md_path=str(md_path),
                work_dir=str(tmp_dir / "run"),
                timeout_s=DEFAULT_RENDER_TIMEOUT_S,
                mode="dual" if dual_mode else "offline",
                extra_env=extra_env or None,
            )
            markdown = validate_markdown_quality(result.get("markdown"))
            await minio.aupload_file(
                bucket_name=RENDER_BUCKET,
                object_name=f"{prefix}/render.md",
                data=markdown.encode("utf-8"),
                content_type="text/markdown",
            )
            # document.json（段落级 [Pxxx] 结构）：P2 编译/翻译的引用锚点来源；可选产物
            document_json = str(result.get("document_json") or "")
            document_content = None
            if document_json.strip():
                from yuxi.literature.artifacts import validate_document

                document = validate_document(document_json.encode("utf-8"))
                document["_image_base"] = f"{prefix}/"
                document_content = json.dumps(document, ensure_ascii=False, sort_keys=True).encode("utf-8")
                await minio.aupload_file(
                    bucket_name=RENDER_BUCKET,
                    object_name=f"{prefix}/document.json",
                    data=document_content,
                    content_type="application/json",
                )
            review_json = str(result.get("review_json") or "")
            if review_json.strip():
                await minio.aupload_file(
                    bucket_name=RENDER_BUCKET,
                    object_name=f"{prefix}/work/review.json",
                    data=review_json.encode("utf-8"),
                    content_type="application/json",
                )
            for image_name in result.get("image_names") or []:
                # 图片在子进程产物目录（product_root/images/，同机路径）读回后上传
                image_bytes = (Path(result["product_root"]) / "images" / image_name).read_bytes()
                content_type = "image/jpeg" if Path(image_name).suffix.lower() in {".jpg", ".jpeg"} else "image/png"
                await minio.aupload_file(
                    bucket_name=RENDER_BUCKET,
                    object_name=f"{prefix}/images/{image_name}",
                    data=image_bytes,
                    content_type=content_type,
                )
        await _finish_success(
            row.id,
            markdown_uri=markdown_uri,
            result=result,
            document_content=document_content,
            expected_attempt=claim_attempt,
            expected_generation=claim_generation,
        )
        logger.info("阅读渲染完成: revision=%s uri=%s", revision_id, markdown_uri)
        return {"revision_id": revision_id, "status": "SUCCEEDED", "markdown_uri": markdown_uri}
    except Exception as exc:  # noqa: BLE001
        reason = f"{type(exc).__name__}: {exc}"
        logger.exception("阅读渲染失败: revision=%s", revision_id)
        requeued = await _finish_failure(
            revision_id,
            row.id,
            reason,
            expected_attempt=claim_attempt,
            expected_generation=claim_generation,
        )
        return {"revision_id": revision_id, "status": "REQUEUED" if requeued else "FAILED", "error": reason}


async def reconcile_stale_literature_renders(ctx: dict | None = None) -> int:
    """对账（cron 每 5 分钟）：孤儿 RUNNING 回收重排；滞留 PENDING 补偿入列。"""
    del ctx  # ARQ cron 注入的作业上下文（对账不使用）
    now = utc_now()
    requeued = 0
    async with pg_manager.get_async_session_context() as session:
        stale_running = (
            (
                await session.execute(
                    select(LiteratureReaderRender)
                    .where(
                        LiteratureReaderRender.status == "RUNNING",
                        LiteratureReaderRender.updated_at < now - timedelta(seconds=RUNNING_LEASE_SECONDS),
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        stale_pending = (
            (
                await session.execute(
                    select(LiteratureReaderRender)
                    .where(
                        LiteratureReaderRender.status == "PENDING",
                        LiteratureReaderRender.updated_at < now - timedelta(seconds=PENDING_REQUEUE_SECONDS),
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        targets = []
        for row in (*stale_running, *stale_pending):
            if row.status == "RUNNING":
                row.attempt += 1
                row.error = "阅读渲染租约超时"
            row.generation += 1
            row.updated_at = now
            if row.attempt >= MAX_RENDER_ATTEMPTS:
                row.status = "FAILED"
                continue
            row.status = "PENDING"
            targets.append((row.revision_id, row.render_version, row.generation, row.attempt))
        await session.commit()
    for revision_id, render_version, generation, attempt in targets:
        if await _dispatch_render(revision_id, render_version, generation, attempt):
            requeued += 1
    if targets:
        logger.info("阅读渲染对账：回收 %d 个滞留任务", len(targets))
    return requeued
