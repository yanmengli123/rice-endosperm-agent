"""服务端可信身份上下文。

PrincipalContext 由登录态在服务端推导（用户 → 租户成员关系），是所有业务资源
租户归属的唯一权威来源。请求体中出现的任何 tenant_id 一律忽略。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.models_business import (
    DEFAULT_TENANT_ID,
    Tenant,
    TenantMembership,
    User,
)


class PrincipalResolutionError(ValueError):
    """认证用户没有唯一、可用的租户身份。"""


def map_membership_role(users_role: str | None) -> str:
    """users.role → 租户成员角色映射。"""
    if users_role == "superadmin":
        return "platform_admin"
    if users_role == "admin":
        return "tenant_admin"
    return "member"


async def resolve_entitlement(db: AsyncSession, uid: str, tenant_id: int):
    """解析用户在租户内的权益（策略与配额唯一权威）；缺失时自愈默认行（platform_only）。"""
    from yuxi.storage.postgres.models_business import TenantUserEntitlement

    result = await db.execute(
        select(TenantUserEntitlement).where(
            TenantUserEntitlement.tenant_id == tenant_id,
            TenantUserEntitlement.uid == uid,
        )
    )
    entitlement = result.scalar_one_or_none()
    if entitlement is not None:
        return entitlement
    db.add(
        TenantUserEntitlement(
            tenant_id=tenant_id,
            uid=uid,
            credential_policy=TenantUserEntitlement.CREDENTIAL_POLICY_PLATFORM_ONLY,
        )
    )
    await db.flush()
    result = await db.execute(
        select(TenantUserEntitlement).where(
            TenantUserEntitlement.tenant_id == tenant_id,
            TenantUserEntitlement.uid == uid,
        )
    )
    return result.scalar_one_or_none()


@dataclass(frozen=True)
class PrincipalContext:
    """一次请求的服务端权威身份：租户、用户与角色。"""

    tenant_id: int
    uid: str
    role: str  # users.role 原始值：superadmin / admin / user
    membership_role: str  # platform_admin / tenant_admin / member
    department_id: int | None


async def ensure_tenant_membership(
    db: AsyncSession,
    user: User,
    *,
    tenant_id: int = DEFAULT_TENANT_ID,
) -> TenantMembership:
    """仅供开户流程显式创建成员关系；业务请求不得借此自愈权限。"""
    tenant_status = (
        await db.execute(select(Tenant.status).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()
    if tenant_status != "active":
        raise PrincipalResolutionError("目标租户不存在或已停用")

    result = await db.execute(
        select(TenantMembership).where(
            TenantMembership.tenant_id == tenant_id,
            TenantMembership.uid == str(user.uid),
        )
    )
    membership = result.scalar_one_or_none()
    if membership is not None:
        if membership.status != "active":
            raise PrincipalResolutionError("租户成员资格已停用，不能由开户流程自动恢复")
        return membership

    membership = TenantMembership(
        tenant_id=tenant_id,
        uid=str(user.uid),
        role=map_membership_role(user.role),
        status="active",
    )
    db.add(membership)
    await db.flush()
    return membership


async def _resolve_active_membership(db: AsyncSession, uid: str) -> TenantMembership:
    result = await db.execute(
        select(TenantMembership)
        .join(Tenant, Tenant.id == TenantMembership.tenant_id)
        .where(
            TenantMembership.uid == uid,
            TenantMembership.status == "active",
            Tenant.status == "active",
        )
        .order_by(TenantMembership.tenant_id)
        .limit(2)
    )
    memberships = list(result.scalars().all())
    if not memberships:
        raise PrincipalResolutionError("当前账号没有可用的租户成员资格")
    if len(memberships) > 1:
        # 协议尚未携带 active_tenant_id，任意选第一项会造成跨租户混淆。
        raise PrincipalResolutionError("当前账号属于多个租户，请先选择活动租户")
    return memberships[0]


async def resolve_tenant_id(db: AsyncSession, uid: str | None) -> int:
    """解析唯一活跃租户；成员资格缺失或歧义时失败关闭。"""
    if not uid or str(uid) == "system":
        return DEFAULT_TENANT_ID
    return int((await _resolve_active_membership(db, str(uid))).tenant_id)


async def resolve_principal(db: AsyncSession, user: User) -> PrincipalContext:
    """由已认证 User 构造服务端权威身份上下文。"""
    membership = await _resolve_active_membership(db, str(user.uid))
    return PrincipalContext(
        tenant_id=int(membership.tenant_id),
        uid=str(user.uid),
        role=user.role,
        membership_role=str(membership.role),
        department_id=user.department_id,
    )
