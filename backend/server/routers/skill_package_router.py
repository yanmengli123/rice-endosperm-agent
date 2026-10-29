"""Versioned Skill package editing on the existing authenticated resource boundary."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from server.utils.auth_middleware import get_db, get_required_user
from yuxi.agents.skills.edit import SkillEditConflict, bound_skill, list_versions, package_state, release_version, save_package
from yuxi.storage.postgres.models_business import User

router = APIRouter()


class SkillRevisionRequest(BaseModel):
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")


class SkillDependencies(BaseModel):
    tool_dependencies: list[str] = Field(default_factory=list)
    mcp_dependencies: list[str] = Field(default_factory=list)
    skill_dependencies: list[str] = Field(default_factory=list)


class SkillPackageRequest(SkillRevisionRequest):
    files: dict[str, str] = Field(default_factory=dict, max_length=2000)
    deletions: list[str] = Field(default_factory=list, max_length=2000)
    dependencies: SkillDependencies | None = None


def raise_edit_error(error):
    raise HTTPException(status_code=409 if isinstance(error, SkillEditConflict) else 400, detail=str(error)) from error


@router.get("/{slug}/content")
async def read_package(slug: str, user: User = Depends(get_required_user), db: AsyncSession = Depends(get_db)):
    try:
        return {"success": True, "data": await package_state(db, slug=slug, operator=user)}
    except ValueError as error:
        raise_edit_error(error)


@router.put("/{slug}/content")
async def update_package(slug: str, payload: SkillPackageRequest, user: User = Depends(get_required_user), db: AsyncSession = Depends(get_db)):
    try:
        result = await save_package(db, slug=slug, operator=user, expected_revision=payload.expected_revision, files=payload.files, deletions=payload.deletions, dependencies=payload.dependencies.model_dump() if payload.dependencies is not None else None)
        return {"success": True, "data": result}
    except ValueError as error:
        raise_edit_error(error)


@router.get("/{slug}/versions")
async def read_versions(slug: str, user: User = Depends(get_required_user), db: AsyncSession = Depends(get_db)):
    try:
        return {"success": True, "data": await list_versions(db, slug=slug, operator=user)}
    except ValueError as error:
        raise_edit_error(error)


@router.post("/{slug}/versions")
async def publish_version(slug: str, payload: SkillRevisionRequest, user: User = Depends(get_required_user), db: AsyncSession = Depends(get_db)):
    try:
        return {"success": True, "data": await release_version(db, slug=slug, operator=user, expected_revision=payload.expected_revision)}
    except ValueError as error:
        raise_edit_error(error)


@router.post("/{slug}/versions/{version}/restore")
async def restore_version(slug: str, version: str, payload: SkillRevisionRequest, user: User = Depends(get_required_user), db: AsyncSession = Depends(get_db)):
    try:
        return {"success": True, "data": await save_package(db, slug=slug, operator=user, expected_revision=payload.expected_revision, restore_version=version)}
    except ValueError as error:
        raise_edit_error(error)


@router.get("/agents/{agent_slug}/bound")
async def read_bound_skill(agent_slug: str, user: User = Depends(get_required_user), db: AsyncSession = Depends(get_db)):
    try:
        return {"success": True, "data": await bound_skill(db, operator=user, agent_slug=agent_slug)}
    except ValueError as error:
        raise_edit_error(error)


@router.post("/agents/{agent_slug}/bound")
async def create_bound_skill(agent_slug: str, user: User = Depends(get_required_user), db: AsyncSession = Depends(get_db)):
    try:
        return {"success": True, "data": await bound_skill(db, operator=user, agent_slug=agent_slug, create=True)}
    except ValueError as error:
        raise_edit_error(error)
