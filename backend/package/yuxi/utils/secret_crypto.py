"""敏感凭据信封加密服务。

模型 API Key、OCR Token 等静态敏感字段统一使用 AES-256-GCM 加密存储：
数据库列与 Redis 缓存中只出现密文，消费端在读取边界解密。

密文格式: ``enc.v1:<base64(nonce | ciphertext | tag)>``

- 主密钥来自环境变量 ``YUXI_SECRET_MASTER_KEY``（至少 32 字符）；生产环境必须显式
  配置，开发环境首次使用时自动生成并写入本地 gitignore 文件以保证跨重启稳定。
- AAD 绑定资源上下文（如 ``model-provider:{provider_id}``），防止密文被挪用到其他
  资源名下；解密时上下文不匹配会直接失败。
"""

from __future__ import annotations

import base64
import os
import secrets
from functools import lru_cache
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from yuxi.utils.logging_config import logger

CIPHER_PREFIX = "enc.v1:"
_NONCE_BYTES = 12
_MASTER_KEY_ENV = "YUXI_SECRET_MASTER_KEY"
_DEV_KEY_FILE = Path(".yuxi-dev-secret-master-key")


class SecretCryptoError(RuntimeError):
    """凭据加解密失败。"""


def is_encrypted(value: str | None) -> bool:
    """判断字符串是否为本服务产生的密文。"""
    return bool(value) and value.startswith(CIPHER_PREFIX)


@lru_cache(maxsize=1)
def _master_key() -> bytes:
    """主密钥统一经 SHA-256 派生为 32 字节，兼容任意长度的口令型环境变量。"""
    import hashlib

    raw = os.getenv(_MASTER_KEY_ENV, "").strip()
    if raw:
        if len(raw) < 32:
            raise SecretCryptoError(f"{_MASTER_KEY_ENV} 至少需要 32 个字符")
        return hashlib.sha256(raw.encode()).digest()

    # 与 auth_utils 的生产判定保持同一环境变量口径
    is_production = os.environ.get("YUXI_ENV", "development").strip().lower() in {"prod", "production"}
    if is_production:
        raise SecretCryptoError(
            f"生产环境必须显式配置 {_MASTER_KEY_ENV}（openssl rand -hex 32）以启用凭据静态加密"
        )

    # 开发环境：生成一次并落到本地文件，保证重启后历史密文仍可解。
    if _DEV_KEY_FILE.exists():
        key = _DEV_KEY_FILE.read_text(encoding="utf-8").strip()
        if len(key) >= 32:
            return hashlib.sha256(key.encode()).digest()
    key = secrets.token_hex(32)
    _DEV_KEY_FILE.write_text(key, encoding="utf-8")
    logger.warning(
        f"未配置 {_MASTER_KEY_ENV}，已生成本地开发主密钥并写入 {_DEV_KEY_FILE}"
        "（该文件已加入 gitignore，生产环境请改用环境变量）"
    )
    return hashlib.sha256(key.encode()).digest()


def encrypt_secret(plaintext: str | None, aad: str) -> str | None:
    """加密明文凭据；空值原样返回，已加密的值幂等透传。"""
    if not plaintext or is_encrypted(plaintext):
        return plaintext
    nonce = secrets.token_bytes(_NONCE_BYTES)
    sealed = AESGCM(_master_key()).encrypt(nonce, plaintext.encode(), aad.encode())
    return CIPHER_PREFIX + base64.urlsafe_b64encode(nonce + sealed).decode()


def decrypt_secret(value: str | None, aad: str) -> str | None:
    """解密密文；非密文格式的存量明文原样返回（由调用方负责惰性升级）。"""
    if not value or not is_encrypted(value):
        return value
    try:
        blob = base64.urlsafe_b64decode(value[len(CIPHER_PREFIX) :].encode())
        plaintext = AESGCM(_master_key()).decrypt(blob[:_NONCE_BYTES], blob[_NONCE_BYTES:], aad.encode())
        return plaintext.decode()
    except Exception as exc:  # 密钥轮换/密文损坏/AAD 不匹配都应尽快暴露
        raise SecretCryptoError(f"凭据解密失败（AAD={aad}）：请检查 YUXI_SECRET_MASTER_KEY 是否变更") from exc


def seal_for_cache(plaintext: str | None, aad: str) -> str:
    """缓存载荷专用：空值归一为空串，保证 JSON 结构稳定。"""
    return encrypt_secret(plaintext, aad) or ""


def unseal_from_cache(value: str | None, aad: str) -> str:
    """缓存读取专用：空串归一为 None 语义，旧格式明文透传由调用方重建兜底。"""
    if not value:
        return ""
    return decrypt_secret(value, aad) or ""
