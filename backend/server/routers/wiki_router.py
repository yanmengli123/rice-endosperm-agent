"""Dynamic LLM-Wiki control-plane HTTP API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from server.utils.auth_middleware import get_admin_user, get_db, get_required_user
from yuxi.knowledge.products.registry import is_derived_product, registry_snapshot
from yuxi.knowledge.runtime import knowledge_base
from yuxi.services.principal import resolve_principal
from yuxi.services.wiki_service import (
    WikiServiceError,
    assert_wiki_source_access,
    build_wiki,
    create_wiki,
    delete_wiki,
    get_wiki_detail,
    list_wiki_claims,
    list_wiki_pages,
    list_wikis,
    navigate_wiki,
    publish_wiki,
    replace_wiki_sources,
    rollback_wiki,
    update_wiki_settings,
)
from yuxi.storage.postgres.models_business import User

wiki = APIRouter(prefix="/knowledge/wikis", tags=["dynamic-wiki"])


class WikiCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=5000)
    source_kb_ids: list[str] = Field(min_length=1, max_length=100)
    update_mode: str = "MANUAL"
    debounce_seconds: int = Field(default=300, ge=30, le=86400)
    auto_publish: bool = False


class WikiSourceUpdateRequest(BaseModel):
    source_kb_ids: list[str] = Field(min_length=1, max_length=100)


class WikiSettingsUpdateRequest(BaseModel):
    update_mode: str = "MANUAL"
    debounce_seconds: int = Field(default=300, ge=30, le=86400)
    auto_publish: bool = False


class WikiBuildRequest(BaseModel):
    force: bool = False


class WikiPublishRequest(BaseModel):
    build_id: str = Field(min_length=1, max_length=64)


class WikiRollbackRequest(BaseModel):
    publication_id: str = Field(min_length=1, max_length=64)


class WikiNavigateRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    limit: int = Field(default=8, ge=1, le=20)


async def _context(db: AsyncSession, user: User) -> tuple[int, set[str]]:
    principal = await resolve_principal(db, user)
    accessible = await knowledge_base.get_databases_by_user(user)
    source_ids = {
        str(item["kb_id"])
        for item in accessible.get("databases") or []
        if item.get("kb_id") and not is_derived_product(str(item.get("kb_type") or ""))
    }
    return principal.tenant_id, source_ids


def _domain_error(exc: WikiServiceError) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc))


@wiki.get("/product-registry")
async def get_product_registry(current_user: User = Depends(get_required_user)):
    del current_user
    return {"products": registry_snapshot()}


@wiki.get("")
async def get_wikis(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_required_user),
):
    tenant_id, accessible_ids = await _context(db, current_user)
    return {
        "wikis": await list_wikis(
            db,
            tenant_id=tenant_id,
            permitted_source_kb_ids=accessible_ids,
        )
    }


@wiki.post("")
async def post_wiki(
    payload: WikiCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    tenant_id, accessible_ids = await _context(db, current_user)
    try:
        created = await create_wiki(
            db,
            tenant_id=tenant_id,
            actor_uid=str(current_user.uid),
            accessible_source_ids=accessible_ids,
            **payload.model_dump(),
        )
        return {"wiki": created}
    except WikiServiceError as exc:
        raise _domain_error(exc) from exc


@wiki.get("/{wiki_id}")
async def get_wiki(
    wiki_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_required_user),
):
    tenant_id, accessible_ids = await _context(db, current_user)
    try:
        await assert_wiki_source_access(
            db,
            wiki_id=wiki_id,
            tenant_id=tenant_id,
            permitted_source_kb_ids=accessible_ids,
        )
        return {"wiki": await get_wiki_detail(db, wiki_id=wiki_id, tenant_id=tenant_id)}
    except WikiServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@wiki.put("/{wiki_id}/sources")
async def put_wiki_sources(
    wiki_id: str,
    payload: WikiSourceUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    tenant_id, accessible_ids = await _context(db, current_user)
    try:
        await assert_wiki_source_access(
            db,
            wiki_id=wiki_id,
            tenant_id=tenant_id,
            permitted_source_kb_ids=accessible_ids,
        )
        result = await replace_wiki_sources(
            db,
            wiki_id=wiki_id,
            tenant_id=tenant_id,
            actor_uid=str(current_user.uid),
            source_kb_ids=payload.source_kb_ids,
            accessible_source_ids=accessible_ids,
        )
        return {"wiki": result}
    except WikiServiceError as exc:
        raise _domain_error(exc) from exc


@wiki.put("/{wiki_id}/settings")
async def put_wiki_settings(
    wiki_id: str,
    payload: WikiSettingsUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    tenant_id, accessible_ids = await _context(db, current_user)
    try:
        await assert_wiki_source_access(
            db,
            wiki_id=wiki_id,
            tenant_id=tenant_id,
            permitted_source_kb_ids=accessible_ids,
        )
        result = await update_wiki_settings(
            db,
            wiki_id=wiki_id,
            tenant_id=tenant_id,
            actor_uid=str(current_user.uid),
            **payload.model_dump(),
        )
        return {"wiki": result}
    except WikiServiceError as exc:
        raise _domain_error(exc) from exc


@wiki.delete("/{wiki_id}")
async def remove_wiki(
    wiki_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    tenant_id, accessible_ids = await _context(db, current_user)
    try:
        await assert_wiki_source_access(
            db,
            wiki_id=wiki_id,
            tenant_id=tenant_id,
            permitted_source_kb_ids=accessible_ids,
        )
        await delete_wiki(
            db,
            wiki_id=wiki_id,
            tenant_id=tenant_id,
            actor_uid=str(current_user.uid),
        )
        return {"message": "动态 Wiki 已删除"}
    except WikiServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@wiki.post("/{wiki_id}/builds")
async def post_wiki_build(
    wiki_id: str,
    payload: WikiBuildRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    tenant_id, accessible_ids = await _context(db, current_user)
    try:
        await assert_wiki_source_access(
            db,
            wiki_id=wiki_id,
            tenant_id=tenant_id,
            permitted_source_kb_ids=accessible_ids,
        )
        return {
            "build": await build_wiki(
                db,
                wiki_id=wiki_id,
                tenant_id=tenant_id,
                actor_uid=str(current_user.uid),
                force=payload.force,
            )
        }
    except WikiServiceError as exc:
        raise _domain_error(exc) from exc


@wiki.post("/{wiki_id}/publications")
async def post_wiki_publication(
    wiki_id: str,
    payload: WikiPublishRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    tenant_id, accessible_ids = await _context(db, current_user)
    try:
        await assert_wiki_source_access(
            db,
            wiki_id=wiki_id,
            tenant_id=tenant_id,
            permitted_source_kb_ids=accessible_ids,
            build_id=payload.build_id,
        )
        return {
            "publication": await publish_wiki(
                db,
                wiki_id=wiki_id,
                tenant_id=tenant_id,
                actor_uid=str(current_user.uid),
                build_id=payload.build_id,
            )
        }
    except WikiServiceError as exc:
        raise _domain_error(exc) from exc


@wiki.post("/{wiki_id}/rollback")
async def post_wiki_rollback(
    wiki_id: str,
    payload: WikiRollbackRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    tenant_id, accessible_ids = await _context(db, current_user)
    try:
        await assert_wiki_source_access(
            db,
            wiki_id=wiki_id,
            tenant_id=tenant_id,
            permitted_source_kb_ids=accessible_ids,
            publication_id=payload.publication_id,
        )
        return {
            "publication": await rollback_wiki(
                db,
                wiki_id=wiki_id,
                tenant_id=tenant_id,
                actor_uid=str(current_user.uid),
                publication_id=payload.publication_id,
            )
        }
    except WikiServiceError as exc:
        raise _domain_error(exc) from exc


@wiki.get("/{wiki_id}/pages")
async def get_wiki_pages(
    wiki_id: str,
    build_id: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_required_user),
):
    tenant_id, accessible_ids = await _context(db, current_user)
    try:
        await assert_wiki_source_access(
            db,
            wiki_id=wiki_id,
            tenant_id=tenant_id,
            permitted_source_kb_ids=accessible_ids,
            build_id=build_id,
            use_current_publication=build_id is None,
        )
        return {"pages": await list_wiki_pages(db, wiki_id=wiki_id, tenant_id=tenant_id, build_id=build_id)}
    except WikiServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@wiki.get("/{wiki_id}/claims")
async def get_wiki_claims(
    wiki_id: str,
    build_id: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_required_user),
):
    tenant_id, accessible_ids = await _context(db, current_user)
    try:
        await assert_wiki_source_access(
            db,
            wiki_id=wiki_id,
            tenant_id=tenant_id,
            permitted_source_kb_ids=accessible_ids,
            build_id=build_id,
            use_current_publication=build_id is None,
        )
        return {"claims": await list_wiki_claims(db, wiki_id=wiki_id, tenant_id=tenant_id, build_id=build_id)}
    except WikiServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@wiki.post("/{wiki_id}/navigate")
async def post_wiki_navigation(
    wiki_id: str,
    payload: WikiNavigateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_required_user),
):
    tenant_id, accessible_ids = await _context(db, current_user)
    try:
        hits = await navigate_wiki(
            db,
            wiki_id=wiki_id,
            tenant_id=tenant_id,
            question=payload.question,
            permitted_source_kb_ids=accessible_ids,
            limit=payload.limit,
        )
        return {"navigation_hits": [item.to_dict() for item in hits]}
    except WikiServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
