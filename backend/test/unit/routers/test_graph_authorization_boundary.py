"""真实 HTTP 路由依赖不能授权 query 中的库、却写入 body 中的另一库。"""

from types import SimpleNamespace
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from server.routers import graph_router
from server.utils.auth_middleware import get_admin_user, get_authenticated_user
from server.utils.knowledge_access import knowledge_base


@pytest.mark.asyncio
async def test_conflicting_query_and_body_scope_is_rejected_before_any_write(monkeypatch):
    user = SimpleNamespace(uid="tenant-b-admin", role="admin", department_id=2)
    app = FastAPI()
    app.include_router(graph_router.graph, prefix="/api")
    app.dependency_overrides[get_authenticated_user] = lambda: user
    app.dependency_overrides[get_admin_user] = lambda: user
    manageable = AsyncMock(return_value=True)
    write = AsyncMock(return_value={})
    monkeypatch.setattr(knowledge_base, "check_manageable", manageable)
    monkeypatch.setattr(graph_router, "_get_graph_kb_record", AsyncMock(return_value=SimpleNamespace(tenant_id=10)))
    monkeypatch.setattr(graph_router.KnowledgeGraphReviewRepository, "upsert_member", write)
    # 旧实现进入 handler 前就应被拒绝；防止测试误连生产 DB。
    from yuxi.services import principal
    from yuxi.storage.postgres.manager import pg_manager

    @asynccontextmanager
    async def session_context():
        yield SimpleNamespace()

    monkeypatch.setattr(pg_manager, "get_async_session_context", session_context)
    monkeypatch.setattr(principal, "resolve_tenant_id", AsyncMock(return_value=10))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.put(
            "/api/graph/governance/members?kb_id=owned-b",
            json={"kb_id": "foreign-a", "uid": user.uid, "capability": "publisher"},
        )
    assert response.status_code == 400
    write.assert_not_awaited()
    manageable.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("target_tenant,expected_status", [(20, 400), (10, 200)])
async def test_member_grant_checks_target_tenant_and_uses_kb_ownership(monkeypatch, target_tenant, expected_status):
    from yuxi.services import principal
    from yuxi.storage.postgres.manager import pg_manager

    @asynccontextmanager
    async def session_context():
        yield SimpleNamespace()

    write = AsyncMock(return_value={})
    monkeypatch.setattr(pg_manager, "get_async_session_context", session_context)
    monkeypatch.setattr(principal, "resolve_tenant_id", AsyncMock(return_value=target_tenant))
    monkeypatch.setattr(graph_router, "_get_graph_kb_record", AsyncMock(return_value=SimpleNamespace(tenant_id=10)))
    monkeypatch.setattr(graph_router.KnowledgeGraphReviewRepository, "upsert_member", write)
    try:
        await graph_router.governance_upsert_member(
            graph_router.MemberUpsertBody(kb_id="owned", uid="target", capability="viewer"),
            current_user=SimpleNamespace(uid="platform-admin", role="superadmin"),
        )
        status = 200
    except graph_router.HTTPException as error:
        status = error.status_code
    assert status == expected_status
    if status == 200:
        assert write.await_args.kwargs["tenant_id"] == 10
    else:
        write.assert_not_awaited()


@pytest.mark.asyncio
async def test_graph_response_forbids_shared_or_browser_storage(monkeypatch):
    app = FastAPI()
    app.include_router(graph_router.graph, prefix="/api")
    app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(uid="owner", role="admin")
    monkeypatch.setattr(knowledge_base, "check_accessible", AsyncMock(return_value=True))
    service = SimpleNamespace(query_full_graph=AsyncMock(return_value={"nodes": [], "edges": []}))
    monkeypatch.setattr(graph_router, "_get_graph_service", AsyncMock(return_value=service))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/graph/subgraph", params={"kb_id": "owned", "full_graph": True})
    assert response.status_code == 200
    assert "no-store" in response.headers.get("cache-control", "")
