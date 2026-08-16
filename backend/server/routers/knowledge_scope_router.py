from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from server.utils.auth_middleware import get_admin_user, get_db, get_required_user
from yuxi.repositories.agent_repository import AgentRepository, user_can_access_agent, user_can_manage_agent
from yuxi.repositories.knowledge_scope_repository import (
    DEFAULT_QA_SCOPE_ID,
    KnowledgeScopeRepository,
    ScopeVersionConflictError,
)
from yuxi.services.knowledge_scope_service import (
    RETRIEVAL_MODES,
    SCOPE_MODES,
    _serialize_agent_scope_config,
    get_default_scope_history,
    get_default_scope_view,
    replay_default_scope_version,
    resolve_effective_knowledge_scope,
    update_default_scope_member,
)
from yuxi.storage.postgres.models_business import User

knowledge_scope = APIRouter(prefix="/knowledge/scopes", tags=["knowledge-scope"])


class ScopeMemberUpdate(BaseModel):
    expected_version: int = Field(..., ge=1)
    enabled: bool = False
    document_enabled: bool = True
    graph_enabled: bool = True
    structured_enabled: bool = True
    evidence_strict: bool = True
    evidence_supporting: bool = True
    evidence_candidate: bool = False
    evidence_rejected: bool = False
    priority: int = Field(100, ge=0, le=1000)


class ScopeResolveRequest(BaseModel):
    agent_slug: str
    session_kb_ids: list[str] | None = None


class AgentScopeUpdate(BaseModel):
    scope_mode: str
    retrieval_mode: str | None = None
    allow_web: bool | None = None


@knowledge_scope.get("/default-qa")
async def get_default_qa_scope(
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    return await get_default_scope_view(db=db, user=current_user)


@knowledge_scope.get("/default-qa/history")
async def get_default_qa_scope_history(
    limit: int = Query(100, ge=1, le=500),
    _current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    return await get_default_scope_history(db=db, limit=limit)


@knowledge_scope.get("/default-qa/versions/{version}")
async def get_default_qa_scope_version(
    version: int,
    _current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await replay_default_scope_version(db=db, version=version)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@knowledge_scope.put("/default-qa/members/{kb_id}")
async def put_default_qa_scope_member(
    kb_id: str,
    payload: ScopeMemberUpdate,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await update_default_scope_member(
            db=db,
            user=current_user,
            kb_id=kb_id,
            expected_version=payload.expected_version,
            values=payload.model_dump(exclude={"expected_version"}),
        )
    except ScopeVersionConflictError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail={"code": "SCOPE_VERSION_CONFLICT", "current_version": exc.current_version, "message": str(exc)},
        ) from exc
    except PermissionError as exc:
        await db.rollback()
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@knowledge_scope.post("/resolve")
async def resolve_scope(
    payload: ScopeResolveRequest,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    agent = await AgentRepository(db).get_by_slug(payload.agent_slug)
    if not agent or not user_can_access_agent(current_user, agent):
        raise HTTPException(status_code=404, detail="智能体不存在或无权访问")
    try:
        snapshot = await resolve_effective_knowledge_scope(
            db=db,
            user=current_user,
            agent_slug=payload.agent_slug,
            session_kb_ids=payload.session_kb_ids,
        )
        await db.commit()
        return {"knowledge_scope_snapshot": snapshot}
    except LookupError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@knowledge_scope.get("/agents/{agent_slug}")
async def get_agent_scope_config(
    agent_slug: str,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    agent = await AgentRepository(db).get_by_slug(agent_slug)
    if not agent or not user_can_access_agent(current_user, agent):
        raise HTTPException(status_code=404, detail="智能体不存在或无权访问")
    config = await KnowledgeScopeRepository(db).get_agent_config(agent_slug)
    fallback = "INHERIT_GLOBAL" if agent_slug == "default-chatbot" else "LEGACY"
    return {"config": _serialize_agent_scope_config(config, fallback_mode=fallback)}


@knowledge_scope.put("/agents/{agent_slug}")
async def put_agent_scope_config(
    agent_slug: str,
    payload: AgentScopeUpdate,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    mode = payload.scope_mode.upper()
    retrieval_mode = payload.retrieval_mode.upper() if payload.retrieval_mode else None
    if mode not in SCOPE_MODES:
        raise HTTPException(status_code=422, detail=f"scope_mode 必须是 {sorted(SCOPE_MODES)} 之一")
    if retrieval_mode is not None and retrieval_mode not in RETRIEVAL_MODES:
        raise HTTPException(status_code=422, detail=f"retrieval_mode 必须是 {sorted(RETRIEVAL_MODES)} 之一")

    agent = await AgentRepository(db).get_by_slug(agent_slug)
    if not agent:
        raise HTTPException(status_code=404, detail="智能体不存在")
    if not user_can_manage_agent(current_user, agent):
        raise HTTPException(status_code=403, detail="不能编辑该智能体的知识范围")
    config = await KnowledgeScopeRepository(db).update_agent_config(
        agent_slug=agent_slug,
        scope_mode=mode,
        retrieval_mode=retrieval_mode,
        allow_web=payload.allow_web,
        actor_uid=str(current_user.uid),
    )
    await db.commit()
    return {"config": _serialize_agent_scope_config(config, fallback_mode=mode), "scope_id": DEFAULT_QA_SCOPE_ID}
