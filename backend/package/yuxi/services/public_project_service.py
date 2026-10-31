"""Owned project workdirs shared by public Sessions and their collaborators."""

from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select

from yuxi.services.public_query_service import unix_time
from yuxi.storage.postgres.models_business import PublicProject


def project_resource(project):
    return {
        "id": project.id,
        "object": "project",
        "name": project.name,
        "archived": project.archived,
        "created_at": unix_time(project.created_at),
    }


async def require_project(db, actor, project_id, *, active=False):
    project = await db.scalar(
        select(PublicProject).where(
            PublicProject.id == project_id,
            PublicProject.uid == actor.uid,
            PublicProject.tenant_id == actor.tenant_id,
            PublicProject.app_id == actor.app_id,
        )
    )
    if project is None:
        raise HTTPException(404, "项目不存在")
    if active and project.archived:
        raise HTTPException(409, "项目已归档")
    return project


async def create_project(db, actor, *, name=None):
    project_id = str(uuid4())
    project = PublicProject(
        id=project_id,
        uid=actor.uid,
        tenant_id=actor.tenant_id,
        app_id=actor.app_id,
        name=(name or "").strip() or project_id,
    )
    db.add(project)
    await db.flush()
    return project


def file_thread_id(session):
    return session.config_snapshot.get("project_id") or session.id
