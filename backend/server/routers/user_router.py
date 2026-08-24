"""用户级配置与凭据路由"""

import asyncio
import re
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from server.utils.auth_middleware import get_current_user, get_db, get_required_user
from yuxi.config import UserConfig, UserConfigSchema
from yuxi.models.providers.cache import model_cache
from yuxi.services.operation_log_service import log_operation
from yuxi.services.run_queue_service import publish_cancel_signal
from yuxi.storage.minio import upload_image_to_minio
from yuxi.storage.postgres.models_business import (
    AGENT_RUN_TERMINAL_STATUSES,
    APIKey,
    AgentEnv,
    AgentRun,
    OperationLog,
    User,
    UserModelPreference,
    UserQuota,
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
    query = select(APIKey).order_by(APIKey.created_at.desc()).offset(skip).limit(limit)
    count_query = select(func.count(APIKey.id))
    if current_user.role != "superadmin":
        query = query.filter(APIKey.user_id == current_user.id)
        count_query = count_query.filter(APIKey.user_id == current_user.id)

    result = await db.execute(query)
    api_keys = result.scalars().all()
    total_result = await db.execute(count_query)

    return {
        "api_keys": [key.to_dict() for key in api_keys],
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

    api_key = APIKey(
        key_hash=key_hash,
        key_prefix=key_prefix,
        name=data.name,
        user_id=target_user.id,
        department_id=target_user.department_id,
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
    api_key = await get_accessible_api_key(db, api_key_id, current_user)

    await db.delete(api_key)
    await db.commit()
    return {"success": True}


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
    db.add(OperationLog(user_id=current_user.id, operation="启用用户", details=f"uid={uid}"))
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
        if not info or info.model_type != "chat":
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
    return {
        "daily": [{"date": str(row.day), "run_count": row.run_count, "tokens": int(row.tokens or 0)} for row in rows],
        "monthly_tokens": int(monthly_tokens or 0),
    }


@user_router.get("/quota")
async def get_my_quota(current_user: User = Depends(get_required_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(UserQuota).filter(UserQuota.uid == current_user.uid))
    quota = result.scalar_one_or_none()
    return {
        "daily_run_limit": quota.daily_run_limit if quota else None,
        "monthly_token_limit": quota.monthly_token_limit if quota else None,
    }


class QuotaUpdate(BaseModel):
    daily_run_limit: int | None = Field(None, ge=1)
    monthly_token_limit: int | None = Field(None, ge=1)


@user_router.get("/manage/{uid}/quota")
async def get_user_quota(
    uid: str, current_user: User = Depends(get_required_user), db: AsyncSession = Depends(get_db)
):
    target = await _load_target_user(db, uid)
    _admin_guard(current_user, target)
    result = await db.execute(select(UserQuota).filter(UserQuota.uid == uid))
    quota = result.scalar_one_or_none()
    return {
        "uid": uid,
        "daily_run_limit": quota.daily_run_limit if quota else None,
        "monthly_token_limit": quota.monthly_token_limit if quota else None,
    }


@user_router.put("/manage/{uid}/quota")
async def put_user_quota(
    uid: str,
    data: QuotaUpdate,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """管理员设置用户配额；字段为 null 表示不限制。"""
    target = await _load_target_user(db, uid)
    _admin_guard(current_user, target)
    result = await db.execute(select(UserQuota).filter(UserQuota.uid == uid))
    quota = result.scalar_one_or_none()
    if quota is None:
        quota = UserQuota(uid=uid, updated_by=current_user.uid)
        db.add(quota)
    quota.daily_run_limit = data.daily_run_limit
    quota.monthly_token_limit = data.monthly_token_limit
    quota.updated_by = current_user.uid
    db.add(
        OperationLog(
            user_id=current_user.id,
            operation="更新用户配额",
            details=(
                f"uid={uid}, daily_run_limit={data.daily_run_limit}, "
                f"monthly_token_limit={data.monthly_token_limit}"
            ),
        )
    )
    await db.commit()
    return {"uid": uid, "daily_run_limit": quota.daily_run_limit, "monthly_token_limit": quota.monthly_token_limit}
