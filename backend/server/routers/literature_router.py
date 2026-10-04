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
from pydantic import BaseModel, ConfigDict, Field

from server.utils.auth_middleware import get_admin_user, get_required_user, get_superadmin_user
from yuxi.literature import bib_import, cards, concepts, diary, enrich as enrich_module
from yuxi.literature import graph_service, readops, review
from yuxi.literature import artifacts, reader_bundle, retrieval, workflow
from yuxi.literature import management
from yuxi.literature import qna
from yuxi.literature import answer_cache, export_vault, library_pipeline, session as lit_session
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
        if request.scope.get("route") and request.scope["route"].name == "restore_library_paper":
            # The normal scope intentionally excludes trash. Restoration must
            # recheck the underlying source ACL including trash, in its service.
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


@literature.get("/drafts")
async def list_parse_only_drafts(current_user: User = Depends(get_required_user)):
    from yuxi.literature.drafts import list_drafts

    return {"items": await list_drafts(await _current_tenant(current_user))}


@literature.get("/history/archives")
async def list_sealed_import_archives(current_user: User = Depends(get_required_user)):
    from yuxi.literature.history_archive import list_archives

    return {"items": await list_archives(await _current_tenant(current_user), current_user.uid)}


@literature.get("/history/archives/{archive_sha256}/download")
async def download_sealed_import_archive(archive_sha256: str, current_user: User = Depends(get_required_user)):
    from yuxi.literature.history_archive import export_archive

    try:
        content = await export_archive(await _current_tenant(current_user), current_user.uid, archive_sha256)
    except PermissionError as exc:
        raise HTTPException(status_code=404, detail="归档不存在或无权访问") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return Response(
        content,
        media_type="application/zip",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'attachment; filename="paperagent-history-{archive_sha256[:12]}.zip"',
        },
    )


@literature.get("/history")
async def list_import_history(
    offset: int = Query(default=0, ge=0),
    category: str = Query(default="", max_length=64),
    current_user: User = Depends(get_required_user),
):
    from yuxi.literature.history_import import list_history

    return await list_history(await _current_tenant(current_user), current_user.uid, offset=offset, category=category)


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


@literature.post("/history/{item_id}/retry-mapping")
async def retry_import_history_mapping(
    item_id: int, body: HistoryMappingRetry, current_user: User = Depends(get_required_user)
):
    from yuxi.literature.history_import import retry_history_mapping

    try:
        return await retry_history_mapping(
            await _current_tenant(current_user),
            current_user.uid,
            item_id,
            expected_digest=body.expected_digest,
            request_id=str(body.request_id),
        )
    except PermissionError as exc:
        raise HTTPException(status_code=404, detail="历史资产不存在或无权访问") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@literature.get("/history/{item_id}/download")
async def download_import_history(item_id: int, current_user: User = Depends(get_required_user)):
    from yuxi.literature.history_import import read_history

    try:
        content = await read_history(await _current_tenant(current_user), current_user.uid, item_id)
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status_code=404, detail="历史资产不存在或无权访问") from exc
    return Response(
        content,
        media_type="application/octet-stream",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'attachment; filename="history-{item_id}.bin"',
        },
    )


@literature.get("/drafts/{revision_id}/document")
async def read_parse_only_draft(revision_id: str, current_user: User = Depends(get_required_user)):
    from yuxi.literature.drafts import read_draft

    try:
        return await read_draft(await _current_tenant(current_user), revision_id)
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status_code=404, detail="草稿不存在或无权访问") from exc


@literature.get("/drafts/{revision_id}/source-pdf")
async def read_parse_only_pdf(revision_id: str, current_user: User = Depends(get_required_user)):
    from yuxi.literature.drafts import read_draft

    try:
        data = await read_draft(await _current_tenant(current_user), revision_id, pdf=True)
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status_code=404, detail="草稿不存在或无权访问") from exc
    return Response(data, media_type="application/pdf", headers={"Cache-Control": "private, no-store"})


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
    content = await _read_bounded_upload(file, limit=50 * 1024 * 1024)
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


@literature.post("/papers/import/paperagent", status_code=202)
async def import_paperagent_endpoint(
    file: UploadFile,
    dry_run: bool = Query(default=True),
    archive_sha256: str = Query(default="", max_length=64),
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
            expected_archive_sha256=archive_sha256,
        )
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


@literature.get("/reports")
async def list_reports_endpoint(current_user: User = Depends(get_required_user)):
    tenant_id = await _current_tenant(current_user)
    return {"reports": await cards.list_reports(tenant_id)}


@literature.get("/reports/{report_id}")
async def read_report_endpoint(report_id: int, current_user: User = Depends(get_required_user)):
    try:
        result = await cards.get_report(await _current_tenant(current_user), report_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="报告不存在或来源不可访问")
    return result


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
        return await review.get_review_page_image(
            tenant_id, revision_id, page, require_dual=require_dual
        )
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


class ProductEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=5_000_000)
    expected_revision: str | None
    expected_source_revision: str | None
    expected_reading_version: int = Field(ge=0)
    expected_card_revision: int = Field(ge=0)
    expected_base_uri: str | None = Field(max_length=1024)


@literature.get("/papers/{rid}/products/{kind}")
async def read_editable_product(rid: str, kind: str, current_user: User = Depends(get_required_user)):
    from yuxi.literature.products import read_product

    try:
        return await read_product(await _current_tenant(current_user), rid, kind)
    except artifacts.RevisionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@literature.put("/papers/{rid}/products/{kind}")
async def edit_product(rid: str, kind: str, payload: ProductEdit, current_user: User = Depends(get_required_user)):
    from yuxi.literature.products import save_product

    try:
        data = payload.model_dump()
        content = data.pop("content")
        return await save_product(
            await _current_tenant(current_user), rid, kind, uid=current_user.uid, content=content, snapshot=data
        )
    except artifacts.RevisionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


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


@literature.post("/papers/{rid}/workflow", status_code=202)
async def start_paper_workflow(rid: str, payload: dict, current_user: User = Depends(get_required_user)):
    """启动处理运行（parse_compile / full / parse_only；幂等复用活跃运行）。"""
    tenant_id = await _current_tenant(current_user)
    try:
        result = await workflow.start_workflow(
            tenant_id=tenant_id,
            paper_rid=rid,
            mode=str(payload.get("mode") or "parse_compile"),
            requested_by=getattr(current_user, "uid", ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return result


@literature.get("/workflow/{run_id}")
async def get_paper_workflow(run_id: str, current_user: User = Depends(get_required_user)):
    tenant_id = await _current_tenant(current_user)
    result = await workflow.get_workflow(run_id, tenant_id)
    if result is None:
        raise HTTPException(status_code=404, detail="运行不存在")
    return result


@literature.post("/workflow/{run_id}/skip-review")
async def skip_review(run_id: str, payload: dict, current_user: User = Depends(get_required_user)):
    """显式跳过复核门控（保存操作者/原因；不改变证据资格）。"""
    tenant_id = await _current_tenant(current_user)
    existing = await workflow.get_workflow(run_id, tenant_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="运行不存在")
    try:
        await workflow.skip_review_gate(
            run_id, operator=getattr(current_user, "uid", ""), reason=str(payload.get("reason") or "")
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"run_id": run_id, "review_skipped": True}


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


@literature.get("/search/compiled")
async def search_compiled_endpoint(
    query: str = Query(..., min_length=1, max_length=500),
    kind: str = Query(default=""),
    limit: int = Query(default=10, ge=1, le=50),
    use_cache: bool = Query(default=True),
    rerank_model: str = Query(default="", max_length=512),
    current_user: User = Depends(get_required_user),
):
    """编译产物检索（笔记+元数据 RRF k=60；导航命中，不进证据通道）。"""
    tenant_id = await _current_tenant(current_user)
    try:
        return await retrieval.search_compiled(
            tenant_id, query, kind=kind, limit=limit, use_cache=use_cache, rerank_model=rerank_model
        )
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


@literature.post("/library/pipeline", status_code=202)
async def run_library_pipeline(payload: dict, current_user: User = Depends(get_required_user)):
    """六步建库一键全流程（bib→清洗→富化→期刊→计量→向量；各步幂等）。"""
    tenant_id = await _current_tenant(current_user)
    try:
        result = await library_pipeline.run_full_pipeline(
            tenant_id,
            bib_text=str(payload.get("bib_text") or ""),
            source_name=str(payload.get("source_name") or ""),
            cleaning_rules=payload.get("cleaning_rules"),
            include_mains=payload.get("include_mains") is True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": 202, **result}


@literature.get("/library/pipeline/{run_id}")
async def library_pipeline_status(run_id: str, current_user: User = Depends(get_required_user)):
    result = await library_pipeline.get_library_run(await _current_tenant(current_user), run_id)
    if result is None:
        raise HTTPException(status_code=404, detail="建库运行不存在")
    return result


@literature.post("/library/journal-metrics")
async def attach_journal_metrics_endpoint(current_user: User = Depends(get_required_user)):
    """关联期刊指标（对齐上游「关联期刊指标（写入 IF / 分区）」按钮）。

    幂等：最新年份优先、只填空、人工锁定（meta_locked）文献受保护；不产生新的解析/证据。
    """
    tenant_id = await _current_tenant(current_user)
    return await library_pipeline.attach_journal_metrics(tenant_id)


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


class MetadataChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, max_length=4096)
    kind: Literal["paper", "thesis", "book", "chapter", "patent", "standard"] = "paper"
    authors_json: list[Annotated[str, Field(max_length=512)]] | None = Field(default=None, max_length=200)
    abstract: str | None = Field(default=None, max_length=50000)
    journal: str | None = Field(default=None, max_length=512)
    issn: str | None = Field(default=None, max_length=32)
    pub_year: int | None = Field(default=None, ge=1000, le=9999)
    keywords_json: list[Annotated[str, Field(max_length=512)]] | None = Field(default=None, max_length=200)
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


class BatchCompileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rids: list[str] = Field(min_length=1, max_length=50)
    level: Literal["L1", "L2", "L3"] = "L1"
    force: bool = False


@literature.post("/library/compile", status_code=202)
async def batch_compile_papers(payload: BatchCompileRequest, current_user: User = Depends(get_required_user)):
    from yuxi.literature.access import build_access

    tenant_id = await _current_tenant(current_user)
    scope = await build_access(current_user, tenant_id)
    rids = list(dict.fromkeys(payload.rids))
    if any(not rid or len(rid) > 160 for rid in rids):
        raise HTTPException(status_code=422, detail="非法文献标识")
    if not set(rids) <= scope.rids:
        raise HTTPException(status_code=404, detail="批量范围含不可访问文献；未入队任何任务")
    results = []
    for rid in rids:
        try:
            result = await enqueue_literature_compile(
                paper_id=await _paper_id_by_rid(tenant_id, rid),
                level=payload.level,
                force=payload.force,
                requested_by=current_user.uid,
            )
            results.append({"rid": rid, "accepted": True, **result})
        except ValueError as exc:
            results.append({"rid": rid, "accepted": False, "error": str(exc)})
    return {"items": results, "accepted": sum(item["accepted"] for item in results)}


@literature.get("/runs")
async def literature_runs(
    limit: int = Query(default=50, ge=1, le=100), current_user: User = Depends(get_required_user)
):
    return {"items": await management.list_runs(await _current_tenant(current_user), limit=limit)}


@literature.get("/sessions")
async def list_literature_sessions(archived: bool = False, current_user: User = Depends(get_required_user)):
    return {
        "items": await lit_session.list_literature_sessions(
            await _current_tenant(current_user), current_user.uid, archived=archived
        )
    }


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
