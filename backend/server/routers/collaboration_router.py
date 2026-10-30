from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from server.utils.auth_middleware import get_db, get_required_user
from yuxi.services import collaboration_service as service
from yuxi.storage.postgres.models_business import User

collaboration = APIRouter(prefix="/agent/runs", tags=["collaboration"])


class GuidanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_run_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=8192)
    request_id: str = Field(min_length=1, max_length=64)


@collaboration.get("/{run_id}/collaboration")
async def get_collaboration(run_id: str, user: User = Depends(get_required_user), db: AsyncSession = Depends(get_db)):
    return await service.tree(db, uid=str(user.uid), run_id=run_id)


@collaboration.post("/{run_id}/collaboration/messages")
async def send_guidance(
    run_id: str, body: GuidanceRequest, user: User = Depends(get_required_user), db: AsyncSession = Depends(get_db)
):
    result = await service.send_message(
        db,
        uid=str(user.uid),
        sender_run_id=run_id,
        target_run_id=body.target_run_id,
        body=body.message,
        request_id=body.request_id,
        sender_kind="user",
    )
    await db.commit()
    return result


@collaboration.post("/{run_id}/collaboration/stop")
async def stop_collaboration(run_id: str, user: User = Depends(get_required_user), db: AsyncSession = Depends(get_db)):
    return await service.stop_group(db, uid=str(user.uid), run_id=run_id)
