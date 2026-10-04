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
import hashlib
import logging
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from yuxi.literature.boundaries import assert_not_evidence_capable
from yuxi.literature.contracts import ResourceKind
from yuxi.literature.identity.rid import make_rid
from yuxi.literature.notes import compiler, value_score
from yuxi.literature.provenance import COMPILE_VERSION, StaleLiteratureInput, require_current_input
from yuxi.literature.reading.service import RENDER_BUCKET
from yuxi.services.run_queue_service import get_arq_pool
from yuxi.storage.minio import get_minio_client
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    LiteratureCompileJob,
    LiteraturePaper,
    KnowledgeParseRevision,
)
from yuxi.utils.datetime_utils import UTC, utc_now

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
        row = None
        revision = None
        if source_revision_id:
            revision = (
                await session.execute(
                    select(KnowledgeParseRevision).where(
                        KnowledgeParseRevision.revision_id == source_revision_id,
                        KnowledgeParseRevision.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if revision is None:
                raise ValueError("解析来源不存在或跨租户")
            row = (
                await session.execute(
                    select(LiteraturePaper)
                    .join(
                        KnowledgeParseRevision, KnowledgeParseRevision.revision_id == LiteraturePaper.source_revision_id
                    )
                    .where(
                        LiteraturePaper.tenant_id == tenant_id,
                        KnowledgeParseRevision.file_id == revision.file_id,
                        KnowledgeParseRevision.kb_id == revision.kb_id,
                    )
                )
            ).scalar_one_or_none()
        if row is None:
            row = (
                await session.execute(
                    select(LiteraturePaper).where(LiteraturePaper.tenant_id == tenant_id, LiteraturePaper.rid == rid)
                )
            ).scalar_one_or_none()
        if row is not None and revision is not None and row.source_revision_id:
            prior = (
                await session.execute(
                    select(KnowledgeParseRevision).where(KnowledgeParseRevision.revision_id == row.source_revision_id)
                )
            ).scalar_one_or_none()
            if prior is None or prior.kb_id != revision.kb_id:
                # A shared DOI must not merge documents across source ACLs.
                rid += "__source-" + hashlib.sha256(revision.kb_id.encode()).hexdigest()[:12]
                row = (
                    await session.execute(
                        select(LiteraturePaper).where(
                            LiteraturePaper.tenant_id == tenant_id, LiteraturePaper.rid == rid
                        )
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
            if source_revision_id and row.source_revision_id != source_revision_id:
                from yuxi.literature.provenance import invalidate_note_publications

                if row.source_revision_id:
                    await invalidate_note_publications(session, row.source_revision_id)
                row.source_revision_id = source_revision_id
                row.reading_version = int(row.reading_version or 1) + 1
                row.ai_value_score = None
                row.topic_score = None
                row.value_score = 0.0
        await session.flush()
        await session.refresh(row)
        await session.commit()
        return row


# ---------------------------------------------------------------- 任务入列


async def enqueue_literature_compile(
    *, paper_id: int, level: str, force: bool = False, requested_by: str = ""
) -> dict[str, Any]:
    """排一个编译任务。

    活跃租约保持幂等；终态任务重跑会清理旧产物指针并获得全新的重试预算。
    ``force`` 仅能接管租约已过期的 PENDING/RUNNING 任务。
    """
    if level not in VALID_LEVELS:
        raise ValueError(f"非法编译级别: {level}")
    async with pg_manager.get_async_session_context() as session:
        paper = (
            await session.execute(select(LiteraturePaper).where(LiteraturePaper.id == paper_id).with_for_update())
        ).scalar_one_or_none()
        if paper is None:
            raise ValueError(f"文献不存在: {paper_id}")
        job = (
            await session.execute(
                select(LiteratureCompileJob).where(
                    LiteratureCompileJob.tenant_id == paper.tenant_id,
                    LiteratureCompileJob.paper_id == paper_id,
                    LiteratureCompileJob.level == level,
                    LiteratureCompileJob.source_revision_id == (paper.source_revision_id or ""),
                    LiteratureCompileJob.reading_version == paper.reading_version,
                    LiteratureCompileJob.card_revision == paper.card_revision,
                    LiteratureCompileJob.generator_version == COMPILE_VERSION,
                )
            )
        ).scalar_one_or_none()
        if job is not None and not force:
            if job.status in ("SUCCEEDED", "RUNNING", "PENDING"):
                return {"job_id": job.id, "status": job.status, "reused": True}
        if job is not None and force and job.status in ("RUNNING", "PENDING"):
            lease_seconds = RUNNING_LEASE_SECONDS if job.status == "RUNNING" else PENDING_REQUEUE_SECONDS
            # These columns are TIMESTAMPTZ in production. SQLite drops tzinfo
            # in unit tests, so its naive round-trip must still be interpreted
            # as UTC rather than as a legacy Asia/Shanghai timestamp.
            updated_at = job.updated_at
            if updated_at is not None and updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=UTC)
            lease_expired = bool(updated_at and updated_at < utc_now() - timedelta(seconds=lease_seconds))
            if not lease_expired:
                # A fresh in-flight task must never be force-reset underneath
                # its worker: that would permit two billable LLM executions for
                # one durable row. The reconciler owns stale-lease recovery.
                return {"job_id": job.id, "status": job.status, "reused": True}
        if job is None:
            job = LiteratureCompileJob(
                tenant_id=paper.tenant_id,
                paper_id=paper_id,
                level=level,
                status="PENDING",
                source_revision_id=paper.source_revision_id or "",
                reading_version=paper.reading_version,
                card_revision=paper.card_revision,
                generator_version=COMPILE_VERSION,
            )
            session.add(job)
        else:
            job.status = "PENDING"
            job.attempt = 0
            job.error = None
            job.note_uri = None
            job.value_score = None
        if requested_by:
            from yuxi.literature.llm_gateway import freeze_literature_policy

            policy = await freeze_literature_policy(
                session,
                tenant_id=int(paper.tenant_id),
                uid=requested_by,
                operation=f"compile_{level.lower()}",
                resource_ref=(
                    f"{paper.rid}:{level}:{paper.source_revision_id or ''}"
                    f":r{paper.reading_version}:c{paper.card_revision}"
                ),
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
    # 统一有效版本入口（P11：人工冻结→裁决→渲染→导入；artifacts.resolve_effective_document）
    from yuxi.literature.artifacts import resolve_effective_document

    effective = await resolve_effective_document(
        int(paper.tenant_id), paper.rid, paper=paper, storage=get_minio_client()
    )
    if effective is None:
        raise ValueError(f"文献无任何正文产物: {paper.rid}")
    document_json = effective["content"]
    if not document_json:
        raise ValueError(f"有效正文内容为空（source={effective['source']}）: {paper.rid}")
    from yuxi.literature.review import para_text

    doc = json.loads(document_json.decode("utf-8"))
    paragraphs = [para_text(p) for p in (doc.get("paragraphs") or []) if isinstance(p, dict) and para_text(p).strip()]
    if not paragraphs:
        raise ValueError(f"document.json 无可用段落: {paper.rid}")
    meta = {
        "title": paper.title or doc.get("metadata", {}).get("title", ""),
        "doi": paper.doi or "",
        "journal": paper.journal or doc.get("metadata", {}).get("journal", ""),
        "year": paper.pub_year or doc.get("metadata", {}).get("year", ""),
        "authors": paper.authors_json or doc.get("metadata", {}).get("authors", []),
        "_paragraph_ids": [
            str(p.get("para_id") or f"legacy-{i:03d}")
            for i, p in enumerate(doc.get("paragraphs") or [], 1)
            if isinstance(p, dict) and para_text(p).strip()
        ],
    }
    return meta, paragraphs, meta.get("journal", "")


def _notes_object_prefix(paper: LiteraturePaper) -> str:
    return (
        f"{_notes_prefix(tenant_id=paper.tenant_id, rid=paper.rid)}/"
        f"{paper.source_revision_id or 'unlinked'}/r{paper.reading_version}/{COMPILE_VERSION}"
    )


async def _finish_compile_success(job_id: int, *, note_uri: str, value_score: float | None) -> str:
    async with pg_manager.get_async_session_context() as session:
        job = (
            await session.execute(select(LiteratureCompileJob).where(LiteratureCompileJob.id == job_id))
        ).scalar_one()
        paper = await require_current_input(session, job)
        from yuxi.literature.products import frozen_uri

        note_uri = await frozen_uri(session, paper, f"note_{job.level}", fallback=note_uri)
        await session.execute(
            update(LiteratureCompileJob)
            .where(LiteratureCompileJob.id == job_id)
            .values(status="SUCCEEDED", note_uri=note_uri, value_score=value_score, error=None, updated_at=utc_now())
        )
        await session.commit()
        return note_uri


async def _finish_l1_l2_bundle(
    job: LiteratureCompileJob,
    *,
    l1_uri: str,
    l2_uri: str | None,
    value_score: float | None,
) -> str:
    """Persist the combined compiler output as two independently visible levels.

    PaperAgent's compiler emits L1 and L2 in one invocation.  Keeping only the
    requested row made the second artifact invisible to the UI and Dynamic Wiki.
    The bundle update is transactional and preserves the frozen execution policy.
    """
    uri_by_level = {"L1": l1_uri}
    if l2_uri:
        uri_by_level["L2"] = l2_uri
    async with pg_manager.get_async_session_context() as session:
        # L1 and L2 may be submitted concurrently.  Serializing on their shared
        # paper row prevents both workers from racing to insert the counterpart
        # job under uq_literature_compile_jobs_key.
        paper = await require_current_input(session, job)
        from yuxi.literature.products import frozen_uri

        uri_by_level = {
            level: await frozen_uri(session, paper, f"note_{level}", fallback=uri)
            for level, uri in uri_by_level.items()
        }
        rows = list(
            (
                await session.execute(
                    select(LiteratureCompileJob).where(
                        LiteratureCompileJob.tenant_id == job.tenant_id,
                        LiteratureCompileJob.paper_id == job.paper_id,
                        LiteratureCompileJob.level.in_(tuple(set(uri_by_level) | {job.level})),
                        LiteratureCompileJob.source_revision_id == job.source_revision_id,
                        LiteratureCompileJob.reading_version == job.reading_version,
                        LiteratureCompileJob.card_revision == job.card_revision,
                        LiteratureCompileJob.generator_version == job.generator_version,
                    )
                )
            ).scalars()
        )
        by_level = {row.level: row for row in rows}
        for level, uri in uri_by_level.items():
            row = by_level.get(level)
            if row is None:
                row = LiteratureCompileJob(
                    tenant_id=job.tenant_id,
                    paper_id=job.paper_id,
                    level=level,
                    source_revision_id=job.source_revision_id,
                    reading_version=job.reading_version,
                    card_revision=job.card_revision,
                    generator_version=job.generator_version,
                    requested_by=job.requested_by or "",
                    llm_run_id=job.llm_run_id,
                    model_spec=job.model_spec,
                    credential_source=job.credential_source,
                    credential_id=job.credential_id,
                    provider_id=job.provider_id,
                    policy_version=job.policy_version,
                )
                session.add(row)
            row.status = "SUCCEEDED"
            row.note_uri = uri
            row.value_score = None if (paper.artifact_pointers or {}).get(f"note_{level}") else value_score
            row.error = None
            row.updated_at = utc_now()
        # A requested L2 whose compiler legitimately produced no L2 remains a
        # successful readable L1 result instead of pointing at a missing object.
        if job.level == "L2" and not l2_uri:
            current = by_level.get("L2")
            if current is not None:
                current.status = "SUCCEEDED"
                current.note_uri = await frozen_uri(session, paper, "note_L2", fallback=uri_by_level["L1"])
                uri_by_level["L2"] = current.note_uri
                current.value_score = None if (paper.artifact_pointers or {}).get("note_L2") else value_score
                current.error = None
        await session.commit()
    return uri_by_level.get(job.level, l1_uri)


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


async def _assemble_l3_candidates(tenant_id: int, paper_rid: str, self_ctx: str) -> list[dict]:
    """L3 相关文献候选装配（P10-A 修订；审计 §二.1/§二.2）。

    上游语义：从本文 L1/L2 提取检索关键词 → 召回相关**已编译**文献（排除自身）
    → 候选上下文（对方 L1 摘要）。

    审计修复：
    - **来源 ACL**：``paper_visibility`` 在候选查询层过滤——无权文献不进入
      L3 输入（跨文献权限闭包基础）。
    - **真实产物路径**：读取编译任务的 ``note_uri``（含版本路径），不拼文件名。
    - 零候选返回空列表（调用方显式报错）。
    """
    import re as _re

    from sqlalchemy import select as _select

    from yuxi.literature.access import paper_visibility
    from yuxi.literature.provenance import current_job

    async with pg_manager.get_async_session_context() as session:
        parent = (
            await session.execute(
                select(LiteraturePaper).where(LiteraturePaper.rid == paper_rid, paper_visibility(tenant_id))
            )
        ).scalar_one_or_none()
        if parent is None:
            return []
        parent_source = (
            select(KnowledgeParseRevision.kb_id)
            .join(LiteraturePaper, LiteraturePaper.source_revision_id == KnowledgeParseRevision.revision_id)
            .where(LiteraturePaper.rid == paper_rid, paper_visibility(tenant_id))
        )
        # L3 notes publish with their parent's source ACL. Until cross-source
        # dependency manifests exist, never embed another source's private text
        # into that publication, even when a background worker can see it.
        same_source_revisions = select(KnowledgeParseRevision.revision_id).where(
            KnowledgeParseRevision.tenant_id == tenant_id,
            KnowledgeParseRevision.kb_id.in_(parent_source),
        )
        # ACL 先于召回 + note_uri 真实路径（每篇取最新成功 L1）
        others = (
            await session.execute(
                _select(LiteraturePaper, LiteratureCompileJob.note_uri)
                .join(
                    LiteratureCompileJob,
                    LiteratureCompileJob.paper_id == LiteraturePaper.id,
                )
                .where(
                    paper_visibility(tenant_id),
                    LiteraturePaper.rid != paper_rid,
                    or_(
                        LiteraturePaper.source_revision_id.in_(same_source_revisions),
                        LiteraturePaper.source_revision_id.is_(None) if parent.source_revision_id is None else False,
                    ),
                    LiteratureCompileJob.tenant_id == tenant_id,
                    LiteratureCompileJob.level == "L1",
                    LiteratureCompileJob.status == "SUCCEEDED",
                    LiteratureCompileJob.note_uri.isnot(None),
                    current_job(LiteratureCompileJob),
                )
                .order_by(LiteratureCompileJob.updated_at.desc())
            )
        ).all()
    # 每篇取最新成功任务的 note_uri
    latest_note_uri: dict[int, tuple[str, str]] = {}
    for paper, note_uri in others:
        latest_note_uri.setdefault(paper.id, (paper.rid, note_uri))
    if not latest_note_uri:
        return []
    # 关键词：本文上下文的高频实词
    words = _re.findall(r"[A-Za-z][A-Za-z-]{3,}", self_ctx)
    freq: dict[str, int] = {}
    for w in words:
        freq[w.lower()] = freq.get(w.lower(), 0) + 1
    keywords = {w for w, _ in sorted(freq.items(), key=lambda kv: -kv[1])[:25]}
    minio = get_minio_client()
    paper_by_id = {p.id: p for p, _ in others}
    candidates: list[dict] = []
    for paper_id, (rid, note_uri) in list(latest_note_uri.items())[:50]:
        other = paper_by_id[paper_id]
        try:
            note = (await minio.adownload_file(RENDER_BUCKET, note_uri.removeprefix("kbasset://"))).decode("utf-8")
        except Exception:  # noqa: BLE001 - 产物缺失跳过（不拼旧路径）
            continue
        title_words = {w.lower() for w in _re.findall(r"[A-Za-z][A-Za-z-]{3,}", str(other.title or ""))}
        note_words = set(_re.findall(r"[A-Za-z][A-Za-z-]{3,}", note[:4000].lower()))
        overlap = len(keywords & (title_words | note_words))
        if overlap < 3:
            continue
        candidates.append(
            {
                "doi": str(other.doi or other.rid),
                "context": note[:3000],
                "connection": f"关键词重合 {overlap}",
                "_overlap": overlap,
            }
        )
    candidates.sort(key=lambda c: -c["_overlap"])
    for c in candidates:
        c.pop("_overlap", None)
    return candidates[:8]


async def process_literature_compile(ctx: dict[str, Any], job_id: int) -> dict[str, Any] | None:
    """ARQ 任务：编译一个文献的一个级别（L1=合并产出 L1+L2；L3=关系层）。"""
    job = await _claim_job(job_id)
    if job is None:
        return None
    try:
        assert_not_evidence_capable(compiler.LITERATURE_NOTES_KB_TYPE)
        async with pg_manager.get_async_session_context() as session:
            paper = await require_current_input(session, job)
        meta, paragraphs, journal_meta = await _load_compile_inputs(paper)
        if job.requested_by:
            from yuxi.literature.cards import list_cards

            cards = await list_cards(int(paper.tenant_id), paper.rid)
            meta["_manual_cards"] = [
                {
                    "type": card["card_type"],
                    "title": card["title"],
                    "annotation": card["content"][:4000],
                    "para_ids": card["para_ids"],
                }
                for card in cards[:20]
            ]
        minio = get_minio_client()
        # Each execution writes a new generation. Force-compiling identical
        # parse/read versions must not overwrite readers' current note bytes.
        prefix = f"{_notes_object_prefix(paper)}/g{uuid.uuid4().hex}"

        if job.level in ("L1", "L2"):
            if job.llm_run_id:
                from yuxi.literature.llm_gateway import governed_runtime, policy_for_job_attempt

                policy = await policy_for_job_attempt(job, operation=f"compile_{job.level.lower()}")
                async with governed_runtime(policy):
                    result = await compiler.compile_l1_l2(meta, paragraphs, journal_meta=journal_meta)
            else:  # pre-0078 jobs and isolated unit fixtures
                result = await compiler.compile_l1_l2(meta, paragraphs, journal_meta=journal_meta)
            note_md = compiler.render_l1_note(meta, result["l1"])
            l1_uri = f"kbasset://{prefix}/L1_note.md"
            await minio.aupload_file(
                bucket_name=RENDER_BUCKET,
                object_name=f"{prefix}/L1_note.md",
                data=note_md.encode("utf-8"),
                content_type="text/markdown",
            )
            l2_uri = None
            if result["l2"]:
                l2_uri = f"kbasset://{prefix}/L2_wiki.md"
                await minio.aupload_file(
                    bucket_name=RENDER_BUCKET,
                    object_name=f"{prefix}/L2_wiki.md",
                    data=compiler.render_l2_wiki(meta, result["l2"]).encode("utf-8"),
                    content_type="text/markdown",
                )
            # L1 结构化数据（concepts/scores）落盘：概念页聚合的优先数据源
            # （concepts.py 兜底解析 L1_note.md，兼容历史产物）
            import json as _json

            await minio.aupload_file(
                bucket_name=RENDER_BUCKET,
                object_name=f"{prefix}/L1_data.json",
                data=_json.dumps(
                    {
                        "one_liner": result["l1"].get("one_liner"),
                        "concepts": result["l1"].get("concepts") or [],
                        "tags": result["l1"].get("tags") or [],
                        "ai_value": result["ai_value"],
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
                content_type="application/json",
            )
            score = value_score.compute_value_score(
                {
                    "ai_value": result["ai_value"],
                    "topic_score": result["topic_score"],
                    "year": meta.get("year"),
                    "journal_info": {"jcr": {"jif": paper.impact_factor, "quartile": paper.quartile}},
                    "has_bib": paper.external_cited is not None,
                    "times_cited": paper.external_cited,
                    "paper_rank": paper.paper_rank or None,
                    "library_citations": None,
                }
            )
            note_uri = await _finish_l1_l2_bundle(
                job,
                l1_uri=l1_uri,
                l2_uri=l2_uri,
                value_score=score,
            )
            try:
                from yuxi.literature.fulltext import sync_note

                async with pg_manager.get_async_session_context() as session:
                    l1_job_id = (
                        await session.execute(
                            select(LiteratureCompileJob.id).where(
                                LiteratureCompileJob.paper_id == job.paper_id,
                                LiteratureCompileJob.tenant_id == job.tenant_id,
                                LiteratureCompileJob.level == "L1",
                                LiteratureCompileJob.source_revision_id == job.source_revision_id,
                                LiteratureCompileJob.reading_version == job.reading_version,
                                LiteratureCompileJob.card_revision == job.card_revision,
                                LiteratureCompileJob.generator_version == job.generator_version,
                            )
                        )
                    ).scalar_one()
                await sync_note(l1_job_id)
            except Exception:  # noqa: BLE001 - committed note remains valid; bounded cron repairs its projection
                logger.exception("笔记已编译，全文导航投影等待补偿: job=%s", job.id)
            # 回填评分 + L3 自动升级（ai_value 可用且 score ≥ 4.0）
            async with pg_manager.get_async_session_context() as session:
                current_paper = await require_current_input(session, job)
                await session.execute(
                    update(LiteraturePaper)
                    .where(LiteraturePaper.id == paper.id, not (current_paper.artifact_pointers or {}).get("note_L1"))
                    .values(
                        ai_value_score=result["ai_value"],
                        topic_score=result["topic_score"],
                        value_score=score,
                        updated_at=utc_now(),
                    )
                )
                await session.commit()
            want_l3 = value_score.auto_upgrade_levels(score, ai_value_available=result["ai_value"] is not None)[1]
            if want_l3 and not (current_paper.artifact_pointers or {}).get("note_L1"):
                await enqueue_literature_compile(
                    paper_id=paper.id,
                    level="L3",
                    requested_by=job.requested_by,
                )
            try:
                from yuxi.literature.notes.wiki_publish import publish_literature_notes

                await publish_literature_notes(
                    int(paper.tenant_id),
                    actor_uid=job.requested_by or "system:literature-compile",
                )
            except Exception:  # noqa: BLE001 - note remains valid; cron repairs navigation publication
                logger.exception("文献笔记已编译，但 Wiki 发布失败，将由对账任务补偿: job=%s", job.id)
            return {"job_id": job.id, "level": job.level, "status": "SUCCEEDED", "note_uri": note_uri}

        # L3：本文编译上下文 = L1 笔记（kbasset 同前缀）；相关文献检索随 P4 计量接入
        from yuxi.literature.readops import get_note_uri

        l1_uri = await get_note_uri(int(paper.tenant_id), paper.rid, "L1")
        if not l1_uri:
            raise ValueError("当前版本 L1 笔记缺失，无法编译 L3（先编译 L1）")
        l1_bytes = await minio.adownload_file(RENDER_BUCKET, l1_uri.removeprefix("kbasset://"))
        self_ctx = l1_bytes.decode("utf-8")
        if len(self_ctx.strip()) < _L3_MIN_SELF_CTX_CHARS:
            raise ValueError("L1 笔记缺失或过短，无法编译 L3（先编译 L1）")
        related = await _assemble_l3_candidates(int(job.tenant_id), paper.rid, self_ctx)
        if not related:
            raise ValueError(
                "缺少相关编译文献：L3 需要库内其他已编译 L1 的文献（先编译更多同主题文献，"
                "或稍后重试——上游语义为跨文献关系分析，不生成单文献空骨架）"
            )
        if job.llm_run_id:
            from yuxi.literature.llm_gateway import governed_runtime, policy_for_job_attempt

            policy = await policy_for_job_attempt(job, operation="compile_l3")
            async with governed_runtime(policy):
                relations_md = await compiler.compile_l3(meta, self_ctx, related_ctxs=related)
        else:  # pre-0078 jobs and isolated unit fixtures
            relations_md = await compiler.compile_l3(meta, self_ctx, related_ctxs=related)
        note_uri = f"kbasset://{prefix}/L3_relations.md"
        await minio.aupload_file(
            bucket_name=RENDER_BUCKET,
            object_name=f"{prefix}/L3_relations.md",
            data=relations_md.encode("utf-8"),
            content_type="text/markdown",
        )
        note_uri = await _finish_compile_success(job.id, note_uri=note_uri, value_score=None)
        try:
            from yuxi.literature.notes.wiki_publish import publish_literature_notes

            await publish_literature_notes(
                int(paper.tenant_id),
                actor_uid=job.requested_by or "system:literature-compile",
            )
        except Exception:  # noqa: BLE001 - note remains valid; cron repairs navigation publication
            logger.exception("文献 L3 已编译，但 Wiki 发布失败，将由对账任务补偿: job=%s", job.id)
        return {"job_id": job.id, "level": job.level, "status": "SUCCEEDED", "note_uri": note_uri}
    except StaleLiteratureInput:
        return {"job_id": job.id, "status": "STALE"}
    except IntegrityError:
        raise
    except Exception as exc:  # noqa: BLE001
        reason = f"{type(exc).__name__}: {exc}"
        logger.exception("编译失败: job=%s", job_id)
        status = await _finish_compile_failure(job_id, reason)
        return {"job_id": job_id, "status": status, "error": reason}


# ---------------------------------------------------------------- 对账


async def reconcile_stale_literature_compiles(ctx: dict | None = None) -> int:
    """对账（cron）：孤儿 RUNNING / 滞留 PENDING 回收重排。"""
    del ctx  # ARQ cron 注入的作业上下文（对账不使用）
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
