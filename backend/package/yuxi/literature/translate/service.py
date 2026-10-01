"""翻译服务（P3，ADR-0010 决议 5 / D2 决策）。

输入：阅读渲染的 document.json（段落 [Pxxx] + 文本）；公式经 latextap 打
[[MATHn]] 标签 → LLM 翻译（Yuxi 模型链）→ reassemble 逐字节回填公式 → zh.md
变体落 MinIO + ``literature_variants`` 行（lang='zh' 进身份）。

D2 落地：变体只是阅读派生物，**不触碰解析权威层**——翻译后英文
evidence_id/quote 定位必须逐条不变（test_translation_evidence_stability 钉死；
结构上由「翻译链路零 knowledge 域写入」保证）。
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update

from yuxi import config
from yuxi.literature.notes.prompts import parse_compile_json
from yuxi.literature.translate import prompts
from yuxi.literature.translate.latextap import reassemble
from yuxi.literature.reading.service import RENDER_BUCKET
from yuxi.services.run_queue_service import get_arq_pool
from yuxi.storage.minio import get_minio_client
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    LiteraturePaper,
    LiteratureReaderRender,
    LiteratureTranslationJob,
    LiteratureVariant,
)
from yuxi.utils.datetime_utils import utc_now

logger = logging.getLogger(__name__)

TRANSLATE_JOB_NAME = "process_literature_translation"
MAX_TRANSLATE_ATTEMPTS = 3
RUNNING_LEASE_SECONDS = 60 * 30
PENDING_REQUEUE_SECONDS = 60 * 10
#: 单次调用输入字符红线（上游 llm_service.MAX_INPUT_CHARS_PER_CALL 口径）
MAX_INPUT_CHARS_PER_CALL = 400_000
VALID_VARIANT_KINDS = ("zh", "en_zh", "summary")


def _job_id_key(job_id: int) -> str:
    return f"lit-translate:{job_id}"


# ---------------------------------------------------------------- 任务入列


async def enqueue_literature_translation(*, paper_id: int, variant_kind: str = "zh", force: bool = False) -> dict:
    if variant_kind not in VALID_VARIANT_KINDS:
        raise ValueError(f"非法变体类型: {variant_kind}")
    async with pg_manager.get_async_session_context() as session:
        paper = (
            await session.execute(select(LiteraturePaper).where(LiteraturePaper.id == paper_id))
        ).scalar_one_or_none()
        if paper is None:
            raise ValueError(f"文献不存在: {paper_id}")
        job = (
            await session.execute(
                select(LiteratureTranslationJob).where(
                    LiteratureTranslationJob.tenant_id == paper.tenant_id,
                    LiteratureTranslationJob.paper_id == paper_id,
                    LiteratureTranslationJob.variant_kind == variant_kind,
                )
            )
        ).scalar_one_or_none()
        if job is not None and not force:
            if job.status in ("SUCCEEDED", "RUNNING", "PENDING"):
                return {"job_id": job.id, "status": job.status, "reused": True}
        if job is None:
            job = LiteratureTranslationJob(
                tenant_id=paper.tenant_id, paper_id=paper_id, variant_kind=variant_kind, status="PENDING"
            )
            session.add(job)
        else:
            job.status = "PENDING"
            job.error = None
        await session.flush()
        job_id = job.id
        await session.commit()
    try:
        queue = await get_arq_pool()
        await queue.enqueue_job(TRANSLATE_JOB_NAME, job_id, _job_id=_job_id_key(job_id))
        deferred = False
    except Exception:  # noqa: BLE001
        deferred = True
        logger.exception("翻译任务已持久化，但即时入队失败，将由对账任务补偿: job=%s", job_id)
    return {"job_id": job_id, "status": "PENDING", "reused": False, "enqueue_deferred": deferred}


# ---------------------------------------------------------------- 翻译执行


async def _translate_document(paragraphs: list[str]) -> list[str]:
    """整篇一次调用（上游默认路径，≤400k 字符红线）；返回逐段中文译文。"""
    prefix, math_list = prompts.build_shared_prefix_with_math(paragraphs)
    if len(prefix) > MAX_INPUT_CHARS_PER_CALL:
        raise ValueError(f"全文前缀 {len(prefix)} 字符超过单次调用红线 {MAX_INPUT_CHARS_PER_CALL}")
    para_ids = [f"P{i:03d}" for i in range(1, len(paragraphs) + 1)]
    prompt = prompts.with_task(prefix, prompts.translate_task(para_ids))
    system, user = prompts.split_task(prompt)

    from yuxi.models.chat import select_model

    model = select_model(model_spec=config.default_model)
    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": user}]
    response = await model.call(messages, stream=False)
    data = parse_compile_json(str(response.content or ""))
    translations = data.get("translations") or []
    by_id = {str(t.get("para_id") or ""): str(t.get("zh") or "") for t in translations if isinstance(t, dict)}
    out: list[str] = []
    for i, _para in enumerate(paragraphs):
        pid = para_ids[i]
        labeled_zh = by_id.get(pid, "")
        if not labeled_zh:
            raise ValueError(f"译文缺少段落 {pid}（整篇契约：必须逐段返回）")
        out.append(reassemble(labeled_zh, math_list))
    return out


async def process_literature_translation(ctx: dict[str, Any], job_id: int) -> dict[str, Any] | None:
    """ARQ 任务：翻译一篇文献（zh 变体）。"""

    async def _claim() -> LiteratureTranslationJob | None:
        async with pg_manager.get_async_session_context() as session:
            job = (
                await session.execute(
                    select(LiteratureTranslationJob).where(LiteratureTranslationJob.id == job_id).with_for_update()
                )
            ).scalar_one_or_none()
            if job is None or job.status != "PENDING":
                return None
            job.status = "RUNNING"
            job.updated_at = utc_now()
            await session.commit()
            return job

    job = await _claim()
    if job is None:
        return None
    try:
        async with pg_manager.get_async_session_context() as session:
            paper = (
                await session.execute(select(LiteraturePaper).where(LiteraturePaper.id == job.paper_id))
            ).scalar_one()
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
        object_base = render.markdown_uri.removeprefix("kbasset://").removesuffix("render.md")
        document_json = await get_minio_client().adownload_file(RENDER_BUCKET, object_base + "document.json")
        doc = json.loads(document_json.decode("utf-8"))
        paragraphs = [
            str(p.get("text") or "")
            for p in (doc.get("paragraphs") or [])
            if isinstance(p, dict) and (p.get("text") or "").strip()
        ]
        if not paragraphs:
            raise ValueError("document.json 无可用段落")

        zh_paragraphs = await _translate_document(paragraphs)
        zh_md = f"# {paper.title or ''}\n\n" + "\n\n".join(zh_paragraphs) + "\n"
        digest = hashlib.sha256(zh_md.encode("utf-8")).hexdigest()
        uri = f"kbasset://{object_base}variants/zh.md"
        await get_minio_client().aupload_file(
            bucket_name=RENDER_BUCKET,
            object_name=uri.removeprefix("kbasset://"),
            data=zh_md.encode("utf-8"),
            content_type="text/markdown",
        )
        async with pg_manager.get_async_session_context() as session:
            await session.merge(
                LiteratureVariant(
                    tenant_id=paper.tenant_id,
                    paper_id=paper.id,
                    parse_revision_id=paper.source_revision_id,
                    variant_kind="zh",
                    lang="zh",
                    uri=uri,
                    content_digest=digest,
                    model_id=config.default_model,
                )
            )
            await session.execute(
                update(LiteratureTranslationJob)
                .where(LiteratureTranslationJob.id == job.id)
                .values(status="SUCCEEDED", variant_uri=uri, error=None, updated_at=utc_now())
            )
            await session.commit()
        return {"job_id": job.id, "status": "SUCCEEDED", "variant_uri": uri}
    except Exception as exc:  # noqa: BLE001
        reason = f"{type(exc).__name__}: {exc}"
        logger.exception("翻译失败: job=%s", job_id)
        async with pg_manager.get_async_session_context() as session:
            row = (
                await session.execute(
                    select(LiteratureTranslationJob).where(LiteratureTranslationJob.id == job_id).with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                return {"job_id": job_id, "status": "MISSING"}
            row.attempt = int(row.attempt or 0) + 1
            row.error = reason[:2000]
            if row.attempt < MAX_TRANSLATE_ATTEMPTS:
                row.status = "PENDING"
                final = "PENDING"
            else:
                row.status = "DEAD"
                final = "DEAD"
            await session.commit()
        if final == "PENDING":
            try:
                queue = await get_arq_pool()
                await queue.enqueue_job(TRANSLATE_JOB_NAME, job_id, _job_id=_job_id_key(job_id))
            except Exception:  # noqa: BLE001
                logger.exception("翻译失败重排入队失败: job=%s", job_id)
        return {"job_id": job_id, "status": final, "error": reason}


async def reconcile_stale_literature_translations() -> int:
    """对账（cron）：孤儿 RUNNING / 滞留 PENDING 回收重排。"""
    now = utc_now()
    targets: list[int] = []
    async with pg_manager.get_async_session_context() as session:
        stale = (
            (
                await session.execute(
                    select(LiteratureTranslationJob)
                    .where(
                        LiteratureTranslationJob.status.in_(("RUNNING", "PENDING")),
                        LiteratureTranslationJob.updated_at < now - timedelta(seconds=RUNNING_LEASE_SECONDS),
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
            await queue.enqueue_job(TRANSLATE_JOB_NAME, job_id, _job_id=_job_id_key(job_id))
        except Exception:  # noqa: BLE001
            logger.exception("翻译对账重排入队失败: job=%s", job_id)
    return len(targets)
