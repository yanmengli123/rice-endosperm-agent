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

from yuxi.literature.notes.prompts import parse_compile_json
from yuxi.literature.provenance import TRANSLATE_VERSION, StaleLiteratureInput, require_current_input
from yuxi.literature.reading.service import RENDER_BUCKET
from yuxi.literature.translate import prompts
from yuxi.literature.translate.latextap import reassemble
from yuxi.services.run_queue_service import get_arq_pool
from yuxi.storage.minio import get_minio_client
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    LiteraturePaper,
    LiteratureReaderRender,
    LiteratureTranslationJob,
    LiteratureVariant,
)
from yuxi.utils.datetime_utils import UTC, utc_now

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


async def enqueue_literature_translation(
    *, paper_id: int, variant_kind: str = "zh", force: bool = False, requested_by: str = ""
) -> dict:
    """排一个翻译任务，且不以 ``force`` 抢占仍持有有效租约的 worker。"""
    if variant_kind not in VALID_VARIANT_KINDS:
        raise ValueError(f"非法变体类型: {variant_kind}")
    async with pg_manager.get_async_session_context() as session:
        paper = (
            await session.execute(select(LiteraturePaper).where(LiteraturePaper.id == paper_id).with_for_update())
        ).scalar_one_or_none()
        if paper is None:
            raise ValueError(f"文献不存在: {paper_id}")
        job = (
            await session.execute(
                select(LiteratureTranslationJob).where(
                    LiteratureTranslationJob.tenant_id == paper.tenant_id,
                    LiteratureTranslationJob.paper_id == paper_id,
                    LiteratureTranslationJob.variant_kind == variant_kind,
                    LiteratureTranslationJob.source_revision_id == (paper.source_revision_id or ""),
                    LiteratureTranslationJob.reading_version == paper.reading_version,
                    LiteratureTranslationJob.generator_version == TRANSLATE_VERSION,
                )
            )
        ).scalar_one_or_none()
        if job is not None and not force:
            if job.status in ("SUCCEEDED", "RUNNING", "PENDING"):
                return {"job_id": job.id, "status": job.status, "reused": True}
        if job is not None and force and job.status in ("RUNNING", "PENDING"):
            lease_seconds = RUNNING_LEASE_SECONDS if job.status == "RUNNING" else PENDING_REQUEUE_SECONDS
            # TIMESTAMPTZ is aware in PostgreSQL; SQLite test round-trips the
            # same column as naive UTC.
            updated_at = job.updated_at
            if updated_at is not None and updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=UTC)
            lease_expired = bool(updated_at and updated_at < utc_now() - timedelta(seconds=lease_seconds))
            if not lease_expired:
                return {"job_id": job.id, "status": job.status, "reused": True}
        if job is None:
            job = LiteratureTranslationJob(
                tenant_id=paper.tenant_id,
                paper_id=paper_id,
                variant_kind=variant_kind,
                status="PENDING",
                source_revision_id=paper.source_revision_id or "",
                reading_version=paper.reading_version,
                generator_version=TRANSLATE_VERSION,
            )
            session.add(job)
        else:
            job.status = "PENDING"
            job.attempt = 0
            job.error = None
            job.variant_uri = None
        if requested_by:
            from yuxi.literature.llm_gateway import freeze_literature_policy

            policy = await freeze_literature_policy(
                session,
                tenant_id=int(paper.tenant_id),
                uid=requested_by,
                operation=f"translate_{variant_kind}",
                resource_ref=f"{paper.rid}:{variant_kind}",
            )
            job.requested_by = requested_by
            job.llm_run_id = policy.run_id
            job.model_spec = policy.model_spec
            job.credential_source = policy.credential_source
            job.credential_id = policy.credential_id
            job.provider_id = policy.provider_id
            job.policy_version = policy.policy_version
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

    from yuxi.literature.llm_gateway import call

    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": user}]
    response = await call(messages)
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


async def _summarize_document(paragraphs: list[str]) -> str:
    """Create the summary variant instead of silently treating it as a translation."""
    source = "\n\n".join(f"[P{i:03d}] {paragraph}" for i, paragraph in enumerate(paragraphs, 1))
    if len(source) > MAX_INPUT_CHARS_PER_CALL:
        raise ValueError(f"全文 {len(source)} 字符超过单次调用红线 {MAX_INPUT_CHARS_PER_CALL}")
    from yuxi.literature.llm_gateway import call

    response = await call(
        [
            {
                "role": "system",
                "content": "你是严谨的学术编辑。仅依据给定论文生成中文结构化摘要，不补造事实。",
            },
            {
                "role": "user",
                "content": (
                    "请用 Markdown 输出研究问题、方法、关键发现、局限和可复用价值；"
                    "关键结论必须附对应的 [Pxxx] 段落编号。\n\n" + source
                ),
            },
        ],
    )
    content = str(response.content or "").strip()
    if not content:
        raise ValueError("摘要模型返回空内容")
    return content + "\n"


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
            paper = await require_current_input(session, job)
            render = (
                await session.execute(
                    select(LiteratureReaderRender)
                    .where(
                        LiteratureReaderRender.revision_id == paper.source_revision_id,
                        LiteratureReaderRender.status == "SUCCEEDED",
                    )
                    .order_by(LiteratureReaderRender.updated_at.desc(), LiteratureReaderRender.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        if render is None:
            raise ValueError(f"解析版本尚无成功的阅读渲染: {paper.source_revision_id}")
        from yuxi.literature.review import document_object_candidates, para_text

        object_base = render.markdown_uri.removeprefix("kbasset://").removesuffix("render.md")
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
            para_text(p) for p in (doc.get("paragraphs") or []) if isinstance(p, dict) and para_text(p).strip()
        ]
        if not paragraphs:
            raise ValueError("document.json 无可用段落")

        alignment: list[dict[str, str]] = []

        async def _generate_variant() -> str:
            nonlocal alignment
            if job.variant_kind == "summary":
                return await _summarize_document(paragraphs)
            zh_paragraphs = await _translate_document(paragraphs)
            alignment = [
                {"para_id": f"P{i:03d}", "original": original, "translated": translated}
                for i, (original, translated) in enumerate(zip(paragraphs, zh_paragraphs, strict=True), 1)
            ]
            if job.variant_kind == "en_zh":
                return (
                    f"# {paper.title or ''}\n\n"
                    + "\n\n".join(f"{item['original']}\n\n> {item['translated']}" for item in alignment)
                    + "\n"
                )
            return f"# {paper.title or ''}\n\n" + "\n\n".join(zh_paragraphs) + "\n"

        if job.llm_run_id:
            from yuxi.literature.llm_gateway import governed_runtime, policy_for_job_attempt

            policy = await policy_for_job_attempt(job, operation=f"translate_{job.variant_kind}")
            async with governed_runtime(policy):
                variant_content = await _generate_variant()
        else:  # pre-0078 jobs and isolated unit fixtures
            variant_content = await _generate_variant()
        digest = hashlib.sha256(variant_content.encode("utf-8")).hexdigest()
        variant_base = f"{object_base}variants/r{job.reading_version}/{TRANSLATE_VERSION}/"
        uri = f"kbasset://{variant_base}{job.variant_kind}.md"
        await get_minio_client().aupload_file(
            bucket_name=RENDER_BUCKET,
            object_name=uri.removeprefix("kbasset://"),
            data=variant_content.encode("utf-8"),
            content_type="text/markdown",
        )
        if alignment:
            await get_minio_client().aupload_file(
                bucket_name=RENDER_BUCKET,
                object_name=f"{variant_base}{job.variant_kind}.json",
                data=json.dumps(
                    {"schema_version": 1, "paragraphs": alignment}, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8"),
                content_type="application/json",
            )
        async with pg_manager.get_async_session_context() as session:
            await require_current_input(session, job)
            variant = (
                await session.execute(
                    select(LiteratureVariant).where(
                        LiteratureVariant.tenant_id == paper.tenant_id,
                        LiteratureVariant.paper_id == paper.id,
                        LiteratureVariant.variant_kind == job.variant_kind,
                        LiteratureVariant.lang == "zh",
                        LiteratureVariant.parse_revision_id == paper.source_revision_id,
                        LiteratureVariant.reading_version == job.reading_version,
                        LiteratureVariant.generator_version == job.generator_version,
                    )
                )
            ).scalar_one_or_none()
            if variant is None:
                variant = LiteratureVariant(
                    tenant_id=paper.tenant_id,
                    paper_id=paper.id,
                    parse_revision_id=paper.source_revision_id,
                    reading_version=job.reading_version,
                    generator_version=job.generator_version,
                    variant_kind=job.variant_kind,
                    lang="zh",
                    uri=uri,
                    content_digest=digest,
                    model_id=job.model_spec,
                )
                session.add(variant)
            else:
                variant.uri = uri
                variant.content_digest = digest
                variant.model_id = job.model_spec
            await session.execute(
                update(LiteratureTranslationJob)
                .where(LiteratureTranslationJob.id == job.id)
                .values(status="SUCCEEDED", variant_uri=uri, error=None, updated_at=utc_now())
            )
            await session.commit()
        return {"job_id": job.id, "status": "SUCCEEDED", "variant_uri": uri}
    except StaleLiteratureInput:
        return {"job_id": job.id, "status": "STALE"}
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


async def reconcile_stale_literature_translations(ctx: dict | None = None) -> int:
    """对账（cron）：孤儿 RUNNING / 滞留 PENDING 回收重排。"""
    del ctx  # ARQ cron 注入的作业上下文（对账不使用）
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
