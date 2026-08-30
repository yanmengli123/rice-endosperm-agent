"""P5 开户编排路由：管理员签发一次性激活凭证；公开端点供桌面端激活换会话。"""


from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.utils.auth_middleware import get_db, get_required_user
from yuxi.storage.postgres.models_business import OnboardingActivation, TenantUserEntitlement, User
from yuxi.services.onboarding_service import (
    OnboardingError,
    consume_onboarding_activation,
    create_onboarding_invitation,
    revoke_onboarding_activation,
)

onboarding = APIRouter(tags=["onboarding"])

ACTIVATION_CODE_PREFIX = "yxact_"


class InvitationCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=20, pattern=r"^[a-zA-Z0-9_]+$")
    display_name: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=8, max_length=128)
    department_id: int
    credential_policy: str = Field("byok_optional")
    daily_run_limit: int | None = Field(None, ge=1)
    monthly_platform_token_limit: int | None = Field(None, ge=1)
    byok_platform_token_exempt: bool = Field(True)
    device_name: str = Field("桌面端", max_length=128)


@onboarding.post("/admin/onboarding/invitations")
async def create_invitation(
    payload: InvitationCreate,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """单事务开户：建户+成员+权益+一次性激活码。明文激活码仅此一次返回。"""
    if payload.credential_policy not in TenantUserEntitlement.CREDENTIAL_POLICIES:
        raise HTTPException(status_code=422, detail="无效的凭据策略")

    try:
        result = await create_onboarding_invitation(
            db,
            operator=current_user,
            username=payload.username,
            display_name=payload.display_name,
            password=payload.password,
            department_id=payload.department_id,
            credential_policy=payload.credential_policy,
            daily_run_limit=payload.daily_run_limit,
            monthly_platform_token_limit=payload.monthly_platform_token_limit,
            byok_platform_token_exempt=payload.byok_platform_token_exempt,
            device_name=payload.device_name,
        )
    except OnboardingError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"error": exc.code, "message": exc.message}) from exc
    return {"success": True, "invitation": result}


@onboarding.post("/admin/onboarding/invitations/{activation_id}/revoke")
async def revoke_invitation(
    activation_id: int,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    ok = await revoke_onboarding_activation(db, operator=current_user, activation_id=activation_id)
    if not ok:
        raise HTTPException(status_code=404, detail="激活记录不存在或无权操作")
    return {"success": True}


@onboarding.get("/admin/onboarding/invitations/{activation_id}")
async def get_invitation(
    activation_id: int,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(OnboardingActivation).where(OnboardingActivation.id == activation_id)
    )
    activation = result.scalar_one_or_none()
    if activation is None:
        raise HTTPException(status_code=404, detail="激活记录不存在")
    # 权限：superadmin 全局；admin 仅本租户
    from yuxi.storage.postgres.models_business import TenantUserEntitlement

    if current_user.role != "superadmin":
        ent = (
            await db.execute(
                select(TenantUserEntitlement).where(
                    TenantUserEntitlement.tenant_id == activation.tenant_id,
                    TenantUserEntitlement.uid == current_user.uid,
                )
            )
        ).scalar_one_or_none()
        if current_user.role != "admin" or ent is None:
            raise HTTPException(status_code=404, detail="激活记录不存在")
    return {
        "id": activation.id,
        "uid": activation.uid,
        "status": activation.status,
        "expires_at": activation.expires_at.isoformat() if activation.expires_at else None,
        "consumed_at": activation.consumed_at.isoformat() if activation.consumed_at else None,
    }


class ActivationExchange(BaseModel):
    activation_code: str = Field(..., min_length=8)
    device_name: str = Field("桌面端", max_length=128)


@onboarding.post("/auth/onboarding/exchange")
async def exchange_activation(payload: ActivationExchange, db: AsyncSession = Depends(get_db)):
    """公开端点：桌面端用一次性激活码换取设备会话对（不签发任何静态 Key）。"""
    code = payload.activation_code.strip()
    if not code.startswith(ACTIVATION_CODE_PREFIX):
        raise HTTPException(status_code=400, detail="激活码格式无效")
    try:
        return await consume_onboarding_activation(db, code)
    except OnboardingError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"error": exc.code, "message": exc.message}) from exc
