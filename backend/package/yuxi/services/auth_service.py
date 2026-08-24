"""认证服务。"""

import hashlib
import secrets
import string
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from yuxi.brands.rice_endosperm import BRAND_NAME
from uuid import uuid4

from yuxi.storage.postgres.models_business import (
    APIKey,
    CLIAuthSession,
    Department,
    DeviceSession,
    DeviceSessionToken,
    User,
)
from yuxi.utils.auth_utils import AuthUtils
from yuxi.utils.datetime_utils import utc_now_naive

CLI_AUTH_SESSION_TTL_SECONDS = 10 * 60
# 设备码签发 API Key 的过渡有效期（天）；OAuth 刷新令牌上线后将逐步替代
DEVICE_API_KEY_TTL_DAYS = 90
# 会话刷新令牌有效期（天）；访问令牌为短时 JWT（30 分钟，携带 sid 声明）
SESSION_REFRESH_TTL_DAYS = 30
SESSION_ACCESS_TTL_MINUTES = 30
CLI_AUTH_POLL_INTERVAL_SECONDS = 2
CLI_AUTH_DEFAULT_KEY_NAME = f"{BRAND_NAME} CLI"
CLI_AUTH_USER_CODE_ALPHABET = "".join(ch for ch in string.ascii_uppercase + string.digits if ch not in "0O1I")

CLI_AUTH_STATUS_PENDING = "pending"
CLI_AUTH_STATUS_APPROVED = "approved"
CLI_AUTH_STATUS_CONSUMED = "consumed"
CLI_AUTH_STATUS_EXPIRED = "expired"


@dataclass
class CLIAuthError(Exception):
    code: str
    message: str
    status_code: int = 400


def _hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _generate_device_code() -> str:
    return f"yxcli_{secrets.token_urlsafe(32)}"


def _generate_user_code() -> str:
    raw = "".join(secrets.choice(CLI_AUTH_USER_CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def _normalize_user_code(value: str) -> str:
    user_code = value.strip().upper()
    raw = user_code.replace("-", "")
    if (
        len(user_code) != 9
        or user_code[4] != "-"
        or len(raw) != 8
        or any(character not in CLI_AUTH_USER_CODE_ALPHABET for character in raw)
    ):
        raise CLIAuthError("invalid_request", "授权码格式无效", status_code=400)
    return user_code


async def _generate_unique_user_code(db: AsyncSession) -> str:
    for _ in range(10):
        user_code = _generate_user_code()
        result = await db.execute(select(CLIAuthSession.id).filter(CLIAuthSession.user_code == user_code))
        if result.scalar_one_or_none() is None:
            return user_code
    raise RuntimeError("无法生成唯一 CLI 授权码")


def _expire_if_needed(session: CLIAuthSession, now=None) -> bool:
    now = now or utc_now_naive()
    if session.status in {CLI_AUTH_STATUS_PENDING, CLI_AUTH_STATUS_APPROVED} and session.expires_at <= now:
        session.status = CLI_AUTH_STATUS_EXPIRED
        return True
    return False


async def create_cli_auth_session(db: AsyncSession, key_name: str | None = None) -> tuple[CLIAuthSession, str]:
    device_code = _generate_device_code()
    now = utc_now_naive()
    session = CLIAuthSession(
        device_code_hash=_hash_secret(device_code),
        user_code=await _generate_unique_user_code(db),
        status=CLI_AUTH_STATUS_PENDING,
        key_name=(key_name or CLI_AUTH_DEFAULT_KEY_NAME).strip() or CLI_AUTH_DEFAULT_KEY_NAME,
        created_at=now,
        expires_at=now + timedelta(seconds=CLI_AUTH_SESSION_TTL_SECONDS),
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session, device_code


async def get_cli_auth_session_for_user(
    db: AsyncSession, user_code: str, *, for_update: bool = False
) -> CLIAuthSession:
    stmt = select(CLIAuthSession).filter(CLIAuthSession.user_code == _normalize_user_code(user_code))
    if for_update:
        stmt = stmt.with_for_update()
    result = await db.execute(stmt)
    session = result.scalar_one_or_none()
    if session is None:
        raise CLIAuthError("not_found", "授权会话不存在", status_code=404)
    if _expire_if_needed(session):
        await db.commit()
        raise CLIAuthError("expired_token", "授权会话已过期", status_code=410)
    return session


async def approve_cli_auth_session(db: AsyncSession, user_code: str, user: User) -> CLIAuthSession:
    session = await get_cli_auth_session_for_user(db, user_code, for_update=True)
    if session.status == CLI_AUTH_STATUS_CONSUMED:
        raise CLIAuthError("already_consumed", "授权会话已完成", status_code=409)
    if session.status == CLI_AUTH_STATUS_APPROVED:
        raise CLIAuthError("already_approved", "授权会话已批准", status_code=409)
    if session.status != CLI_AUTH_STATUS_PENDING:
        raise CLIAuthError("invalid_state", "授权会话状态无效", status_code=409)

    session.status = CLI_AUTH_STATUS_APPROVED
    session.approved_user_id = user.id
    session.approved_at = utc_now_naive()
    await db.commit()
    await db.refresh(session)
    return session


async def exchange_cli_auth_token(db: AsyncSession, device_code: str) -> dict:
    result = await db.execute(
        select(CLIAuthSession).filter(CLIAuthSession.device_code_hash == _hash_secret(device_code)).with_for_update()
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise CLIAuthError("invalid_request", "授权会话不存在", status_code=404)
    if _expire_if_needed(session):
        await db.commit()
        raise CLIAuthError("expired_token", "授权会话已过期", status_code=410)
    if session.status == CLI_AUTH_STATUS_PENDING:
        raise CLIAuthError("authorization_pending", "等待浏览器授权", status_code=400)
    if session.status == CLI_AUTH_STATUS_CONSUMED:
        raise CLIAuthError("already_consumed", "授权会话已完成", status_code=409)
    if session.status != CLI_AUTH_STATUS_APPROVED or not session.approved_user_id:
        raise CLIAuthError("invalid_state", "授权会话状态无效", status_code=409)

    user_result = await db.execute(
        select(User, Department.name)
        .outerjoin(Department, User.department_id == Department.id)
        .filter(User.id == session.approved_user_id, User.is_deleted == 0)
    )
    row = user_result.one_or_none()
    if row is None:
        raise CLIAuthError("invalid_user", "授权用户不存在", status_code=409)
    user, department_name = row
    if user.is_disabled:
        raise CLIAuthError("invalid_user", "授权用户已被停用", status_code=403)
    if user.department_id is None:
        raise CLIAuthError("department_required", "授权用户尚未绑定部门", status_code=400)

    full_key, key_hash, key_prefix = AuthUtils.generate_api_key()
    api_key = APIKey(
        key_hash=key_hash,
        key_prefix=key_prefix,
        name=session.key_name,
        user_id=user.id,
        department_id=user.department_id,
        created_by=str(user.id),
        # 设备流签发的 Key 非永久凭证：90 天滚动过期（OAuth 刷新令牌上线前的过渡措施）
        expires_at=utc_now_naive() + timedelta(days=DEVICE_API_KEY_TTL_DAYS),
    )
    db.add(api_key)
    await db.flush()

    session.status = CLI_AUTH_STATUS_CONSUMED
    session.api_key_id = api_key.id
    session.consumed_at = utc_now_naive()
    await db.flush()

    # P2：同时签发可旋转的设备会话对（新客户端优先使用，旧客户端继续用过渡 Key）
    device_session, refresh_token = await issue_device_session(db, user)
    access_token = AuthUtils.create_access_token(
        {"sub": str(user.id), "auth_version": user.auth_version, "sid": str(device_session.family_id)},
        expires_delta=timedelta(minutes=SESSION_ACCESS_TTL_MINUTES),
    )

    session.status = CLI_AUTH_STATUS_CONSUMED
    await db.commit()
    await db.refresh(api_key)

    user_data = user.to_dict()
    user_data["department_name"] = department_name

    # 隔离标识以数据库为权威（迁移已回填，终身不变）；兜底兼容极端未回填场景
    account_scope_id = user.account_scope_id or AuthUtils.account_scope_id(user.uid)

    return {
        "api_key": api_key.to_dict(),
        "secret": full_key,
        "user": user_data,
        "account_scope_id": account_scope_id,
        "session": {
            "session_id": str(device_session.family_id),
            "access_token": access_token,
            "refresh_token": refresh_token,
            "access_expires_in": SESSION_ACCESS_TTL_MINUTES * 60,
        },
    }


async def issue_device_session(db: AsyncSession, user: User) -> tuple[DeviceSession, str]:
    """创建会话族并签发首个刷新令牌。"""
    now = utc_now_naive()
    session = DeviceSession(family_id=str(uuid4()), uid=user.uid, status="active", created_at=now)
    db.add(session)
    await db.flush()

    refresh_token = "yxrt_" + secrets.token_urlsafe(48)
    db.add(
        DeviceSessionToken(
            session_id=session.id,
            token_hash=_hash_secret(refresh_token),
            expires_at=now + timedelta(days=SESSION_REFRESH_TTL_DAYS),
        )
    )
    await db.flush()
    return session, refresh_token


async def rotate_device_session_refresh(db: AsyncSession, refresh_token: str) -> dict:
    """旋转刷新令牌；检测到已消费令牌再次出示时撤销整个会话族（重放攻击）。"""
    result = await db.execute(
        select(DeviceSessionToken).filter(DeviceSessionToken.token_hash == _hash_secret(refresh_token))
    )
    token_row = result.scalar_one_or_none()
    if token_row is None:
        raise CLIAuthError("invalid_grant", "刷新令牌无效", status_code=401)

    session_result = await db.execute(
        select(DeviceSession).filter(DeviceSession.id == token_row.session_id).with_for_update()
    )
    session = session_result.scalar_one_or_none()
    if session is None or session.status != "active":
        raise CLIAuthError("session_revoked", "会话已撤销", status_code=401)

    now = utc_now_naive()
    if token_row.used_at is not None:
        # 重放：撤销整个会话族
        session.status = "revoked"
        await db.commit()
        raise CLIAuthError("reuse_detected", "检测到刷新令牌重放，会话已撤销", status_code=401)
    if token_row.expires_at <= now:
        raise CLIAuthError("invalid_grant", "刷新令牌已过期", status_code=401)

    user_result = await db.execute(select(User).filter(User.uid == session.uid, User.is_deleted == 0))
    user = user_result.scalar_one_or_none()
    if user is None or user.is_disabled:
        raise CLIAuthError("invalid_user", "账号不可用", status_code=403)

    token_row.used_at = now
    session.last_refreshed_at = now
    new_refresh = "yxrt_" + secrets.token_urlsafe(48)
    db.add(
        DeviceSessionToken(
            session_id=session.id,
            token_hash=_hash_secret(new_refresh),
            expires_at=now + timedelta(days=SESSION_REFRESH_TTL_DAYS),
        )
    )
    access_token = AuthUtils.create_access_token(
        {"sub": str(user.id), "auth_version": user.auth_version, "sid": str(session.family_id)},
        expires_delta=timedelta(minutes=SESSION_ACCESS_TTL_MINUTES),
    )
    await db.commit()
    return {
        "session_id": str(session.family_id),
        "uid": user.uid,
        "account_scope_id": user.account_scope_id or AuthUtils.account_scope_id(user.uid),
        "access_token": access_token,
        "refresh_token": new_refresh,
        "access_expires_in": SESSION_ACCESS_TTL_MINUTES * 60,
    }


async def revoke_device_session(db: AsyncSession, uid: str, family_id: str) -> bool:
    """用户主动下线某台设备：按会话族撤销。"""
    result = await db.execute(
        select(DeviceSession).filter(DeviceSession.family_id == family_id, DeviceSession.uid == uid)
    )
    session = result.scalar_one_or_none()
    if session is None:
        return False
    session.status = "revoked"
    await db.commit()
    return True


async def list_device_sessions(db: AsyncSession, uid: str) -> list[dict]:
    result = await db.execute(
        select(DeviceSession)
        .filter(DeviceSession.uid == uid, DeviceSession.status == "active")
        .order_by(DeviceSession.created_at.desc())
    )
    return [
        {
            "session_id": s.family_id,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "last_refreshed_at": s.last_refreshed_at.isoformat() if s.last_refreshed_at else None,
        }
        for s in result.scalars().all()
    ]
