"""文献域阅读视图渲染服务（P1，ADR-0010 决议 7/10）。

编排形态对齐 ``scientific_pdf_ingest_service``：PG 行是持久真相（PENDING →
RUNNING → SUCCEEDED/FAILED），ARQ 只是调度器；入列失败交给对账 cron 补偿；
渲染执行经 :mod:`yuxi.literature.reading.process` 在子进程完成（不阻塞 loop）。

不变量 L0-2（由 test_render_fingerprint_isolation 钉死）：渲染只读
``mineru_markdown`` / ``original_pdf`` 工件，只写 MinIO 派生物与
``literature_reader_renders`` 行——不写 evidence_span、不进 Milvus、
不改 ``parser_fingerprint``。
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select, update

from yuxi.literature.contracts import READER_RENDER_VERSION
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
    LiteratureReaderRender,
)
from yuxi.utils.datetime_utils import utc_now
from yuxi.knowledge.utils.text_utils import validate_markdown_quality

logger = logging.getLogger(__name__)

RENDER_JOB_NAME = "process_literature_reader_render"
RENDER_BUCKET = "knowledgebases"
MAX_RENDER_ATTEMPTS = 3
#: RUNNING 超过该时长视为孤儿（对账 cron 回收重排）。
RUNNING_LEASE_SECONDS = 60 * 30
#: PENDING 超过该时长视为入列丢失（Redis 断连期间的补偿场景）。
PENDING_REQUEUE_SECONDS = 60 * 10


def _render_job_id(revision_id: str, attempt: int) -> str:
    return f"lit-render:{revision_id}:{attempt}"


def _render_object_prefix(*, tenant_id: int, source_sha256: str, revision_id: str) -> str:
    """阅读派生物在 MinIO 的规范前缀（与解析工件同树、独立 reader_render 命名空间）。"""
    return f"tenants/{tenant_id}/documents/{source_sha256}/reader_render/{revision_id}"


async def enqueue_reader_render(*, revision_id: str, force: bool = False) -> dict[str, Any]:
    """为解析版本排一个阅读渲染任务（幂等：成功且同版本则跳过，RUNNING 则跳过）。"""
    async with pg_manager.get_async_session_context() as session:
        revision = (
            await session.execute(
                select(KnowledgeParseRevision).where(KnowledgeParseRevision.revision_id == revision_id)
            )
        ).scalar_one_or_none()
        if revision is None:
            raise ValueError(f"解析版本不存在: {revision_id}")
        row = (
            await session.execute(
                select(LiteratureReaderRender).where(LiteratureReaderRender.revision_id == revision_id)
            )
        ).scalar_one_or_none()
        if row is not None and not force:
            if row.status == "SUCCEEDED" and row.render_version == READER_RENDER_VERSION:
                return {"revision_id": revision_id, "status": "SUCCEEDED", "reused": True}
            if row.status == "RUNNING":
                return {"revision_id": revision_id, "status": "RUNNING", "reused": True}
        if row is None:
            row = LiteratureReaderRender(
                revision_id=revision_id,
                tenant_id=revision.tenant_id,
                render_version=READER_RENDER_VERSION,
                status="PENDING",
                attempt=0,
            )
            session.add(row)
            await session.flush()
        else:
            row.status = "PENDING"
            row.error = None
        revision_id_value = revision_id
        attempt = int(row.attempt or 0)
        await session.commit()

    enqueue_deferred = False
    try:
        queue = await get_arq_pool()
        await queue.enqueue_job(RENDER_JOB_NAME, revision_id_value, _job_id=_render_job_id(revision_id_value, attempt))
    except Exception:  # noqa: BLE001
        enqueue_deferred = True
        logger.exception("阅读渲染任务已持久化，但即时入队失败，将由对账任务补偿: %s", revision_id_value)
    return {
        "revision_id": revision_id_value,
        "status": "PENDING",
        "reused": False,
        "enqueue_deferred": enqueue_deferred,
    }


async def _claim_render(revision_id: str) -> LiteratureReaderRender | None:
    """PENDING → RUNNING 认领（行级守卫；重复投递由 ARQ _job_id 幂等吸收）。"""
    async with pg_manager.get_async_session_context() as session:
        row = (
            await session.execute(
                select(LiteratureReaderRender)
                .where(LiteratureReaderRender.revision_id == revision_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None or row.status != "PENDING":
            return None
        row.status = "RUNNING"
        row.updated_at = utc_now()
        await session.commit()
        return row


async def _load_render_inputs(revision_id: str) -> tuple[KnowledgeParseRevision, dict[str, str]]:
    """取渲染输入：revision 行 + {mineru_markdown, original_pdf} 的 object_uri。"""
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
                        KnowledgeParseArtifact.kind.in_(("mineru_markdown", "original_pdf")),
                    )
                )
            )
            .scalars()
            .all()
        )
    by_kind = {artifact.kind: artifact.object_uri for artifact in artifacts}
    missing = [kind for kind in ("mineru_markdown", "original_pdf") if kind not in by_kind]
    if missing:
        raise ValueError(f"解析版本缺少渲染输入工件 {missing}: {revision_id}")
    return revision, by_kind


async def _finish_success(row_id: int, *, markdown_uri: str, result: dict[str, Any]) -> None:
    async with pg_manager.get_async_session_context() as session:
        await session.execute(
            update(LiteratureReaderRender)
            .where(LiteratureReaderRender.id == row_id)
            .values(
                status="SUCCEEDED",
                markdown_uri=markdown_uri,
                image_count=int(result.get("image_count") or 0),
                latex_count=int(result.get("latex_count") or 0),
                source_digest=str(result.get("source_digest") or ""),
                error=None,
                updated_at=utc_now(),
            )
        )
        await session.commit()


async def _finish_failure(revision_id: str, row_id: int, reason: str) -> bool:
    """失败处置：attempt 内回 PENDING 并重排，超限置 FAILED。返回是否已重排。"""
    async with pg_manager.get_async_session_context() as session:
        row = (
            await session.execute(
                select(LiteratureReaderRender).where(LiteratureReaderRender.id == row_id).with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        row.attempt = int(row.attempt or 0) + 1
        row.error = reason[:2000]
        row.updated_at = utc_now()
        requeued = False
        if row.attempt < MAX_RENDER_ATTEMPTS:
            row.status = "PENDING"
            requeued = True
        else:
            row.status = "FAILED"
        attempt = row.attempt
        await session.commit()
    if requeued:
        try:
            queue = await get_arq_pool()
            await queue.enqueue_job(RENDER_JOB_NAME, revision_id, _job_id=_render_job_id(revision_id, attempt))
        except Exception:  # noqa: BLE001
            logger.exception("阅读渲染失败重排入队失败，将由对账任务补偿: %s", revision_id)
    return requeued


async def process_literature_reader_render(ctx: dict[str, Any], revision_id: str) -> dict[str, Any] | None:
    """ARQ 任务：渲染一个解析版本的阅读视图（子进程执行，见决议 7）。"""
    row = await _claim_render(revision_id)
    if row is None:
        return None
    try:
        revision, inputs = await _load_render_inputs(revision_id)
        minio = get_minio_client()
        markdown_bytes = await minio.adownload_file(RENDER_BUCKET, inputs["mineru_markdown"])
        pdf_bytes = await minio.adownload_file(RENDER_BUCKET, inputs["original_pdf"])

        prefix = _render_object_prefix(
            tenant_id=revision.tenant_id, source_sha256=revision.source_sha256, revision_id=revision_id
        )
        markdown_uri = f"kbasset://{prefix}/render.md"

        with tempfile.TemporaryDirectory(prefix="lit-render-") as tmp:
            tmp_dir = Path(tmp)
            pdf_path = tmp_dir / "source.pdf"
            pdf_path.write_bytes(pdf_bytes)
            md_path = tmp_dir / "full.md"
            md_path.write_bytes(markdown_bytes)
            result = await asyncio.to_thread(
                render_in_subprocess,
                pdf_path=str(pdf_path),
                md_path=str(md_path),
                work_dir=str(tmp_dir / "run"),
                timeout_s=DEFAULT_RENDER_TIMEOUT_S,
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
            if document_json.strip():
                await minio.aupload_file(
                    bucket_name=RENDER_BUCKET,
                    object_name=f"{prefix}/document.json",
                    data=document_json.encode("utf-8"),
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
        await _finish_success(row.id, markdown_uri=markdown_uri, result=result)
        logger.info("阅读渲染完成: revision=%s uri=%s", revision_id, markdown_uri)
        return {"revision_id": revision_id, "status": "SUCCEEDED", "markdown_uri": markdown_uri}
    except Exception as exc:  # noqa: BLE001
        reason = f"{type(exc).__name__}: {exc}"
        logger.exception("阅读渲染失败: revision=%s", revision_id)
        requeued = await _finish_failure(revision_id, row.id, reason)
        return {"revision_id": revision_id, "status": "REQUEUED" if requeued else "FAILED", "error": reason}


async def reconcile_stale_literature_renders() -> int:
    """对账（cron 每 5 分钟）：孤儿 RUNNING 回收重排；滞留 PENDING 补偿入列。"""
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
            row.status = "PENDING"
            row.updated_at = now
            targets.append((row.revision_id, int(row.attempt or 0)))
        await session.commit()
    for revision_id, attempt in targets:
        try:
            queue = await get_arq_pool()
            await queue.enqueue_job(RENDER_JOB_NAME, revision_id, _job_id=_render_job_id(revision_id, attempt))
            requeued += 1
        except Exception:  # noqa: BLE001
            logger.exception("对账重排入队失败（下一轮重试）: %s", revision_id)
    if targets:
        logger.info("阅读渲染对账：回收 %d 个滞留任务", len(targets))
    return requeued
