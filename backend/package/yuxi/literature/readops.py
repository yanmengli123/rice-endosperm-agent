"""文献域读模型与任务投影（P6a，供 HTTP 路由与前端消费；无业务写逻辑）。

纪律：路由薄层（ARCHITECTURE.md）；租户权威 PrincipalContext；产物只返
kbasset:// 逻辑 URI，物理布局不出本域（由 /api/literature/assets 解析）。
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from sqlalchemy import func, select

from yuxi.literature.access import current_access, paper_visibility
from yuxi.literature.provenance import current_job, source_matches
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeBase,
    KnowledgeFile,
    KnowledgeParseRevision,
    LiteratureAttachment,
    LiteratureCompileJob,
    LiteraturePaper,
    LiteratureReaderRender,
    LiteratureTranslationJob,
    LiteratureVariant,
)

_ALLOWED_ASSET_SEGMENTS = {"reader_render", "variants", "notes", "attachments", "reports", "artifacts"}


async def read_current_note(tenant_id: int, rid: str, level: str, *, max_chars: int = 12000) -> str:
    """Bounded navigation text from the current successful job, never evidence."""
    from yuxi.literature.access import build_access
    from yuxi.literature.reading.service import RENDER_BUCKET
    from yuxi.storage.minio import get_minio_client

    uri = await get_note_uri(tenant_id, rid, level)
    if not uri:
        raise ValueError("当前版本笔记不存在")
    data = await get_minio_client().adownload_file(RENDER_BUCKET, uri.removeprefix("kbasset://"))
    scope = current_access()
    if scope is not None and rid not in (await build_access(scope.user, tenant_id, rids=(rid,))).rids:
        raise PermissionError("笔记来源权限已撤销")
    if await get_note_uri(tenant_id, rid, level) != uri:
        raise ValueError("笔记版本已变更，请重新读取")
    return data.decode("utf-8")[: max(1, min(max_chars, 20000))]


_QUARTILE_VALUES = ("Q1", "Q2", "Q3", "Q4")
#: 「未知分区」筛选字面量（前端「未知分区」选项）；对应 quartile == ""
_QUARTILE_UNKNOWN = ("none", "unknown", "未知")


def _parse_quartiles(raw: str) -> list[str] | None:
    """分区筛选解析（逗号分隔，Q1–Q4；``none`` 表示未知分区）；空串表示不筛选。"""
    values = [item.strip() for item in (raw or "").split(",") if item.strip()]
    if not values:
        return None
    parsed: list[str] = []
    for value in values:
        upper = value.upper()
        if upper in _QUARTILE_VALUES:
            parsed.append(upper)
        elif value.lower() in _QUARTILE_UNKNOWN:
            parsed.append("")
        else:
            raise ValueError("分区筛选只能是 Q1–Q4 或 none（未知分区）")
    return sorted(set(parsed))


async def list_papers(
    tenant_id: int,
    *,
    query: str = "",
    kind: str = "",
    page: int = 1,
    page_size: int = 20,
    journal: str = "",
    quartile: str = "",
    min_if: float | None = None,
    year_min: int | None = None,
    year_max: int | None = None,
    compiled_level: str = "",
    source: str = "",
    sort: str = "updated",
) -> dict[str, Any]:
    """文献列表（检索词匹配标题/DOI；类型芯片筛选；分页）。"""
    async with pg_manager.get_async_session_context() as session:
        conditions = [paper_visibility(tenant_id)]
        if query.strip():
            from yuxi.knowledge.evidence.verbatim import escape_like

            like = f"%{escape_like(query.strip())}%"
            conditions.append(
                func.coalesce(LiteraturePaper.title, "").ilike(like, escape="/")
                | func.coalesce(LiteraturePaper.doi, "").ilike(like, escape="/")
            )
        if kind.strip():
            conditions.append(LiteraturePaper.kind == kind.strip())
        if journal.strip():
            from yuxi.knowledge.evidence.verbatim import escape_like

            conditions.append(LiteraturePaper.journal.ilike(f"%{escape_like(journal.strip())}%", escape="/"))
        quartiles = _parse_quartiles(quartile)
        if quartiles is not None:
            conditions.append(LiteraturePaper.quartile.in_(quartiles))
        if min_if is not None:
            if min_if < 0:
                raise ValueError("最低影响因子不能为负")
            conditions.append(LiteraturePaper.impact_factor >= min_if)
        if year_min is not None:
            conditions.append(LiteraturePaper.pub_year >= year_min)
        if year_max is not None:
            conditions.append(LiteraturePaper.pub_year <= year_max)
        if year_min is not None and year_max is not None and year_min > year_max:
            raise ValueError("年份起点不能晚于终点")
        if source == "pdf":
            conditions.append(LiteraturePaper.source_revision_id.isnot(None))
        elif source == "catalog":
            conditions.append(LiteraturePaper.source_revision_id.is_(None))
        elif source:
            raise ValueError("来源筛选必须为 pdf/catalog")
        compiled = (
            select(LiteratureCompileJob.id)
            .where(
                LiteratureCompileJob.paper_id == LiteraturePaper.id,
                current_job(LiteratureCompileJob),
                LiteratureCompileJob.status == "SUCCEEDED",
            )
            .correlate(LiteraturePaper)
        )
        if compiled_level == "missing":
            conditions.append(~compiled.exists())
        elif compiled_level:
            if compiled_level not in {"any", "L1", "L2", "L3"}:
                raise ValueError("非法编译级别筛选")
            if compiled_level != "any":
                compiled = compiled.where(LiteratureCompileJob.level == compiled_level)
            conditions.append(compiled.exists())
        sorting = {
            "updated": LiteraturePaper.updated_at.desc(),
            "year": LiteraturePaper.pub_year.desc().nulls_last(),
            "cited": LiteraturePaper.external_cited.desc().nulls_last(),
            "rank": LiteraturePaper.paper_rank.desc(),
            "value": LiteraturePaper.value_score.desc(),
            "title": LiteraturePaper.title.asc(),
        }
        if sort not in sorting:
            raise ValueError("非法排序方式")
        base = select(LiteraturePaper).where(*conditions)
        total = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
        rows = (
            (
                await session.execute(
                    base.order_by(sorting[sort], LiteraturePaper.id.desc())
                    .offset(max(0, (page - 1) * page_size))
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )
    return {
        "total": int(total),
        "page": page,
        "page_size": page_size,
        "items": [
            {
                "rid": row.rid,
                "kind": row.kind,
                "doi": row.doi,
                "title": row.title,
                "value_score": row.value_score,
                "ai_value_score": row.ai_value_score,
                "paper_rank": row.paper_rank,
                "impact_factor": row.impact_factor,
                "quartile": row.quartile,
                "pub_year": row.pub_year,
                "journal": row.journal,
                "external_cited": row.external_cited,
                "source_revision_id": row.source_revision_id,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
            for row in rows
        ],
    }


async def get_paper_detail(tenant_id: int, rid: str) -> dict[str, Any] | None:
    """文献详情：元数据 + 编译/翻译任务状态 + 变体 + 附件（一处聚合）。"""
    async with pg_manager.get_async_session_context() as session:
        paper = (
            await session.execute(
                select(LiteraturePaper).where(paper_visibility(tenant_id), LiteraturePaper.rid == rid)
            )
        ).scalar_one_or_none()
        if paper is None:
            return None
        compile_jobs = (
            (await session.execute(select(LiteratureCompileJob).where(LiteratureCompileJob.paper_id == paper.id)))
            .scalars()
            .all()
        )
        variants = (
            (await session.execute(select(LiteratureVariant).where(LiteratureVariant.paper_id == paper.id)))
            .scalars()
            .all()
        )
        translation_jobs = (
            (
                await session.execute(
                    select(LiteratureTranslationJob).where(LiteratureTranslationJob.paper_id == paper.id)
                )
            )
            .scalars()
            .all()
        )
        latest_translations = {}
        for job in sorted(translation_jobs, key=lambda item: item.id, reverse=True):
            if source_matches(job, paper) and job.generator_version in {"literature-translate-v1", "supertranslate-native-v1"}:
                latest_translations.setdefault(job.variant_kind, job)
        attachments = (
            (await session.execute(select(LiteratureAttachment).where(LiteratureAttachment.paper_id == paper.id)))
            .scalars()
            .all()
        )
        render = None
        if paper.source_revision_id:
            render = (
                await session.execute(
                    select(LiteratureReaderRender)
                    .where(LiteratureReaderRender.revision_id == paper.source_revision_id)
                    .order_by(LiteratureReaderRender.updated_at.desc(), LiteratureReaderRender.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
    return {
        "rid": paper.rid,
        "kind": paper.kind,
        "doi": paper.doi,
        "title": paper.title,
        "source_revision_id": paper.source_revision_id,
        "reading_version": paper.reading_version,
        "source": current_access().sources.get(rid) if current_access() else None,
        "value_score": paper.value_score,
        "ai_value_score": paper.ai_value_score,
        "paper_rank": paper.paper_rank,
        "cocitation_cluster": paper.cocitation_cluster,
        "impact_factor": paper.impact_factor,
        "quartile": paper.quartile,
        "render": (
            {
                "status": render.status,
                "markdown_uri": render.markdown_uri,
                "image_count": render.image_count,
                "render_version": render.render_version,
            }
            if render
            else None
        ),
        "compile_jobs": [
            {"level": job.level, "status": job.status, "note_uri": job.note_uri, "error": job.error}
            for job in compile_jobs
            if source_matches(job, paper) and job.generator_version in {"literature-compile-v1", "paperagent-import-v4"}
        ],
        "variants": [
            {
                "variant_kind": v.variant_kind,
                "lang": v.lang,
                "uri": v.uri,
                "parse_revision_id": v.parse_revision_id,
            }
            for v in variants
            if v.parse_revision_id == paper.source_revision_id
            and v.reading_version == paper.reading_version
            and v.generator_version in {"literature-translate-v1", "paperagent-import-v4", "supertranslate-native-v1"}
        ],
        "translation_jobs": [
            {
                "job_id": job.id,
                "variant_kind": job.variant_kind,
                "status": job.status,
                "variant_uri": job.variant_uri,
                "error": job.error,
                "progress": job.progress,
                "stage": (job.execution_json or {}).get("stage"),
                "cancel_requested": job.cancel_requested,
                "options": {
                    key: value
                    for key, value in (job.options_json or {}).items()
                    if key in {"batch_chars", "reasoning_effort"}
                },
            }
            for job in latest_translations.values()
        ],
        "attachments": [
            {"kind": a.kind, "filename": a.filename, "uri": a.uri, "text_uri": a.text_uri} for a in attachments
        ],
        "historical_compile_jobs": [
            {
                "level": j.level,
                "status": j.status,
                "source_revision_id": j.source_revision_id,
                "reading_version": j.reading_version,
            }
            for j in compile_jobs
            if not source_matches(j, paper)
        ],
    }


async def get_note_uri(tenant_id: int, rid: str, level: str) -> str | None:
    """读 L1/L2/L3 笔记的逻辑 URI（内容经 assets 端点取）。"""
    if level not in ("L1", "L2", "L3"):
        raise ValueError(f"非法笔记级别: {level}")
    async with pg_manager.get_async_session_context() as session:
        paper = (
            await session.execute(
                select(LiteraturePaper).where(paper_visibility(tenant_id), LiteraturePaper.rid == rid)
            )
        ).scalar_one_or_none()
        if paper is None:
            return None
        if (paper.artifact_pointers or {}).get(f"note_{level}"):
            from yuxi.literature.products import frozen_uri

            return await frozen_uri(session, paper, f"note_{level}", fallback=None)
        job = (
            await session.execute(
                select(LiteratureCompileJob)
                .where(
                    LiteratureCompileJob.paper_id == paper.id,
                    LiteratureCompileJob.level == level,
                    current_job(LiteratureCompileJob, paper),
                    LiteratureCompileJob.status == "SUCCEEDED",
                )
                .order_by(LiteratureCompileJob.updated_at.desc(), LiteratureCompileJob.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    return job.note_uri if job else None


#: 摄入阶段展示映射：``KnowledgeParseRevision.status`` → 中文阶段说明。
#: 只做展示映射，不改变知识域/文献域各自的状态机（ADR-0010 决议 5b 反对双栈回流）。
#: 未知状态（上游升级新增）回退为原始状态字符串，绝不猜测成成功/失败。
_INGEST_STAGE_LABELS = {
    "PENDING": "已提交，排队中",
    "WAITING_CACHE": "解析准备（缓存检查）",
    "RUNNING": "解析中（云供应商）",
    "BUILDING": "构建解析产物",
    "INDEXING": "索引构建中",
    "INDEXED_FULL": "已索引（完整），等待激活",
    "INDEXED_CONTENT_ONLY": "已索引（仅内容），等待激活",
    "INDEXED_TEXT_ONLY": "已索引（纯文本），等待激活",
    "ACTIVE": "激活中",
    "READY": "解析就绪",
    "PARSED_ONLY": "仅解析完成（草稿）",
    "REJECTED": "质量门禁拒绝",
    "FAILED": "处理失败",
    "SKIPPED": "已跳过（重复/复用）",
    "CANCELLED": "已取消",
}


#: 摄入状态语义（**权威定义**，随投影下发布尔位；前端不得自建状态集合——
#: 后端新增状态时前端零改动即正确）。active=仍在处理（含知识域内部激活动作）；
#: failure=失败类；terminal=用户视角已有终局；retryable=存在用户可执行的补救
#: （FAILED 可重新导入同一文件，sha 复用会命中同一 revision 单号）。
_INGEST_ACTIVE_STATUSES = frozenset(
    {
        "PENDING",
        "WAITING_CACHE",
        "RUNNING",
        "BUILDING",
        "INDEXING",
        "INDEXED_FULL",
        "INDEXED_CONTENT_ONLY",
        "INDEXED_TEXT_ONLY",
        "ACTIVE",
    }
)
_INGEST_FAILURE_STATUSES = frozenset({"REJECTED", "FAILED"})
_INGEST_TERMINAL_STATUSES = frozenset({"READY", "PARSED_ONLY", "REJECTED", "FAILED", "SKIPPED", "CANCELLED"})

#: 域任务（渲染/编译/翻译）的展示阶段与同一套权威布尔位。
_DOMAIN_STAGE_LABELS = {
    "PENDING": "排队中",
    "CANCEL_REQUESTED": "取消中",
    "RUNNING": "执行中",
    "SUCCEEDED": "已完成",
    "FAILED": "失败",
    "DEAD": "已死亡",
    "STALE": "已滞留",
    "CANCELLED": "已取消",
}
_DOMAIN_ACTIVE_STATUSES = frozenset({"PENDING", "RUNNING", "CANCEL_REQUESTED"})
_DOMAIN_FAILURE_STATUSES = frozenset({"FAILED", "DEAD", "STALE"})
_DOMAIN_TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED", "DEAD", "STALE", "CANCELLED"})


def _domain_task_flags(status: str) -> dict[str, bool]:
    return {
        "stage": _DOMAIN_STAGE_LABELS.get(status, status),
        "is_active": status in _DOMAIN_ACTIVE_STATUSES,
        "is_terminal": status in _DOMAIN_TERMINAL_STATUSES,
        "is_failure": status in _DOMAIN_FAILURE_STATUSES,
    }


def _ingest_task_flags(status: str, *, registered: bool) -> dict[str, Any]:
    failure = status in _INGEST_FAILURE_STATUSES
    # 已登记为文献且非失败 ⇒ 用户视角入库完成（知识域内部激活动作不再关心）
    terminal = (registered and not failure) or status in _INGEST_TERMINAL_STATUSES
    return {
        "is_active": not terminal and not failure,
        "is_terminal": terminal,
        "is_failure": failure,
        "retryable": status == "FAILED",
    }


async def _project_ingest_tasks(session, tenant_id: int, limit: int) -> list[dict[str, Any]]:
    """租户内 PDF 契约摄入投影：从提交那一刻起可见，消灭导入后的可见性真空。

    权限红线：必须以 ``KnowledgeParseRevision.tenant_id`` 过滤并限定 ``pdf_evidence``
    契约知识源；**不得**复用跨租户的 ``list_literature_pipeline_tasks``（那是超管
    任务中心的口径，spans tenants）。尚未登记为文献的 revision 通过
    ``source_revision_id`` 反查 rid，登记完成即原地可点击。
    """
    scope = current_access()
    acl = True if scope is None else (
        KnowledgeBase.kb_id.in_(getattr(scope, "allowed_kbs", ())) if scope.tenant_id == tenant_id else False
    )
    rows = (
        await session.execute(
            select(KnowledgeParseRevision, KnowledgeFile.original_filename, KnowledgeFile.filename)
            .join(KnowledgeFile, KnowledgeFile.file_id == KnowledgeParseRevision.file_id)
            .join(KnowledgeBase, KnowledgeBase.kb_id == KnowledgeParseRevision.kb_id)
            .where(
                KnowledgeParseRevision.tenant_id == tenant_id,
                KnowledgeBase.tenant_id == tenant_id,
                KnowledgeBase.governance_status.notin_(("ARCHIVED", "DELETED")),
                KnowledgeBase.contract_key == "pdf_evidence",
                KnowledgeFile.kb_id == KnowledgeBase.kb_id,
                acl,
            )
            .order_by(KnowledgeParseRevision.updated_at.desc())
            .limit(limit)
        )
    ).all()
    if not rows:
        return []
    revision_ids = [row[0].revision_id for row in rows]
    rid_by_revision = {
        source_revision: rid
        for source_revision, rid in (
            await session.execute(
                select(LiteraturePaper.source_revision_id, LiteraturePaper.rid).where(
                    paper_visibility(tenant_id),
                    LiteraturePaper.source_revision_id.in_(revision_ids),
                )
            )
        ).all()
    }
    return [
        {
            "type": "ingest",
            "ref": revision.revision_id,
            "status": revision.status,
            # 已登记为文献且非失败类终态 ⇒ 从用户视角入库已完成（可读），
            # 覆盖 INDEXED_*/ACTIVE 等仍在等待知识域内部激活动作的中间标签
            "stage": (
                "入库完成（已登记文献）"
                if rid_by_revision.get(revision.revision_id)
                and revision.status not in ("REJECTED", "FAILED", "SKIPPED", "CANCELLED", "PARSED_ONLY")
                else _INGEST_STAGE_LABELS.get(revision.status, revision.status)
            ),
            **_ingest_task_flags(revision.status, registered=bool(rid_by_revision.get(revision.revision_id))),
            "detail": original or filename or revision.revision_id,
            "paper_rid": rid_by_revision.get(revision.revision_id),
            "processing_mode": revision.processing_mode,
            "attempt": revision.attempt,
            "error": revision.error_message,
            "created_at": revision.created_at.isoformat() if revision.created_at else None,
            "updated_at": revision.updated_at.isoformat() if revision.updated_at else None,
        }
        for revision, original, filename in rows
    ]


async def list_paper_tasks(tenant_id: int, *, limit: int = 100) -> list[dict[str, Any]]:
    """任务投影（入库/渲染合并；任务中心与前端轮询共用）。

    入库段来自知识域 ``KnowledgeParseRevision``（租户内 + pdf_evidence 契约），
    让"导入 PDF 后到文献登记前"的数分钟处理过程从第一秒起用户可见。
    """
    async with pg_manager.get_async_session_context() as session:
        renders = (
            (
                await session.execute(
                    select(LiteratureReaderRender)
                    .where(LiteratureReaderRender.tenant_id == tenant_id)
                    .where(
                        LiteratureReaderRender.revision_id.in_(
                            select(LiteraturePaper.source_revision_id).where(paper_visibility(tenant_id))
                        )
                    )
                    .order_by(LiteratureReaderRender.updated_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        ingest = await _project_ingest_tasks(session, tenant_id, limit)
    tasks: list[dict[str, Any]] = []
    tasks.extend(ingest)
    tasks.extend(
        {
            "type": "reader_render",
            "ref": row.revision_id,
            "status": row.status,
            **_domain_task_flags(row.status),
            "attempt": row.attempt,
            "error": row.error,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
        for row in renders
    )
    tasks.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return tasks[:limit]


_TASK_CENTER_STATUS = {
    "PENDING": "queued",
    "CANCEL_REQUESTED": "running",  # 已请求取消但尚未收敛，仍是活跃态
    "RUNNING": "running",
    "SUCCEEDED": "success",
    "FAILED": "failed",
    "DEAD": "failed",
    "STALE": "failed",
    # 必须显式映射：未命中会 fallback 到 "running"，让已取消任务在任务中心
    # 显示为"运行中"，且与 AgentRun 的终态语义不一致。
    "CANCELLED": "cancelled",
}


async def list_literature_pipeline_tasks(*, status: str | None = None, limit: int = 100) -> dict[str, Any]:
    """Project durable literature jobs into the administrator task center.

    Literature jobs execute in ARQ and therefore cannot be represented by the
    API process-local ``Tasker``.  This projection intentionally spans tenants;
    its only HTTP consumer is the super-administrator task endpoint.
    """
    bounded_limit = max(1, min(int(limit or 100), 100))
    async with pg_manager.get_async_session_context() as session:
        renders = list(
            (
                await session.execute(
                    select(LiteratureReaderRender)
                    .order_by(LiteratureReaderRender.updated_at.desc())
                    .limit(bounded_limit)
                )
            )
            .scalars()
            .all()
        )

    projected: list[dict[str, Any]] = []

    def append_task(
        row: Any,
        *,
        task_type: str,
        name: str,
        ref: str,
        payload: dict[str, Any],
    ) -> None:
        mapped_status = _TASK_CENTER_STATUS.get(str(row.status or "").upper(), "running")
        if status and mapped_status != status:
            return
        projected.append(
            {
                "id": f"literature:{task_type}:{row.id}",
                "name": name,
                "type": f"literature_{task_type}",
                "status": mapped_status,
                "progress": float(row.progress or 0) * 100 if getattr(row, "variant_kind", None) == "pdf_zh" else 100.0 if mapped_status == "success" else 50.0 if mapped_status == "running" else 0.0,
                "message": str(row.error or "") if mapped_status == "failed" else None,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                "started_at": None,
                "completed_at": row.updated_at.isoformat() if mapped_status in {"success", "failed"} else None,
                "error": row.error if mapped_status == "failed" else None,
                "cancel_requested": bool(getattr(row, "cancel_requested", False)),
                "cancelable": False,
                "deletable": False,
                "tenant_id": int(row.tenant_id),
                "payload": {"ref": ref, "attempt": row.attempt, **payload},
            }
        )

    for row in renders:
        append_task(
            row,
            task_type="reader_render",
            name=f"文献阅读包渲染：{row.revision_id}",
            ref=row.revision_id,
            payload={"revision_id": row.revision_id},
        )

    projected.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    projected = projected[:bounded_limit]
    status_counter = Counter(item["status"] for item in projected)
    type_counter = Counter(item["type"] for item in projected)
    return {
        "tasks": projected,
        "summary": {
            "total": len(projected),
            "filtered_total": len(projected),
            "status_counts": dict(status_counter),
            "type_counts": dict(type_counter),
        },
    }


def validate_literature_asset_uri(uri: str, tenant_id: int) -> str:
    """校验 kbasset 文献域资产 URI：租户匹配 + 白名单命名空间 + 禁穿越。

    返回 MinIO object 名；非法即抛 ValueError（前端永不接触物理布局）。
    """
    text = str(uri or "").strip()
    if not text.startswith("kbasset://"):
        raise ValueError("仅接受 kbasset:// 逻辑 URI")
    raw = text.removeprefix("kbasset://")
    if ".." in raw or "\\" in raw:
        raise ValueError("URI 含非法路径段")
    parts = [p for p in raw.split("/") if p]
    if len(parts) < 4 or parts[0] != "tenants":
        raise ValueError("URI 形态非法")
    try:
        uri_tenant = int(parts[1])
    except ValueError as exc:
        raise ValueError("URI 租户段非法") from exc
    if uri_tenant != tenant_id:
        raise ValueError("跨租户资产访问被拒绝")
    if not any(segment in _ALLOWED_ASSET_SEGMENTS for segment in parts):
        raise ValueError("URI 命名空间不在文献域白名单内")
    return raw
