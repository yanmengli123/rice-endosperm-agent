"""文献域 HTTP 路由（P6a，薄层：请求解析/鉴权/装配，无业务逻辑）。

- 租户权威 `PrincipalContext`：请求体 tenant_id 一律忽略（AGENTS.md 硬性）；
- 渲染/翻译等长任务一律 202 + job 投影，不同步等待；
- 产物只返 kbasset:// 逻辑 URI，字节经 /api/literature/assets 鉴权解析。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile

from server.utils.auth_middleware import get_required_user
from yuxi.literature import readops
from yuxi.literature.attachments import register_attachment
from yuxi.literature.reading.service import RENDER_BUCKET, enqueue_reader_render
from yuxi.literature.translate.service import enqueue_literature_translation
from yuxi.services.principal import resolve_tenant_id
from yuxi.storage.minio import get_minio_client
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import User

literature = APIRouter(prefix="/literature", tags=["literature"])

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
    """触发阅读渲染（入队，202；D6：用户手动触发，不自动挂接）。"""
    tenant_id = await _current_tenant(current_user)
    detail = await readops.get_paper_detail(tenant_id, rid)
    if detail is None:
        raise HTTPException(status_code=404, detail="文献不存在或无权访问")
    revision_id = detail.get("source_revision_id")
    if not revision_id:
        raise HTTPException(status_code=409, detail="文献未关联解析版本，无法渲染")
    result = await enqueue_reader_render(revision_id=revision_id)
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
        result = await enqueue_literature_translation(paper_id=paper_id, variant_kind=variant_kind)
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
    try:
        data = await get_minio_client().adownload_file(RENDER_BUCKET, object_name)
    except Exception as exc:  # noqa: BLE001 - 对象不存在/存储不可用统一 404
        raise HTTPException(status_code=404, detail="资产不存在") from exc
    suffix = "." + object_name.rsplit(".", 1)[-1].lower() if "." in object_name else ""
    from fastapi import Response

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
                select(LiteraturePaper).where(LiteraturePaper.tenant_id == tenant_id, LiteraturePaper.rid == rid)
            )
        ).scalar_one_or_none()
    if paper is None:
        raise HTTPException(status_code=404, detail="文献不存在或无权访问")
    return paper.id
