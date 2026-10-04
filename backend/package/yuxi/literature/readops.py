"""文献域读模型与任务投影（P6a，供 HTTP 路由与前端消费；无业务写逻辑）。

纪律：路由薄层（ARCHITECTURE.md）；租户权威 PrincipalContext；产物只返
kbasset:// 逻辑 URI，物理布局不出本域（由 /api/literature/assets 解析）。
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from yuxi.literature.access import paper_visibility
from yuxi.literature.access import current_access
from yuxi.literature.provenance import current_job, source_matches

from sqlalchemy import func, select

from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
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
    from yuxi.storage.minio import get_minio_client
    from yuxi.literature.reading.service import RENDER_BUCKET
    from yuxi.literature.access import build_access

    uri = await get_note_uri(tenant_id, rid, level)
    if not uri:
        raise ValueError("当前版本笔记不存在")
    data = await get_minio_client().adownload_file(RENDER_BUCKET, uri.removeprefix("kbasset://"))
    scope = current_access()
    if scope is not None and rid not in (await build_access(scope.user, tenant_id)).rids:
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
            and v.generator_version in {"literature-translate-v1", "paperagent-import-v4"}
        ],
        "translation_jobs": [
            {
                "variant_kind": job.variant_kind,
                "status": job.status,
                "variant_uri": job.variant_uri,
                "error": job.error,
            }
            for job in translation_jobs
            if source_matches(job, paper) and job.generator_version == "literature-translate-v1"
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


async def list_paper_tasks(tenant_id: int, *, limit: int = 100) -> list[dict[str, Any]]:
    """任务投影（渲染/编译/翻译三表合并，任务中心与前端轮询共用）。"""
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
        compiles = (
            (
                await session.execute(
                    select(LiteratureCompileJob)
                    .where(LiteratureCompileJob.tenant_id == tenant_id)
                    .where(
                        LiteratureCompileJob.paper_id.in_(select(LiteraturePaper.id).where(paper_visibility(tenant_id)))
                    )
                    .order_by(LiteratureCompileJob.updated_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        translations = (
            (
                await session.execute(
                    select(LiteratureTranslationJob)
                    .where(LiteratureTranslationJob.tenant_id == tenant_id)
                    .where(
                        LiteratureTranslationJob.paper_id.in_(
                            select(LiteraturePaper.id).where(paper_visibility(tenant_id))
                        )
                    )
                    .order_by(LiteratureTranslationJob.updated_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
    tasks: list[dict[str, Any]] = []
    tasks.extend(
        {
            "type": "reader_render",
            "ref": row.revision_id,
            "status": row.status,
            "attempt": row.attempt,
            "error": row.error,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
        for row in renders
    )
    tasks.extend(
        {
            "type": "compile",
            "ref": f"{row.paper_id}:{row.level}",
            "status": row.status,
            "attempt": row.attempt,
            "error": row.error,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
        for row in compiles
    )
    tasks.extend(
        {
            "type": "translation",
            "ref": f"{row.paper_id}:{row.variant_kind}",
            "status": row.status,
            "attempt": row.attempt,
            "error": row.error,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
        for row in translations
    )
    tasks.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return tasks[:limit]


_TASK_CENTER_STATUS = {
    "PENDING": "queued",
    "RUNNING": "running",
    "SUCCEEDED": "success",
    "FAILED": "failed",
    "DEAD": "failed",
    "STALE": "failed",
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
        compiles = list(
            (
                await session.execute(
                    select(LiteratureCompileJob, LiteraturePaper.rid, LiteraturePaper.title)
                    .join(LiteraturePaper, LiteraturePaper.id == LiteratureCompileJob.paper_id)
                    .order_by(LiteratureCompileJob.updated_at.desc())
                    .limit(bounded_limit)
                )
            ).all()
        )
        translations = list(
            (
                await session.execute(
                    select(LiteratureTranslationJob, LiteraturePaper.rid, LiteraturePaper.title)
                    .join(LiteraturePaper, LiteraturePaper.id == LiteratureTranslationJob.paper_id)
                    .order_by(LiteratureTranslationJob.updated_at.desc())
                    .limit(bounded_limit)
                )
            ).all()
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
                "progress": 100.0 if mapped_status == "success" else 50.0 if mapped_status == "running" else 0.0,
                "message": str(row.error or "") if mapped_status == "failed" else None,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                "started_at": None,
                "completed_at": row.updated_at.isoformat() if mapped_status in {"success", "failed"} else None,
                "error": row.error if mapped_status == "failed" else None,
                "cancel_requested": False,
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
    for row, rid, title in compiles:
        append_task(
            row,
            task_type="compile",
            name=f"文献笔记 {row.level}：{title or rid}",
            ref=f"{rid}:{row.level}",
            payload={"rid": rid, "paper_id": row.paper_id, "level": row.level},
        )
    for row, rid, title in translations:
        append_task(
            row,
            task_type="translation",
            name=f"文献翻译 {row.variant_kind}：{title or rid}",
            ref=f"{rid}:{row.variant_kind}",
            payload={"rid": rid, "paper_id": row.paper_id, "variant_kind": row.variant_kind},
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
