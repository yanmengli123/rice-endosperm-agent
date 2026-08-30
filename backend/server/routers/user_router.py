"""用户级配置与凭据路由"""

import asyncio
import csv
import io
import re
import secrets
from datetime import timedelta
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from server.utils.auth_middleware import get_admin_user, get_current_user, get_db, get_required_user
from yuxi.config import UserConfig, UserConfigSchema
from yuxi.models.providers.cache import model_cache
from yuxi.services.operation_log_service import log_operation, resolve_operator_tenant_id
from yuxi.services.run_queue_service import publish_cancel_signal
from yuxi.storage.minio import upload_image_to_minio
from yuxi.storage.postgres.models_business import (
    AGENT_RUN_TERMINAL_STATUSES,
    APIKey,
    AgentEnv,
    AgentRun,
    CLIAuthSession,
    Conversation,
    Message,
    OperationLog,
    TenantUserEntitlement,
    UsageLedger,
    User,
    UserModelPreference,
)
from yuxi.utils.auth_utils import AuthUtils
from yuxi.utils.datetime_utils import coerce_any_to_utc_datetime, format_utc_datetime, utc_now_naive

user_router = APIRouter(prefix="/user", tags=["user"])

ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_ENV_COUNT = 200
MAX_ENV_KEY_LENGTH = 128
MAX_ENV_VALUE_LENGTH = 32768
MAX_USER_IMAGE_SIZE_BYTES = 5 * 1024 * 1024


class APIKeyCreate(BaseModel):
    name: str
    user_id: int | None = None
    department_id: int | None = None
    expires_at: str | None = None


class APIKeyUpdate(BaseModel):
    name: str | None = None
    expires_at: str | None = None
    is_enabled: bool | None = None


class APIKeyResponse(BaseModel):
    id: int
    key_prefix: str
    name: str
    user_id: int
    department_id: int | None
    expires_at: str | None
    is_enabled: bool
    last_used_at: str | None
    created_by: str
    created_at: str
    owner_uid: str | None = None
    owner_username: str | None = None


class APIKeyCreateResponse(BaseModel):
    api_key: APIKeyResponse
    secret: str


class AgentEnvUpdate(BaseModel):
    env: dict[str, Any] = Field(default_factory=dict)


class AgentEnvResponse(BaseModel):
    env: dict[str, str]
    updated_at: str | None = None


async def get_logged_in_user(user: User | None = Depends(get_current_user)) -> User:
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="请登录后再访问",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


@user_router.get("/config", response_model=dict)
async def get_user_config(
    current_user: User = Depends(get_logged_in_user),
    db: AsyncSession = Depends(get_db),
):
    user_config = await UserConfig.load(db, current_user.uid)
    return user_config.dump_config()


@user_router.put("/config", response_model=dict)
async def update_user_config(
    data: UserConfigSchema,
    current_user: User = Depends(get_logged_in_user),
    db: AsyncSession = Depends(get_db),
):
    user_config = await UserConfig(uid=current_user.uid, schema=data).save(db)
    return user_config.dump_config()


@user_router.post("/upload-image", response_model=dict)
async def upload_user_image(file: UploadFile = File(...), current_user: User = Depends(get_required_user)):
    try:
        image_url = await upload_image_to_minio(
            file,
            object_prefix=f"images/{current_user.uid}",
            max_size_bytes=MAX_USER_IMAGE_SIZE_BYTES,
            too_large_message="图片大小不能超过 5MB",
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return {"success": True, "image_url": image_url, "url": image_url}


def validate_agent_env(env: dict[str, Any]) -> dict[str, str]:
    if len(env) > MAX_ENV_COUNT:
        raise HTTPException(status_code=400, detail=f"环境变量数量不能超过 {MAX_ENV_COUNT} 个")

    normalized: dict[str, str] = {}
    for key, value in env.items():
        if not isinstance(key, str):
            raise HTTPException(status_code=400, detail="环境变量名必须是字符串")
        name = key.strip()
        if not name:
            raise HTTPException(status_code=400, detail="环境变量名不能为空")
        if len(name) > MAX_ENV_KEY_LENGTH:
            raise HTTPException(status_code=400, detail=f"环境变量名长度不能超过 {MAX_ENV_KEY_LENGTH}")
        if not ENV_KEY_PATTERN.match(name):
            raise HTTPException(status_code=400, detail=f"环境变量名 {name} 格式不正确")
        if name in normalized:
            raise HTTPException(status_code=400, detail=f"环境变量名 {name} 重复")
        if not isinstance(value, str):
            raise HTTPException(status_code=400, detail=f"环境变量 {name} 的值必须是字符串")
        if len(value) > MAX_ENV_VALUE_LENGTH:
            raise HTTPException(status_code=400, detail=f"环境变量 {name} 的值过长")
        normalized[name] = value
    return normalized


def ensure_api_key_owner(api_key: APIKey, current_user: User) -> None:
    if api_key.user_id != current_user.id and current_user.role != "superadmin":
        raise HTTPException(status_code=403, detail="无权操作此 API Key")


async def get_accessible_api_key(db: AsyncSession, api_key_id: int, current_user: User) -> APIKey:
    result = await db.execute(select(APIKey).filter(APIKey.id == api_key_id))
    api_key = result.scalar_one_or_none()
    if not api_key:
        raise HTTPException(status_code=404, detail="API Key 不存在")
    ensure_api_key_owner(api_key, current_user)
    return api_key


@user_router.get("/apikey/", response_model=dict)
async def list_api_keys(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    query = (
        select(APIKey, User.uid, User.username)
        .join(User, User.id == APIKey.user_id)
        .order_by(APIKey.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    count_query = select(func.count(APIKey.id))
    if current_user.role != "superadmin":
        query = query.filter(APIKey.user_id == current_user.id)
        count_query = count_query.filter(APIKey.user_id == current_user.id)

    result = await db.execute(query)
    api_key_rows = result.all()
    total_result = await db.execute(count_query)

    api_keys = []
    for api_key, owner_uid, owner_username in api_key_rows:
        item = api_key.to_dict()
        item["owner_uid"] = owner_uid
        item["owner_username"] = owner_username
        api_keys.append(item)

    return {
        "api_keys": api_keys,
        "total": total_result.scalar(),
    }


@user_router.post("/apikey/", response_model=APIKeyCreateResponse)
async def create_api_key(
    data: APIKeyCreate,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    if data.user_id and data.user_id != current_user.id and current_user.role != "superadmin":
        raise HTTPException(status_code=403, detail="无权为其他用户创建 API Key")

    target_user = current_user
    if data.user_id:
        result = await db.execute(select(User).filter(User.id == data.user_id))
        user = result.scalar_one_or_none()
        if not user or user.is_deleted:
            raise HTTPException(status_code=404, detail="关联的用户不存在")
        target_user = user

    if target_user.is_disabled:
        raise HTTPException(status_code=400, detail="不能为已停用用户创建 API Key")
    if target_user.department_id is None:
        raise HTTPException(status_code=400, detail="用户尚未绑定部门")

    if data.department_id is not None and data.department_id != target_user.department_id:
        raise HTTPException(status_code=403, detail="API Key 部门必须与关联用户部门一致")

    full_key, key_hash, key_prefix = AuthUtils.generate_api_key()
    expires_at = None
    if data.expires_at:
        aware_dt = coerce_any_to_utc_datetime(data.expires_at)
        if aware_dt:
            expires_at = aware_dt.replace(tzinfo=None)

    from yuxi.services.principal import resolve_tenant_id

    api_key = APIKey(
        key_hash=key_hash,
        key_prefix=key_prefix,
        name=data.name,
        user_id=target_user.id,
        department_id=target_user.department_id,
        tenant_id=await resolve_tenant_id(db, target_user.uid),
        expires_at=expires_at,
        created_by=str(current_user.id),
    )

    db.add(api_key)
    await db.commit()
    await db.refresh(api_key)

    return APIKeyCreateResponse(
        api_key=APIKeyResponse(**api_key.to_dict()),
        secret=full_key,
    )


@user_router.get("/apikey/{api_key_id}", response_model=dict)
async def get_api_key(
    api_key_id: int,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    api_key = await get_accessible_api_key(db, api_key_id, current_user)
    return {"api_key": api_key.to_dict()}


@user_router.put("/apikey/{api_key_id}", response_model=dict)
async def update_api_key(
    api_key_id: int,
    data: APIKeyUpdate,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    api_key = await get_accessible_api_key(db, api_key_id, current_user)

    if data.name is not None:
        api_key.name = data.name
    if data.expires_at is not None:
        aware_dt = coerce_any_to_utc_datetime(data.expires_at)
        api_key.expires_at = aware_dt.replace(tzinfo=None) if aware_dt else None
    if data.is_enabled is not None:
        api_key.is_enabled = data.is_enabled

    await db.commit()
    await db.refresh(api_key)
    return {"api_key": api_key.to_dict()}


@user_router.delete("/apikey/{api_key_id}", response_model=dict)
async def delete_api_key(
    api_key_id: int,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """真·删除：先断开设备码会话对密钥行的引用，再物理删除。

    usage_ledger 不引用 api_keys，历史用量对账不受影响；
    删除动作与操作者一并写入审计日志。"""
    api_key = await get_accessible_api_key(db, api_key_id, current_user)

    await db.execute(
        CLIAuthSession.__table__.update()
        .where(CLIAuthSession.api_key_id == api_key.id)
        .values(api_key_id=None)
    )
    key_prefix = api_key.key_prefix
    owner_uid_result = await db.execute(
        select(User.uid).where(User.id == api_key.user_id)
    )
    owner_uid = owner_uid_result.scalar_one_or_none()
    await db.delete(api_key)
    await db.commit()

    await log_operation(
        db,
        current_user.id,
        "删除 API Key",
        f"目标用户 uid={owner_uid}, prefix={key_prefix}",
        None if current_user.id == api_key.user_id else None,
    )
    return {"success": True, "message": "密钥已删除"}


@user_router.post("/apikey/{api_key_id}/regenerate", response_model=APIKeyCreateResponse)
async def regenerate_api_key(
    api_key_id: int,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    api_key = await get_accessible_api_key(db, api_key_id, current_user)

    full_key, key_hash, key_prefix = AuthUtils.generate_api_key()
    api_key.key_hash = key_hash
    api_key.key_prefix = key_prefix

    await db.commit()
    await db.refresh(api_key)

    return APIKeyCreateResponse(
        api_key=APIKeyResponse(**api_key.to_dict()),
        secret=full_key,
    )


@user_router.get("/agent-env", response_model=AgentEnvResponse)
async def get_agent_env(
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(AgentEnv).filter(AgentEnv.uid == current_user.uid))
    agent_env = result.scalar_one_or_none()
    if agent_env is None:
        return AgentEnvResponse(env={})
    return AgentEnvResponse(env=agent_env.env or {}, updated_at=format_utc_datetime(agent_env.updated_at))


@user_router.put("/agent-env", response_model=AgentEnvResponse)
async def update_agent_env(
    data: AgentEnvUpdate,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    env = validate_agent_env(data.env)
    result = await db.execute(select(AgentEnv).filter(AgentEnv.uid == current_user.uid))
    current_agent_env = result.scalar_one_or_none()
    if current_agent_env is not None and (current_agent_env.env or {}) == env:
        return AgentEnvResponse(
            env=current_agent_env.env or {},
            updated_at=format_utc_datetime(current_agent_env.updated_at),
        )

    now = utc_now_naive()
    stmt = (
        pg_insert(AgentEnv)
        .values(uid=current_user.uid, env=env, updated_at=now)
        .on_conflict_do_update(
            index_elements=[AgentEnv.uid],
            set_={"env": env, "updated_at": now},
        )
        .returning(AgentEnv)
    )
    await db.execute(stmt)
    await db.commit()
    # 直接返回刚写入的 env/now，避免身份映射中的旧实例属性导致返回陈旧值
    return AgentEnvResponse(env=env, updated_at=format_utc_datetime(now))

# =============================================================================
# === 企业级用户管理（管理员） ===
# =============================================================================


async def _load_target_user(db: AsyncSession, uid: str) -> User:
    result = await db.execute(select(User).filter(User.uid == uid, User.is_deleted == 0))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
    return user


def _admin_guard(current_user: User, target: User) -> None:
    """停用/启用/配额目标的权限与保护规则。"""
    if current_user.role not in {"admin", "superadmin"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    if target.id == current_user.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="不能对自己执行该操作")
    if target.role == "superadmin" and current_user.role != "superadmin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="仅超级管理员可操作超级管理员")
    if current_user.role == "admin":
        if target.department_id != current_user.department_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="只能管理本部门用户")
        if target.role != "user":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="部门管理员只能管理普通用户")


@user_router.post("/manage/{uid}/disable")
async def disable_user(uid: str, current_user: User = Depends(get_required_user), db: AsyncSession = Depends(get_db)):
    """停用用户：立即拒绝登录/API Key，并取消尚未结束的运行。"""
    target = await _load_target_user(db, uid)
    _admin_guard(current_user, target)
    target.is_disabled = True
    target.auth_version += 1
    active_runs = (
        await db.execute(
            select(AgentRun).filter(
                AgentRun.uid == target.uid,
                AgentRun.status.notin_(AGENT_RUN_TERMINAL_STATUSES),
            )
        )
    ).scalars().all()
    for run in active_runs:
        run.status = "cancel_requested"
        run.updated_at = utc_now_naive()
    active_run_ids = [run.id for run in active_runs]
    db.add(
        OperationLog(
            user_id=current_user.id,
            tenant_id=await resolve_operator_tenant_id(db, current_user.id),
            operation="停用用户",
            details=f"uid={uid}, 取消活动运行 {len(active_run_ids)} 个",
        )
    )
    await db.commit()
    if active_run_ids:
        await asyncio.gather(*(publish_cancel_signal(run_id) for run_id in active_run_ids))
    return {"uid": uid, "is_disabled": True, "cancelled_runs": len(active_run_ids)}


@user_router.post("/manage/{uid}/enable")
async def enable_user(uid: str, current_user: User = Depends(get_required_user), db: AsyncSession = Depends(get_db)):
    """启用用户：恢复账号访问；API Key 自身的启停状态保持不变。"""
    target = await _load_target_user(db, uid)
    _admin_guard(current_user, target)
    target.is_disabled = False
    target.auth_version += 1
    db.add(
        OperationLog(
            user_id=current_user.id,
            tenant_id=await resolve_operator_tenant_id(db, current_user.id),
            operation="启用用户",
            details=f"uid={uid}",
        )
    )
    await db.commit()
    return {"uid": uid, "is_disabled": False}


# =============================================================================
# === 用户级默认模型偏好 ===
# =============================================================================


class ModelPreferenceUpdate(BaseModel):
    chat_model_spec: str | None = Field(None, max_length=200, description="为空表示清除偏好，回落智能体/系统默认")


@user_router.get("/model-preference")
async def get_model_preference(current_user: User = Depends(get_required_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(UserModelPreference).filter(UserModelPreference.uid == current_user.uid))
    pref = result.scalar_one_or_none()
    return {"chat_model_spec": pref.chat_model_spec if pref else None}


class UserCredentialPayload(BaseModel):
    provider_id: str | None = Field(None, min_length=1, max_length=100)
    api_key: str = Field(..., min_length=4, max_length=500)
    label: str | None = Field(None, max_length=128)
    protocol: str | None = Field(None, description="自定义端点协议：openai / anthropic")
    base_url: str | None = Field(None, max_length=1000)
    model: str | None = Field(None, max_length=255)
    activate_as_default: bool = True


class UserCredentialJsonImportPayload(BaseModel):
    configuration: str | dict[str, Any]
    label: str | None = Field(None, max_length=128)
    activate_as_default: bool = True


async def _ensure_user_byok_allowed(db: AsyncSession, uid: str) -> int:
    from yuxi.services.principal import resolve_entitlement, resolve_tenant_id

    tenant_id = await resolve_tenant_id(db, uid)
    entitlement = await resolve_entitlement(db, uid, tenant_id)
    if entitlement.credential_policy == TenantUserEntitlement.CREDENTIAL_POLICY_PLATFORM_ONLY:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "byok_not_allowed",
                "message": "当前账号未启用自有模型，请联系管理员将模型策略设为 BYOK 可选",
                "action": "contact_admin",
            },
        )
    return tenant_id


async def _set_user_model_preference_row(
    db: AsyncSession,
    *,
    uid: str,
    model_spec: str,
) -> None:
    result = await db.execute(select(UserModelPreference).where(UserModelPreference.uid == uid))
    preference = result.scalar_one_or_none()
    if preference is None:
        db.add(UserModelPreference(uid=uid, chat_model_spec=model_spec, updated_by=uid))
    else:
        preference.chat_model_spec = model_spec
        preference.updated_by = uid


async def _load_manage_target(db: AsyncSession, uid: str, current_user: User) -> User:
    """加载管理目标用户并套用部门边界（superadmin 全局、admin 本部门普通成员）。"""
    result = await db.execute(select(User).where(User.uid == uid, User.is_deleted == 0))
    target_user = result.scalar_one_or_none()
    if target_user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    _admin_guard(current_user, target_user)
    return target_user


@user_router.get("/manage/{uid}/api-keys", response_model=dict)
async def list_managed_api_keys(
    uid: str,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """列出目标用户的全部 API Keys（含已撤销，供管理与对账）。"""
    target_user = await _load_manage_target(db, uid, current_user)
    rows = await db.execute(
        select(APIKey)
        .where(APIKey.user_id == target_user.id)
        .order_by(APIKey.created_at.desc())
    )
    return {
        "keys": [
            {
                "id": key.id,
                "key_prefix": key.key_prefix,
                "name": key.name,
                "purpose": key.purpose,
                "status": "enabled" if key.is_enabled else "disabled",
                "expires_at": key.expires_at.isoformat() if key.expires_at else None,
                "last_used_at": key.last_used_at.isoformat() if key.last_used_at else None,
            }
            for key in rows.scalars().all()
        ]
    }


@user_router.post("/manage/{uid}/api-keys/{key_id}/reset", response_model=dict)
async def reset_managed_api_key(
    uid: str,
    key_id: int,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """重置目标用户的指定 Key：轮换哈希并返回新明文（仅此一次）。"""
    target_user = await _load_manage_target(db, uid, current_user)
    row_result = await db.execute(
        select(APIKey).where(APIKey.id == key_id, APIKey.user_id == target_user.id)
    )
    api_key = row_result.scalar_one_or_none()
    if api_key is None:
        raise HTTPException(status_code=404, detail="密钥不存在")

    full_key, key_hash, key_prefix = AuthUtils.generate_api_key()
    api_key.key_hash = key_hash
    api_key.key_prefix = key_prefix
    api_key.is_enabled = True
    await db.commit()

    await log_operation(
        db, current_user.id, "重置用户 API Key",
        f"目标 uid={uid}, prefix={key_prefix}", None,
    )
    return {"secret": full_key, "key_prefix": key_prefix}


@user_router.delete("/manage/{uid}/api-keys/{key_id}", response_model=dict)
async def delete_managed_api_key(
    uid: str,
    key_id: int,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """真·删除目标用户的指定 Key：先断开设备码会话引用再物理删除。"""
    target_user = await _load_manage_target(db, uid, current_user)
    row_result = await db.execute(
        select(APIKey).where(APIKey.id == key_id, APIKey.user_id == target_user.id)
    )
    api_key = row_result.scalar_one_or_none()
    if api_key is None:
        raise HTTPException(status_code=404, detail="密钥不存在")

    await db.execute(
        CLIAuthSession.__table__.update()
        .where(CLIAuthSession.api_key_id == api_key.id)
        .values(api_key_id=None)
    )
    key_prefix = api_key.key_prefix
    await db.delete(api_key)
    await db.commit()

    await log_operation(
        db, current_user.id, "删除用户 API Key",
        f"目标 uid={uid}, prefix={key_prefix}", None,
    )
    return {"success": True, "message": "密钥已删除"}


@user_router.post("/manage/{uid}/password-reset", response_model=dict)
async def reset_managed_password(
    uid: str,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """生成随机初始密码并重置；明文仅本次响应返回。"""
    import string as _string

    target_user = await _load_manage_target(db, uid, current_user)
    alphabet = _string.ascii_letters + _string.digits
    new_password = "".join(secrets.choice(alphabet) for _ in range(16))
    target_user.password_hash = AuthUtils.hash_password(new_password)
    target_user.auth_version += 1
    target_user.login_failed_count = 0
    target_user.login_locked_until = None
    # 密码重置必须同步撤销其全部设备会话（旋转刷新令牌随 auth_version 失效前先断族）
    from yuxi.storage.postgres.models_business import DeviceSession

    await db.execute(
        DeviceSession.__table__.update()
        .where(DeviceSession.uid == target_user.uid)
        .values(status="revoked")
    )
    await db.commit()

    await log_operation(db, current_user.id, "重置用户密码", f"目标 uid={uid}", None)
    return {"success": True, "username": target_user.username, "new_password": new_password}


@user_router.get("/manage/{uid}/stats")
async def get_managed_user_stats(
    uid: str,
    days: int = Query(14, ge=1, le=90),
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """单用户监控面板数据：按日 run/token 趋势 + 权益配额 + 分域用量。"""
    target_user = await _load_manage_target(db, uid, current_user)
    from yuxi.services.principal import resolve_tenant_id

    tenant_id = await resolve_tenant_id(db, target_user.uid)

    day_start = utc_now_naive().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
    daily_rows = (
        await db.execute(
            select(
                func.date(AgentRun.created_at).label("day"),
                func.count(AgentRun.id).label("runs"),
                func.coalesce(func.sum(AgentRun.total_tokens), 0).label("tokens"),
            )
            .where(AgentRun.uid == target_user.uid, AgentRun.created_at >= day_start)
            .group_by(func.date(AgentRun.created_at))
            .order_by(func.date(AgentRun.created_at))
        )
    ).all()
    daily = [
        {"date": row.day.isoformat(), "runs": int(row.runs), "tokens": int(row.tokens)}
        for row in daily_rows
    ]

    total_runs_row = (
        await db.execute(select(func.count(AgentRun.id)).where(AgentRun.uid == target_user.uid))
    ).scalar()
    total_tokens_row = (
        await db.execute(
            select(func.coalesce(func.sum(AgentRun.total_tokens), 0)).where(
                AgentRun.uid == target_user.uid
            )
        )
    ).scalar()
    byok_tokens_row = (
        await db.execute(
            select(func.coalesce(func.sum(UsageLedger.total_tokens), 0)).where(
                UsageLedger.uid == target_user.uid,
                UsageLedger.tenant_id == tenant_id,
                UsageLedger.credential_source == "user_byok",
            )
        )
    ).scalar()
    platform_tokens_row = (
        await db.execute(
            select(func.coalesce(func.sum(UsageLedger.total_tokens), 0)).where(
                UsageLedger.uid == target_user.uid,
                UsageLedger.tenant_id == tenant_id,
                UsageLedger.credential_source.in_(("platform", "legacy_unknown")),
            )
        )
    ).scalar()

    entitlement = (
        await db.execute(
            select(TenantUserEntitlement).where(
                TenantUserEntitlement.tenant_id == tenant_id,
                TenantUserEntitlement.uid == target_user.uid,
            )
        )
    ).scalar_one_or_none()

    return {
        "uid": target_user.uid,
        "username": target_user.username,
        "total_runs": int(total_runs_row or 0),
        "total_tokens": int(total_tokens_row or 0),
        "platform_tokens": int(platform_tokens_row or 0),
        "byok_tokens": int(byok_tokens_row or 0),
        "daily": daily,
        "entitlement": {
            "credential_policy": entitlement.credential_policy if entitlement else "byok_optional",
            "daily_run_limit": entitlement.daily_run_limit if entitlement else None,
            "monthly_platform_token_limit": entitlement.monthly_platform_token_limit if entitlement else None,
            "policy_version": entitlement.policy_version if entitlement else None,
        },
    }


@user_router.get("/manage/{uid}/conversations")
async def list_user_conversations_for_admin(
    uid: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """管理员查看指定用户的会话列表（仅 user/assistant 问答线程）。"""
    target_result = await db.execute(select(User).where(User.uid == uid, User.is_deleted == 0))
    target_user = target_result.scalar_one_or_none()
    if target_user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    _admin_guard(current_user, target_user)

    from yuxi.repositories.conversation_repository import ConversationRepository

    repo = ConversationRepository(db)
    offset = (page - 1) * page_size
    conversations = await repo.list_conversations(
        uid=target_user.uid,
        limit=page_size,
        offset=offset,
        exclude_sources=("agent",),
    )
    total = (
        await db.execute(
            select(func.count(Conversation.id)).where(
                Conversation.uid == target_user.uid,
                Conversation.status == "active",
                func.coalesce(Conversation.extra_metadata["source"].as_string(), "") != "agent",
            )
        )
    ).scalar() or 0
    return {
        "uid": target_user.uid,
        "username": target_user.username,
        "page": page,
        "page_size": page_size,
        "total": int(total),
        "conversations": [
            {
                "thread_id": c.thread_id,
                "title": c.title,
                "is_pinned": bool(c.is_pinned),
                "created_at": c.created_at.isoformat() if c.created_at else None,
                "updated_at": c.updated_at.isoformat() if c.updated_at else None,
            }
            for c in conversations
        ],
    }


@user_router.get("/manage/{uid}/conversations/{thread_id}/messages")
async def list_user_conversation_messages_for_admin(
    uid: str,
    thread_id: str,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """管理员查看指定用户某次会话的完整问答（问题与答案按时间序返回）。"""
    target_result = await db.execute(select(User).where(User.uid == uid, User.is_deleted == 0))
    target_user = target_result.scalar_one_or_none()
    if target_user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    _admin_guard(current_user, target_user)

    from yuxi.repositories.conversation_repository import ConversationRepository

    repo = ConversationRepository(db)
    conversation = await repo.get_conversation_by_thread_id(thread_id, uid=target_user.uid)
    if conversation is None or conversation.status == "deleted":
        raise HTTPException(status_code=404, detail="会话不存在")
    messages = await repo.get_messages(conversation.id)
    return {
        "thread_id": thread_id,
        "title": conversation.title,
        "messages": [
            {
                "id": message.id,
                "role": message.role,
                "content": message.content or "",
                "created_at": message.created_at.isoformat() if message.created_at else None,
            }
            for message in messages
            if message.role in ("user", "assistant") and (message.content or "").strip()
        ],
    }


def _csv_safe(value: Any) -> str:
    text_value = str(value or "")
    if text_value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + text_value
    return text_value


@user_router.get("/manage/{uid}/conversations-export")
async def export_user_conversations_for_admin(
    uid: str,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """导出目标用户的全部有效问答为 UTF-8 CSV，并记录敏感导出审计。"""
    target_user = await _load_manage_target(db, uid, current_user)
    rows = (
        await db.execute(
            select(Conversation, Message)
            .join(Message, Message.conversation_id == Conversation.id)
            .where(
                Conversation.uid == target_user.uid,
                Conversation.status == "active",
                Message.role.in_(("user", "assistant")),
                func.length(func.trim(Message.content)) > 0,
            )
            .order_by(Conversation.created_at, Message.created_at, Message.id)
        )
    ).all()

    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["会话ID", "会话标题", "会话创建时间", "消息时间", "角色", "内容"])
    for conversation, chat_message in rows:
        writer.writerow(
            [
                _csv_safe(conversation.thread_id),
                _csv_safe(conversation.title or "未命名会话"),
                conversation.created_at.isoformat() if conversation.created_at else "",
                chat_message.created_at.isoformat() if chat_message.created_at else "",
                "提问" if chat_message.role == "user" else "回答",
                _csv_safe(chat_message.content),
            ]
        )

    db.add(
        OperationLog(
            user_id=current_user.id,
            tenant_id=await resolve_operator_tenant_id(db, current_user.id),
            operation="导出用户问答记录",
            details=f"uid={target_user.uid}, conversations={len({row[0].id for row in rows})}, messages={len(rows)}",
        )
    )
    await db.commit()
    filename = quote(f"{target_user.username}-问答记录.csv")
    return Response(
        content=output.getvalue().encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


@user_router.get("/model-credentials")
async def list_model_credentials(
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """列出当前用户的模型凭据（只含掩码，永不回显明文）。"""
    from yuxi.services.user_credential_service import list_user_credentials

    return {"credentials": await list_user_credentials(db, current_user.uid)}


@user_router.put("/model-credentials")
async def upsert_model_credential(
    payload: UserCredentialPayload,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """创建/替换本供应商下的自有凭据（BYOK）。"""
    from yuxi.services.user_credential_service import (
        custom_model_spec,
        upsert_user_credential,
        validate_custom_model_configuration,
    )

    custom_fields = (payload.protocol, payload.base_url, payload.model)
    is_custom = any(value is not None for value in custom_fields)
    if is_custom:
        if not all(isinstance(value, str) and value.strip() for value in custom_fields):
            raise HTTPException(status_code=422, detail="自定义模型必须同时填写 protocol、base_url 和 model")
        try:
            normalized = validate_custom_model_configuration(
                protocol=payload.protocol or "",
                base_url=payload.base_url or "",
                api_key=payload.api_key,
                model_id=payload.model or "",
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        provider_id = normalized["provider_id"]
    else:
        known_providers = {info.provider_id for info in model_cache.get_all_specs()}
        provider_id = (payload.provider_id or "").strip()
        if provider_id not in known_providers:
            raise HTTPException(status_code=422, detail=f"未知模型供应商: '{provider_id}'")
        normalized = {}

    tenant_id = await _ensure_user_byok_allowed(db, current_user.uid)

    credential = await upsert_user_credential(
        db,
        uid=current_user.uid,
        provider_id=provider_id,
        api_key=payload.api_key,
        label=payload.label,
        tenant_id=tenant_id,
        protocol=normalized.get("protocol"),
        base_url=normalized.get("base_url"),
        model_id=normalized.get("model_id"),
    )
    model_spec = custom_model_spec(credential)
    if model_spec and payload.activate_as_default:
        await _set_user_model_preference_row(db, uid=current_user.uid, model_spec=model_spec)
    await db.commit()
    return {
        "credential_id": credential.id,
        "provider_id": credential.provider_id,
        "masked_hint": credential.masked_hint,
        "status": credential.status,
        "protocol": credential.protocol,
        "base_url": credential.base_url,
        "model_id": credential.model_id,
        "model_spec": model_spec,
    }


@user_router.post("/model-credentials/import")
async def import_model_credential_json(
    payload: UserCredentialJsonImportPayload,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """导入 Claude Code 风格 JSON；只提取三项模型配置，不执行或保存任意环境变量。"""
    from yuxi.services.user_credential_service import (
        custom_model_spec,
        parse_claude_model_configuration,
        upsert_user_credential,
    )

    try:
        normalized = parse_claude_model_configuration(payload.configuration)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    tenant_id = await _ensure_user_byok_allowed(db, current_user.uid)
    credential = await upsert_user_credential(
        db,
        uid=current_user.uid,
        provider_id=normalized["provider_id"],
        api_key=normalized["api_key"],
        label=payload.label or "JSON 导入模型",
        tenant_id=tenant_id,
        protocol=normalized["protocol"],
        base_url=normalized["base_url"],
        model_id=normalized["model_id"],
    )
    model_spec = custom_model_spec(credential)
    if model_spec and payload.activate_as_default:
        await _set_user_model_preference_row(db, uid=current_user.uid, model_spec=model_spec)
    await db.commit()
    return {
        "credential_id": credential.id,
        "provider_id": credential.provider_id,
        "masked_hint": credential.masked_hint,
        "status": credential.status,
        "protocol": credential.protocol,
        "base_url": credential.base_url,
        "model_id": credential.model_id,
        "model_spec": model_spec,
        "ignored_fields": normalized.get("ignored_fields", []),
    }


@user_router.delete("/model-credentials/{credential_id}")
async def delete_model_credential(
    credential_id: int,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """撤销并删除自有凭据；进行中的任务回落平台凭据。"""
    from yuxi.services.user_credential_service import delete_user_credential

    ok = await delete_user_credential(db, current_user.uid, credential_id)
    if not ok:
        raise HTTPException(status_code=404, detail="凭据不存在")
    await db.commit()
    return {"success": True}


@user_router.put("/model-preference")
async def put_model_preference(
    data: ModelPreferenceUpdate,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """保存用户级默认聊天模型；解析优先级：请求级 > 用户级 > 智能体级 > 系统级。"""
    spec = (data.chat_model_spec or "").strip() or None
    if spec:
        info = model_cache.get_model_info(spec)
        from yuxi.services.user_credential_service import list_active_custom_model_specs

        custom_specs = await list_active_custom_model_specs(db, current_user.uid)
        if (not info or info.model_type != "chat") and spec not in custom_specs:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"未找到可用聊天模型: {spec}")

    result = await db.execute(select(UserModelPreference).filter(UserModelPreference.uid == current_user.uid))
    pref = result.scalar_one_or_none()
    if pref is None:
        if spec is None:
            return {"chat_model_spec": None}
        pref = UserModelPreference(uid=current_user.uid, chat_model_spec=spec, updated_by=current_user.uid)
        db.add(pref)
    else:
        pref.chat_model_spec = spec
        pref.updated_by = current_user.uid
    db.add(
        OperationLog(
            user_id=current_user.id,
            tenant_id=await resolve_operator_tenant_id(db, current_user.id),
            operation="更新默认聊天模型",
            details=f"chat_model_spec={spec or '系统默认'}",
        )
    )
    await db.commit()
    return {"chat_model_spec": spec}


# =============================================================================
# === 用量与配额 ===
# =============================================================================


@user_router.get("/usage")
async def get_my_usage(
    days: int = Query(14, ge=1, le=90),
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """按天返回当前用户的 run 数与 token 用量（来自 agent_runs 终态计量）。"""
    from yuxi.services.principal import resolve_tenant_id

    tenant_id = await resolve_tenant_id(db, current_user.uid)
    since = utc_now_naive() - timedelta(days=days)
    month_start = utc_now_naive().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    rows = (
        await db.execute(
            select(
                func.date(AgentRun.created_at).label("day"),
                func.count(AgentRun.id).label("run_count"),
                func.coalesce(func.sum(AgentRun.total_tokens), 0).label("tokens"),
            )
            .filter(AgentRun.uid == current_user.uid, AgentRun.created_at >= since)
            .group_by(func.date(AgentRun.created_at))
            .order_by(func.date(AgentRun.created_at).desc())
        )
    ).all()
    monthly_tokens = (
        await db.execute(
            select(func.coalesce(func.sum(AgentRun.total_tokens), 0)).filter(
                AgentRun.uid == current_user.uid, AgentRun.created_at >= month_start
            )
        )
    ).scalar()
    monthly_platform_tokens = (
        await db.execute(
            select(func.coalesce(func.sum(UsageLedger.total_tokens), 0)).where(
                UsageLedger.uid == current_user.uid,
                UsageLedger.tenant_id == tenant_id,
                UsageLedger.created_at >= month_start,
                UsageLedger.credential_source.in_(("platform", "legacy_unknown")),
            )
        )
    ).scalar()
    monthly_byok_tokens = (
        await db.execute(
            select(func.coalesce(func.sum(UsageLedger.total_tokens), 0)).where(
                UsageLedger.uid == current_user.uid,
                UsageLedger.tenant_id == tenant_id,
                UsageLedger.created_at >= month_start,
                UsageLedger.credential_source == "user_byok",
            )
        )
    ).scalar()
    return {
        "daily": [{"date": str(row.day), "run_count": row.run_count, "tokens": int(row.tokens or 0)} for row in rows],
        "monthly_tokens": int(monthly_tokens or 0),
        "monthly_platform_tokens": int(monthly_platform_tokens or 0),
        "monthly_byok_tokens": int(monthly_byok_tokens or 0),
    }


@user_router.get("/quota")
async def get_my_quota(current_user: User = Depends(get_required_user), db: AsyncSession = Depends(get_db)):
    """P5：读自身权益（策略+配额）；兼容旧字段名 monthly_token_limit。"""
    from yuxi.services.principal import resolve_entitlement, resolve_tenant_id
    from yuxi.services.user_credential_service import list_user_credentials as _list_credentials

    tenant_id = await resolve_tenant_id(db, current_user.uid)
    entitlement = await resolve_entitlement(db, current_user.uid, tenant_id)
    credentials = await _list_credentials(db, current_user.uid)
    has_byok = any(c["status"] == "active" for c in credentials)
    return {
        "tenant_id": tenant_id,
        "daily_run_limit": entitlement.daily_run_limit,
        "monthly_token_limit": entitlement.monthly_platform_token_limit,
        "model_access_policy": entitlement.credential_policy,
        "byok_platform_token_exempt": entitlement.byok_platform_token_exempt,
        "policy_version": entitlement.policy_version,
        "has_active_byok": bool(has_byok),
    }


class QuotaUpdate(BaseModel):
    daily_run_limit: int | None = Field(None, ge=1)
    monthly_token_limit: int | None = Field(None, ge=1)
    model_access_policy: str | None = Field(None, description="platform_only / byok_optional / byok_required")
    byok_platform_token_exempt: bool | None = Field(
        None, description="自有密钥流量是否豁免平台月度 token 限额"
    )


@user_router.get("/manage/{uid}/quota")
async def get_user_quota(
    uid: str, current_user: User = Depends(get_required_user), db: AsyncSession = Depends(get_db)
):
    target = await _load_target_user(db, uid)
    _admin_guard(current_user, target)
    from yuxi.services.principal import resolve_entitlement, resolve_tenant_id

    tenant_id = await resolve_tenant_id(db, uid)
    entitlement = await resolve_entitlement(db, uid, tenant_id)
    return {
        "uid": uid,
        "daily_run_limit": entitlement.daily_run_limit,
        "monthly_token_limit": entitlement.monthly_platform_token_limit,
        "model_access_policy": entitlement.credential_policy,
        "byok_platform_token_exempt": entitlement.byok_platform_token_exempt,
        "policy_version": entitlement.policy_version,
    }


@user_router.put("/manage/{uid}/quota")
async def put_user_quota(
    uid: str,
    data: QuotaUpdate,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """管理员设置用户权益（P5：策略+配额统一写入 tenant_user_entitlements）。"""
    target = await _load_target_user(db, uid)
    _admin_guard(current_user, target)
    from yuxi.services.principal import resolve_entitlement, resolve_tenant_id

    if data.model_access_policy is not None and data.model_access_policy not in (
        TenantUserEntitlement.CREDENTIAL_POLICIES
    ):
        raise HTTPException(status_code=422, detail="无效的模型接入策略")

    tenant_id = await resolve_tenant_id(db, uid)
    entitlement = await resolve_entitlement(db, uid, tenant_id)
    fields_set = data.model_fields_set
    if "daily_run_limit" in fields_set:
        entitlement.daily_run_limit = data.daily_run_limit
    if "monthly_token_limit" in fields_set:
        entitlement.monthly_platform_token_limit = data.monthly_token_limit
    if data.model_access_policy is not None:
        entitlement.credential_policy = data.model_access_policy
    # 平台 token 池只统计 platform/legacy_unknown；BYOK 永远独立计量。
    # 保留兼容字段，但不允许把用户自费 token 混回企业平台配额。
    entitlement.byok_platform_token_exempt = (
        entitlement.credential_policy != TenantUserEntitlement.CREDENTIAL_POLICY_PLATFORM_ONLY
    )
    entitlement.policy_version += 1
    entitlement.updated_by = current_user.uid
    db.add(
        OperationLog(
            user_id=current_user.id,
            tenant_id=await resolve_operator_tenant_id(db, current_user.id),
            operation="更新用户配额",
            details=(
                f"uid={uid}, daily_run_limit={entitlement.daily_run_limit}, "
                f"monthly_platform_token_limit={entitlement.monthly_platform_token_limit}, "
                f"model_access_policy={entitlement.credential_policy}, "
                f"byok_exempt={entitlement.byok_platform_token_exempt}, "
                f"policy_version={entitlement.policy_version}"
            ),
        )
    )
    await db.commit()
    return {
        "uid": uid,
        "daily_run_limit": entitlement.daily_run_limit,
        "monthly_token_limit": entitlement.monthly_platform_token_limit,
        "model_access_policy": entitlement.credential_policy,
        "byok_platform_token_exempt": entitlement.byok_platform_token_exempt,
        "policy_version": entitlement.policy_version,
    }
