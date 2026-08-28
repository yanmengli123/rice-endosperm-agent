"""用户自带模型凭据（BYOK）服务：加密存储、解析链覆盖与 SSRF 校验。"""

from __future__ import annotations

import ipaddress
import json
import re
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from yuxi.storage.postgres.models_business import (
    UserModelCredential,
    UserModelPreference,
)
from yuxi.utils.logging_config import logger
from yuxi.utils.secret_crypto import decrypt_secret, encrypt_secret

CUSTOM_MODEL_PROTOCOLS = {"openai", "anthropic"}
CUSTOM_PROVIDER_IDS = {
    "openai": "user-openai-compatible",
    "anthropic": "user-anthropic-compatible",
}
MAX_CONFIGURATION_JSON_BYTES = 64 * 1024
_MARKDOWN_URL_RE = re.compile(r"^\[(https?://[^\]]+)]\((https?://[^)]+)\)$")


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
    *,
    tenant_id: int | None = None,
    protocol: str | None = None,
    base_url: str | None = None,
    model_id: str | None = None,
) -> UserModelCredential:
    """签发新版本的 BYOK 凭据（P5 版本化不可变）。

    替换密钥 = 旧行置 superseded 并指向新行；历史 run 冻结的 credential_id
    永远指向不可变的历史版本。明文只在本函数内存中出现，落库即密文。
    """
    result = await db.execute(
        select(UserModelCredential).where(
            UserModelCredential.uid == uid,
            UserModelCredential.provider_id == provider_id,
            UserModelCredential.status == UserModelCredential.CREDENTIAL_STATUS_ACTIVE,
        )
    )
    previous = result.scalar_one_or_none()
    sealed = encrypt_secret(api_key, _aad(uid, provider_id))
    credential = UserModelCredential(
        uid=uid,
        provider_id=provider_id,
        label=(label or (previous.label if previous else None) or "我的凭据")[:128],
        api_key_ciphertext=sealed,
        protocol=protocol or (previous.protocol if previous else None),
        base_url=base_url or (previous.base_url if previous else None),
        model_id=model_id or (previous.model_id if previous else None),
        masked_hint=_mask(api_key),
        status=UserModelCredential.CREDENTIAL_STATUS_ACTIVE,
        version=(previous.version + 1) if previous else 1,
        tenant_id=tenant_id or (previous.tenant_id if previous else None),
    )
    db.add(credential)
    await db.flush()
    if previous is not None:
        previous.status = UserModelCredential.CREDENTIAL_STATUS_SUPERSEDED
        previous.superseded_by_id = credential.id
        await db.flush()
    return credential


async def revoke_user_credential(db: AsyncSession, uid: str, credential_id: int) -> bool:
    """逻辑撤销：保留行供历史 run 审计与用量对账。"""
    result = await db.execute(
        select(UserModelCredential).where(
            UserModelCredential.id == credential_id,
            UserModelCredential.uid == uid,
        )
    )
    credential = result.scalar_one_or_none()
    if credential is None:
        return False
    from yuxi.utils.datetime_utils import utc_now_naive

    credential.status = UserModelCredential.CREDENTIAL_STATUS_REVOKED
    credential.revoked_at = utc_now_naive()
    model_spec = custom_model_spec(credential)
    if model_spec:
        preference_result = await db.execute(
            select(UserModelPreference).where(UserModelPreference.uid == uid)
        )
        preference = preference_result.scalar_one_or_none()
        if preference is not None and preference.chat_model_spec == model_spec:
            preference.chat_model_spec = None
            preference.updated_by = uid
    await db.flush()
    return True


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
            "protocol": c.protocol,
            "base_url": c.base_url,
            "model_id": c.model_id,
            "model_spec": custom_model_spec(c),
            "status": c.status,
            "last_tested_at": c.last_tested_at.isoformat() if c.last_tested_at else None,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c in result.scalars().all()
    ]


async def delete_user_credential(db: AsyncSession, uid: str, credential_id: int) -> bool:
    """用户侧"删除"= 逻辑撤销（append 审计语义：历史 run 的凭据引用仍可追溯）。"""
    return await revoke_user_credential(db, uid, credential_id)


async def get_active_user_credential(db: AsyncSession, uid: str, provider_id: str) -> UserModelCredential | None:
    result = await db.execute(
        select(UserModelCredential).where(
            UserModelCredential.uid == uid,
            UserModelCredential.provider_id == provider_id,
            UserModelCredential.status == UserModelCredential.CREDENTIAL_STATUS_ACTIVE,
        )
    )
    return result.scalar_one_or_none()


async def open_user_credential_key(
    db: AsyncSession,
    uid: str,
    credential_id: int,
    *,
    expected_provider_id: str,
) -> str | None:
    """按冻结引用解密；同时校验所有者、状态和供应商，防止引用被挪用。"""
    result = await db.execute(select(UserModelCredential).where(UserModelCredential.id == credential_id))
    credential = result.scalar_one_or_none()
    if (
        credential is None
        or credential.uid != uid
        or credential.provider_id != expected_provider_id
        or credential.status != UserModelCredential.CREDENTIAL_STATUS_ACTIVE
    ):
        return None
    return decrypt_secret(credential.api_key_ciphertext, _aad(uid, credential.provider_id))


async def open_user_credential_runtime(
    db: AsyncSession,
    uid: str,
    credential_id: int,
    *,
    expected_provider_id: str,
) -> dict[str, str | None] | None:
    """解封运行时凭据及其用户级端点；所有归属和状态检查均失败关闭。"""
    result = await db.execute(select(UserModelCredential).where(UserModelCredential.id == credential_id))
    credential = result.scalar_one_or_none()
    if (
        credential is None
        or credential.uid != uid
        or credential.provider_id != expected_provider_id
        or credential.status != UserModelCredential.CREDENTIAL_STATUS_ACTIVE
    ):
        return None
    return {
        "api_key": decrypt_secret(
            credential.api_key_ciphertext,
            _aad(uid, credential.provider_id),
        ),
        "protocol": credential.protocol,
        "base_url": credential.base_url,
        "model_id": credential.model_id,
        "model_spec": custom_model_spec(credential),
    }


def custom_model_spec(credential: UserModelCredential) -> str | None:
    if credential.protocol not in CUSTOM_MODEL_PROTOCOLS or not credential.model_id or not credential.base_url:
        return None
    return f"{credential.provider_id}:{credential.model_id}"


async def list_active_custom_model_specs(db: AsyncSession, uid: str) -> set[str]:
    result = await db.execute(
        select(UserModelCredential).where(
            UserModelCredential.uid == uid,
            UserModelCredential.status == UserModelCredential.CREDENTIAL_STATUS_ACTIVE,
            UserModelCredential.protocol.is_not(None),
            UserModelCredential.base_url.is_not(None),
            UserModelCredential.model_id.is_not(None),
        )
    )
    return {spec for credential in result.scalars().all() if (spec := custom_model_spec(credential))}


async def resolve_user_provider_key(db: AsyncSession, uid: str, provider_id: str) -> str | None:
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
    normalized = str(raw_url).strip()
    markdown_match = _MARKDOWN_URL_RE.fullmatch(normalized)
    if markdown_match:
        if markdown_match.group(1) != markdown_match.group(2):
            raise ValueError("Base URL 的 Markdown 链接文本与目标不一致")
        normalized = markdown_match.group(1)
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Base URL 必须是合法的 http(s) 地址")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Base URL 不允许携带凭据、查询参数或片段")

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


def validate_custom_model_configuration(
    *,
    protocol: str,
    base_url: str,
    api_key: str,
    model_id: str,
) -> dict[str, str]:
    normalized_protocol = str(protocol).strip().lower()
    if normalized_protocol not in CUSTOM_MODEL_PROTOCOLS:
        raise ValueError("API 协议仅支持 openai 或 anthropic")
    normalized_key = str(api_key).strip()
    normalized_model = str(model_id).strip()
    if len(normalized_key) < 4 or len(normalized_key) > 500:
        raise ValueError("API Key 长度必须为 4 到 500 个字符")
    if not normalized_model or len(normalized_model) > 255 or any(ord(char) < 32 for char in normalized_model):
        raise ValueError("model 必须为不超过 255 个字符的有效模型名称")
    validated_base_url = validate_public_base_url(base_url)
    if urlparse(validated_base_url).scheme != "https":
        raise ValueError("用户级 API Base URL 必须使用 HTTPS")
    return {
        "protocol": normalized_protocol,
        "provider_id": CUSTOM_PROVIDER_IDS[normalized_protocol],
        "base_url": validated_base_url,
        "api_key": normalized_key,
        "model_id": normalized_model,
    }


def parse_claude_model_configuration(raw_configuration: str | dict[str, Any]) -> dict[str, Any]:
    """把 Claude Code 风格 JSON 收敛为 Yuxi 支持的最小 Anthropic 配置。

    未识别字段不会进入进程环境，也不会透传到模型 SDK；调用方可展示 ignored_fields。
    """
    if isinstance(raw_configuration, str):
        if len(raw_configuration.encode("utf-8")) > MAX_CONFIGURATION_JSON_BYTES:
            raise ValueError("JSON 配置不能超过 64 KiB")
        try:
            payload = json.loads(raw_configuration)
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON 格式错误（第 {exc.lineno} 行，第 {exc.colno} 列）") from exc
    elif isinstance(raw_configuration, dict):
        payload = raw_configuration
    else:
        raise ValueError("配置必须是 JSON 对象或 JSON 文本")

    if not isinstance(payload, dict):
        raise ValueError("配置根节点必须是 JSON 对象")
    if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > MAX_CONFIGURATION_JSON_BYTES:
        raise ValueError("JSON 配置不能超过 64 KiB")
    env = payload.get("env")
    if not isinstance(env, dict):
        raise ValueError("JSON 必须包含 env 对象")
    api_key = env.get("ANTHROPIC_API_KEY")
    base_url = env.get("ANTHROPIC_BASE_URL")
    model_id = (
        env.get("ANTHROPIC_MODEL")
        or env.get("ANTHROPIC_DEFAULT_SONNET_MODEL")
        or env.get("ANTHROPIC_DEFAULT_SONNET_MODEL_NAME")
    )
    if not all(isinstance(value, str) and value.strip() for value in (api_key, base_url, model_id)):
        raise ValueError("env 必须包含 ANTHROPIC_API_KEY、ANTHROPIC_BASE_URL 和 ANTHROPIC_MODEL")

    normalized = validate_custom_model_configuration(
        protocol="anthropic",
        base_url=base_url,
        api_key=api_key,
        model_id=model_id,
    )
    supported = {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME",
    }
    normalized["ignored_fields"] = sorted(str(key) for key in env if key not in supported)
    if "includeCoAuthoredBy" in payload:
        normalized["ignored_fields"].append("includeCoAuthoredBy")
    return normalized
