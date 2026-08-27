"""Encrypted, tenant-scoped credentials for MCP transports."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.models_business import MCPUserCredential
from yuxi.utils.datetime_utils import utc_now_naive
from yuxi.utils.secret_crypto import decrypt_secret, encrypt_secret

SUPPORTED_AUTH_TYPES = {"bearer", "api_key", "oauth2_client"}


def _aad(tenant_id: int, uid: str, name: str) -> str:
    return f"mcp-credential:{tenant_id}:{uid}:{name}"


def _mask(secret: str) -> str:
    if len(secret) < 10:
        return secret[:2] + "****"
    return f"{secret[:4]}****{secret[-4:]}"


async def create_mcp_credential(
    db: AsyncSession,
    *,
    tenant_id: int,
    uid: str,
    name: str,
    auth_type: str,
    secret: str,
    metadata: dict | None = None,
) -> MCPUserCredential:
    clean_name = name.strip()
    if not clean_name or len(clean_name) > 128:
        raise ValueError("credential name is required and must not exceed 128 characters")
    if auth_type not in SUPPORTED_AUTH_TYPES:
        raise ValueError(f"unsupported MCP credential auth_type: {auth_type}")
    if not secret or len(secret) > 8192:
        raise ValueError("credential secret is required and must not exceed 8192 characters")
    existing = await db.scalar(
        select(MCPUserCredential).where(
            MCPUserCredential.tenant_id == tenant_id,
            MCPUserCredential.uid == uid,
            MCPUserCredential.name == clean_name,
            MCPUserCredential.status == "active",
        )
    )
    if existing is not None:
        raise ValueError("an active MCP credential with this name already exists")
    credential = MCPUserCredential(
        tenant_id=tenant_id,
        uid=uid,
        name=clean_name,
        auth_type=auth_type,
        secret_ciphertext=encrypt_secret(secret, _aad(tenant_id, uid, clean_name)),
        masked_hint=_mask(secret),
        metadata_json=dict(metadata or {}),
        status="active",
    )
    db.add(credential)
    await db.flush()
    return credential


async def list_mcp_credentials(db: AsyncSession, *, tenant_id: int, uid: str) -> list[dict]:
    result = await db.execute(
        select(MCPUserCredential)
        .where(MCPUserCredential.tenant_id == tenant_id, MCPUserCredential.uid == uid)
        .order_by(MCPUserCredential.id)
    )
    return [
        {
            "credential_id": item.id,
            "name": item.name,
            "auth_type": item.auth_type,
            "masked_hint": item.masked_hint,
            "metadata": item.metadata_json or {},
            "status": item.status,
            "created_at": item.created_at.isoformat() if item.created_at else None,
        }
        for item in result.scalars().all()
    ]


async def revoke_mcp_credential(db: AsyncSession, *, tenant_id: int, uid: str, credential_id: int) -> bool:
    credential = await db.scalar(
        select(MCPUserCredential).where(
            MCPUserCredential.id == credential_id,
            MCPUserCredential.tenant_id == tenant_id,
            MCPUserCredential.uid == uid,
        )
    )
    if credential is None:
        return False
    credential.status = "revoked"
    credential.revoked_at = utc_now_naive()
    await db.flush()
    return True


async def open_mcp_credential(
    db: AsyncSession,
    *,
    tenant_id: int,
    uid: str,
    credential_id: int,
) -> tuple[str, str, dict] | None:
    credential = await db.scalar(
        select(MCPUserCredential).where(
            MCPUserCredential.id == credential_id,
            MCPUserCredential.tenant_id == tenant_id,
            MCPUserCredential.uid == uid,
            MCPUserCredential.status == "active",
        )
    )
    if credential is None:
        return None
    secret = decrypt_secret(
        credential.secret_ciphertext,
        _aad(tenant_id, uid, credential.name),
    )
    return credential.auth_type, secret, dict(credential.metadata_json or {})


def inject_credential(config: dict, opened: tuple[str, str, dict]) -> dict:
    """Materialize auth only in memory immediately before opening a session."""
    auth_type, secret, metadata = opened
    runtime = dict(config)
    headers = dict(runtime.get("headers") or {})
    env = dict(runtime.get("env") or {})
    if auth_type == "bearer":
        headers["Authorization"] = f"Bearer {secret}"
    elif auth_type == "api_key":
        target = str(metadata.get("target") or "header")
        name = str(metadata.get("name") or "X-API-Key")
        if target == "env":
            env[name] = secret
        else:
            headers[name] = secret
    elif auth_type == "oauth2_client":
        # OAuth token exchange belongs to the egress gateway. Passing a client
        # secret directly to an arbitrary MCP server is intentionally forbidden.
        raise ValueError("oauth2_client credentials require the configured MCP egress gateway")
    runtime["headers"] = headers or None
    runtime["env"] = env or None
    return runtime


__all__ = [
    "SUPPORTED_AUTH_TYPES",
    "create_mcp_credential",
    "list_mcp_credentials",
    "revoke_mcp_credential",
    "open_mcp_credential",
    "inject_credential",
]
