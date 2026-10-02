"""文献域 HTTP 路由（P6a，薄层：请求解析/鉴权/装配，无业务逻辑）。

- 租户权威 `PrincipalContext`：请求体 tenant_id 一律忽略（AGENTS.md 硬性）；
- 渲染/翻译等长任务一律 202 + job 投影，不同步等待；
- 产物只返 kbasset:// 逻辑 URI，字节经 /api/literature/assets 鉴权解析。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, UploadFile

from server.utils.auth_middleware import get_required_user, get_superadmin_user
from yuxi.literature import bib_import, cards, concepts, diary, enrich as enrich_module
from yuxi.literature import graph_service, readops, review
from yuxi.literature import reader_bundle
from yuxi.literature.access import access_scope, authorize_asset, paper_visibility
from yuxi.literature.attachments import register_attachment
from yuxi.literature.import_archive import import_paperagent_archive
from yuxi.literature.reading.service import RENDER_BUCKET, enqueue_reader_render
from yuxi.literature.notes.service import enqueue_literature_compile
from yuxi.literature.translate.service import enqueue_literature_translation
from yuxi.services.principal import resolve_tenant_id
from yuxi.storage.minio import get_minio_client
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import User


async def authorize_literature_request(request: Request, current_user: User = Depends(get_required_user)):
    tenant_id = await _current_tenant(current_user)
    async with access_scope(current_user, tenant_id) as scope:
        rid = request.path_params.get("rid")
        if rid is not None and rid not in scope.rids:
            raise HTTPException(status_code=404, detail="文献不存在或无权访问")
        yield scope


literature = APIRouter(prefix="/literature", tags=["literature"], dependencies=[Depends(authorize_literature_request)])

_ASSET_CONTENT_TYPES = {
    ".md": "text/markdown",
    ".json": "application/json",
    ".txt": "text/plain",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


async def _current_tenant(current_user: User) -> int:
    async with pg_manager.get_async_session_context() as session:
        return await resolve_tenant_id(session, getattr(current_user, "uid", None))


@literature.get("/papers")
async def list_papers(
    query: str = Query(default=""),
    kind: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(get_required_user),
):
    tenant_id = await _current_tenant(current_user)
    return await readops.list_papers(tenant_id, query=query, kind=kind, page=page, page_size=page_size)


@literature.get("/papers/{rid}")
async def get_paper(rid: str, current_user: User = Depends(get_required_user)):
    tenant_id = await _current_tenant(current_user)
    detail = await readops.get_paper_detail(tenant_id, rid)
    if detail is None:
        raise HTTPException(status_code=404, detail="文献不存在或无权访问")
    return detail


@literature.get("/papers/{rid}/notes/{level}")
async def get_paper_note(rid: str, level: str, current_user: User = Depends(get_required_user)):
    tenant_id = await _current_tenant(current_user)
    try:
        note_uri = await readops.get_note_uri(tenant_id, rid, level)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not note_uri:
        raise HTTPException(status_code=404, detail="笔记不存在或尚未编译")
    return {"level": level, "note_uri": note_uri}


@literature.post("/papers/{rid}/render", status_code=202)
async def trigger_render(rid: str, current_user: User = Depends(get_required_user)):
    """手动补偿阅读渲染（PDF 激活已自动登记并入队）。"""
    tenant_id = await _current_tenant(current_user)
    detail = await readops.get_paper_detail(tenant_id, rid)
    if detail is None:
        raise HTTPException(status_code=404, detail="文献不存在或无权访问")
    revision_id = detail.get("source_revision_id")
    if not revision_id:
        raise HTTPException(status_code=409, detail="文献未关联解析版本，无法渲染")
    result = await enqueue_reader_render(revision_id=revision_id)
    return {"status": 202, **result}


@literature.post("/papers/{rid}/compile", status_code=202)
async def trigger_compile(
    rid: str,
    level: str = Query(default="L1"),
    force: bool = Query(default=False),
    current_user: User = Depends(get_required_user),
):
    """触发 L1/L2/L3 编译（202 入队；L1=L1+L2 合并，L3 需价值分达标或 force）。"""
    tenant_id = await _current_tenant(current_user)
    paper_id = await _paper_id_by_rid(tenant_id, rid)
    try:
        result = await enqueue_literature_compile(
            paper_id=paper_id,
            level=level,
            force=force,
            requested_by=current_user.uid,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": 202, **result}


@literature.post("/papers/{rid}/translation", status_code=202)
async def trigger_translation(
    rid: str,
    variant_kind: str = Query(default="zh"),
    current_user: User = Depends(get_required_user),
):
    tenant_id = await _current_tenant(current_user)
    detail = await readops.get_paper_detail(tenant_id, rid)
    if detail is None:
        raise HTTPException(status_code=404, detail="文献不存在或无权访问")
    paper_id = await _paper_id_by_rid(tenant_id, rid)
    try:
        result = await enqueue_literature_translation(
            paper_id=paper_id,
            variant_kind=variant_kind,
            requested_by=current_user.uid,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": 202, **result}


@literature.get("/papers/{rid}/attachments")
async def list_attachments(rid: str, current_user: User = Depends(get_required_user)):
    tenant_id = await _current_tenant(current_user)
    detail = await readops.get_paper_detail(tenant_id, rid)
    if detail is None:
        raise HTTPException(status_code=404, detail="文献不存在或无权访问")
    return {"attachments": detail["attachments"]}


@literature.post("/papers/{rid}/attachments", status_code=201)
async def upload_attachment(
    rid: str,
    file: UploadFile,
    kind: str = Query(default="si"),
    current_user: User = Depends(get_required_user),
):
    """依附资料上传（0 API 本地抽文本；越权文件名 422）。"""
    tenant_id = await _current_tenant(current_user)
    detail = await readops.get_paper_detail(tenant_id, rid)
    if detail is None:
        raise HTTPException(status_code=404, detail="文献不存在或无权访问")
    paper_id = await _paper_id_by_rid(tenant_id, rid)
    content = await file.read()
    try:
        result = await register_attachment(
            tenant_id=tenant_id, paper_id=paper_id, kind=kind, filename=file.filename or "", content=content
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": 201, **result}


@literature.get("/tasks")
async def list_tasks(
    limit: int = Query(default=100, ge=1, le=200),
    current_user: User = Depends(get_required_user),
):
    """文献域任务投影（渲染/编译/翻译；供任务中心与前端轮询）。"""
    tenant_id = await _current_tenant(current_user)
    return {"tasks": await readops.list_paper_tasks(tenant_id, limit=limit)}


@literature.get("/assets")
async def get_asset(
    uri: str = Query(...),
    current_user: User = Depends(get_required_user),
):
    """kbasset 文献域资产解析（鉴权代理：租户匹配 + 命名空间白名单）。"""
    tenant_id = await _current_tenant(current_user)
    try:
        object_name = readops.validate_literature_asset_uri(uri, tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if not await authorize_asset(object_name):
        raise HTTPException(status_code=404, detail="资产不存在或无权访问")
    try:
        data = await get_minio_client().adownload_file(RENDER_BUCKET, object_name)
    except Exception as exc:  # noqa: BLE001 - 对象不存在/存储不可用统一 404
        raise HTTPException(status_code=404, detail="资产不存在") from exc
    suffix = "." + object_name.rsplit(".", 1)[-1].lower() if "." in object_name else ""
    return Response(
        content=data,
        media_type=_ASSET_CONTENT_TYPES.get(suffix, "application/octet-stream"),
        headers={"Cache-Control": "private, max-age=60"},
    )


async def _paper_id_by_rid(tenant_id: int, rid: str) -> int:
    from sqlalchemy import select

    from yuxi.storage.postgres.models_knowledge import LiteraturePaper

    async with pg_manager.get_async_session_context() as session:
        paper = (
            await session.execute(
                select(LiteraturePaper).where(paper_visibility(tenant_id), LiteraturePaper.rid == rid)
            )
        ).scalar_one_or_none()
    if paper is None:
        raise HTTPException(status_code=404, detail="文献不存在或无权访问")
    return paper.id


# ---------------------------------------------------------------- P7：G2 导入


@literature.post("/papers/import/bib", status_code=202)
async def import_bib_endpoint(
    file: UploadFile,
    current_user: User = Depends(get_required_user),
):
    """WoS/BibTeX 导入（幂等：DOI/RID 已存在跳过）。"""
    tenant_id = await _current_tenant(current_user)
    text = (await file.read()).decode("utf-8", errors="replace")
    report = await bib_import.import_bib(tenant_id, text, source_name=file.filename or "")
    return {"status": 202, **report}


@literature.post("/papers/import/markdown", status_code=201)
async def import_markdown_endpoint(
    payload: dict,
    current_user: User = Depends(get_required_user),
):
    """Markdown 直导（免解析、0 API；无 DOI 时指纹身份）。"""
    tenant_id = await _current_tenant(current_user)
    try:
        result = await bib_import.import_markdown_document(
            tenant_id,
            markdown=str(payload.get("markdown") or ""),
            title=str(payload.get("title") or ""),
            doi=str(payload.get("doi") or ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": 201, **result}


@literature.post("/papers/import/paperagent", status_code=202)
async def import_paperagent_endpoint(
    file: UploadFile,
    dry_run: bool = Query(default=True),
    current_user: User = Depends(get_superadmin_user),
):
    """Admin-only PaperAgent DATA_FORMAT 4 ZIP import; dry-run is the default."""
    tenant_id = await _current_tenant(current_user)
    try:
        report = await import_paperagent_archive(
            tenant_id=tenant_id,
            upload=file,
            dry_run=dry_run,
            actor_uid=current_user.uid,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": 202, **report}


# ---------------------------------------------------------------- P7：G1 富化


@literature.post("/papers/{rid}/enrich", status_code=202)
async def enrich_paper_endpoint(rid: str, current_user: User = Depends(get_required_user)):
    """富化单篇（Crossref/OpenAlex/S2 免费源，只填空字段）。"""
    tenant_id = await _current_tenant(current_user)
    result = await enrich_module.enrich_paper(tenant_id, rid)
    return {"status": 202, **result}


@literature.post("/enrich/pending", status_code=202)
async def enrich_pending_endpoint(
    batch_size: int = Query(default=50, ge=1, le=200),
    current_user: User = Depends(get_required_user),
):
    """批量富化缺元数据文献。"""
    tenant_id = await _current_tenant(current_user)
    result = await enrich_module.enrich_pending(tenant_id, batch_size=batch_size)
    return {"status": 202, **result}


# ---------------------------------------------------------------- P7：G5 日记


@literature.get("/diary/days")
async def diary_days(
    month: str = Query(default=""),
    current_user: User = Depends(get_required_user),
):
    tenant_id = await _current_tenant(current_user)
    days = await diary.list_diary_days(tenant_id, current_user.uid, month=month or None)
    return {"days": days}


@literature.get("/diary/{entry_date}")
async def diary_day(entry_date: str, current_user: User = Depends(get_required_user)):
    tenant_id = await _current_tenant(current_user)
    try:
        return await diary.get_diary(tenant_id, current_user.uid, entry_date)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@literature.put("/diary/{entry_date}")
async def diary_write(
    entry_date: str,
    payload: dict,
    current_user: User = Depends(get_required_user),
):
    tenant_id = await _current_tenant(current_user)
    try:
        result = await diary.write_diary(tenant_id, current_user.uid, entry_date, str(payload.get("content") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return result


# ---------------------------------------------------------------- P7：G6 卡片/综述


@literature.get("/papers/{rid}/cards")
async def list_cards_endpoint(
    rid: str,
    card_type: str = Query(default=""),
    current_user: User = Depends(get_required_user),
):
    tenant_id = await _current_tenant(current_user)
    return {"cards": await cards.list_cards(tenant_id, rid, card_type=card_type or None)}


@literature.post("/papers/{rid}/cards", status_code=201)
async def write_card_endpoint(rid: str, payload: dict, current_user: User = Depends(get_required_user)):
    tenant_id = await _current_tenant(current_user)
    try:
        result = await cards.write_card(
            tenant_id,
            rid,
            card_type=str(payload.get("card_type") or ""),
            content=str(payload.get("content") or ""),
            title=str(payload.get("title") or ""),
            para_ids=payload.get("para_ids") or [],
            tags=payload.get("tags") or [],
            slug=payload.get("slug"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": 201, **result}


@literature.get("/reports")
async def list_reports_endpoint(current_user: User = Depends(get_required_user)):
    tenant_id = await _current_tenant(current_user)
    return {"reports": await cards.list_reports(tenant_id)}


@literature.post("/reports/generate", status_code=202)
async def generate_report_endpoint(payload: dict, current_user: User = Depends(get_required_user)):
    """综述生成（多篇 L1 聚合 → LLM；异步产物经 reports 列表查询）。"""
    tenant_id = await _current_tenant(current_user)
    rids = [str(r) for r in (payload.get("rids") or [])]
    title = str(payload.get("title") or "文献综述")
    try:
        result = await cards.generate_review_report(
            tenant_id, rids, title=title, created_by=getattr(current_user, "uid", "")
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": 202, **result}


# ---------------------------------------------------------------- P7：G4 复核


@literature.post("/papers/{rid}/review/dual-channel", status_code=202)
async def enqueue_dual_channel(rid: str, current_user: User = Depends(get_required_user)):
    """触发双通道渲染（可选增强；凭据未配置 fail-fast 422）。"""
    tenant_id = await _current_tenant(current_user)
    detail = await readops.get_paper_detail(tenant_id, rid)
    if detail is None:
        raise HTTPException(status_code=404, detail="文献不存在或无权访问")
    revision_id = detail.get("source_revision_id")
    if not revision_id:
        raise HTTPException(status_code=409, detail="文献未关联解析版本")
    try:
        result = await review.enqueue_dual_channel_render(revision_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": 202, **result}


@literature.get("/papers/{rid}/review")
async def get_review_endpoint(rid: str, current_user: User = Depends(get_required_user)):
    tenant_id = await _current_tenant(current_user)
    detail = await readops.get_paper_detail(tenant_id, rid)
    if detail is None:
        raise HTTPException(status_code=404, detail="文献不存在或无权访问")
    revision_id = detail.get("source_revision_id")
    if not revision_id:
        raise HTTPException(status_code=409, detail="文献未关联解析版本")
    try:
        return await review.get_review_items(tenant_id, revision_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@literature.post("/papers/{rid}/review/decide")
async def decide_review_endpoint(rid: str, payload: dict, current_user: User = Depends(get_required_user)):
    """四选一仲裁（A/B/C/D；幂等 upsert）。"""
    tenant_id = await _current_tenant(current_user)
    detail = await readops.get_paper_detail(tenant_id, rid)
    if detail is None:
        raise HTTPException(status_code=404, detail="文献不存在或无权访问")
    revision_id = detail.get("source_revision_id") or ""
    try:
        result = await review.decide(
            tenant_id,
            revision_id,
            report_idx=int(payload.get("report_idx", -1)),
            choice=str(payload.get("choice") or ""),
            resolved_text=payload.get("resolved_text"),
            para_id=str(payload.get("para_id") or ""),
            decided_by=getattr(current_user, "uid", ""),
            legacy_ack=bool(payload.get("legacy_ack")),
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return result


# ---------------------------------------------------------------- P7：G3 图谱


@literature.get("/graph/network")
async def graph_network_endpoint(
    min_citations: int = Query(default=0, ge=0),
    limit: int = Query(default=300, ge=1, le=5000),
    current_user: User = Depends(get_required_user),
):
    tenant_id = await _current_tenant(current_user)
    return await graph_service.graph_network(tenant_id, min_citations=min_citations, limit=limit)


@literature.get("/graph/neighbors/{rid}")
async def graph_neighbors_endpoint(rid: str, current_user: User = Depends(get_required_user)):
    tenant_id = await _current_tenant(current_user)
    return await graph_service.graph_neighbors(tenant_id, rid)


@literature.get("/graph/top")
async def graph_top_endpoint(
    limit: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(get_required_user),
):
    tenant_id = await _current_tenant(current_user)
    return {"top": await graph_service.graph_top(tenant_id, limit=limit)}


# ---------------------------------------------------------------- P7.1：修正回放 / AI 建议 / 图谱投影


@literature.post("/papers/{rid}/review/apply")
async def apply_review_endpoint(rid: str, current_user: User = Depends(get_required_user)):
    """修正回放：裁决 → document_resolved + render_resolved（原始产物保留审计）。"""
    tenant_id = await _current_tenant(current_user)
    detail = await readops.get_paper_detail(tenant_id, rid)
    if detail is None:
        raise HTTPException(status_code=404, detail="文献不存在或无权访问")
    revision_id = detail.get("source_revision_id") or ""
    try:
        manifest = await review.apply_decisions(tenant_id, revision_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return manifest


@literature.get("/papers/{rid}/review/resolved")
async def resolved_manifest_endpoint(rid: str, current_user: User = Depends(get_required_user)):
    tenant_id = await _current_tenant(current_user)
    detail = await readops.get_paper_detail(tenant_id, rid)
    if detail is None:
        raise HTTPException(status_code=404, detail="文献不存在或无权访问")
    revision_id = detail.get("source_revision_id") or ""
    manifest = await review.get_resolved_manifest(tenant_id, revision_id)
    return {"resolved": manifest is not None, "manifest": manifest}


@literature.post("/papers/{rid}/review/suggest")
async def suggest_review_endpoint(rid: str, payload: dict, current_user: User = Depends(get_required_user)):
    """AI 仲裁建议（人机回环：只建议，裁决仍需人工四选一）。"""
    tenant_id = await _current_tenant(current_user)
    detail = await readops.get_paper_detail(tenant_id, rid)
    if detail is None:
        raise HTTPException(status_code=404, detail="文献不存在或无权访问")
    revision_id = detail.get("source_revision_id") or ""
    try:
        result = await review.ai_suggest(
            tenant_id,
            revision_id,
            int(payload.get("report_idx", -1)),
            requested_by=current_user.uid,
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return result


@literature.post("/graph/project", status_code=202)
async def project_graph_endpoint(current_user: User = Depends(get_required_user)):
    """引用图投影到 Neo4j（PG 仍为 canonical；多跳/图治理消费）。"""
    tenant_id = await _current_tenant(current_user)
    try:
        result = await graph_service.project_to_neo4j(tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": 202, **result}


# ---------------------------------------------------------------- P8：阅读器 / 概念页 / 主题 MOC


@literature.get("/papers/{rid}/reader")
async def get_reader_bundle_endpoint(rid: str, current_user: User = Depends(get_required_user)):
    """阅读包：原文 markdown（图片 URL 已重写为鉴权端点；修正版优先）。"""
    tenant_id = await _current_tenant(current_user)
    bundle = await reader_bundle.get_reader_bundle(tenant_id, rid)
    if bundle is None:
        raise HTTPException(status_code=404, detail="文献不存在或未关联解析版本")
    return bundle


@literature.get("/papers/{rid}/variants/{variant_kind}")
async def get_variant_endpoint(rid: str, variant_kind: str, current_user: User = Depends(get_required_user)):
    """译文内容（zh/en_zh/summary，直接文本；对照阅读由前端分段对齐）。"""
    tenant_id = await _current_tenant(current_user)
    if variant_kind not in ("zh", "en_zh", "summary"):
        raise HTTPException(status_code=422, detail=f"非法变体类型: {variant_kind}")
    result = await reader_bundle.get_variant_content(tenant_id, rid, variant_kind)
    if result is None:
        raise HTTPException(status_code=404, detail="文献不存在")
    return result


@literature.get("/papers/{rid}/parallel")
async def get_parallel_endpoint(rid: str, current_user: User = Depends(get_required_user)):
    """Return source and translation paragraphs aligned by stable paragraph id."""
    tenant_id = await _current_tenant(current_user)
    result = await reader_bundle.get_parallel_content(tenant_id, rid)
    if result is None:
        raise HTTPException(status_code=404, detail="文献不存在")
    return result


@literature.get("/papers/{rid}/source-pdf")
async def get_source_pdf_endpoint(rid: str, current_user: User = Depends(get_required_user)):
    """源 PDF 代理（原始上传件；inline 展示）。"""
    tenant_id = await _current_tenant(current_user)
    result = await reader_bundle.get_source_pdf(tenant_id, rid)
    if result is None:
        raise HTTPException(status_code=404, detail="源 PDF 不可用（文献未关联解析版本或对象缺失）")
    data, _object = result
    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{rid}.pdf"', "Cache-Control": "private, max-age=300"},
    )


@literature.get("/concepts")
async def list_concepts_endpoint(current_user: User = Depends(get_required_user)):
    """概念页聚合（≥2 文献成页；≥3 标 mature；L1_data.json 优先、笔记解析兜底）。"""
    tenant_id = await _current_tenant(current_user)
    return {"concepts": await concepts.aggregate_concepts(tenant_id)}


@literature.post("/reports/topic-moc", status_code=202)
async def generate_topic_moc_endpoint(payload: dict, current_user: User = Depends(get_required_user)):
    """主题 MOC 生成（概念/标题匹配 → L1 聚合 → LLM 导读；落 reports）。"""
    tenant_id = await _current_tenant(current_user)
    try:
        result = await concepts.generate_topic_moc(
            tenant_id, topic=str(payload.get("topic") or ""), created_by=getattr(current_user, "uid", "")
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": 202, **result}
