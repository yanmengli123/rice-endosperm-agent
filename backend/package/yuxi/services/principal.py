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
    TenantMembership,
    User,
)


def map_membership_role(users_role: str | None) -> str:
    """users.role → 租户成员角色映射。"""
    if users_role == "superadmin":
        return "platform_admin"
    if users_role == "admin":
        return "tenant_admin"
    return "member"


@dataclass(frozen=True)
class PrincipalContext:
    """一次请求的服务端权威身份：租户、用户与角色。"""

    tenant_id: int
    uid: str
    role: str  # users.role 原始值：superadmin / admin / user
    membership_role: str  # platform_admin / tenant_admin / member
    department_id: int | None


async def resolve_tenant_id(db: AsyncSession, uid: str | None) -> int:
    """解析用户的活跃租户；缺失成员关系时自愈归入默认租户。

    在调用方当前事务内 flush，保证同事务后续写入可见。
    """
    if not uid:
        return DEFAULT_TENANT_ID
    result = await db.execute(
        select(TenantMembership.tenant_id)
        .where(
            TenantMembership.uid == uid,
            TenantMembership.status == "active",
        )
        .order_by(TenantMembership.tenant_id)
        .limit(1)
    )
    tenant_id = result.scalar_one_or_none()
    if tenant_id is not None:
        return int(tenant_id)

    user = (
        await db.execute(select(User.role).where(User.uid == uid, User.is_deleted == 0))
    ).scalar_one_or_none()
    role = map_membership_role(user)
    db.add(
        TenantMembership(
            tenant_id=DEFAULT_TENANT_ID,
            uid=uid,
            role=role,
            status="active",
        )
    )
    await db.flush()
    return DEFAULT_TENANT_ID


async def resolve_principal(db: AsyncSession, user: User) -> PrincipalContext:
    """由已认证 User 构造服务端权威身份上下文。"""
    membership_role = "member"
    if user.role == "superadmin":
        membership_role = "platform_admin"
    elif user.role == "admin":
        membership_role = "tenant_admin"
    return PrincipalContext(
        tenant_id=await resolve_tenant_id(db, user.uid),
        uid=str(user.uid),
        role=user.role,
        membership_role=membership_role,
        department_id=user.department_id,
    )
