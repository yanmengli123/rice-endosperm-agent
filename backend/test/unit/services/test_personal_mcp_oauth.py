from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.agents.mcp import oauth
from yuxi.storage.postgres.models_business import Base, MCPOAuthAttempt, MCPOAuthConfig, MCPOAuthConnection


@pytest.fixture
async def setup_oauth(monkeypatch):
    monkeypatch.setenv("YUXI_PUBLIC_URL", "https://yuxi.example")
    monkeypatch.setenv("YUXI_SECRET_MASTER_KEY", "oauth-unit-test-master-key-with-more-than-32-characters")
    from yuxi.utils.secret_crypto import _master_key

    _master_key.cache_clear()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda conn: Base.metadata.create_all(
                conn, tables=[MCPOAuthConfig.__table__, MCPOAuthConnection.__table__, MCPOAuthAttempt.__table__]
            )
        )

    async def identity(*_args):
        pass

    monkeypatch.setattr(oauth, "_active_identity", identity)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        server = SimpleNamespace(slug="papers", url="https://mcp.example/mcp", transport="streamable_http")
        config = dict(
            authorization_endpoint="https://id.example/authorize",
            token_endpoint="https://id.example/token",
            resource=server.url,
            client_id="client",
            scope="read",
            token_endpoint_auth_method="client_secret_post",
        )
        await oauth.configure(
            db, tenant_id=1, server=server, config=config, client_secret="client-secret", enabled=True
        )
        await db.commit()
        yield db, server, config
    await engine.dispose()
    _master_key.cache_clear()


async def _begin(db, server, uid="alice"):
    result = await oauth.begin_authorization(db, tenant_id=1, uid=uid, server=server)
    await db.commit()
    return result


async def _finish(db, server, transaction):
    await oauth.finish_authorization(
        db, slug=server.slug, state=transaction["state"], browser=transaction["browser"], code="provider-code"
    )
    await db.commit()


async def test_pkce_encryption_browser_binding_and_single_use(setup_oauth, monkeypatch):
    db, server, _ = setup_oauth
    transaction = await _begin(db, server)
    params = parse_qs(urlsplit(transaction["authorization_url"]).query)
    assert params["code_challenge_method"] == ["S256"]
    assert params["resource"] == [server.url]
    assert params["redirect_uri"] == ["https://yuxi.example/api/system/mcp-oauth/callback/papers"]
    attempt = await db.scalar(select(MCPOAuthAttempt))
    assert attempt.state_hash != transaction["state"]
    assert attempt.verifier_ciphertext.startswith("enc.v1:")
    sealed = attempt.verifier_ciphertext
    with pytest.raises(oauth.OAuthError, match="浏览器"):
        await oauth.finish_authorization(
            db, slug=server.slug, state=transaction["state"], browser="other-browser", code="code"
        )
    await db.rollback()

    async def exchange_checked(config, tenant, slug, fields):
        import base64
        import hashlib

        assert fields["code_verifier"] not in sealed
        assert (
            base64.urlsafe_b64encode(hashlib.sha256(fields["code_verifier"].encode()).digest()).rstrip(b"=").decode()
            == params["code_challenge"][0]
        )
        return {
            "access_token": "access-secret",
            "refresh_token": "refresh-secret",
            "token_type": "Bearer",
            "expires_in": 3600,
        }

    monkeypatch.setattr(oauth, "_exchange", exchange_checked)
    await _finish(db, server, transaction)
    connection = await oauth._connection(db, 1, "alice", "papers")
    assert connection.status == "connected"
    assert "access-secret" not in connection.tokens_ciphertext
    with pytest.raises(oauth.OAuthError, match="已使用"):
        await _finish(db, server, transaction)


async def test_failed_exchange_still_consumes_state(setup_oauth, monkeypatch):
    db, server, _ = setup_oauth
    transaction = await _begin(db, server)

    async def exchange(*_args):
        raise oauth.OAuthError("provider unavailable")

    monkeypatch.setattr(oauth, "_exchange", exchange)
    with pytest.raises(oauth.OAuthError, match="provider unavailable"):
        await _finish(db, server, transaction)
    await db.rollback()
    with pytest.raises(oauth.OAuthError, match="已使用"):
        await _finish(db, server, transaction)


@pytest.mark.parametrize("change", ["revoke", "config"])
async def test_inflight_callback_cannot_undo_revocation_or_config_change(setup_oauth, monkeypatch, change):
    db, server, config = setup_oauth
    transaction = await _begin(db, server)

    async def exchange(*_args):
        if change == "revoke":
            await oauth.revoke(db, tenant_id=1, uid="alice", slug="papers")
        else:
            await oauth.configure(db, tenant_id=1, server=server, config=config, client_secret=None, enabled=True)
        await db.commit()
        return {"access_token": "access", "token_type": "Bearer", "expires_in": 3600}

    monkeypatch.setattr(oauth, "_exchange", exchange)
    with pytest.raises(oauth.OAuthError, match="变更"):
        await _finish(db, server, transaction)
    connection = await oauth._connection(db, 1, "alice", "papers")
    assert connection.tokens_ciphertext is None


async def test_refresh_rotation_and_identity_isolation(setup_oauth, monkeypatch):
    db, server, _ = setup_oauth
    transaction = await _begin(db, server)

    async def exchange(*_args):
        return {"access_token": "old-access", "refresh_token": "old-refresh", "token_type": "Bearer", "expires_in": 10}

    monkeypatch.setattr(oauth, "_exchange", exchange)
    await _finish(db, server, transaction)
    calls = []

    async def refresh(_config, _tenant, _slug, fields):
        calls.append(fields)
        assert fields["refresh_token"] == "old-refresh"
        return {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "token_type": "Bearer",
            "expires_in": 3600,
        }

    monkeypatch.setattr(oauth, "_exchange", refresh)
    config = await oauth.personal_runtime_config(db, server=server, tenant_id=1, uid="alice", config={})
    assert config["headers"]["Authorization"] == "Bearer new-access"
    await oauth.personal_runtime_config(db, server=server, tenant_id=1, uid="alice", config={})
    assert len(calls) == 1
    with pytest.raises(oauth.OAuthError, match="授权"):
        await oauth.personal_runtime_config(db, server=server, tenant_id=1, uid="bob", config={})
    assert await oauth.personal_runtime_config(db, server=server, tenant_id=2, uid="alice", config={}) is None
    await oauth.revoke(db, tenant_id=1, uid="alice", slug="papers")
    with pytest.raises(oauth.OAuthError, match="授权"):
        await oauth.personal_runtime_config(db, server=server, tenant_id=1, uid="alice", config={})


async def test_resource_change_never_sends_token_to_new_url(setup_oauth):
    db, server, _ = setup_oauth
    server.url = "https://attacker.example/mcp"
    with pytest.raises(oauth.OAuthError, match="地址已变更"):
        await oauth.personal_runtime_config(db, server=server, tenant_id=1, uid="alice", config={})


async def test_token_exchange_uses_form_auth_and_sanitizes_provider_errors(setup_oauth, monkeypatch):
    import httpx

    db, server, _ = setup_oauth
    config = await oauth.get_config(db, 1, server.slug)

    async def validated(url, **_kwargs):
        return url

    monkeypatch.setattr(oauth, "validate_remote_url_dns", validated)

    def handler(request):
        assert request.url == "https://id.example/token"
        assert b"client_secret=client-secret" in request.content
        assert b"resource=https%3A%2F%2Fmcp.example%2Fmcp" in request.content
        return httpx.Response(400, json={"error_description": "client-secret must not leak"})

    original_client = httpx.AsyncClient

    def client(**kwargs):
        assert kwargs["follow_redirects"] is False and kwargs["trust_env"] is False
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(oauth.httpx, "AsyncClient", client)
    with pytest.raises(oauth.OAuthError) as error:
        await oauth._exchange(config, 1, server.slug, {"grant_type": "refresh_token", "refresh_token": "refresh"})
    assert "client-secret" not in str(error.value)


@pytest.mark.parametrize(
    "endpoint", ["http://id.example/token", "https://127.0.0.1/token", "https://id.example/token?client_secret=bad"]
)
async def test_provider_endpoint_policy(setup_oauth, endpoint):
    db, server, config = setup_oauth
    config["token_endpoint"] = endpoint
    with pytest.raises(ValueError):
        await oauth.configure(db, tenant_id=1, server=server, config=config, client_secret=None, enabled=True)


async def test_already_assembled_tool_rechecks_revocation_and_caller(monkeypatch):
    from yuxi.agents.mcp import langchain_adapter as adapter, service
    from yuxi.agents.mcp.execution import McpExecutionContext, reset_mcp_execution_context, set_mcp_execution_context
    from yuxi.agents.mcp.host import LegacyLangChainHost, McpHostError

    calls = []

    async def call_tool(slug, config, name, arguments):
        calls.append(config["headers"]["Authorization"])
        return SimpleNamespace(text="ok", to_dict=lambda: {"text": "ok"})

    monkeypatch.setattr(adapter, "_host_resolver", lambda: SimpleNamespace(call_tool=call_tool))
    descriptor = LegacyLangChainHost()._descriptor_from_raw(
        "papers",
        SimpleNamespace(
            name="lookup", description="lookup", args_schema={"type": "object", "properties": {}}, metadata={}
        ),
    )
    binding = {"tenant_id": 1, "uid": "alice", "slug": "papers"}
    tool = adapter.build_mcp_base_tool(
        descriptor,
        {
            "transport": "streamable_http",
            "url": "https://mcp.example/mcp",
            "headers": {"Authorization": "Bearer stale-token"},
            "_personal_oauth": binding,
        },
    )
    available = True

    async def fresh(_slug):
        return {"headers": {"Authorization": "Bearer fresh-token"}, "_personal_oauth": binding} if available else None

    monkeypatch.setattr(service, "get_enabled_mcp_server_config", fresh)
    token = set_mcp_execution_context(McpExecutionContext(tenant_id=1, uid="alice"))
    try:
        await tool.coroutine()
        available = False
        with pytest.raises(McpHostError, match="不可用"):
            await tool.coroutine()
    finally:
        reset_mcp_execution_context(token)
    token = set_mcp_execution_context(McpExecutionContext(tenant_id=1, uid="bob"))
    try:
        with pytest.raises(McpHostError, match="身份已变更"):
            await tool.coroutine()
    finally:
        reset_mcp_execution_context(token)
    assert calls == ["Bearer fresh-token"]
