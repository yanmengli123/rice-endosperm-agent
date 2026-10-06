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
import asyncio
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, or_, select, update

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
    LiteratureTranslationTask,
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
#: 分批因子（移植 PaperAgent paperkb/translate/pipeline.py 非紧凑路径）：正文先按
#: 冻结预算组批，短文可整篇入一批；缺段/畸形 JSON 有界补跑。单批输出 token
#: ≈ 上限字符 ×0.5，须留足余量避开供应商输出硬顶（上游实测 glm ~11K token 顶，
#: 12000 字符/批 → ~6K token 稳落顶内）；批内应译未译另有单段补跑兜底。
MAX_BATCH_CHARS = 12_000
MAX_BATCH_PARAS = 24
MAX_BATCH_CALLS = 64
# complete_json 每次最多额外修复一次：本预算对应最多 128 次真实模型请求。
VALID_VARIANT_KINDS = ("zh", "en_zh", "summary")

#: 「拒绝/占位」式译文判据（上游批4 防御移植）：这类文本不是译文，命中即视为
#: 缺段走补跑，绝不落进 zh/en_zh 变体污染阅读。
_REFUSAL_RE = re.compile(
    r"(原文未提供|未提供该段|无法翻译|无法完成翻译|无法进行翻译|不能翻译|缺少原文|原文缺失|"
    r"未给出原文|not provided in the (original|source)|cannot translate|unable to translate|no source text)",
    re.IGNORECASE,
)


def _batch_chars_limit() -> int:
    """单批正文字符上限（env ``YUXI_TRANSLATE_BATCH_CHARS`` 可调，下限 1000）。"""
    raw = (os.environ.get("YUXI_TRANSLATE_BATCH_CHARS") or "").strip()
    try:
        return max(1000, int(raw)) if raw else MAX_BATCH_CHARS
    except ValueError:
        return MAX_BATCH_CHARS


def _is_refusal(zh: str) -> bool:
    return bool(_REFUSAL_RE.search(zh or ""))


def _split_sentences(text: str, max_chars: int) -> list[str]:
    """超长段落按句子边界切成 ≤max_chars 的块（上游口径）；单句超限硬切兜底。"""
    parts = re.split(r"(?<=[。！？；.!?;])\s+|(?<=\n)", text)
    chunks: list[str] = []
    cur = ""
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if len(cur) + len(part) + 1 <= max_chars:
            cur = (cur + " " + part).strip()
        else:
            if cur:
                chunks.append(cur)
            while len(part) > max_chars:
                boundary = max_chars
                for tag in re.finditer(r"\[\[MATH\d+\]\]", part):
                    if tag.start() < boundary < tag.end():
                        boundary = tag.start()
                        break
                if not boundary:
                    raise ValueError("批大小不足以容纳公式标签")
                chunks.append(part[:boundary])
                part = part[boundary:]
            cur = part
    if cur:
        chunks.append(cur)
    return chunks or [text[:max_chars]]


def _make_batches(paragraphs: list[str], indexes: list[int], *, max_chars: int, max_paras: int) -> list[list[int]]:
    """段落边界组批（上游语义硬约束）：只在段边界切批，永远整段入批；
    单段自身超过 ``max_chars`` 时独占一批（不与前后段合并）。"""
    batches: list[list[int]] = []
    cur: list[int] = []
    size = 0
    for idx in indexes:
        n = len(paragraphs[idx])
        if cur and (len(cur) >= max_paras or size + n > max_chars):
            batches.append(cur)
            cur = [idx]
            size = n
        else:
            cur.append(idx)
            size += n
    if cur:
        batches.append(cur)
    return batches


def _job_id_key(job_id: int, attempt: int = 0, run_id: str | None = None) -> str:
    base = f"lit-translate:{job_id}" + (f":run:{run_id}" if run_id else "")
    return base if attempt == 0 else f"{base}:attempt:{attempt}"


# ---------------------------------------------------------------- 任务入列


async def _precheck_translation_model(paper_id: int, requested_by: str, *, options: dict) -> None:
    """入队前供应商可达性预检（事务外执行，不持有任何行锁）。

    使用此次提交冻结的配置解析 spec；网络可达不代表凭据/模型已验收。
    不可达抛 ModelPrecheckError（ValueError 子类 → HTTP 422），
    任务不创建、重试预算与配额零消耗。
    """
    from yuxi.literature.llm_gateway import resolve_effective_model_spec
    from yuxi.literature.model_precheck import precheck_model_available

    async with pg_manager.get_async_session_context() as session:
        paper = (
            await session.execute(select(LiteraturePaper.tenant_id).where(LiteraturePaper.id == paper_id))
        ).scalar_one_or_none()
        if paper is None:
            raise ValueError(f"文献不存在: {paper_id}")
        spec = await resolve_effective_model_spec(session, uid=requested_by, explicit_spec=options.get("model_spec"))
        from yuxi.models.providers.cache import model_cache
        from yuxi.services.user_credential_service import get_active_user_credential
        from yuxi.storage.postgres.models_business import TenantUserEntitlement

        entitlement = (
            await session.execute(
                select(TenantUserEntitlement).where(
                    TenantUserEntitlement.tenant_id == int(paper),
                    TenantUserEntitlement.uid == requested_by,
                )
            )
        ).scalar_one_or_none()
        info = model_cache.get_model_info(spec)
        endpoint = info.base_url if info else None
        if entitlement is None or entitlement.credential_policy != "platform_only":
            credential = await get_active_user_credential(session, requested_by, spec.split(":", 1)[0])
            if credential is not None and credential.base_url:
                endpoint = credential.base_url
    await precheck_model_available(spec, base_url=endpoint)


async def _find_translation_submission(session, paper, variant_kind, config_digest):
    return (
        await session.execute(
            select(LiteratureTranslationJob)
            .where(
                LiteratureTranslationJob.tenant_id == paper.tenant_id,
                LiteratureTranslationJob.paper_id == paper.id,
                LiteratureTranslationJob.variant_kind == variant_kind,
                LiteratureTranslationJob.source_revision_id == (paper.source_revision_id or ""),
                LiteratureTranslationJob.reading_version == paper.reading_version,
                LiteratureTranslationJob.generator_version == TRANSLATE_VERSION,
                LiteratureTranslationJob.config_digest == config_digest,
            )
            .order_by(LiteratureTranslationJob.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _can_reuse_translation(session, job, *, force: bool) -> bool:
    if job is None or (await session.execute(_newer_translation_submission(job))).scalar_one_or_none() is not None:
        return False
    if not force:
        return job.status in {"PENDING", "RUNNING", "SUCCEEDED"}
    if job.status not in {"PENDING", "RUNNING"}:
        return False
    lease_seconds = RUNNING_LEASE_SECONDS if job.status == "RUNNING" else PENDING_REQUEUE_SECONDS
    updated = job.updated_at
    if updated is not None and updated.tzinfo is None:  # SQLite round-trip only.
        updated = updated.replace(tzinfo=UTC)
    return not (updated is not None and updated < utc_now() - timedelta(seconds=lease_seconds))


async def enqueue_literature_translation(
    *, paper_id: int, variant_kind: str = "zh", force: bool = False, requested_by: str = ""
) -> dict:
    """排一个翻译任务，且不以 ``force`` 抢占仍持有有效租约的 worker。"""
    if variant_kind not in VALID_VARIANT_KINDS:
        raise ValueError(f"非法变体类型: {variant_kind}")
    from yuxi.literature.translate.settings import translation_options, options_digest

    # Existing products/in-flight work stay usable during a provider outage.
    # Snapshot options once, probe outside locks, then recheck reuse under the paper lock.
    async with pg_manager.get_async_session_context() as session:
        paper = (
            await session.execute(select(LiteraturePaper).where(LiteraturePaper.id == paper_id))
        ).scalar_one_or_none()
        if paper is None:
            raise ValueError(f"文献不存在: {paper_id}")
        options = await translation_options(int(paper.tenant_id), requested_by) if requested_by else {}
        config_digest = options_digest(options) if options else ""
        job = await _find_translation_submission(session, paper, variant_kind, config_digest)
        if await _can_reuse_translation(session, job, force=force):
            return {"job_id": job.id, "status": job.status, "reused": True}
    if requested_by:
        await _precheck_translation_model(paper_id, requested_by, options=options)
    superseded_run_id = None
    async with pg_manager.get_async_session_context() as session:
        paper = (
            await session.execute(select(LiteraturePaper).where(LiteraturePaper.id == paper_id).with_for_update())
        ).scalar_one_or_none()
        if paper is None:
            raise ValueError(f"文献不存在: {paper_id}")
        job = await _find_translation_submission(session, paper, variant_kind, config_digest)
        if await _can_reuse_translation(session, job, force=force):
            return {"job_id": job.id, "status": job.status, "reused": True}
        generation = int(job.generation or 0) + 1 if job is not None else 0
        if job is not None and job.status in ("RUNNING", "PENDING"):
            superseded_run_id = job.llm_run_id
            job.status, job.error = "STALE", "新提交或过期租约由新代次接管；旧任务不可发布"
        job = LiteratureTranslationJob(
            tenant_id=paper.tenant_id,
            paper_id=paper_id,
            variant_kind=variant_kind,
            status="PENDING",
            source_revision_id=paper.source_revision_id or "",
            reading_version=paper.reading_version,
            generator_version=TRANSLATE_VERSION,
            config_digest=config_digest,
            options_json=options,
            generation=generation,
        )
        session.add(job)
        if requested_by:
            from yuxi.literature.llm_gateway import freeze_literature_policy

            policy = await freeze_literature_policy(
                session,
                tenant_id=int(paper.tenant_id),
                uid=requested_by,
                operation=f"translate_{variant_kind}",
                resource_ref=f"{paper.rid}:{variant_kind}:{config_digest}:g{generation}",
                model_spec=options.get("model_spec"),
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
        attempt = int(job.attempt or 0)
        run_id = job.llm_run_id
        await session.commit()
    if superseded_run_id:
        from yuxi.literature.llm_gateway import fail_unstarted_run

        try:
            await fail_unstarted_run(superseded_run_id, error_type="TranslationSuperseded")
        except Exception as exc:  # noqa: BLE001 - durable submission survives cleanup failure
            logger.warning("旧翻译原生预约释放失败: error=%s", type(exc).__name__)
    try:
        queue = await get_arq_pool()
        queued = await queue.enqueue_job(TRANSLATE_JOB_NAME, job_id, _job_id=_job_id_key(job_id, attempt, run_id))
        deferred = queued is None
    except Exception:  # noqa: BLE001
        deferred = True
        logger.exception("翻译任务已持久化，但即时入队失败，将由对账任务补偿: job=%s", job_id)
    return {"job_id": job_id, "status": "PENDING", "reused": False, "enqueue_deferred": deferred}


# ---------------------------------------------------------------- 翻译执行


async def _translate_call(complete_json, prefix: str, para_ids: list[str]) -> dict[str, str]:
    """一次标注调用（整篇或一批）：返回 {para_id: zh}；拒绝式译文剔除（视为缺段走补跑）。"""
    prompt = prompts.with_task(prefix, prompts.translate_task(para_ids))
    system, user = prompts.split_task(prompt)
    data, _raw = await complete_json(system, user)
    out: dict[str, str] = {}
    allowed = set(para_ids)
    for item in data.get("translations") or []:
        if not isinstance(item, dict):
            continue
        pid = str(item.get("para_id") or "")
        zh = str(item.get("zh") or "")
        if pid not in allowed:
            continue
        if pid in out:
            raise ValueError(f"译文包含重复段落 {pid}")
        if zh and not _is_refusal(zh):
            out[pid] = zh
    return out


def _newer_translation_submission(job):
    """Any later submission supersedes a reading result, including a failed one.

    A failed latest configuration does not authorize an older worker to spend
    or publish again. Probes are independent explicit-cost experiments.
    """
    model = type(job)
    conditions = [model.tenant_id == job.tenant_id, model.id > job.id]
    if isinstance(job, LiteratureTranslationJob):
        conditions.extend(
            (
                model.paper_id == job.paper_id,
                model.variant_kind == job.variant_kind,
                model.source_revision_id == job.source_revision_id,
                model.reading_version == job.reading_version,
            )
        )
    elif isinstance(job, LiteratureTranslationTask) and job.task_kind == "draft":
        conditions.extend(
            (
                model.requested_by == job.requested_by,
                model.task_kind == "draft",
                model.source_kind == job.source_kind,
                model.source_ref == job.source_ref,
            )
        )
    else:
        return None
    return select(model.id).where(*conditions).limit(1)


async def renew_translation_lease(job, attempt: int) -> None:
    """Renew between bounded model calls; a lost claim must never spend again."""
    model = type(job)
    conditions = [model.id == job.id, model.status == "RUNNING", model.attempt == attempt]
    newer = _newer_translation_submission(job)
    if newer is not None:
        # Keep EXISTS independent of the UPDATE target's implicit correlation.
        conditions.append(~newer.correlate(None).exists())
    async with pg_manager.get_async_session_context() as db:
        result = await db.execute(update(model).where(*conditions).values(updated_at=utc_now()))
        renewed = result.rowcount == 1
        await db.commit()
    if not renewed:
        raise StaleLiteratureInput("翻译执行租约已失效；停止后续模型调用")


async def _translate_document(
    paragraphs: list[str],
    *,
    max_chars: int | None = None,
    checkpoint: Callable[[], Awaitable[None]] | None = None,
) -> list[str]:
    """按冻结的正文预算组批，短文整篇入一批；每次只发送本批原文。

    分批口径移植上游 PaperAgent（translate/pipeline.py）：
    - 只在段边界切批，单段超上限独占一批；超长单段按句切块（块标记
      ``{pid}__cN``）译后按序拼接，切块基于**标签化文本**保 [[MATHn]] 纪律；
    - 批内应译未译段落单段补跑（模型输出触顶截断的兜底，单段输出小必不截断）；
    - 任一段落兜底后仍缺失才向上抛（进任务级 attempt 重试）。
    """
    if sum(map(len, paragraphs)) > MAX_INPUT_CHARS_PER_CALL:
        raise ValueError(f"全文字符超过单任务红线 {MAX_INPUT_CHARS_PER_CALL}")
    max_chars = max_chars or _batch_chars_limit()
    labeled, math_list = prompts.label_paragraphs(paragraphs)
    para_ids = [f"P{i:03d}" for i in range(1, len(paragraphs) + 1)]

    from yuxi.literature.notes.compiler import complete_json

    async def complete_batch(system, user):
        if checkpoint is not None:
            await checkpoint()
        result = await complete_json(system, user)
        if checkpoint is not None:
            await checkpoint()
        return result

    # Expand long paragraphs to actual labelled sentence chunks. Each call's
    # source contains only its batch, not the original full-paper prefix.
    units, unit_ids, groups = [], [], []
    for pid, text in zip(para_ids, labeled, strict=True):
        chunks = _split_sentences(text, max_chars) if len(text) > max_chars else [text]
        ids = [f"{pid}__c{i}" for i in range(len(chunks))] if len(chunks) > 1 else [pid]
        units.extend(chunks)
        unit_ids.extend(ids)
        groups.append(ids)
    batches = _make_batches(units, list(range(len(units))), max_chars=max_chars, max_paras=MAX_BATCH_PARAS)
    if len(batches) > MAX_BATCH_CALLS:
        raise ValueError(f"分批数 {len(batches)} 超过单任务调用上限 {MAX_BATCH_CALLS}")
    by_id: dict[str, str] = {}
    calls = 0
    for batch in batches:
        if calls >= MAX_BATCH_CALLS:
            raise ValueError("翻译调用预算已用尽")
        ids = [unit_ids[i] for i in batch]
        prefix = prompts.CTX_HEADER + "\n".join(f"[{unit_ids[i]}] {units[i]}" for i in batch)
        try:
            calls += 1
            by_id.update(await _translate_call(complete_batch, prefix, ids))
        except StaleLiteratureInput:
            raise
        except Exception as exc:  # noqa: BLE001 - deterministic missing-unit repair, never raw provider output
            logger.warning("翻译批次契约失败: %s", type(exc).__name__)
        missing = [i for i in batch if unit_ids[i] not in by_id]
        if missing and len(batch) > 1:
            if calls >= MAX_BATCH_CALLS:
                raise ValueError("翻译调用预算已用尽")
            calls += 1
            repaired = await _translate_call(
                complete_batch,
                prompts.CTX_HEADER + "\n".join(f"[{unit_ids[i]}] {units[i]}" for i in missing),
                [unit_ids[i] for i in missing],
            )
            by_id.update(repaired)
        for i in missing:
            pid = unit_ids[i]
            if pid not in by_id:
                if calls >= MAX_BATCH_CALLS:
                    raise ValueError("翻译调用预算已用尽")
                calls += 1
                by_id.update(await _translate_call(complete_batch, prompts.CTX_HEADER + f"[{pid}] {units[i]}", [pid]))
            if pid not in by_id:
                raise ValueError(f"译文缺少段落 {pid}（单段补跑仍未返回）")
        if calls > MAX_BATCH_CALLS:
            raise ValueError("翻译调用预算已用尽")
    out: list[str] = []
    for i, pid in enumerate(para_ids):
        labeled_zh = "".join(by_id.get(unit_id, "") for unit_id in groups[i])
        if not labeled_zh:
            raise ValueError(f"译文缺少段落 {pid}（整篇契约：必须逐段返回）")
        tags = r"\[\[MATH\d+\]\]"
        if re.findall(tags, labeled_zh) != re.findall(tags, labeled[i]):
            raise ValueError(f"译文公式标签不完整或顺序变更: {pid}")
        out.append(reassemble(labeled_zh, math_list))
    return out


async def _summarize_document(paragraphs: list[str], para_ids: list[str] | None = None) -> str:
    """Create the summary variant instead of silently treating it as a translation."""
    para_ids = para_ids or [f"P{i:03d}" for i in range(1, len(paragraphs) + 1)]
    source = "\n\n".join(f"[{pid}] {paragraph}" for pid, paragraph in zip(para_ids, paragraphs, strict=True))
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


async def _finish_translation_failure(job_id: int, claim_attempt: int, reason: str) -> str:
    async with pg_manager.get_async_session_context() as session:
        row = (
            await session.execute(
                select(LiteratureTranslationJob).where(LiteratureTranslationJob.id == job_id).with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            return "MISSING"
        if row.status != "RUNNING" or row.attempt != claim_attempt:
            return "STALE"
        row.attempt = int(row.attempt or 0) + 1
        attempt = row.attempt
        run_id = row.llm_run_id
        row.error = reason[:2000]
        final = "PENDING" if row.attempt < MAX_TRANSLATE_ATTEMPTS else "DEAD"
        row.status = final
        await session.commit()
    if final == "PENDING":
        try:
            queue = await get_arq_pool()
            await queue.enqueue_job(TRANSLATE_JOB_NAME, job_id, _job_id=_job_id_key(job_id, attempt, run_id))
        except Exception:  # noqa: BLE001
            logger.exception("翻译失败重排入队失败: job=%s", job_id)
    return final


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
    claim_attempt = job.attempt
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
        if render is None and not (paper.artifact_pointers or {}).get("document"):
            raise ValueError(f"解析版本尚无成功的阅读渲染: {paper.source_revision_id}")
        from yuxi.literature.artifacts import resolve_effective_document
        from yuxi.literature.review import para_text

        # 统一有效版本入口（P11：人工冻结→裁决→渲染→导入）
        effective = await resolve_effective_document(
            int(paper.tenant_id), paper.rid, paper=paper, storage=get_minio_client()
        )
        if effective is None:
            raise ValueError(f"文献无任何正文产物: {paper.rid}")
        document_json = effective["content"]
        if not document_json:
            raise ValueError(f"有效正文内容为空（source={effective['source']}）: {paper.rid}")
        object_base = (
            render.markdown_uri.removeprefix("kbasset://").removesuffix("render.md")
            if render
            else f"tenants/{paper.tenant_id}/literature/variants/{paper.rid}/"
        )
        doc = json.loads(document_json.decode("utf-8"))
        paragraph_rows = [p for p in (doc.get("paragraphs") or []) if isinstance(p, dict) and para_text(p).strip()]
        paragraphs = [para_text(p) for p in paragraph_rows]
        if not paragraphs:
            raise ValueError("document.json 无可用段落")

        alignment: list[dict[str, str]] = []

        async def _generate_variant() -> str:
            nonlocal alignment
            if job.variant_kind == "summary":
                return await _summarize_document(
                    paragraphs,
                    [str(row.get("para_id") or f"legacy-{i:03d}") for i, row in enumerate(paragraph_rows, 1)],
                )
            zh_paragraphs = await _translate_document(
                paragraphs,
                max_chars=(job.options_json or {}).get("batch_chars"),
                checkpoint=lambda: renew_translation_lease(job, claim_attempt),
            )
            alignment = [
                {
                    "para_id": str(row.get("para_id") or f"legacy-{i:03d}"),
                    "original": original,
                    "translated": translated,
                }
                for i, (row, original, translated) in enumerate(
                    zip(paragraph_rows, paragraphs, zh_paragraphs, strict=True), 1
                )
            ]
            if job.variant_kind == "en_zh":
                # 双语排版交给模板注册表（纯函数、零 LLM）：切换模板只需重新排版，
                # 不必重跑翻译链。alignment 已按 para_id 对齐，可反复重渲染。
                from yuxi.literature.templates import render_markdown

                return render_markdown(
                    [
                        {"type": "paragraph", "text_en": item["original"], "text_zh": item["translated"]}
                        for item in alignment
                    ],
                    references=None,
                    template=job.template or None,
                    title=paper.title or "",
                )
            return f"# {paper.title or ''}\n\n" + "\n\n".join(zh_paragraphs) + "\n"

        if job.llm_run_id:
            from yuxi.literature.llm_gateway import governed_runtime, policy_for_job_attempt

            policy = await policy_for_job_attempt(job, operation=f"translate_{job.variant_kind}")
            from yuxi.literature.translate.settings import model_options

            async with governed_runtime(policy, model_options=model_options(job.options_json or {})):
                variant_content = await _generate_variant()
        else:  # pre-0078 jobs and isolated unit fixtures
            variant_content = await _generate_variant()
        digest = hashlib.sha256(variant_content.encode("utf-8")).hexdigest()
        variant_base = f"{object_base}variants/r{job.reading_version}/{TRANSLATE_VERSION}/j{job.id}/{digest}/"
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
            paper = await require_current_input(session, job)
            fresh_job = await session.get(LiteratureTranslationJob, job.id, with_for_update=True)
            newer = (await session.execute(_newer_translation_submission(job))).scalar_one_or_none()
            if fresh_job.status != "RUNNING" or fresh_job.attempt != claim_attempt or newer is not None:
                if fresh_job.status == "RUNNING" and fresh_job.attempt == claim_attempt:
                    fresh_job.status, fresh_job.error = "STALE", "新翻译配置任务已接管；旧译文未发布"
                    await session.commit()
                return {"job_id": job.id, "status": "STALE"}
            from yuxi.literature.products import frozen_uri

            preserved_uri = await frozen_uri(session, paper, f"variant_{job.variant_kind}", fallback=uri)
            if preserved_uri != uri:
                from yuxi.storage.postgres.models_knowledge import LiteratureArtifactRevision

                digest = (
                    await session.execute(
                        select(LiteratureArtifactRevision.content_digest).where(
                            LiteratureArtifactRevision.revision_id
                            == paper.artifact_pointers[f"variant_{job.variant_kind}"]
                        )
                    )
                ).scalar_one()
                uri = preserved_uri
            variant = (
                await session.execute(
                    select(LiteratureVariant).where(
                        LiteratureVariant.tenant_id == paper.tenant_id,
                        LiteratureVariant.paper_id == paper.id,
                        LiteratureVariant.variant_kind == job.variant_kind,
                        LiteratureVariant.lang == "zh",
                        LiteratureVariant.parse_revision_id == (paper.source_revision_id or ""),
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
        from yuxi.literature.llm_gateway import fail_unstarted_run

        await fail_unstarted_run(job.llm_run_id, error_type="StaleLiteratureInput")
        return {"job_id": job.id, "status": "STALE"}
    except asyncio.CancelledError:
        await asyncio.shield(
            _finish_translation_failure(job_id, claim_attempt, "WORKER_INTERRUPTED: 执行进程中断，等待重试")
        )
        raise
    except Exception as exc:  # noqa: BLE001
        from yuxi.literature.llm_gateway import fail_unstarted_run

        await fail_unstarted_run(job.llm_run_id, error_type=type(exc).__name__)
        reason = f"{type(exc).__name__}: {exc}"
        logger.exception("翻译失败: job=%s", job_id)
        final = await _finish_translation_failure(job_id, claim_attempt, reason)
        return {"job_id": job_id, "status": final, "error": reason}


async def reconcile_stale_literature_translations(ctx: dict | None = None) -> int:
    """对账（cron）：孤儿 RUNNING / 滞留 PENDING 回收重排。"""
    del ctx  # ARQ cron 注入的作业上下文（对账不使用）
    now = utc_now()
    targets: list[tuple[int, int, str | None]] = []
    async with pg_manager.get_async_session_context() as session:
        stale = (
            (
                await session.execute(
                    select(LiteratureTranslationJob)
                    .where(
                        or_(
                            and_(
                                LiteratureTranslationJob.status == "RUNNING",
                                LiteratureTranslationJob.updated_at < now - timedelta(seconds=RUNNING_LEASE_SECONDS),
                            ),
                            and_(
                                LiteratureTranslationJob.status == "PENDING",
                                LiteratureTranslationJob.updated_at < now - timedelta(seconds=PENDING_REQUEUE_SECONDS),
                            ),
                        ),
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        for job in stale:
            if job.status == "RUNNING":
                job.attempt += 1
            job.status = "DEAD" if job.attempt >= MAX_TRANSLATE_ATTEMPTS else "PENDING"
            job.updated_at = now
            if job.status == "PENDING":
                targets.append((job.id, job.attempt, job.llm_run_id))
        await session.commit()
    for job_id, attempt, run_id in targets:
        try:
            queue = await get_arq_pool()
            await queue.enqueue_job(TRANSLATE_JOB_NAME, job_id, _job_id=_job_id_key(job_id, attempt, run_id))
        except Exception:  # noqa: BLE001
            logger.exception("翻译对账重排入队失败: job=%s", job_id)
    return len(targets)
