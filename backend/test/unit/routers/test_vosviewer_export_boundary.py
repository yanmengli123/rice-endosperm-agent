"""真实 ASGI 依赖与交付边界；数据库/图数据库 I/O 在此层使用替身。"""

import io
import json
import threading
import zipfile
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from server.routers import graph_router
from server.utils.auth_middleware import get_authenticated_user, get_required_user
from server.utils.knowledge_access import knowledge_base
from yuxi.knowledge.graphs.vosviewer_export import issue_snapshot


@pytest.fixture
def export_boundary(monkeypatch):
    user = SimpleNamespace(uid="export-admin", role="admin", department_id=1)
    data = {
        "nodes": [
            {"id": "n1", "entity_id": "e1", "name": "基因 A", "type": "Gene", "node_kind": "entity"},
            {"id": "n2", "entity_id": "e2", "name": "基因 B", "type": "Gene", "node_kind": "entity"},
            {"id": "n3", "entity_id": "e3", "name": "孤立实体", "type": "Gene", "node_kind": "entity"},
        ],
        "edges": [
            {
                "id": "r1",
                "source_id": "n1",
                "target_id": "n2",
                "type": "REGULATES",
                "triple_id": "t1",
                "properties": {"review_status": "APPROVED"},
            }
        ],
        "meta": {"scope": "full_graph", "review_policy": "approved_only", "generated_at": "2026-10-08T00:00:00+00:00"},
        "truncated": False,
        "truncation_reasons": [],
    }
    snapshot = issue_snapshot(
        data,
        kb_id="owned",
        actor_uid=user.uid,
        query={"full_graph": True, "exclude_chunk": True, "review_policy": "approved_only"},
    )
    service = SimpleNamespace(query_full_graph=AsyncMock(return_value=deepcopy(data)))
    monkeypatch.setattr(graph_router, "_get_graph_service", AsyncMock(return_value=service))
    manageable = AsyncMock(return_value=True)
    monkeypatch.setattr(knowledge_base, "check_manageable", manageable)
    monkeypatch.setattr(
        graph_router.KnowledgeGraphReviewRepository, "get_member_capability", AsyncMock(return_value=None)
    )
    audit = AsyncMock()
    monkeypatch.setattr(graph_router.KnowledgeGraphReviewRepository, "append_audit", audit)

    @asynccontextmanager
    async def session():
        yield SimpleNamespace()

    monkeypatch.setattr(graph_router.pg_manager, "get_async_session_context", session)
    monkeypatch.setattr(graph_router, "resolve_tenant_id", AsyncMock(return_value=777))
    app = FastAPI()
    app.include_router(graph_router.graph, prefix="/api")
    app.dependency_overrides[get_authenticated_user] = lambda: user
    app.dependency_overrides[get_required_user] = lambda: user
    return SimpleNamespace(
        app=app, user=user, data=data, snapshot=snapshot, service=service, manageable=manageable, audit=audit
    )


async def request_export(boundary, **overrides):
    body = {"kb_id": "owned", "snapshot_token": boundary.snapshot["token"], **overrides}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=boundary.app), base_url="http://test") as client:
        return await client.post("/api/graph/vosviewer/export", json=body)


@pytest.mark.asyncio
async def test_export_delivers_same_snapshot_off_event_loop_and_audits_authoritative_tenant(
    export_boundary, monkeypatch
):
    build = graph_router.build_vosviewer_package
    main_thread = threading.get_ident()
    threads = []

    def checked_build(**kwargs):
        threads.append(threading.get_ident())
        return build(**kwargs)

    monkeypatch.setattr(graph_router, "build_vosviewer_package", checked_build)
    response = await request_export(export_boundary, tenant_id=999)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert threads and threads[0] != main_thread
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        facts = [json.loads(line) for line in archive.read("facts.jsonl").splitlines()]
    assert manifest["snapshot_id"] == export_boundary.snapshot["id"]
    assert facts[0]["predicate"] == "REGULATES"
    assert manifest["counts"]["entities"] == 3
    assert manifest["counts"]["distinct_facts"] == 1
    assert export_boundary.audit.await_args.kwargs["tenant_id"] == 777
    assert export_boundary.manageable.await_count == 2


@pytest.mark.asyncio
async def test_standard_user_cannot_export_even_if_graph_is_accessible(export_boundary):
    export_boundary.user.role = "user"
    response = await request_export(export_boundary)
    assert response.status_code == 403
    export_boundary.service.query_full_graph.assert_not_awaited()
    export_boundary.audit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["actor", "token", "kb"])
async def test_snapshot_identity_or_signature_mismatch_is_rejected_before_graph_query(export_boundary, change):
    overrides = {}
    if change == "actor":
        export_boundary.user.uid = "another-admin"
    elif change == "token":
        overrides["snapshot_token"] = export_boundary.snapshot["token"] + "x"
    else:
        overrides["kb_id"] = "foreign"
    response = await request_export(export_boundary, **overrides)
    assert response.status_code == 400
    export_boundary.service.query_full_graph.assert_not_awaited()
    export_boundary.audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_changed_graph_cannot_be_delivered_under_loaded_snapshot(export_boundary):
    export_boundary.service.query_full_graph.return_value["edges"][0]["type"] = "INHIBITS"
    response = await request_export(export_boundary)
    assert response.status_code == 409
    export_boundary.audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_revocation_during_build_prevents_delivery_and_success_audit(export_boundary):
    export_boundary.manageable.side_effect = [True, False]
    response = await request_export(export_boundary)
    assert response.status_code == 404
    export_boundary.audit.assert_not_awaited()
