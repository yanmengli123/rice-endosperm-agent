"""租户成员只读端点（P1）。

租户管理写操作（邀请/审批/角色变更）在 P4 企业治理阶段交付；
本路由仅提供当前登录用户可见的成员信息。
"""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.utils.auth_middleware import get_db, get_required_user
from yuxi.services.principal import resolve_principal
from yuxi.storage.postgres.models_business import TenantMembership, User

tenants = APIRouter(prefix="/tenant", tags=["tenants"])


@tenants.get("/members")
async def list_tenant_members(
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """列出当前用户所属租户的活跃成员（uid、显示名、租户内角色）。"""
    principal = await resolve_principal(db, current_user)
    result = await db.execute(
        select(TenantMembership.uid, TenantMembership.role, User.username)
        .join(User, User.uid == TenantMembership.uid)
        .where(
            TenantMembership.tenant_id == principal.tenant_id,
            TenantMembership.status == "active",
            User.is_deleted == 0,
        )
        .order_by(TenantMembership.created_at.asc())
    )
    members = [
        {"uid": uid, "username": username, "membership_role": role}
        for uid, role, username in result.all()
    ]
    return {"tenant_id": principal.tenant_id, "members": members}
