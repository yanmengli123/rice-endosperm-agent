"""用户自带模型凭据（BYOK）服务：加密存储、解析链覆盖与 SSRF 校验。"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.models_business import (
    UserModelCredential,
)
from yuxi.utils.logging_config import logger
from yuxi.utils.secret_crypto import decrypt_secret, encrypt_secret


def _aad(uid: str, provider_id: str) -> str:
    return f"user-credential:{uid}:{provider_id}"


def _mask(api_key: str) -> str:
    if len(api_key) <= 10:
        return api_key[:2] + "****"
    return f"{api_key[:6]}****{api_key[-4:]}"


async def upsert_user_credential(
    db: AsyncSession,
    uid: str,
    provider_id: str,
    api_key: str,
    label: str | None = None,
) -> UserModelCredential:
    """创建或替换用户在某供应商下的凭据；密文落库，接口永不回显明文。"""
    result = await db.execute(
        select(UserModelCredential).where(
            UserModelCredential.uid == uid,
            UserModelCredential.provider_id == provider_id,
        )
    )
    credential = result.scalar_one_or_none()
    sealed = encrypt_secret(api_key, _aad(uid, provider_id))
    if credential is None:
        credential = UserModelCredential(
            uid=uid,
            provider_id=provider_id,
            label=(label or "我的凭据")[:128],
            api_key_ciphertext=sealed,
            masked_hint=_mask(api_key),
            status=UserModelCredential.CREDENTIAL_STATUS_ACTIVE,
        )
        db.add(credential)
    else:
        credential.label = (label or credential.label or "我的凭据")[:128]
        credential.api_key_ciphertext = sealed
        credential.masked_hint = _mask(api_key)
        credential.status = UserModelCredential.CREDENTIAL_STATUS_ACTIVE
        credential.revoked_at = None
    await db.flush()
    return credential


async def list_user_credentials(db: AsyncSession, uid: str) -> list[dict]:
    result = await db.execute(
        select(UserModelCredential).where(UserModelCredential.uid == uid).order_by(UserModelCredential.id)
    )
    return [
        {
            "credential_id": c.id,
            "provider_id": c.provider_id,
            "label": c.label,
            "masked_hint": c.masked_hint,
            "status": c.status,
            "last_tested_at": c.last_tested_at.isoformat() if c.last_tested_at else None,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c in result.scalars().all()
    ]


async def delete_user_credential(db: AsyncSession, uid: str, credential_id: int) -> bool:
    result = await db.execute(
        select(UserModelCredential).where(
            UserModelCredential.id == credential_id,
            UserModelCredential.uid == uid,
        )
    )
    credential = result.scalar_one_or_none()
    if credential is None:
        return False
    await db.delete(credential)
    await db.flush()
    return True


async def get_active_user_credential(
    db: AsyncSession, uid: str, provider_id: str
) -> UserModelCredential | None:
    result = await db.execute(
        select(UserModelCredential).where(
            UserModelCredential.uid == uid,
            UserModelCredential.provider_id == provider_id,
            UserModelCredential.status == UserModelCredential.CREDENTIAL_STATUS_ACTIVE,
        )
    )
    return result.scalar_one_or_none()


async def open_user_credential_key(db: AsyncSession, uid: str, credential_id: int) -> str | None:
    """Worker 执行期按凭据 id 解密；凭据已撤销/删除时返回 None（调用方回落平台 Key）。"""
    result = await db.execute(select(UserModelCredential).where(UserModelCredential.id == credential_id))
    credential = result.scalar_one_or_none()
    if (
        credential is None
        or credential.uid != uid
        or credential.status != UserModelCredential.CREDENTIAL_STATUS_ACTIVE
    ):
        return None
    return decrypt_secret(credential.api_key_ciphertext, _aad(uid, credential.provider_id))


async def resolve_user_provider_key(
    db: AsyncSession, uid: str, provider_id: str
) -> str | None:
    """返回该用户在此供应商下的活跃凭据明文；未配置则 None（回落平台凭据）。"""
    result = await db.execute(
        select(UserModelCredential).where(
            UserModelCredential.uid == uid,
            UserModelCredential.provider_id == provider_id,
            UserModelCredential.status == UserModelCredential.CREDENTIAL_STATUS_ACTIVE,
        )
    )
    credential = result.scalar_one_or_none()
    if credential is None:
        return None
    try:
        return decrypt_secret(credential.api_key_ciphertext, _aad(uid, provider_id))
    except Exception as exc:  # 主密钥轮换等：明确报错而非静默用平台 Key
        logger.error(f"用户凭据解密失败 uid={uid} provider={provider_id}: {exc}")
        raise


def validate_public_base_url(raw_url: str, *, allow_loopback: bool = False) -> str:
    """SSRF 防护：仅接受 http(s)，且主机不得为私网/环回/链路本地地址。

    管理员配置的平台供应商走独立通道不受此限制；本函数用于任何用户可提供的
    自定义 Base URL。
    """
    parsed = urlparse(str(raw_url).strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Base URL 必须是合法的 http(s) 地址")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Base URL 不允许携带凭据或片段")

    hostname = parsed.hostname.strip("[]").lower()
    is_loopback_host = hostname in {"localhost", "127.0.0.1", "::1"}
    if is_loopback_host:
        if allow_loopback:
            return parsed.geturl()
        raise ValueError("Base URL 不允许指向本机地址")
    try:
        addr = ipaddress.ip_address(hostname)
    except ValueError:
        return parsed.geturl()  # 域名：交由出站 DNS/代理层二次防护
    if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
        raise ValueError("Base URL 不允许指向内网或保留地址")
    return parsed.geturl()
