from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from server.routers import knowledge_mcp_router as transport
from yuxi.services import principal


@pytest.fixture
def mcp_auth(monkeypatch):
    user = SimpleNamespace(uid="reader")
    key = SimpleNamespace(purpose="external_agent", scopes=["knowledge:read"], tenant_id=7)
    verify = AsyncMock(return_value=(user, key))
    monkeypatch.setattr(transport, "_verify_api_key", verify)
    monkeypatch.setattr(principal, "resolve_principal", AsyncMock(return_value=SimpleNamespace(tenant_id=7)))
    return user, key, verify


def request(headers):
    return Request({"type": "http", "headers": [(key.encode(), value.encode()) for key, value in headers.items()]})


async def test_mcp_auth_requires_a_live_scoped_key_and_matching_tenant(mcp_auth):
    user, key, verify = mcp_auth
    req = request({"authorization": "Bearer yxkey_test"})
    assert await transport.authenticate_knowledge_request(req, None) is user
    key.scopes = []
    with pytest.raises(HTTPException) as denied:
        await transport.authenticate_knowledge_request(req, None)
    assert denied.value.status_code == 403
    key.scopes = ["knowledge:read"]
    key.tenant_id = 8
    with pytest.raises(HTTPException):
        await transport.authenticate_knowledge_request(req, None)
    verify.return_value = (None, None)
    with pytest.raises(HTTPException) as revoked:
        await transport.authenticate_knowledge_request(req, None)
    assert revoked.value.status_code == 401
    with pytest.raises(HTTPException):
        await transport.authenticate_knowledge_request(request({"authorization": "Bearer jwt"}), None)
    with pytest.raises(HTTPException):
        await transport.authenticate_knowledge_request(request({"authorization": "Bearer yxkey_test", "origin": "https://example.com"}), None)


async def test_real_streamable_http_handshake_tools_and_revocation(monkeypatch, mcp_auth):
    _, _, verify = mcp_auth

    @asynccontextmanager
    async def session():
        yield None

    monkeypatch.setattr(transport.pg_manager, "get_async_session_context", session)
    call = AsyncMock(return_value={"results": [{"file_id": "f1", "content": "source"}]})
    monkeypatch.setattr(transport, "execute_knowledge_tool", call)
    mcp = transport.KnowledgeMCP()
    headers = {"Authorization": "Bearer yxkey_test", "Accept": "application/json, text/event-stream"}
    async with mcp.manager.run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mcp), base_url="http://localhost", headers=headers) as client:
            initialized = await client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "compat-test", "version": "1"}}})
            assert initialized.status_code == 200
            assert "serverInfo" in initialized.json()["result"]
            tools = await client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            assert {item["name"] for item in tools.json()["result"]["tools"]} == set(transport.TOOLS)
            result = await client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "query_kb", "arguments": {"kb_id": "kb1", "query_text": "Wx"}}})
            assert result.json()["result"]["structuredContent"]["results"][0]["file_id"] == "f1"
            call.assert_awaited_once()
            verify.return_value = (None, None)
            revoked = await client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}})
            assert revoked.status_code == 401
