"""Product JWT-only derived annotations; separate from AgentRun's public contract."""

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from server.utils.auth_middleware import get_db, get_required_user
from yuxi.services import source_annotation_service as service
from yuxi.storage.postgres.models_business import User

annotations = APIRouter(prefix="/agent/runs", tags=["来源标注"])


async def product_user(request: Request, response: Response, user: User = Depends(get_required_user)):
    response.headers["Cache-Control"] = "private, no-store"
    if getattr(request.state, "api_key_purpose", None) is not None:
        raise HTTPException(403, "来源标注仅支持产品用户登录")
    return user


@annotations.get("/{run_id}/source-annotations")
async def get_annotations(run_id: str, user: User = Depends(product_user), db: AsyncSession = Depends(get_db)):
    return await service.get_annotations(db, user=user, run_id=run_id)


@annotations.post("/{run_id}/source-annotations")
async def create_annotations(run_id: str, user: User = Depends(product_user), db: AsyncSession = Depends(get_db)):
    return await service.annotate(db, user=user, run_id=run_id)
