"""P5 开户与激活编排：管理员单事务建户，一次性激活码换取设备会话。

设计要点：
- 建户、租户成员、权益、激活凭证、审计共用同一个请求级 AsyncSession，
  中途只 flush，最外层统一 commit——任何一步失败整体回滚（含审计）。
- 激活码明文仅在创建响应返回一次，库中只存 SHA-256 哈希；24 小时有效、单次消费。
- 激活 exchange 不签发任何静态 API Key，只发放设备会话对。
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from yuxi.services.principal import resolve_tenant_id
from yuxi.storage.postgres.models_business import (
    OnboardingActivation,
    OperationLog,
    TenantUserEntitlement,
    User,
)
from yuxi.utils.auth_utils import AuthUtils
from yuxi.utils.datetime_utils import utc_now_naive

ACTIVATION_TTL_HOURS = 24


class OnboardingError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)


def _hash_code(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _generate_activation_code() -> str:
    return "yxact_" + secrets.token_urlsafe(32)


async def create_onboarding_invitation(
    db: AsyncSession,
    *,
    operator: User,
    username: str,
    display_name: str,
    password: str,
    department_id: int,
    credential_policy: str = "platform_only",
    daily_run_limit: int | None = None,
    monthly_platform_token_limit: int | None = None,
    byok_platform_token_exempt: bool = False,
    device_name: str = "桌面端",
) -> dict:
    """单事务完成：建户 → 成员 → 权益 → 激活凭证 → 审计。"""
    from yuxi.storage.postgres.models_business import TenantMembership

    uid = username.strip().lower()
    now = utc_now_naive()

    existing = await db.execute(select(User.uid).where((User.username == username) | (User.uid == uid)))
    if existing.scalar_one_or_none() is not None:
        raise OnboardingError("duplicate_user", "用户名已存在", 409)

    user = User(
        username=username.strip(),
        uid=uid,
        password_hash=AuthUtils.hash_password(password),
        role="user",  # users.role 沿用旧枚举；租户内角色由 membership 表表达
        department_id=department_id,
        created_at=now,
    )
    db.add(user)
    await db.flush()

    tenant_id = await resolve_tenant_id(db, uid)

    db.add(
        TenantMembership(
            tenant_id=tenant_id,
            uid=uid,
            role="member",
            status="active",
            created_at=now,
        )
    )
    db.add(
        TenantUserEntitlement(
            tenant_id=tenant_id,
            uid=uid,
            credential_policy=credential_policy,
            daily_run_limit=daily_run_limit,
            monthly_platform_token_limit=monthly_platform_token_limit,
            byok_platform_token_exempt=byok_platform_token_exempt,
            updated_by=operator.uid,
        )
    )

    activation_code = _generate_activation_code()
    db.add(
        OnboardingActivation(
            code_hash=_hash_code(activation_code),
            uid=uid,
            tenant_id=tenant_id,
            issued_by=operator.uid,
            device_name=device_name[:128],
            expires_at=now + timedelta(hours=ACTIVATION_TTL_HOURS),
        )
    )
    db.add(
        OperationLog(
            user_id=operator.id,
            operation="开户签发激活凭证",
            details=f"uid={uid}, policy={credential_policy}, device={device_name}",
        )
    )
    await db.commit()

    return {
        "uid": uid,
        "username": user.username,
        "initial_password": password,
        "activation_code": activation_code,
        "expires_in_hours": ACTIVATION_TTL_HOURS,
        "model_access_policy": credential_policy,
    }


async def consume_onboarding_activation(db: AsyncSession, activation_code: str) -> dict:
    """消费激活码：校验有效性后签发设备会话对（不发任何静态 Key）。"""
    code_hash = _hash_code(activation_code.strip())
    result = await db.execute(
        select(OnboardingActivation).filter(OnboardingActivation.code_hash == code_hash).with_for_update()
    )
    activation = result.scalar_one_or_none()
    if activation is None:
        raise OnboardingError("invalid_code", "激活码无效", 404)

    now = utc_now_naive()
    if activation.status == OnboardingActivation.STATUS_CONSUMED:
        raise OnboardingError("already_consumed", "激活码已被使用", 409)
    if activation.status == OnboardingActivation.STATUS_REVOKED:
        raise OnboardingError("revoked", "激活码已被撤销", 409)
    if activation.status != OnboardingActivation.STATUS_ACTIVE or activation.expires_at <= now:
        activation.status = OnboardingActivation.STATUS_EXPIRED
        await db.commit()
        raise OnboardingError("expired", "激活码已过期", 410)

    user_result = await db.execute(select(User).filter(User.uid == activation.uid, User.is_deleted == 0))
    user = user_result.scalar_one_or_none()
    if user is None or user.is_disabled:
        raise OnboardingError("invalid_user", "账号不可用", 403)

    from yuxi.services.auth_service import issue_device_session

    session, refresh_token = await issue_device_session(db, user)
    access_token = AuthUtils.create_access_token(
        {"sub": str(user.id), "auth_version": user.auth_version, "sid": str(session.family_id)},
        expires_delta=timedelta(minutes=30),
    )

    activation.status = OnboardingActivation.STATUS_CONSUMED
    activation.consumed_at = now
    await db.commit()

    return {
        "session": {
            "session_id": str(session.family_id),
            "access_token": access_token,
            "refresh_token": refresh_token,
            "access_expires_in": 30 * 60,
        },
        "user": {"uid": user.uid, "username": user.username},
        "account_scope_id": user.account_scope_id or AuthUtils.account_scope_id(user.uid),
    }


async def revoke_onboarding_activation(db: AsyncSession, *, operator: User, activation_id: int) -> bool:
    result = await db.execute(select(OnboardingActivation).filter(OnboardingActivation.id == activation_id))
    activation = result.scalar_one_or_none()
    if activation is None:
        return False
    from yuxi.storage.postgres.models_business import TenantUserEntitlement

    entitlement = (
        await db.execute(
            select(TenantUserEntitlement).where(
                TenantUserEntitlement.tenant_id == activation.tenant_id,
                TenantUserEntitlement.uid == activation.uid,
            )
        )
    ).scalar_one_or_none()
    # 权限：platform 全局；tenant_admin 仅本租户
    if operator.role != "superadmin":
        if operator.role != "admin" or operator.department_id is None or entitlement is None:
            return False
    activation.status = OnboardingActivation.STATUS_REVOKED
    activation.revoked_at = utc_now_naive()
    await db.commit()
    return True
