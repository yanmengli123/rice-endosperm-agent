"""L1/L2/L3 编译服务（P2，ADR-0010 决议 3/11）。

形态对齐 ``scientific_pdf_ingest_service`` / ``reading.service``：PG 行是持久
真相，ARQ 只是调度器；attempt 上限置 DEAD（补上游「无重试预算/死信」缺口）；
对账 cron 回收孤儿。升级链口径（上游自纠后）：L1 全做（合并产出 L1+L2）；
L3 需 ai_value 可用且 value_score ≥ 4.0。

编译输入：``literature_papers.source_revision_id`` → 阅读渲染 SUCCEEDED 的
document.json（段落 [Pxxx] 锚点）。编译产物落 MinIO
``tenants/{t}/literature/notes/{rid}/``，**不写 evidence_spans、不进 Milvus**。
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from yuxi.literature.boundaries import assert_not_evidence_capable
from yuxi.literature.contracts import ResourceKind
from yuxi.literature.identity.rid import make_rid
from yuxi.literature.notes import compiler, value_score
from yuxi.literature.reading.service import RENDER_BUCKET
from yuxi.services.run_queue_service import get_arq_pool
from yuxi.storage.minio import get_minio_client
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    LiteratureCompileJob,
    LiteraturePaper,
    LiteratureReaderRender,
)
from yuxi.utils.datetime_utils import utc_now

logger = logging.getLogger(__name__)

COMPILE_JOB_NAME = "process_literature_compile"
MAX_COMPILE_ATTEMPTS = 3
RUNNING_LEASE_SECONDS = 60 * 30
PENDING_REQUEUE_SECONDS = 60 * 10
VALID_LEVELS = ("L1", "L2", "L3")

_L3_MIN_SELF_CTX_CHARS = 80


def _compile_job_id(job_id: int) -> str:
    return f"lit-compile:{job_id}"


def _notes_prefix(*, tenant_id: int, rid: str) -> str:
    return f"tenants/{tenant_id}/literature/notes/{rid}"


# ---------------------------------------------------------------- 论文登记


async def register_literature_paper(
    *,
    tenant_id: int,
    kind: ResourceKind = "paper",
    doi: str = "",
    isbn: str = "",
    cnki: str = "",
    arxiv: str = "",
    fingerprint: str = "",
    title: str = "",
    source_revision_id: str | None = None,
) -> LiteraturePaper:
    """登记/更新文献身份（幂等：同租户同 RID 复用既有行）。RID 语义经 P0 身份模块。"""
    rid = make_rid(kind, doi=doi, isbn=isbn, cnki=cnki, arxiv=arxiv, fingerprint=fingerprint)
    async with pg_manager.get_async_session_context() as session:
        row = (
            await session.execute(
                select(LiteraturePaper).where(LiteraturePaper.tenant_id == tenant_id, LiteraturePaper.rid == rid)
            )
        ).scalar_one_or_none()
        if row is None:
            row = LiteraturePaper(
                tenant_id=tenant_id,
                rid=rid,
                kind=kind,
                doi=(doi or None),
                title=title or None,
                source_revision_id=source_revision_id,
            )
            session.add(row)
        else:
            if title and not row.title:
                row.title = title
            if source_revision_id and not row.source_revision_id:
                row.source_revision_id = source_revision_id
        await session.flush()
        await session.refresh(row)
        await session.commit()
        return row


# ---------------------------------------------------------------- 任务入列


async def enqueue_literature_compile(*, paper_id: int, level: str, force: bool = False) -> dict[str, Any]:
    """排一个编译任务（幂等：SUCCEEDED/RUNNING 跳过；FAILED 重置重跑）。"""
    if level not in VALID_LEVELS:
        raise ValueError(f"非法编译级别: {level}")
    async with pg_manager.get_async_session_context() as session:
        paper = (
            await session.execute(select(LiteraturePaper).where(LiteraturePaper.id == paper_id))
        ).scalar_one_or_none()
        if paper is None:
            raise ValueError(f"文献不存在: {paper_id}")
        job = (
            await session.execute(
                select(LiteratureCompileJob).where(
                    LiteratureCompileJob.tenant_id == paper.tenant_id,
                    LiteratureCompileJob.paper_id == paper_id,
                    LiteratureCompileJob.level == level,
                )
            )
        ).scalar_one_or_none()
        if job is not None and not force:
            if job.status in ("SUCCEEDED", "RUNNING", "PENDING"):
                return {"job_id": job.id, "status": job.status, "reused": True}
        if job is None:
            job = LiteratureCompileJob(tenant_id=paper.tenant_id, paper_id=paper_id, level=level, status="PENDING")
            session.add(job)
        else:
            job.status = "PENDING"
            job.error = None
        await session.flush()
        job_id = job.id
        await session.commit()

    enqueue_deferred = False
    try:
        queue = await get_arq_pool()
        await queue.enqueue_job(COMPILE_JOB_NAME, job_id, _job_id=_compile_job_id(job_id))
    except Exception:  # noqa: BLE001
        enqueue_deferred = True
        logger.exception("编译任务已持久化，但即时入队失败，将由对账任务补偿: job=%s", job_id)
    return {"job_id": job_id, "status": "PENDING", "reused": False, "enqueue_deferred": enqueue_deferred}


# ---------------------------------------------------------------- 任务执行


async def _claim_job(job_id: int) -> LiteratureCompileJob | None:
    async with pg_manager.get_async_session_context() as session:
        job = (
            await session.execute(
                select(LiteratureCompileJob).where(LiteratureCompileJob.id == job_id).with_for_update()
            )
        ).scalar_one_or_none()
        if job is None or job.status != "PENDING":
            return None
        job.status = "RUNNING"
        job.updated_at = utc_now()
        await session.commit()
        return job


async def _load_compile_inputs(paper: LiteraturePaper) -> tuple[dict, list[str], str]:
    """编译输入：(meta, paragraphs, journal_meta)。段落来自阅读渲染的 document.json。"""
    if not paper.source_revision_id:
        raise ValueError(f"文献未关联解析版本: {paper.rid}")
    async with pg_manager.get_async_session_context() as session:
        render = (
            await session.execute(
                select(LiteratureReaderRender).where(
                    LiteratureReaderRender.revision_id == paper.source_revision_id,
                    LiteratureReaderRender.status == "SUCCEEDED",
                )
            )
        ).scalar_one_or_none()
        if render is None:
            raise ValueError(f"解析版本尚无成功的阅读渲染: {paper.source_revision_id}")
    if not render.markdown_uri or not render.markdown_uri.startswith("kbasset://"):
        raise ValueError(f"渲染产物 URI 非法: {render.markdown_uri!r}")
    # 修正版优先（人工裁决回放，review.document_object_candidates）；无裁决回落 canonical
    from yuxi.literature.review import document_object_candidates

    document_json = None
    for candidate in document_object_candidates(render.markdown_uri):
        try:
            document_json = await get_minio_client().adownload_file(RENDER_BUCKET, candidate)
            break
        except Exception:  # noqa: BLE001 - 候选缺失试下一个
            continue
    if document_json is None:
        raise ValueError(f"渲染产物 document.json 不可读: {render.markdown_uri}")
    doc = json.loads(document_json.decode("utf-8"))
    paragraphs = [
        str(p.get("text") or "")
        for p in (doc.get("paragraphs") or [])
        if isinstance(p, dict) and (p.get("text") or "").strip()
    ]
    if not paragraphs:
        raise ValueError(f"document.json 无可用段落: {render.markdown_uri}")
    meta = {
        "title": paper.title or doc.get("metadata", {}).get("title", ""),
        "doi": paper.doi or "",
        "journal": doc.get("metadata", {}).get("journal", ""),
        "year": doc.get("metadata", {}).get("year", ""),
        "authors": doc.get("metadata", {}).get("authors", []),
    }
    return meta, paragraphs, meta.get("journal", "")


def _notes_object_prefix(paper: LiteraturePaper) -> str:
    # notes 以 rid 为主键路径（与前缀缓存无关）
    return _notes_prefix(tenant_id=paper.tenant_id, rid=paper.rid)


async def _finish_compile_success(job_id: int, *, note_uri: str, value_score: float | None) -> None:
    async with pg_manager.get_async_session_context() as session:
        await session.execute(
            update(LiteratureCompileJob)
            .where(LiteratureCompileJob.id == job_id)
            .values(status="SUCCEEDED", note_uri=note_uri, value_score=value_score, error=None, updated_at=utc_now())
        )
        await session.commit()


async def _finish_compile_failure(job_id: int, reason: str) -> str:
    """attempt 内回 PENDING 并重排；超限置 DEAD。返回最终状态。"""
    requeued = False
    async with pg_manager.get_async_session_context() as session:
        job = (
            await session.execute(
                select(LiteratureCompileJob).where(LiteratureCompileJob.id == job_id).with_for_update()
            )
        ).scalar_one_or_none()
        if job is None:
            return "MISSING"
        job.attempt = int(job.attempt or 0) + 1
        job.error = reason[:2000]
        if job.attempt < MAX_COMPILE_ATTEMPTS:
            job.status = "PENDING"
            requeued = True
        else:
            job.status = "DEAD"
        await session.commit()
    if requeued:
        try:
            queue = await get_arq_pool()
            await queue.enqueue_job(COMPILE_JOB_NAME, job_id, _job_id=_compile_job_id(job_id))
        except Exception:  # noqa: BLE001
            logger.exception("编译失败重排入队失败，将由对账任务补偿: job=%s", job_id)
    return "PENDING" if requeued else "DEAD"


async def process_literature_compile(ctx: dict[str, Any], job_id: int) -> dict[str, Any] | None:
    """ARQ 任务：编译一个文献的一个级别（L1=合并产出 L1+L2；L3=关系层）。"""
    job = await _claim_job(job_id)
    if job is None:
        return None
    try:
        assert_not_evidence_capable(compiler.LITERATURE_NOTES_KB_TYPE)
        async with pg_manager.get_async_session_context() as session:
            paper = (
                await session.execute(select(LiteraturePaper).where(LiteraturePaper.id == job.paper_id))
            ).scalar_one()
        meta, paragraphs, journal_meta = await _load_compile_inputs(paper)
        minio = get_minio_client()
        prefix = _notes_object_prefix(paper)

        if job.level in ("L1", "L2"):
            result = await compiler.compile_l1_l2(meta, paragraphs, journal_meta=journal_meta)
            note_md = compiler.render_l1_note(meta, result["l1"])
            note_uri = f"kbasset://{prefix}/L1_note.md"
            await minio.aupload_file(
                bucket_name=RENDER_BUCKET,
                object_name=f"{prefix}/L1_note.md",
                data=note_md.encode("utf-8"),
                content_type="text/markdown",
            )
            if result["l2"]:
                await minio.aupload_file(
                    bucket_name=RENDER_BUCKET,
                    object_name=f"{prefix}/L2_wiki.md",
                    data=compiler.render_l2_wiki(meta, result["l2"]).encode("utf-8"),
                    content_type="text/markdown",
                )
            score = value_score.compute_value_score(
                {
                    "ai_value": result["ai_value"],
                    "topic_score": result["topic_score"],
                    "year": meta.get("year"),
                    "journal_info": None,
                    "has_bib": False,
                    "paper_rank": paper.paper_rank or None,
                    "library_citations": None,
                }
            )
            await _finish_compile_success(job.id, note_uri=note_uri, value_score=score)
            # 回填评分 + L3 自动升级（ai_value 可用且 score ≥ 4.0）
            async with pg_manager.get_async_session_context() as session:
                await session.execute(
                    update(LiteraturePaper)
                    .where(LiteraturePaper.id == paper.id)
                    .values(
                        ai_value_score=result["ai_value"],
                        topic_score=result["topic_score"],
                        value_score=score,
                        updated_at=utc_now(),
                    )
                )
                await session.commit()
            want_l3 = value_score.auto_upgrade_levels(score, ai_value_available=result["ai_value"] is not None)[1]
            if want_l3:
                await enqueue_literature_compile(paper_id=paper.id, level="L3")
            return {"job_id": job.id, "level": job.level, "status": "SUCCEEDED", "note_uri": note_uri}

        # L3：本文编译上下文 = L1 笔记（kbasset 同前缀）；相关文献检索随 P4 计量接入
        l1_object = f"{prefix}/L1_note.md"
        l1_bytes = await minio.adownload_file(RENDER_BUCKET, l1_object)
        self_ctx = l1_bytes.decode("utf-8")
        if len(self_ctx.strip()) < _L3_MIN_SELF_CTX_CHARS:
            raise ValueError("L1 笔记缺失或过短，无法编译 L3（先编译 L1）")
        relations_md = await compiler.compile_l3(meta, self_ctx, related_ctxs=[])
        note_uri = f"kbasset://{prefix}/L3_relations.md"
        await minio.aupload_file(
            bucket_name=RENDER_BUCKET,
            object_name=f"{prefix}/L3_relations.md",
            data=relations_md.encode("utf-8"),
            content_type="text/markdown",
        )
        await _finish_compile_success(job.id, note_uri=note_uri, value_score=None)
        return {"job_id": job.id, "level": job.level, "status": "SUCCEEDED", "note_uri": note_uri}
    except IntegrityError:
        raise
    except Exception as exc:  # noqa: BLE001
        reason = f"{type(exc).__name__}: {exc}"
        logger.exception("编译失败: job=%s", job_id)
        status = await _finish_compile_failure(job_id, reason)
        return {"job_id": job_id, "status": status, "error": reason}


# ---------------------------------------------------------------- 对账


async def reconcile_stale_literature_compiles() -> int:
    """对账（cron）：孤儿 RUNNING / 滞留 PENDING 回收重排。"""
    now = utc_now()
    targets: list[int] = []
    async with pg_manager.get_async_session_context() as session:
        stale = (
            (
                await session.execute(
                    select(LiteratureCompileJob)
                    .where(
                        LiteratureCompileJob.status.in_(("RUNNING", "PENDING")),
                        LiteratureCompileJob.updated_at < now - timedelta(seconds=RUNNING_LEASE_SECONDS),
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        for job in stale:
            job.status = "PENDING"
            job.updated_at = now
            targets.append(job.id)
        await session.commit()
    for job_id in targets:
        try:
            queue = await get_arq_pool()
            await queue.enqueue_job(COMPILE_JOB_NAME, job_id, _job_id=_compile_job_id(job_id))
        except Exception:  # noqa: BLE001
            logger.exception("编译对账重排入队失败（下一轮重试）: job=%s", job_id)
    if targets:
        logger.info("编译对账：回收 %d 个滞留任务", len(targets))
    return len(targets)
