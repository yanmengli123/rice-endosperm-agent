import hashlib
import os
import re
import secrets
from urllib.parse import quote, urlparse
from yuxi.utils import logger

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status, UploadFile, File
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import APIKey, Department, OperationLog, User
from yuxi.repositories.user_repository import UserRepository
from yuxi.repositories.department_repository import DepartmentRepository
from server.utils.auth_middleware import (
    get_admin_user,
    get_authenticated_user,
    get_superadmin_user,
    get_db,
    get_required_user,
)
from yuxi.utils.auth_utils import AuthUtils
from yuxi.services.user_identity_service import generate_unique_uid, validate_username, is_valid_phone_number
from yuxi.services.operation_log_service import log_operation, resolve_operator_tenant_id
from yuxi.services.auth_service import (
    CLI_AUTH_POLL_INTERVAL_SECONDS,
    CLI_AUTH_SESSION_TTL_SECONDS,
    CLIAuthError,
    approve_cli_auth_session,
    create_cli_auth_session,
    exchange_cli_auth_token,
    get_cli_auth_session_for_user,
    list_device_sessions,
    revoke_device_session,
    rotate_device_session_refresh,
)
from yuxi.storage.minio import upload_image_to_minio
from yuxi.storage.minio.client import normalize_public_minio_url
from yuxi.utils.datetime_utils import utc_now_naive

# OIDC 认证相关导入
from yuxi.services.oidc_service import (
    get_oidc_config_handler,
    oidc_callback_handler,
    oidc_exchange_code_handler,
    oidc_login_url_handler,
)

# 创建路由器
auth = APIRouter(prefix="/auth", tags=["authentication"])

# 不存在账号时执行等成本的 Argon2 校验，避免通过响应时间枚举登录标识。
# 该哈希只对应固定占位文本，不是任何真实用户凭证。
DUMMY_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$SDj0/iiy9nalX9mDvqU25A$"
    "KKlDtRXMd/aecDyi04DAM/YiMJWhksK9xoi7A8EKchk"
)


# 请求和响应模型
class Token(BaseModel):
    access_token: str
    token_type: str
    user_id: int
    username: str
    uid: str  # 用于登录的user_id
    phone_number: str | None = None
    avatar: str | None = None
    role: str
    department_id: int | None = None
    department_name: str | None = None


class UserCreate(BaseModel):
    username: str
    password: str = Field(min_length=8)
    role: str = "user"
    phone_number: str | None = None
    department_id: int | None = None


class UserUpdate(BaseModel):
    username: str | None = None
    password: str | None = Field(default=None, min_length=8)
    role: str | None = None
    phone_number: str | None = None
    avatar: str | None = None
    department_id: int | None = None


class UserProfileUpdate(BaseModel):
    username: str | None = None
    phone_number: str | None = None


class UserResponse(BaseModel):
    id: int
    username: str
    uid: str
    phone_number: str | None = None
    avatar: str | None = None
    role: str
    department_id: int | None = None
    department_name: str | None = None  # 部门名称
    created_at: str
    last_login: str | None = None
    is_disabled: bool = False


class UserAccessOption(BaseModel):
    uid: str
    username: str
    role: str
    department_id: int | None = None
    department_name: str | None = None


class InitializeAdmin(BaseModel):
    uid: str  # 直接输入用户ID
    password: str = Field(min_length=8)
    phone_number: str | None = None


class UsernameValidation(BaseModel):
    username: str


class UidGeneration(BaseModel):
    username: str
    uid: str
    is_available: bool


class OIDCConfigResponse(BaseModel):
    """OIDC 配置响应"""

    enabled: bool
    login_url: str | None = None
    provider_name: str | None = "OIDC登录"


class OIDCLoginResponse(BaseModel):
    """OIDC 登录响应"""

    access_token: str
    token_type: str
    user_id: int
    username: str
    uid: str
    phone_number: str | None = None
    avatar: str | None = None
    role: str
    department_id: int | None = None
    department_name: str | None = None


class CLIAuthSessionCreate(BaseModel):
    key_name: str | None = Field(default=None, max_length=100)


class CLIAuthTokenRequest(BaseModel):
    device_code: str


class CLIAuthSessionCreateResponse(BaseModel):
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: int


class CLIAuthSessionResponse(BaseModel):
    user_code: str
    status: str
    key_name: str
    created_at: str
    expires_at: str
    approved_at: str | None = None


class CLIAuthApproveResponse(BaseModel):
    user_code: str
    status: str
    approved_at: str | None = None


class DeviceSessionPair(BaseModel):
    """P2 会话对：短时访问令牌（携带 sid 声明）+ 可旋转刷新令牌。"""

    session_id: str
    access_token: str
    refresh_token: str
    access_expires_in: int


class CLIAuthTokenResponse(BaseModel):
    api_key: dict
    secret: str
    user: dict
    account_scope_id: str
    # P2：会话对。缺失该字段的历史客户端回退静态 Key；v0.1.9+ 客户端优先使用会话。
    session: DeviceSessionPair | None = None


# =============================================================================
# === 工具函数 ===
# =============================================================================


def _raise_cli_auth_error(exc: CLIAuthError) -> None:
    raise HTTPException(
        status_code=exc.status_code,
        detail={"error": exc.code, "message": exc.message},
    ) from exc


# 路由：登录获取令牌
# =============================================================================
# === 认证分组 ===
# =============================================================================


@auth.post("/token", response_model=Token)
async def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends(), db: AsyncSession = Depends(get_db)):
    # 查找用户 - 支持user_id和phone_number登录
    login_identifier = form_data.username  # OAuth2表单中的username字段作为登录标识符

    # 尝试通过user_id查找
    result = await db.execute(select(User).filter(User.uid == login_identifier))
    user = result.scalar_one_or_none()

    # 如果通过user_id没找到，尝试通过phone_number查找
    if not user:
        result = await db.execute(select(User).filter(User.phone_number == login_identifier))
        user = result.scalar_one_or_none()

    # 如果用户不存在，执行一次真实 Argon2 校验以降低时序枚举风险。
    if not user:
        AuthUtils.verify_password(DUMMY_PASSWORD_HASH, form_data.password)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="登录标识或密码错误",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 验证密码
    if not AuthUtils.verify_password(user.password_hash, form_data.password):
        # 已删除/停用账号不累计失败次数，也不向未持有正确密码的人泄露账号状态。
        if user.is_deleted or getattr(user, "is_disabled", False):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="登录标识或密码错误",
                headers={"WWW-Authenticate": "Bearer"},
            )
        # 密码错误，增加失败次数
        user.increment_failed_login()
        await db.commit()

        # 记录失败操作
        await log_operation(db, user.id if user else None, "登录失败", f"密码错误，失败次数: {user.login_failed_count}")

        # 检查是否需要锁定
        if user.is_login_locked():
            remaining_time = user.get_remaining_lock_time()
            raise HTTPException(
                status_code=status.HTTP_423_LOCKED,
                detail=f"由于多次登录失败，账户已被锁定 {remaining_time} 秒",
                headers={"WWW-Authenticate": "Bearer", "X-Lock-Remaining": str(remaining_time)},
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="登录标识或密码错误",
                headers={"WWW-Authenticate": "Bearer"},
            )

    # 只有持有正确密码后才披露账号注销/停用状态。
    if user.is_deleted:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="该账户已注销",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if getattr(user, "is_disabled", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="账号已被停用，请联系管理员",
        )

    # 锁定期内即使密码正确也不能登录。
    if user.is_login_locked():
        remaining_time = user.get_remaining_lock_time()
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail=f"登录被锁定，请等待 {remaining_time} 秒后再试",
            headers={"WWW-Authenticate": "Bearer", "X-Lock-Remaining": str(remaining_time)},
        )

    # 登录成功，重置失败计数器
    user.reset_failed_login()
    user.last_login = utc_now_naive()
    await db.commit()

    # 生成访问令牌
    token_data = {"sub": str(user.id), "auth_version": user.auth_version}
    access_token = AuthUtils.create_access_token(token_data)

    # 记录登录操作
    await log_operation(db, user.id, "登录")

    # 获取部门名称
    department_name = None
    if user.department_id:
        result = await db.execute(select(Department.name).filter(Department.id == user.department_id))
        department_name = result.scalar_one_or_none()

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user_id": user.id,
        "username": user.username,
        "uid": user.uid,
        "phone_number": user.phone_number,
        "avatar": normalize_public_minio_url(user.avatar),
        "role": user.role,
        "department_id": user.department_id,
        "department_name": department_name,
    }


# =============================================================================
# === 自助注册分组 ===
# =============================================================================


class SelfRegisterRequest(BaseModel):
    uid: str = Field(..., description="登录标识，3-20 位字母/数字/下划线")
    username: str = Field(..., description="显示名称")
    password: str = Field(..., min_length=8, max_length=128, description="密码，8-128 位")
    invite_code: str | None = Field(None, max_length=256, description="邀请码（启用邀请码时必填）")


def _register_enabled() -> bool:
    return os.getenv("REGISTER_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


@auth.get("/register-config")
async def get_register_config():
    """返回非敏感注册开关，供登录页决定是否展示自助注册入口。"""
    return {
        "enabled": _register_enabled(),
        "invite_required": bool(os.getenv("REGISTER_INVITE_CODE", "").strip()),
    }


@auth.post("/register")
async def self_register(data: SelfRegisterRequest, db: AsyncSession = Depends(get_db)):
    """自助注册普通用户；由 REGISTER_ENABLED 显式开启，可选邀请码与默认部门。"""
    if not _register_enabled():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="系统未开放自助注册")

    invite_code = os.getenv("REGISTER_INVITE_CODE", "").strip()
    provided_invite = (data.invite_code or "").strip()
    if invite_code and not secrets.compare_digest(
        hashlib.sha256(provided_invite.encode("utf-8")).digest(),
        hashlib.sha256(invite_code.encode("utf-8")).digest(),
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="邀请码不正确")

    if not re.match(r"^[a-zA-Z0-9_]+$", data.uid) or not (3 <= len(data.uid) <= 20):
        detail_msg = "用户ID只能包含字母、数字和下划线，长度3-20"
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=detail_msg,
        )

    username_ok, username_error = validate_username(data.username)
    if not username_ok:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=username_error)

    existing = (
        await db.execute(select(User).filter((User.uid == data.uid) | (User.username == data.username)))
    ).scalar_one_or_none()
    if existing:
        conflict = "登录标识" if existing.uid == data.uid else "显示名称"
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"该{conflict}已被使用")

    department_id = None
    default_department_id = os.getenv("REGISTER_DEPARTMENT_ID", "").strip()
    if default_department_id.isdigit():
        department = (
            await db.execute(select(Department).filter(Department.id == int(default_department_id)))
        ).scalar_one_or_none()
        if department is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="注册部门配置无效")
        department_id = department.id

    new_user = User(
        username=data.username.strip(),
        uid=data.uid,
        password_hash=AuthUtils.hash_password(data.password),
        role="user",
        department_id=department_id,
        last_login=utc_now_naive(),
    )
    db.add(new_user)
    try:
        await db.flush()
        from yuxi.services.principal import ensure_tenant_membership

        await ensure_tenant_membership(db, new_user)
        db.add(
            OperationLog(
                user_id=new_user.id,
                tenant_id=await resolve_operator_tenant_id(db, new_user.id),
                operation="自助注册",
                details=f"uid={new_user.uid}",
            )
        )
        await db.commit()
        await db.refresh(new_user)
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="登录标识或显示名称已被使用") from exc

    access_token = AuthUtils.create_access_token(
        {"sub": str(new_user.id), "auth_version": new_user.auth_version}
    )
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user_id": new_user.id,
        "username": new_user.username,
        "uid": new_user.uid,
        "role": new_user.role,
        "department_id": new_user.department_id,
    }


# =============================================================================
# === CLI 浏览器登录授权分组 ===
# =============================================================================


@auth.post("/cli/sessions", response_model=CLIAuthSessionCreateResponse)
async def create_cli_session(data: CLIAuthSessionCreate, db: AsyncSession = Depends(get_db)):
    public_web_url = os.getenv("YUXI_PUBLIC_WEB_URL", "http://localhost:5175").strip().rstrip("/")
    parsed_web_url = urlparse(public_web_url)
    is_loopback = parsed_web_url.hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        not parsed_web_url.hostname
        or parsed_web_url.username
        or parsed_web_url.password
        or parsed_web_url.query
        or parsed_web_url.fragment
        or (parsed_web_url.scheme != "https" and not (parsed_web_url.scheme == "http" and is_loopback))
    ):
        raise HTTPException(status_code=500, detail="YUXI_PUBLIC_WEB_URL 配置无效")
    session, device_code = await create_cli_auth_session(db, key_name=data.key_name)
    verification_uri = f"{public_web_url}/auth/cli/authorize"
    return CLIAuthSessionCreateResponse(
        device_code=device_code,
        user_code=session.user_code,
        verification_uri=verification_uri,
        verification_uri_complete=f"{verification_uri}?user_code={quote(session.user_code)}",
        expires_in=CLI_AUTH_SESSION_TTL_SECONDS,
        interval=CLI_AUTH_POLL_INTERVAL_SECONDS,
    )


@auth.get("/cli/sessions/{user_code}", response_model=CLIAuthSessionResponse)
async def get_cli_session(
    user_code: str,
    _current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        session = await get_cli_auth_session_for_user(db, user_code)
    except CLIAuthError as exc:
        _raise_cli_auth_error(exc)
    return CLIAuthSessionResponse(**session.to_dict())


@auth.post("/cli/sessions/{user_code}/approve", response_model=CLIAuthApproveResponse)
async def approve_cli_session(
    user_code: str,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        session = await approve_cli_auth_session(db, user_code, current_user)
    except CLIAuthError as exc:
        _raise_cli_auth_error(exc)
    return CLIAuthApproveResponse(**session.to_dict())


@auth.post("/cli/sessions/token", response_model=CLIAuthTokenResponse)
async def exchange_cli_session_token(data: CLIAuthTokenRequest, db: AsyncSession = Depends(get_db)):
    try:
        return await exchange_cli_auth_token(db, data.device_code)
    except CLIAuthError as exc:
        _raise_cli_auth_error(exc)


# 路由：校验是否需要初始化管理员
@auth.get("/check-first-run")
async def check_first_run():
    is_first_run = await pg_manager.async_check_first_run()
    return {"first_run": is_first_run}


# 路由：初始化管理员账户
@auth.post("/initialize", response_model=Token)
async def initialize_admin(admin_data: InitializeAdmin, db: AsyncSession = Depends(get_db)):
    # 检查是否是首次运行
    if not await pg_manager.async_check_first_run():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="系统已经初始化，无法再次创建初始管理员",
        )

    # 创建管理员账户
    hashed_password = AuthUtils.hash_password(admin_data.password)

    # 验证用户ID格式（只支持字母数字和下划线）
    if not re.match(r"^[a-zA-Z0-9_]+$", admin_data.uid):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="用户ID只能包含字母、数字和下划线",
        )

    if len(admin_data.uid) < 3 or len(admin_data.uid) > 20:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="用户ID长度必须在3-20个字符之间",
        )

    # 验证手机号格式（如果提供了）
    if admin_data.phone_number and not is_valid_phone_number(admin_data.phone_number):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="手机号格式不正确")

    # 由于是首次初始化，直接使用输入的user_id
    uid = admin_data.uid

    # 创建默认部门
    dept_repo = DepartmentRepository()
    default_department = await dept_repo.create(
        {
            "name": "默认部门",
            "description": "系统初始化时创建的默认部门",
        }
    )

    # 创建管理员用户
    user_repo = UserRepository()
    new_admin = await user_repo.create(
        {
            "username": admin_data.uid,
            "uid": uid,
            "phone_number": admin_data.phone_number,
            "avatar": None,
            "password_hash": hashed_password,
            "role": "superadmin",
            "department_id": default_department.id,
            "last_login": utc_now_naive(),
        }
    )

    from yuxi.services.principal import ensure_tenant_membership

    await ensure_tenant_membership(db, new_admin)

    # 生成访问令牌
    token_data = {"sub": str(new_admin.id), "auth_version": new_admin.auth_version}
    access_token = AuthUtils.create_access_token(token_data)

    # 记录操作
    await log_operation(db, new_admin.id, "系统初始化", "创建超级管理员账户")

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user_id": new_admin.id,
        "username": new_admin.username,
        "uid": new_admin.uid,
        "phone_number": new_admin.phone_number,
        "avatar": new_admin.avatar,
        "role": new_admin.role,
    }


# 路由：获取当前用户信息
# =============================================================================
# === 用户信息分组 ===
# =============================================================================


@auth.get("/me", response_model=UserResponse)
async def read_users_me(current_user: User = Depends(get_authenticated_user), db: AsyncSession = Depends(get_db)):
    """获取当前登录用户的个人信息"""
    user_dict = current_user.to_dict()

    if current_user.department_id:
        result = await db.execute(select(Department.name).filter(Department.id == current_user.department_id))
        user_dict["department_name"] = result.scalar_one_or_none()

    return user_dict


# 路由：更新个人资料
@auth.put("/profile", response_model=UserResponse)
async def update_profile(
    profile_data: UserProfileUpdate,
    request: Request,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """更新当前用户的个人资料"""
    update_details = []

    # 更新用户名（仅允许修改显示名，不修改 user_id）
    if profile_data.username is not None:
        # 验证用户名格式
        is_valid, error_msg = validate_username(profile_data.username)
        if not is_valid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_msg,
            )

        # 检查用户名是否已被其他用户使用
        result = await db.execute(
            select(User).filter(User.username == profile_data.username, User.id != current_user.id)
        )
        existing_user = result.scalar_one_or_none()
        if existing_user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="用户名已存在",
            )

        current_user.username = profile_data.username
        update_details.append(f"用户名: {profile_data.username}")

    # 更新手机号
    if profile_data.phone_number is not None:
        # 如果手机号不为空，验证格式
        if profile_data.phone_number and not is_valid_phone_number(profile_data.phone_number):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="手机号格式不正确")

        # 检查手机号是否已被其他用户使用
        if profile_data.phone_number:
            result = await db.execute(
                select(User).filter(User.phone_number == profile_data.phone_number, User.id != current_user.id)
            )
            existing_phone = result.scalar_one_or_none()
            if existing_phone:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="手机号已被其他用户使用")

        current_user.phone_number = profile_data.phone_number
        update_details.append(f"手机号: {profile_data.phone_number or '已清空'}")

    await db.commit()

    # 记录操作
    if update_details:
        await log_operation(db, current_user.id, "更新个人资料", f"更新个人资料: {', '.join(update_details)}", request)

    return current_user.to_dict()


# 路由：创建新用户（管理员权限）
# =============================================================================
# === 用户管理分组 ===
# =============================================================================


@auth.post("/users", response_model=UserResponse)
async def create_user(
    user_data: UserCreate,
    request: Request,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """创建新用户（管理员权限）"""
    user_repo = UserRepository()

    # 验证用户名
    is_valid, error_msg = validate_username(user_data.username)
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_msg,
        )

    # 检查用户名是否已存在
    users = await user_repo.list_users()
    if any(u.username == user_data.username for u in users):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="用户名已存在",
        )

    # 检查手机号是否已存在（如果提供了）
    if user_data.phone_number:
        if await user_repo.exists_by_phone(user_data.phone_number):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="手机号已存在",
            )

    # 生成唯一的 uid
    existing_uids = await user_repo.get_all_uids()
    uid = generate_unique_uid(user_data.username, existing_uids)

    # 创建新用户
    hashed_password = AuthUtils.hash_password(user_data.password)

    # 检查角色权限
    # 禁止创建超级管理员账户（系统只能有一个超级管理员）
    if user_data.role == "superadmin":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="不能创建超级管理员账户",
        )

    # 管理员只能创建普通用户
    if current_user.role == "admin" and user_data.role != "user":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="管理员只能创建普通用户账户",
        )

    # 部门分配逻辑
    if current_user.role == "superadmin":
        # 超级管理员创建用户时，使用指定的部门或默认部门
        department_id = user_data.department_id
        if department_id is None:
            # 获取默认部门
            dept_repo = DepartmentRepository()
            departments = await dept_repo.list_departments()
            default_dept = next((d for d in departments if d.name == "默认部门"), None)
            department_id = default_dept.id if default_dept else None
    else:
        # 普通管理员创建用户时，自动继承该管理员的部门
        department_id = current_user.department_id
        if department_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="管理员必须属于部门才能创建用户",
            )
        # 非超级管理员不能指定部门
        if user_data.department_id is not None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="普通管理员不能指定部门",
            )

    new_user = await user_repo.create(
        {
            "username": user_data.username,
            "uid": uid,
            "phone_number": user_data.phone_number,
            "password_hash": hashed_password,
            "role": user_data.role,
            "department_id": department_id,
        }
    )

    # P1：开户流程显式把新用户加入创建者的活动租户。
    from yuxi.services.principal import ensure_tenant_membership, resolve_principal

    principal = await resolve_principal(db, current_user)
    await ensure_tenant_membership(db, new_user, tenant_id=principal.tenant_id)

    # 记录操作
    await log_operation(
        db, current_user.id, "创建用户", f"创建用户: {user_data.username}, 角色: {user_data.role}", request
    )

    return new_user.to_dict()


# 路由：获取所有用户（管理员权限）
@auth.get("/users", response_model=list[UserResponse])
async def read_users(
    skip: int = 0, limit: int = 100, current_user: User = Depends(get_admin_user), db: AsyncSession = Depends(get_db)
):
    user_repo = UserRepository()

    # 部门隔离逻辑
    if current_user.role == "superadmin":
        # 超级管理员可以看到所有用户
        users_with_dept = await user_repo.list_with_department(skip=skip, limit=limit)
    else:
        # 普通管理员只能看到本部门用户
        users_with_dept = await user_repo.list_with_department(
            skip=skip, limit=limit, department_id=current_user.department_id
        )

    users = []
    for user, dept_name in users_with_dept:
        user_dict = user.to_dict()
        user_dict["department_name"] = dept_name
        users.append(user_dict)
    return users


def _ensure_user_in_current_department(current_user: User, target_user: User) -> None:
    if current_user.role == "superadmin":
        return
    if target_user.department_id != current_user.department_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="只能管理本部门用户",
        )


@auth.get("/users/access-options", response_model=list[UserAccessOption])
async def read_user_access_options(
    skip: int = 0,
    limit: int = 1000,
    current_user: User = Depends(get_admin_user),
):
    user_repo = UserRepository()
    if current_user.role == "superadmin":
        users_with_dept = await user_repo.list_with_department(skip=skip, limit=limit)
    else:
        users_with_dept = await user_repo.list_with_department(
            skip=skip, limit=limit, department_id=current_user.department_id
        )
    return [
        {
            "uid": user.uid,
            "username": user.username,
            "role": user.role,
            "department_id": user.department_id,
            "department_name": dept_name,
        }
        for user, dept_name in users_with_dept
    ]


# 路由：获取特定用户信息（管理员权限）
@auth.get("/users/{user_id}", response_model=UserResponse)
async def read_user(user_id: int, current_user: User = Depends(get_admin_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).filter(User.id == user_id, User.is_deleted == 0))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="用户不存在",
        )
    _ensure_user_in_current_department(current_user, user)
    return user.to_dict()


# 路由：更新用户信息（管理员权限）
@auth.put("/users/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: int,
    user_data: UserUpdate,
    request: Request,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).filter(User.id == user_id, User.is_deleted == 0))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="用户不存在",
        )

    _ensure_user_in_current_department(current_user, user)

    # 检查权限
    if user.role == "superadmin" and current_user.role != "superadmin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="只有超级管理员才能修改超级管理员账户",
        )

    # 超级管理员账户不能被降级（只能由其他超级管理员修改）
    if user.role == "superadmin" and user_data.role and user_data.role != "superadmin" and current_user.id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="不能降级超级管理员账户",
        )

    if current_user.role == "admin":
        if user.role != "user":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="管理员只能修改普通用户账户",
            )
        if user_data.role is not None and user_data.role != "user":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="管理员只能将用户角色设置为普通用户",
            )

    # 更新信息
    update_details = []
    invalidate_sessions = False

    if user_data.username is not None:
        # 检查用户名是否已被其他用户使用
        result = await db.execute(select(User).filter(User.username == user_data.username, User.id != user_id))
        existing_user = result.scalar_one_or_none()
        if existing_user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="用户名已存在",
            )
        user.username = user_data.username
        update_details.append(f"用户名: {user_data.username}")

    if user_data.password is not None:
        user.password_hash = AuthUtils.hash_password(user_data.password)
        invalidate_sessions = True
        update_details.append("密码已更新")

    if user_data.role is not None:
        # 检查是否将管理员降级为普通用户
        if user.role == "admin" and user_data.role == "user" and user.department_id is not None:
            admin_count = await UserRepository().get_admin_count_in_department(
                user.department_id, exclude_user_id=user_id
            )
            if admin_count <= 1:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="不能将管理员降级为普通用户，因为该用户是当前部门的唯一管理员",
                )
        user.role = user_data.role
        invalidate_sessions = True
        update_details.append(f"角色: {user_data.role}")

    if user_data.phone_number is not None:
        if user_data.phone_number and not is_valid_phone_number(user_data.phone_number):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="手机号格式不正确")
        if user_data.phone_number:
            existing_phone = (
                await db.execute(
                    select(User).filter(User.phone_number == user_data.phone_number, User.id != user_id)
                )
            ).scalar_one_or_none()
            if existing_phone:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="手机号已被使用")
        user.phone_number = user_data.phone_number
        update_details.append(f"手机号: {user_data.phone_number or '已清空'}")

    if user_data.avatar is not None:
        user.avatar = user_data.avatar
        update_details.append(f"头像: {user_data.avatar or '已清空'}")

    # 部门修改权限控制（只有超级管理员可以修改用户部门）
    if user_data.department_id is not None and user_data.department_id != user.department_id:
        if current_user.role != "superadmin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="只有超级管理员才能修改用户部门",
            )

        department = (
            await db.execute(select(Department).filter(Department.id == user_data.department_id))
        ).scalar_one_or_none()
        if department is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="目标部门不存在")

        # 检查该用户是否是当前部门的唯一管理员
        if user.role == "admin" and user.department_id is not None:
            admin_count = await UserRepository().get_admin_count_in_department(
                user.department_id, exclude_user_id=user_id
            )
            if admin_count <= 1:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="不能修改该用户的部门，因为该用户是当前部门的唯一管理员",
                )

        enabled_keys = (
            await db.execute(select(APIKey).filter(APIKey.user_id == user.id, APIKey.is_enabled.is_(True)))
        ).scalars().all()
        for api_key in enabled_keys:
            api_key.is_enabled = False

        user.department_id = user_data.department_id
        invalidate_sessions = True
        update_details.append(f"部门ID: {user_data.department_id}，已停用旧部门 API Key {len(enabled_keys)} 个")

    if invalidate_sessions:
        user.auth_version += 1

    await db.commit()

    # 记录操作
    await log_operation(db, current_user.id, "更新用户", f"更新用户ID {user_id}: {', '.join(update_details)}", request)

    return user.to_dict()


# 路由：删除用户（管理员权限）
@auth.delete("/users/{user_id}", response_model=dict)
async def delete_user(
    user_id: int, request: Request, current_user: User = Depends(get_admin_user), db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(User).filter(User.id == user_id, User.is_deleted == 0))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="用户不存在",
        )

    _ensure_user_in_current_department(current_user, user)

    # 不能删除超级管理员账户
    if user.role == "superadmin":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="不能删除超级管理员账户",
        )

    if current_user.role == "admin" and user.role != "user":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="管理员只能删除普通用户账户",
        )

    # 检查是否是部门的唯一管理员
    if user.role == "admin" and current_user.role != "superadmin":
        result = await db.execute(
            select(func.count(User.id)).filter(
                User.department_id == user.department_id, User.role == "admin", User.is_deleted == 0
            )
        )
        admin_count = result.scalar()
        if admin_count <= 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="不能删除部门唯一的管理员",
            )

    # 不能删除自己的账户
    if user.id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="不能删除自己的账户",
        )

    # 检查是否已经被删除
    if user.is_deleted:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="该用户已经被删除",
        )

    deletion_detail = f"删除用户: {user.username}, ID: {user.id}, 角色: {user.role}"

    user.is_deleted = 1
    user.deleted_at = utc_now_naive()
    user.username = f"已注销用户-{user.id}"
    user.phone_number = None  # 清空手机号，释放该手机号供其他用户使用
    user.password_hash = "DELETED"  # 禁止登录
    user.avatar = None  # 清空头像
    api_key_result = await db.execute(select(APIKey).filter(APIKey.user_id == user.id))
    for api_key in api_key_result.scalars().all():
        api_key.is_enabled = False

    await db.commit()

    # 记录操作
    await log_operation(db, current_user.id, "删除用户", deletion_detail, request)

    return {"success": True, "message": "用户已删除"}


# 路由：验证用户名并生成user_id
@auth.post("/validate-username", response_model=UidGeneration)
async def validate_username_and_generate_uid(
    validation_data: UsernameValidation,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """验证用户名格式并生成可用的user_id"""
    # 验证用户名格式
    is_valid, error_msg = validate_username(validation_data.username)
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_msg,
        )

    # 检查用户名是否已存在
    result = await db.execute(select(User).filter(User.username == validation_data.username))
    existing_user = result.scalar_one_or_none()
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="用户名已存在",
        )

    # 生成唯一的 uid
    result = await db.execute(select(User.uid))
    existing_uids = [uid for (uid,) in result.all()]
    uid = generate_unique_uid(validation_data.username, existing_uids)

    return UidGeneration(username=validation_data.username, uid=uid, is_available=True)


# 路由：检查 uid 是否可用
@auth.get("/check-uid/{uid}")
async def check_uid_availability(
    uid: str, current_user: User = Depends(get_admin_user), db: AsyncSession = Depends(get_db)
):
    """检查 uid 是否可用"""
    result = await db.execute(select(User).filter(User.uid == uid))
    existing_user = result.scalar_one_or_none()
    return {"uid": uid, "is_available": existing_user is None}


# 路由：上传用户头像
@auth.post("/upload-avatar")
async def upload_user_avatar(
    file: UploadFile = File(...), current_user: User = Depends(get_required_user), db: AsyncSession = Depends(get_db)
):
    """上传用户头像"""
    try:
        avatar_url = await upload_image_to_minio(
            file,
            object_prefix=f"avatar/{current_user.id}",
            max_size_bytes=5 * 1024 * 1024,
            too_large_message="文件大小不能超过5MB",
        )

        current_user.avatar = avatar_url
        await db.commit()
        await log_operation(db, current_user.id, "上传头像", f"更新头像: {avatar_url}")

        return {"success": True, "avatar_url": avatar_url, "message": "头像上传成功"}

    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"头像上传失败: {str(e)}")


# 路由：模拟用户登录（超级管理员专用）
@auth.post("/impersonate/{user_id}", response_model=Token)
async def impersonate_user(
    user_id: int,
    request: Request,
    current_user: User = Depends(get_superadmin_user),
    db: AsyncSession = Depends(get_db),
):
    """超级管理员模拟其他用户登录"""
    # 查找目标用户
    result = await db.execute(select(User).filter(User.id == user_id, User.is_deleted == 0))
    target_user = result.scalar_one_or_none()
    if target_user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="用户不存在",
        )

    # 不能模拟超级管理员
    if target_user.role == "superadmin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="不能模拟超级管理员账户",
        )

    # 生成访问令牌
    token_data = {"sub": str(target_user.id), "auth_version": target_user.auth_version}
    access_token = AuthUtils.create_access_token(token_data)

    # 获取部门名称
    department_name = None
    if target_user.department_id:
        result = await db.execute(select(Department.name).filter(Department.id == target_user.department_id))
        department_name = result.scalar_one_or_none()

    # 记录操作（危险操作标记）
    await log_operation(db, current_user.id, "⚠️ 危险操作-模拟用户", f"模拟用户: {target_user.username}", request)

    # 控制台警告日志
    logger.warning(f"⚠️ [危险操作] 超级管理员 {current_user.username} 模拟登录用户: {target_user.username}")

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user_id": target_user.id,
        "username": target_user.username,
        "uid": target_user.uid,
        "phone_number": target_user.phone_number,
        "avatar": normalize_public_minio_url(target_user.avatar),
        "role": target_user.role,
        "department_id": target_user.department_id,
        "department_name": department_name,
    }


# =============================================================================
# === OIDC 认证分组 ===
# =============================================================================


@auth.post("/cli/token/refresh")
async def refresh_cli_token(payload: dict, db: AsyncSession = Depends(get_db)):
    """P2：旋转设备会话刷新令牌；重放检测触发会话族撤销。"""
    refresh_token = str(payload.get("refresh_token") or "")
    if not refresh_token:
        raise HTTPException(status_code=400, detail="缺少 refresh_token")
    try:
        return await rotate_device_session_refresh(db, refresh_token)
    except CLIAuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@auth.get("/sessions")
async def list_my_sessions(
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """列出当前用户的活跃设备会话。"""
    return {"sessions": await list_device_sessions(db, current_user.uid)}


@auth.delete("/sessions/{family_id}")
async def revoke_my_session(
    family_id: str,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """下线指定设备会话。"""
    ok = await revoke_device_session(db, current_user.uid, family_id)
    if not ok:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"success": True}


@auth.get("/oidc/config", response_model=OIDCConfigResponse)
async def get_oidc_config():
    """获取 OIDC 配置（供前端使用）"""
    return await get_oidc_config_handler()


@auth.get("/oidc/login-url")
async def get_oidc_login_url(redirect_path: str = "/"):
    """获取 OIDC 登录 URL"""
    return await oidc_login_url_handler(redirect_path)


@auth.get("/oidc/callback", response_class=RedirectResponse)
async def oidc_callback(request: Request, code: str, state: str, db: AsyncSession = Depends(get_db)):
    """处理 OIDC 回调 - 重定向到前端 Vue 路由"""
    return await oidc_callback_handler(code, state, db, request)


@auth.post("/oidc/exchange-code", response_model=OIDCLoginResponse)
async def oidc_exchange_code(code: str = Body(..., embed=True)):
    """使用一次性 code 交换 OIDC 登录数据"""
    return await oidc_exchange_code_handler(code)
