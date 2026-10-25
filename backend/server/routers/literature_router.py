"""文献域 HTTP 路由（P6a，薄层：请求解析/鉴权/装配，无业务逻辑）。

- 租户权威 `PrincipalContext`：请求体 tenant_id 一律忽略（AGENTS.md 硬性）；
- 渲染/翻译等长任务一律 202 + job 投影，不同步等待；
- 产物只返 kbasset:// 逻辑 URI，字节经 /api/literature/assets 鉴权解析。
"""

from __future__ import annotations

import json
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from yuxi.literature import (
    answer_cache,
    artifacts,
    bib_import,
    cards,
    diary,
    export_vault,
    graph_service,
    management,
    qna,
    reader_bundle,
    readops,
    retrieval,
    review,
)
from yuxi.literature import enrich as enrich_module
from yuxi.literature import metrics as metrics_module
from yuxi.literature import session as lit_session
from yuxi.literature.access import access_scope, asset_access_scope, authorize_asset, paper_visibility
from yuxi.literature.attachments import register_attachment
from yuxi.literature.reading.service import RENDER_BUCKET, enqueue_reader_render
from yuxi.services.principal import resolve_tenant_id
from yuxi.storage.minio import get_minio_client
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import User

from server.utils.auth_middleware import get_admin_user, get_required_user, require_scope


async def authorize_literature_request(
    request: Request, response: Response, current_user: User = Depends(get_required_user)
):
    response.headers["Cache-Control"] = "private, no-store"
    await require_scope("literature:read" if request.method in ("GET", "HEAD") else "literature:write")(request)
    tenant_id = await _current_tenant(current_user)
    rid = request.path_params.get("rid")
    route = request.scope.get("route")
    selection = {}
    if route and route.name in {"list_papers", "list_tasks"}:
        selection = {"catalog_only": True}
    elif route and "/literature/papers/{rid}" in route.path:
        selection = {"rids": (rid,)}
    elif route and (
        "/literature/pdf-translation/" in route.path
        or route.name in {"get_translation_settings", "save_translation_settings", "list_output_templates"}
    ):
        selection = {"rids": ()}
    manager = access_scope(current_user, tenant_id, **selection)
    if route and route.name == "get_asset":
        manager = asset_access_scope(
            current_user, tenant_id, request.query_params.get("uri", "").removeprefix("kbasset://")
        )
    async with manager as scope:
        if request.scope.get("route") and request.scope["route"].name in {
            "restore_library_paper",
            "trash_library_paper",
        }:
            # The normal scope intentionally excludes trash. Restoration and
            # repeat-trash must recheck the underlying source ACL including
            # trash in their service (409 "already trashed" vs 404).
            yield scope
            return
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


async def _read_bounded_upload(file: UploadFile, *, limit: int) -> bytes:
    """Bound allocation before parsing, including uploads without Content-Length."""
    chunks = []
    size = 0
    while chunk := await file.read(min(1024 * 1024, limit - size + 1)):
        size += len(chunk)
        if size > limit:
            raise HTTPException(status_code=413, detail=f"文件超过大小限制（{limit // (1024 * 1024)} MiB）")
        chunks.append(chunk)
    return b"".join(chunks)


@literature.get("/papers")
async def list_papers(
    query: str = Query(default="", max_length=500),
    kind: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    journal: str = Query(default="", max_length=512),
    quartile: str = Query(default="", max_length=64),
    min_if: float | None = Query(default=None, ge=0, le=1000),
    year_min: int | None = Query(default=None, ge=1000, le=9999),
    year_max: int | None = Query(default=None, ge=1000, le=9999),
    compiled_level: str = Query(default=""),
    source: str = Query(default=""),
    sort: str = Query(default="updated"),
    current_user: User = Depends(get_required_user),
):
    tenant_id = await _current_tenant(current_user)
    try:
        return await readops.list_papers(
            tenant_id,
            query=query,
            kind=kind,
            page=page,
            page_size=page_size,
            journal=journal,
            quartile=quartile,
            min_if=min_if,
            year_min=year_min,
            year_max=year_max,
            compiled_level=compiled_level,
            source=source,
            sort=sort,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@literature.get("/resolve-link")
async def resolve_reader_link(
    target: str = Query(min_length=1, max_length=500),
    source_rid: str = Query(default="", max_length=255),
    current_user: User = Depends(get_required_user),
):
    from yuxi.literature.management import resolve_reader_target

    try:
        return await resolve_reader_target(await _current_tenant(current_user), target, source_rid=source_rid)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class HistoryMappingRetry(BaseModel):
    expected_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_id: UUID


@literature.get("/papers/{rid}")
async def get_paper(rid: str, current_user: User = Depends(get_required_user)):
    tenant_id = await _current_tenant(current_user)
    detail = await readops.get_paper_detail(tenant_id, rid)
    if detail is None:
        raise HTTPException(status_code=404, detail="文献不存在或无权访问")
    return detail


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
    content = await _read_bounded_upload(file, limit=50 * 1024 * 1024)
    try:
        result = await register_attachment(
            tenant_id=tenant_id, paper_id=paper_id, kind=kind, filename=file.filename or "", content=content
        )
    except PermissionError as exc:
        raise HTTPException(status_code=404, detail="文献不存在或无权访问，附件未登记") from exc
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
    async with asset_access_scope(current_user, tenant_id, object_name):
        if not await authorize_asset(object_name):
            raise HTTPException(status_code=404, detail="资产来源已变更或无权访问")
    suffix = "." + object_name.rsplit(".", 1)[-1].lower() if "." in object_name else ""
    return Response(
        content=data,
        media_type=_ASSET_CONTENT_TYPES.get(suffix, "application/octet-stream"),
        headers={"Cache-Control": "private, no-store"},
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
    text = (await _read_bounded_upload(file, limit=10 * 1024 * 1024)).decode("utf-8", errors="replace")
    report = await bib_import.import_bib(tenant_id, text, source_name=file.filename or "")
    return {"status": 202, **report}


@literature.post("/papers/import/markdown", status_code=201)
async def import_markdown_endpoint(
    payload: dict,
    current_user: User = Depends(get_required_user),
):
    """Markdown 直导（免解析、0 API；无 DOI 时指纹身份）。"""
    tenant_id = await _current_tenant(current_user)
    markdown = payload.get("markdown") or ""
    title = payload.get("title") or ""
    doi = payload.get("doi") or ""
    if not all(isinstance(value, str) for value in (markdown, title, doi)):
        raise HTTPException(status_code=422, detail="Markdown、标题与 DOI 必须是文本")
    if len(markdown.encode("utf-8")) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Markdown 不得超过 5 MiB")
    if len(title) > 4096 or len(doi) > 512:
        raise HTTPException(status_code=422, detail="标题或 DOI 超过长度限制")
    try:
        result = await bib_import.import_markdown_document(
            tenant_id,
            markdown=markdown,
            title=title,
            doi=doi,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": 201, **result}


@literature.post("/papers/import/references", status_code=202)
async def import_references_endpoint(
    file: UploadFile,
    format: Literal[
        "auto",
        "pubmed-nbib",
        "pubmed-csv",
        "wos-txt",
        "wos-csv",
        "cnki-xls",
        "wanfang-xlsx",
        "ris",
        "endnote",
        "bibtex",
    ] = Query(default="auto"),
    current_user: User = Depends(get_required_user),
):
    """文献清单导入（PubMed .nbib / WoS Tab-delimited / RIS / EndNote；幂等：DOI/PMID/指纹已存在跳过）。"""
    from yuxi.literature import reference_import

    tenant_id = await _current_tenant(current_user)
    data = await _read_bounded_upload(file, limit=reference_import.MAX_REFERENCE_BYTES)
    try:
        report = await reference_import.import_references(tenant_id, data, fmt=format, source_name=file.filename or "")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": 202, **report}


# ---------------------------------------------------------------- 期刊指标（JCR / 中科院）


#: 期刊指标导入是租户级共享目录的写入（影响同租户所有人），因此要求管理员角色；
#: 预览只读本人上传的文件解析结果，普通登录用户即可。
@literature.post("/journals/preview")
async def preview_journals_table_endpoint(
    file: UploadFile,
    current_user: User = Depends(get_required_user),
):
    """JCR/中科院指标表预览（只回摘要：sheet/年份/列/行数 + 样例 ≤2 行，不 dump 全文）。"""
    from yuxi.literature import journals

    data = await _read_bounded_upload(file, limit=journals.MAX_JOURNAL_FILE_BYTES)
    try:
        return journals.preview_journals_table(data, file.filename or "")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@literature.post("/journals/import", status_code=201)
async def import_journals_table_endpoint(
    file: UploadFile,
    current_user: User = Depends(get_admin_user),
):
    """导入 JCR/中科院指标表（幂等 upsert；租户来自 PrincipalContext，请求体 tenant_id 忽略）。"""
    from yuxi.literature import journals

    tenant_id = await _current_tenant(current_user)
    data = await _read_bounded_upload(file, limit=journals.MAX_JOURNAL_FILE_BYTES)
    try:
        report = await journals.import_journals_table(tenant_id, data, file.filename or "")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": 201, **report}


@literature.get("/journals/status")
async def journals_status_endpoint(current_user: User = Depends(get_required_user)):
    """期刊映射 / IF 覆盖快照（对齐上游 /api/lit/status 的映射数与覆盖数）。"""
    from yuxi.literature import journals

    return await journals.journal_metrics_status(await _current_tenant(current_user))


# ---------------------------------------------------------------- P7：G1 富化


@literature.post("/papers/{rid}/enrich", status_code=202)
async def enrich_paper_endpoint(rid: str, current_user: User = Depends(get_required_user)):
    """富化单篇（Crossref/OpenAlex/S2 免费源，只填空字段）。"""
    tenant_id = await _current_tenant(current_user)
    result = await enrich_module.enrich_paper(tenant_id, rid)
    return {"status": 202, **result}


@literature.get("/metrics/quality")
async def get_quality_metrics(
    days: int = Query(default=7, ge=1, le=90),
    current_user: User = Depends(get_admin_user),
):
    """文献域质量指标（拒绝率/降级率/修复成功率 + 任务/入库健康），投产观测口径。"""
    from yuxi.literature.quality_metrics import quality_metrics

    return await quality_metrics(await _current_tenant(current_user), days=days)


def _bibliometric_filters(
    batch_ids: list[str] = Query(default=[], max_length=50),
    only_imports: bool = Query(default=False),
    database: Literal["", "pubmed", "wos", "cnki", "wanfang", "other"] = Query(default=""),
    query: str = Query(default="", max_length=200),
    year_from: int | None = Query(default=None, ge=1500, le=2100),
    year_to: int | None = Query(default=None, ge=1500, le=2100),
):
    return {
        "batch_ids": batch_ids,
        "only_imports": only_imports,
        "database": database,
        "query": query,
        "year_from": year_from,
        "year_to": year_to,
    }


@literature.get("/reference-batches")
async def list_reference_batches_endpoint(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1000000),
    current_user: User = Depends(get_required_user),
):
    from yuxi.literature.reference_batches import list_batches

    tenant_id = await _current_tenant(current_user)
    return await list_batches(tenant_id, limit=limit, offset=offset)


@literature.get("/metrics/sources")
async def get_bibliometric_sources_endpoint(current_user: User = Depends(get_required_user)):
    from yuxi.literature.reference_batches import list_sources

    try:
        return await list_sources(await _current_tenant(current_user))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@literature.get("/metrics/records")
async def get_bibliometric_records_endpoint(
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1000000),
    filters: dict = Depends(_bibliometric_filters),
    current_user: User = Depends(get_required_user),
):
    from yuxi.literature.reference_batches import list_records

    tenant_id = await _current_tenant(current_user)
    try:
        return await list_records(tenant_id, limit=limit, offset=offset, **filters)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@literature.get("/metrics/bibliometrics")
async def get_bibliometrics_endpoint(
    journals_top: int = Query(default=20, ge=1, le=100),
    filters: dict = Depends(_bibliometric_filters),
    current_user: User = Depends(get_required_user),
):
    """文献计量总览（趋势/期刊/机构/国家；同清单范围，排除参考条目/回收站，50000 篇预算）。"""
    from yuxi.literature import bibliometrics

    tenant_id = await _current_tenant(current_user)
    try:
        return await bibliometrics.bibliometrics(tenant_id, journals_top=journals_top, **filters)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@literature.get("/metrics/networks")
async def get_metric_networks_endpoint(
    network_type: Literal["keyword", "author"] = Query(default="keyword", alias="type"),
    top: int = Query(default=50, ge=5, le=200),
    min_pair: int = Query(default=2, ge=1, le=100),
    filters: dict = Depends(_bibliometric_filters),
    current_user: User = Depends(get_required_user),
):
    """共现网络数据（关键词/作者：节点=频次 TopN，边=同篇共现次数 ≥ min_pair）。"""
    from yuxi.literature import bibliometrics

    tenant_id = await _current_tenant(current_user)
    try:
        return await bibliometrics.metric_network(
            tenant_id,
            field="keywords" if network_type == "keyword" else "authors",
            top=top,
            min_pair=min_pair,
            **filters,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@literature.post("/ingest/{revision_id}/retry", status_code=202)
async def retry_failed_ingest(
    revision_id: str,
    current_user: User = Depends(get_admin_user),
):
    """死信人工恢复：FAILED/REJECTED 终态解析版本复位并重新入队（管理员操作）。"""
    from yuxi.services.scientific_pdf_ingest_service import request_scientific_pdf_retry

    tenant_id = await _current_tenant(current_user)
    try:
        result = await request_scientific_pdf_retry(
            revision_id=revision_id, tenant_id=tenant_id, operator_id=current_user.uid
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": 202, **result}


@literature.post("/enrich/pending", status_code=202)
async def enrich_pending_endpoint(
    batch_size: int = Query(default=50, ge=1, le=200),
    current_user: User = Depends(get_required_user),
):
    """批量富化缺元数据文献（202 入队后台执行；逐篇串行外部调用不再阻塞 HTTP）。"""
    tenant_id = await _current_tenant(current_user)
    try:
        result = await enrich_module.enqueue_enrich_pending(tenant_id, batch_size=batch_size)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - Redis 入队不可用时不假装已受理
        raise HTTPException(status_code=503, detail=f"批量富化入队失败，请稍后重试: {type(exc).__name__}") from exc
    return {"status": 202, **result}


# ---------------------------------------------------------------- P7：G5 日记


@literature.get("/diary/days")
async def diary_days(
    month: str = Query(default=""),
    current_user: User = Depends(get_required_user),
):
    tenant_id = await _current_tenant(current_user)
    if not month:
        return {"days": await diary.list_diary_days(tenant_id, current_user.uid)}
    try:
        return await diary.activity_calendar(tenant_id, current_user.uid, month)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@literature.get("/diary/export")
async def export_diary_month(month: str = Query(...), current_user: User = Depends(get_required_user)):
    try:
        content = await diary.export_month(await _current_tenant(current_user), current_user.uid, month)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return Response(
        content=content,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="reading-diary-{month}.md"'},
    )


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


@literature.get("/cards")
async def list_global_cards(current_user: User = Depends(get_required_user)):
    return {"cards": await cards.list_cards(await _current_tenant(current_user), "")}


@literature.post("/cards", status_code=201)
async def write_global_card(payload: dict, current_user: User = Depends(get_required_user)):
    return await write_card_endpoint("", payload, current_user)


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
            overwrite=payload.get("overwrite") is True,
            expected_digest=str(payload.get("expected_digest") or ""),
        )
    except cards.CardConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": 201, **result}


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


@literature.get("/papers/{rid}/review/page-image")
async def get_review_page_image_endpoint(
    rid: str,
    page: int = Query(..., ge=1, description="1 起的 PDF 页码"),
    require_dual: bool = Query(False, description="是否限定双通道渲染版本"),
    current_user: User = Depends(get_required_user),
):
    """复核页图 + 该页差异框（对标上游整页 PNG + 红框高亮）。

    与 ``GET /papers/{rid}/review`` 同口径做租户/来源校验；页图字节仍走
    ``GET /api/literature/assets?uri=kbasset://...`` 鉴权代理，本端点只给
    URI 与未烘焙的 bbox 列表——红框由前端叠加，避免同一页图重复渲染存储。
    """
    tenant_id = await _current_tenant(current_user)
    detail = await readops.get_paper_detail(tenant_id, rid)
    if detail is None:
        raise HTTPException(status_code=404, detail="文献不存在或无权访问")
    revision_id = detail.get("source_revision_id")
    if not revision_id:
        raise HTTPException(status_code=409, detail="文献未关联解析版本")
    try:
        return await review.get_review_page_image(tenant_id, revision_id, page, require_dual=require_dual)
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
            item_id=str(payload.get("item_id") or ""),
            render_id=str(payload.get("render_id") or ""),
            render_digest=str(payload.get("render_digest") or ""),
            require_binding=True,
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return result


# ---------------------------------------------------------------- P7：G3 图谱


@literature.get("/graph/network")
async def graph_network_endpoint(
    min_citations: int = Query(default=0, ge=0),
    limit: int = Query(default=300, ge=1, le=5000),
    year_min: int | None = Query(default=None, ge=1000, le=9999),
    year_max: int | None = Query(default=None, ge=1000, le=9999),
    min_library_citations: int = Query(default=0, ge=0),
    min_times_cited: int = Query(default=0, ge=0),
    min_impact_factor: float = Query(default=0, ge=0, allow_inf_nan=False),
    quartiles: str = Query(default="", max_length=64),
    cluster: int | None = Query(default=None, ge=0),
    in_kb_only: bool = False,
    exclude_references: bool = False,
    exclude_isolated: bool = True,
    citation_source: Literal["filtered", "library", "wos"] = "filtered",
    sort_by: Literal["library_citations", "times_cited", "impact_factor", "paper_rank", "year"] = "library_citations",
    preview: bool = False,
    current_user: User = Depends(get_required_user),
):
    tenant_id = await _current_tenant(current_user)
    try:
        return await graph_service.graph_network(
            tenant_id,
            min_citations=min_citations,
            limit=limit,
            year_min=year_min,
            year_max=year_max,
            min_library_citations=min_library_citations,
            min_times_cited=min_times_cited,
            min_impact_factor=min_impact_factor,
            quartiles=[value.strip() for value in quartiles.split(",") if value.strip()],
            cluster=cluster,
            in_kb_only=in_kb_only,
            exclude_references=exclude_references,
            exclude_isolated=exclude_isolated,
            citation_source=citation_source,
            sort_by=sort_by,
            preview=preview,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


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
    return JSONResponse(bundle, headers={"Cache-Control": "private, no-store"})


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
        headers={"Content-Disposition": f'inline; filename="{rid}.pdf"', "Cache-Control": "private, no-store"},
    )


# ---------------------------------------------------------------- P10-B/C/D：产物版本 / 处理编排 / 检索


@literature.get("/papers/{rid}/document")
async def get_effective_document(rid: str, current_user: User = Depends(get_required_user)):
    """当前有效正文（人工冻结→裁决→渲染→导入，artifacts 统一入口）。"""
    tenant_id = await _current_tenant(current_user)
    result = await artifacts.resolve_effective_document(tenant_id, rid)
    if result is None:
        raise HTTPException(status_code=404, detail="文献无任何正文产物")
    return {
        "source": result["source"],
        "revision_id": result["revision_id"],
        "frozen": result["frozen"],
        "digest": result["digest"],
        "source_revision_id": result["source_revision_id"],
        "reading_version": result["reading_version"],
        "document": json.loads(result["content"]),
        "expected_revision": result["revision_id"] if result["revision_id"].startswith("lar_") else None,
    }


@literature.put("/papers/{rid}/document")
async def save_document_revision(rid: str, payload: dict, current_user: User = Depends(get_required_user)):
    """人工保存正文修订（乐观并发：expected_revision 不匹配返回 409）。"""
    tenant_id = await _current_tenant(current_user)
    if not {"expected_revision", "expected_source_revision", "expected_reading_version"} <= payload.keys():
        raise HTTPException(status_code=422, detail="必须提交正文指针、来源和阅读版本的并发校验字段")
    try:
        document = payload.get("document", payload.get("content"))
        content = json.dumps(document, ensure_ascii=False) if isinstance(document, dict) else str(document or "")
        result = await artifacts.store_revision(
            tenant_id,
            rid,
            artifacts.ArtifactKind.DOCUMENT,
            content=content.encode("utf-8"),
            content_type="application/json",
            operator=getattr(current_user, "uid", ""),
            freeze=bool(payload.get("freeze", True)),
            expected_current_revision=payload.get("expected_revision"),
            expected_source_revision=payload.get("expected_source_revision", artifacts._UNSPECIFIED),
            expected_reading_version=payload.get("expected_reading_version", artifacts._UNSPECIFIED),
        )
    except artifacts.RevisionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return result


@literature.get("/search/catalog")
async def search_catalog_endpoint(
    query: str = Query(default="", max_length=500),
    kind: str = Query(default=""),
    order_by: str = Query(default="relevance"),
    limit: int = Query(default=20, ge=1, le=100),
    use_cache: bool = Query(default=True),
    rerank_model: str = Query(default="", max_length=512),
    current_user: User = Depends(get_required_user),
):
    """目录检索（元数据；相关度/被引/年份/PaperRank 排序）。"""
    tenant_id = await _current_tenant(current_user)
    try:
        return {
            "results": await retrieval.search_catalog(
                tenant_id,
                query,
                kind=kind,
                order_by=order_by,
                limit=limit,
                use_cache=use_cache,
                rerank_model=rerank_model,
            )
        }
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# ---------------------------------------------------------------- P11：六步建库 / Obsidian 导出 / 专属会话 / 缓存


@literature.post("/indexes/navigation", status_code=202)
async def build_navigation_index(payload: dict, current_user: User = Depends(get_required_user)):
    from yuxi.literature.navigation_index import enqueue_navigation_index

    tenant_id = await _current_tenant(current_user)
    try:
        return await enqueue_navigation_index(
            tenant_id, requested_by=current_user.uid, model_spec=str(payload.get("model_spec") or "")
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@literature.post("/library/journal-metrics")
async def attach_journal_metrics_endpoint(current_user: User = Depends(get_required_user)):
    """关联期刊指标（对齐上游「关联期刊指标（写入 IF / 分区）」按钮）。

    幂等：最新年份优先、只填空、人工锁定（meta_locked）文献受保护；不产生新的解析/证据。
    """
    tenant_id = await _current_tenant(current_user)
    return await metrics_module.attach_journal_metrics(tenant_id)


@literature.post("/library/cleaning/preview")
async def preview_library_cleaning(payload: dict, current_user: User = Depends(get_required_user)):
    from yuxi.literature.cleaning import preview_cleaning

    try:
        return await preview_cleaning(
            await _current_tenant(current_user),
            rules=payload.get("rules") or {},
            include_mains=payload.get("include_mains") is True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@literature.post("/library/cleaning/{plan_id}/confirm")
async def confirm_library_cleaning(plan_id: str, payload: dict, current_user: User = Depends(get_required_user)):
    from yuxi.literature.cleaning import confirm_cleaning

    try:
        return await confirm_cleaning(await _current_tenant(current_user), plan_id, str(payload.get("plan_hash") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@literature.get("/library/trash")
async def library_trash(current_user: User = Depends(get_required_user)):
    from yuxi.literature.cleaning import list_trash

    return {"items": await list_trash(await _current_tenant(current_user))}


@literature.post("/papers/{rid}/trash")
async def trash_library_paper(rid: str, payload: dict, current_user: User = Depends(get_required_user)):
    """手动移入回收站（可恢复软删除，永不物理删除）。

    - 原因必填进审计（trashed_by/trashed_reason）；
    - 目录/检索/图谱立即隐藏；不触碰知识源证据与解析版本；
    - 重复导入同一文件不自动恢复，恢复是回收站里的显式动作。
    """
    tenant_id = await _current_tenant(current_user)
    from yuxi.literature.cleaning import trash_paper

    try:
        return await trash_paper(tenant_id, rid, reason=str(payload.get("reason") or ""))
    except ValueError as exc:
        message = str(exc)
        status = 404 if ("不存在" in message or "撤销" in message) else (409 if "已在回收站" in message else 422)
        raise HTTPException(status_code=status, detail=message) from exc


@literature.post("/library/trash/{rid}/restore")
async def restore_library_paper(rid: str, current_user: User = Depends(get_required_user)):
    from yuxi.literature.cleaning import restore_paper

    try:
        return await restore_paper(await _current_tenant(current_user), rid)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@literature.get("/indexes/navigation/{index_id}")
async def navigation_index_status(index_id: str, current_user: User = Depends(get_required_user)):
    from sqlalchemy import select
    from yuxi.storage.postgres.models_knowledge import LiteratureNavigationIndex

    tenant_id = await _current_tenant(current_user)
    async with pg_manager.get_async_session_context() as db:
        row = (
            await db.execute(
                select(LiteratureNavigationIndex).where(
                    LiteratureNavigationIndex.index_id == index_id,
                    LiteratureNavigationIndex.tenant_id == tenant_id,
                    LiteratureNavigationIndex.requested_by == current_user.uid,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="导航索引任务不存在")
        return {
            "index_id": row.index_id,
            "status": row.status,
            "attempt": row.attempt,
            "document_count": row.document_count,
            "error": row.error,
        }


@literature.get("/export/obsidian")
async def export_obsidian(
    rids: list[str] | None = Query(default=None), current_user: User = Depends(get_required_user)
):
    """Obsidian 兼容导出（zip：笔记+译文+图片+双链+清单）。"""
    tenant_id = await _current_tenant(current_user)
    if rids is not None and (not rids or len(rids) > 1000 or any(not rid or len(rid) > 160 for rid in rids)):
        raise HTTPException(status_code=422, detail="导出 rids 必须为 1–1000 个有界资源键")
    try:
        data = await export_vault.export_obsidian_vault(tenant_id, rids=rids)
    except export_vault.ExportLimitError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="literature_vault.zip"'},
    )


@literature.get("/export/business-backup")
async def export_business_backup(current_user: User = Depends(get_required_user)):
    """当前有权业务快照；不导出凭据、ACL、账本或活跃任务。"""
    from yuxi.literature.backup import export_business_backup as export_backup

    tenant_id = await _current_tenant(current_user)
    try:
        data = await export_backup(tenant_id, current_user.uid)
    except export_vault.ExportLimitError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="literature_business_backup.zip"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@literature.post("/sessions/paper/{rid}")
async def create_paper_session(rid: str, current_user: User = Depends(get_required_user)):
    """文献专属会话：冻结该文献上下文（笔记状态+摘要+附件清单）。"""
    tenant_id = await _current_tenant(current_user)
    try:
        context = await lit_session.create_literature_conversation(
            tenant_id=tenant_id, user=current_user, kind="paper", rid=rid
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return context


@literature.post("/sessions/library")
async def create_library_session(current_user: User = Depends(get_required_user)):
    """全库会话（检索/管理操作上下文）。"""
    tenant_id = await _current_tenant(current_user)
    return await lit_session.create_literature_conversation(tenant_id=tenant_id, user=current_user, kind="lit")


@literature.post("/sessions")
async def create_literature_session(payload: dict, current_user: User = Depends(get_required_user)):
    tenant_id = await _current_tenant(current_user)
    try:
        return await lit_session.create_literature_conversation(
            tenant_id=tenant_id,
            user=current_user,
            kind=str(payload.get("kind") or "global-qa"),
            rid=str(payload.get("rid") or ""),
            agent_slug=str(payload.get("agent_slug") or "default-chatbot"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@literature.post("/cache/invalidate/{rid}")
async def invalidate_paper_cache(rid: str, current_user: User = Depends(get_required_user)):
    """文献变更后失效相关答案缓存（权限/版本安全兜底）。"""
    await _current_tenant(current_user)  # 权限检查
    count = await answer_cache.invalidate_for_paper(rid)
    return {"invalidated": count}


def _search_cache_pattern(tenant_id: int, *, global_scope: bool = False) -> str:
    # v3 键带租户前缀；全局模式仅供超级管理员维护使用（跨租户影响面显式化）。
    return "yuxi:lit:search:*" if global_scope else f"yuxi:lit:search:t{tenant_id}:*"


async def _scan_search_cache_keys(pattern: str) -> list[str]:
    from yuxi.storage.redis import get_async_redis_client

    client = await get_async_redis_client()
    if not client:
        return []
    return [key async for key in client.scan_iter(match=pattern, count=200)]


@literature.get("/search/cache/stats")
async def search_cache_stats(
    scope: str = Query(default="tenant", pattern="^(tenant|global)$"),
    current_user: User = Depends(get_admin_user),
):
    """检索缓存统计（租户范围；global 仅超级管理员，避免跨租户干扰面）。"""
    tenant_id = await _current_tenant(current_user)
    global_scope = scope == "global" and getattr(current_user, "role", "") == "superadmin"
    keys = await _scan_search_cache_keys(_search_cache_pattern(tenant_id, global_scope=global_scope))
    return {
        "entries": len(keys),
        "ttl_seconds": 300,
        "pattern": _search_cache_pattern(tenant_id, global_scope=global_scope),
    }


@literature.post("/search/cache/clear")
async def clear_search_cache(
    scope: str = Query(default="tenant", pattern="^(tenant|global)$"),
    current_user: User = Depends(get_admin_user),
):
    """清空检索缓存（默认仅本租户；缓存键由内容版本清单派生，清空只损失热度不损失正确性）。"""
    tenant_id = await _current_tenant(current_user)
    global_scope = scope == "global" and getattr(current_user, "role", "") == "superadmin"
    from yuxi.storage.redis import get_async_redis_client

    keys = await _scan_search_cache_keys(_search_cache_pattern(tenant_id, global_scope=global_scope))
    deleted = 0
    if keys:
        client = await get_async_redis_client()
        if client:
            for chunk_start in range(0, len(keys), 200):
                deleted += int(await client.delete(*keys[chunk_start : chunk_start + 200]))
    return {"cleared": deleted}


@literature.post("/library/reconcile")
async def reconcile_library(payload: dict, current_user: User = Depends(get_admin_user)):
    """手动对账（对标 PaperAgent「同步磁盘」）：修复缺登记/缺渲染/滞留激活事件。

    dry_run 默认开——先预览待对账数量，确认后带 dry_run=false 执行；
    修复动作幂等（与每 5 分钟的 cron 同一入口），范围限定当前租户。
    """
    tenant_id = await _current_tenant(current_user)
    from yuxi.literature.reconcile import reconcile_scientific_literature_papers

    dry_run = bool(payload.get("dry_run", True))
    limit = int(payload.get("limit") or 100)
    if not 1 <= limit <= 1000:
        raise HTTPException(status_code=422, detail="limit 取值范围 1-1000")
    repaired = await reconcile_scientific_literature_papers(limit=limit, dry_run=dry_run, tenant_id=tenant_id)
    return {"dry_run": dry_run, "repaired": repaired if not dry_run else None, "pending": repaired if dry_run else 0}


class MetadataChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")
    doi: str | None = Field(default=None, max_length=256)
    title: str | None = Field(default=None, max_length=4096)
    kind: Literal["paper", "thesis", "book", "chapter", "patent", "standard"] = "paper"
    authors_json: list[Annotated[str, Field(max_length=512)]] | None = Field(default=None, max_length=200)
    abstract: str | None = Field(default=None, max_length=50000)
    journal: str | None = Field(default=None, max_length=512)
    issn: str | None = Field(default=None, max_length=32)
    pub_year: int | None = Field(default=None, ge=1000, le=9999)
    keywords_json: list[Annotated[str, Field(max_length=512)]] | None = Field(default=None, max_length=200)
    wos_categories_json: list[Annotated[str, Field(max_length=512)]] | None = Field(default=None, max_length=200)
    research_areas_json: list[Annotated[str, Field(max_length=512)]] | None = Field(default=None, max_length=200)
    meta_locked: bool = True


class MetadataUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_token: str = Field(min_length=64, max_length=64)
    changes: MetadataChanges


@literature.get("/papers/{rid}/metadata")
async def paper_metadata(rid: str, current_user: User = Depends(get_required_user)):
    try:
        return await management.get_metadata(await _current_tenant(current_user), rid)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@literature.put("/papers/{rid}/metadata")
async def update_paper_metadata(rid: str, payload: MetadataUpdate, current_user: User = Depends(get_required_user)):
    try:
        return await management.update_metadata(
            await _current_tenant(current_user),
            rid,
            expected_token=payload.expected_token,
            changes=payload.changes.model_dump(exclude_unset=True),
        )
    except management.MetadataConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@literature.get("/runs")
async def literature_runs(
    limit: int = Query(default=50, ge=1, le=100), current_user: User = Depends(get_required_user)
):
    return {"items": await management.list_runs(await _current_tenant(current_user), limit=limit)}


@literature.get("/sessions")
async def list_literature_sessions(
    archived: bool = False,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    current_user: User = Depends(get_required_user),
):
    # 多取一行只为计算 has_more；next_offset 按实际返回数推进
    items = await lit_session.list_literature_sessions(
        await _current_tenant(current_user),
        current_user.uid,
        archived=archived,
        offset=offset,
        limit=limit + 1,
    )
    has_more = len(items) > limit
    items = items[:limit]
    return {"items": items, "has_more": has_more, "next_offset": offset + len(items)}


@literature.get("/sessions/search")
async def search_literature_history(
    query: str = Query(..., min_length=1, max_length=500),
    archived: bool = False,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(get_required_user),
):
    from yuxi.literature.session_search import search_history

    try:
        return await search_history(
            await _current_tenant(current_user), current_user.uid, query, archived=archived, offset=offset, limit=limit
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


class SessionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=255)
    status: Literal["active", "archived"] | None = None


@literature.put("/sessions/{thread_id}")
async def update_literature_session(
    thread_id: str, payload: SessionUpdate, current_user: User = Depends(get_required_user)
):
    try:
        return await lit_session.update_literature_session(
            await _current_tenant(current_user), current_user.uid, thread_id, title=payload.title, status=payload.status
        )
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@literature.get("/sessions/{thread_id}/qna-preview")
async def preview_session_qna(thread_id: str, current_user: User = Depends(get_required_user)):
    try:
        return await qna.answer_preview(await _current_tenant(current_user), current_user.uid, thread_id)
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class QnaWriteback(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_id: int = Field(gt=0)
    expected_digest: str = Field(min_length=64, max_length=64)


@literature.post("/sessions/{thread_id}/qna-writeback", status_code=201)
async def writeback_session_qna(thread_id: str, payload: QnaWriteback, current_user: User = Depends(get_required_user)):
    try:
        return await qna.writeback(
            await _current_tenant(current_user),
            current_user.uid,
            thread_id,
            message_id=payload.message_id,
            expected_digest=payload.expected_digest,
        )
    except cards.CardConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
