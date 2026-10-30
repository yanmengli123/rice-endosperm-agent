"""Personal OAuth authorization for administrator-configured remote MCP servers.

Tokens are resolved at each invocation. Config revision, resource URL, browser,
user, tenant and authorization epoch are independently checked at the boundary.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from datetime import UTC, timedelta
from urllib.parse import quote_plus, urlencode, urlsplit
from uuid import uuid4

import httpx
from sqlalchemy import delete, select, update

from yuxi.agents.mcp.policy import PolicyError
from yuxi.agents.mcp.security import validate_remote_url_dns, validate_remote_url_static
from yuxi.storage.postgres.models_business import MCPOAuthAttempt, MCPOAuthConfig, MCPOAuthConnection, User
from yuxi.utils.datetime_utils import utc_now
from yuxi.utils.secret_crypto import decrypt_secret, encrypt_secret


class OAuthError(PolicyError):
    pass


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def cookie_name(state: str) -> str:
    return f"yuxi_mcp_oauth_{digest(state)[:16]}"


def _aware(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _aad(kind, tenant_id, slug, uid="") -> str:
    return f"mcp-oauth:{kind}:{tenant_id}:{slug}:{uid}"


def callback_uri(slug: str) -> str:
    origin = os.getenv("YUXI_PUBLIC_URL", "").strip().rstrip("/")
    parsed = urlsplit(origin)
    local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    development = os.getenv("YUXI_ENV", "development") not in {"prod", "production"}
    if (parsed.scheme != "https" and not (development and local and parsed.scheme == "http")) or not parsed.hostname:
        raise OAuthError("请先配置 YUXI_PUBLIC_URL 为服务的 HTTPS 外部访问地址")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path:
        raise OAuthError("YUXI_PUBLIC_URL 必须只包含协议、域名和端口")
    if not slug or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in slug):
        raise OAuthError("MCP 标识不能用于 OAuth 回调")
    return f"{origin}/api/system/mcp-oauth/callback/{slug}"


async def get_config(db, tenant_id, slug, *, lock=False):
    statement = select(MCPOAuthConfig).where(MCPOAuthConfig.tenant_id == tenant_id, MCPOAuthConfig.server_slug == slug)
    if lock:
        statement = statement.with_for_update()
    return await db.scalar(statement.execution_options(populate_existing=True))


async def configure(db, *, tenant_id, server, config: dict, client_secret: str | None, enabled: bool):
    if server.transport not in {"sse", "streamable_http"}:
        raise OAuthError("个人 OAuth 仅支持远程 HTTP MCP")
    data = dict(config)
    for name in ("authorization_endpoint", "token_endpoint", "resource"):
        data[name] = validate_remote_url_static(str(data.get(name) or ""), allow_insecure_http=False)
        if urlsplit(data[name]).scheme != "https":
            raise OAuthError("OAuth 端点必须使用 HTTPS")
        if urlsplit(data[name]).query:
            raise OAuthError("OAuth 端点不能预置查询参数")
    if data["resource"] != server.url:
        raise OAuthError("OAuth resource 必须与当前 MCP URL 完全一致")
    if data.get("token_endpoint_auth_method") not in {"none", "client_secret_post", "client_secret_basic"}:
        raise OAuthError("不支持的 token_endpoint_auth_method")
    callback_uri(server.slug)
    item = await get_config(db, tenant_id, server.slug, lock=True)
    if item is None:
        item = MCPOAuthConfig(tenant_id=tenant_id, server_slug=server.slug)
        db.add(item)
    item.revision, item.config_json, item.enabled = str(uuid4()), data, enabled
    if client_secret is not None:
        item.client_secret_ciphertext = encrypt_secret(client_secret, _aad("client", tenant_id, server.slug))
    if data["token_endpoint_auth_method"] != "none" and not item.client_secret_ciphertext:
        raise OAuthError("所选客户端认证方式需要 client_secret")
    await db.flush()
    return item


async def _connection(db, tenant_id, uid, slug, *, lock=False):
    statement = select(MCPOAuthConnection).where(
        MCPOAuthConnection.tenant_id == tenant_id, MCPOAuthConnection.uid == uid, MCPOAuthConnection.server_slug == slug
    )
    if lock:
        statement = statement.with_for_update()
    return await db.scalar(statement.execution_options(populate_existing=True))


async def begin_authorization(db, *, tenant_id, uid, server):
    config = await get_config(db, tenant_id, server.slug, lock=True)
    if config is None or not config.enabled or config.config_json["resource"] != server.url:
        raise OAuthError("此 MCP 尚未配置可用的个人 OAuth")
    connection = await _connection(db, tenant_id, uid, server.slug, lock=True)
    if connection is None:
        connection = MCPOAuthConnection(tenant_id=tenant_id, uid=uid, server_slug=server.slug)
        db.add(connection)
    connection.revision, connection.auth_epoch = config.revision, str(uuid4())
    connection.status, connection.tokens_ciphertext, connection.expires_at = "pending", None, None
    state, verifier, browser = secrets.token_urlsafe(32), secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    redirect = callback_uri(server.slug)
    now = utc_now()
    # Bound retention without keeping stale authorization codes/verifiers forever.
    await db.execute(delete(MCPOAuthAttempt).where(MCPOAuthAttempt.expires_at < now))
    db.add(
        MCPOAuthAttempt(
            state_hash=digest(state),
            tenant_id=tenant_id,
            uid=uid,
            server_slug=server.slug,
            revision=config.revision,
            auth_epoch=connection.auth_epoch,
            browser_hash=digest(browser),
            verifier_ciphertext=encrypt_secret(verifier, _aad("pkce", tenant_id, server.slug, digest(state))),
            redirect_uri=redirect,
            expires_at=now + timedelta(minutes=10),
        )
    )
    fields = {
        "response_type": "code",
        "client_id": config.config_json["client_id"],
        "redirect_uri": redirect,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "resource": config.config_json["resource"],
    }
    if config.config_json.get("scope"):
        fields["scope"] = config.config_json["scope"]
    await db.flush()
    return {
        "authorization_url": config.config_json["authorization_endpoint"] + "?" + urlencode(fields),
        "state": state,
        "browser": browser,
        "redirect_uri": redirect,
    }


async def _exchange(config, tenant_id, slug, fields):
    data = dict(fields, client_id=config.config_json["client_id"], resource=config.config_json["resource"])
    auth = None
    method = config.config_json["token_endpoint_auth_method"]
    if method != "none":
        secret = decrypt_secret(config.client_secret_ciphertext, _aad("client", tenant_id, slug))
        if method == "client_secret_post":
            data["client_secret"] = secret
        else:
            auth = httpx.BasicAuth(quote_plus(data.pop("client_id")), quote_plus(secret))
    endpoint = await validate_remote_url_dns(config.config_json["token_endpoint"], allow_insecure_http=False)
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as client:
            async with client.stream(
                "POST", endpoint, data=data, auth=auth, headers={"Accept": "application/json"}
            ) as response:
                if response.status_code != 200:
                    raise OAuthError("OAuth 令牌交换失败，请重新授权")
                payload = bytearray()
                async for chunk in response.aiter_bytes():
                    payload.extend(chunk)
                    if len(payload) > 65536:
                        raise OAuthError("OAuth 令牌响应过大")
        result = json.loads(payload)
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (httpx.HTTPError, ValueError):
        raise OAuthError("OAuth 令牌服务不可用或响应无效") from None


def _store_tokens(connection, payload, *, previous=None):
    token = payload.get("access_token")
    if not isinstance(token, str) or not token or len(token) > 16384 or any(c.isspace() for c in token):
        raise OAuthError("OAuth 服务没有返回有效的 access_token")
    if str(payload.get("token_type", "")).lower() != "bearer":
        raise OAuthError("OAuth 服务必须返回 Bearer token_type")
    try:
        lifetime = int(payload["expires_in"])
        if not 0 < lifetime <= 366 * 86400:
            raise ValueError
    except (KeyError, ValueError, TypeError, OverflowError):
        raise OAuthError("OAuth 服务必须返回有效的 expires_in") from None
    refresh = payload.get("refresh_token", (previous or {}).get("refresh_token"))
    if refresh is not None and (not isinstance(refresh, str) or len(refresh) > 16384 or not refresh):
        raise OAuthError("OAuth refresh_token 无效")
    connection.tokens_ciphertext = encrypt_secret(
        json.dumps({"access_token": token, "refresh_token": refresh}),
        _aad("tokens", connection.tenant_id, connection.server_slug, connection.uid),
    )
    connection.expires_at, connection.status = utc_now() + timedelta(seconds=lifetime), "connected"
    connection.updated_at = utc_now()


async def _active_identity(db, tenant_id, uid):
    # Re-check identity at the unauthenticated callback as well as at invocation.
    from yuxi.services.principal import resolve_principal

    user = await db.scalar(select(User).where(User.uid == uid, User.is_deleted == 0, User.is_disabled.is_(False)))
    if user is None or (await resolve_principal(db, user)).tenant_id != tenant_id:
        raise OAuthError("授权所属账户已停用或已更换租户")


async def finish_authorization(db, *, slug, state, browser, code):
    if not state or len(state) > 128 or not browser or not code or len(code) > 8192:
        raise OAuthError("OAuth 回调参数无效")
    now = utc_now()
    attempt = await db.scalar(
        update(MCPOAuthAttempt)
        .where(
            MCPOAuthAttempt.state_hash == digest(state),
            MCPOAuthAttempt.server_slug == slug,
            MCPOAuthAttempt.browser_hash == digest(browser),
            MCPOAuthAttempt.used_at.is_(None),
            MCPOAuthAttempt.expires_at > now,
        )
        .values(used_at=now)
        .returning(MCPOAuthAttempt)
    )
    if attempt is None:
        raise OAuthError("OAuth 授权已过期、已使用或不属于当前浏览器")
    # Commit consumption before network I/O, including when a provider exchange fails.
    attempt_data = {
        name: getattr(attempt, name)
        for name in ("tenant_id", "uid", "revision", "auth_epoch", "verifier_ciphertext", "redirect_uri")
    }
    await db.commit()
    config = await get_config(db, attempt_data["tenant_id"], slug)
    if config is None or not config.enabled or config.revision != attempt_data["revision"]:
        raise OAuthError("MCP OAuth 配置已变更，请重新授权")
    verifier = decrypt_secret(
        attempt_data["verifier_ciphertext"], _aad("pkce", attempt_data["tenant_id"], slug, digest(state))
    )
    payload = await _exchange(
        config,
        attempt_data["tenant_id"],
        slug,
        {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": attempt_data["redirect_uri"],
        },
    )
    config = await get_config(db, attempt_data["tenant_id"], slug, lock=True)
    connection = await _connection(db, attempt_data["tenant_id"], attempt_data["uid"], slug, lock=True)
    if (
        config is None
        or not config.enabled
        or config.revision != attempt_data["revision"]
        or connection is None
        or connection.auth_epoch != attempt_data["auth_epoch"]
    ):
        raise OAuthError("授权期间配置或连接已变更，请重新授权")
    await _active_identity(db, connection.tenant_id, connection.uid)
    _store_tokens(connection, payload)
    await db.flush()


async def revoke(db, *, tenant_id, uid, slug):
    # Match begin/finish lock order to avoid deadlocks.
    await get_config(db, tenant_id, slug, lock=True)
    connection = await _connection(db, tenant_id, uid, slug, lock=True)
    if connection:
        connection.auth_epoch = str(uuid4())
        connection.status, connection.tokens_ciphertext, connection.expires_at = "revoked", None, None
        await db.flush()


async def personal_runtime_config(db, *, server, tenant_id, uid, config):
    oauth = await get_config(db, tenant_id, server.slug, lock=True)
    if oauth is None or not oauth.enabled:
        return None
    if server.url != oauth.config_json["resource"]:
        raise OAuthError("MCP 地址已变更，管理员需重新确认 OAuth 配置")
    connection = await _connection(db, tenant_id, uid, server.slug, lock=True)
    if (
        connection is None
        or connection.status != "connected"
        or connection.revision != oauth.revision
        or not connection.tokens_ciphertext
    ):
        raise OAuthError(f"MCP '{server.slug}' 需要在个人设置中授权")
    await _active_identity(db, tenant_id, uid)
    tokens = json.loads(decrypt_secret(connection.tokens_ciphertext, _aad("tokens", tenant_id, server.slug, uid)))
    if not connection.expires_at or _aware(connection.expires_at) <= utc_now() + timedelta(seconds=60):
        if not tokens.get("refresh_token"):
            raise OAuthError("个人 MCP 授权已过期，请重新授权")
        payload = await _exchange(
            oauth, tenant_id, server.slug, {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]}
        )
        _store_tokens(connection, payload, previous=tokens)
        await db.flush()
        tokens = json.loads(decrypt_secret(connection.tokens_ciphertext, _aad("tokens", tenant_id, server.slug, uid)))
    headers = {key: value for key, value in (config.get("headers") or {}).items() if key.lower() != "authorization"}
    headers["Authorization"] = "Bearer " + tokens["access_token"]
    return dict(config, headers=headers, _personal_oauth={"tenant_id": tenant_id, "uid": uid, "slug": server.slug})
