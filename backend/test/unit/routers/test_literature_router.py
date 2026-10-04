"""文献域路由契约测试（P6a）。

钉死：请求体/查询串中的 tenant_id 一律被忽略（AGENTS.md 硬性）；跨租户 404；
长任务 202 语义；kbasset 资产解析的租户/命名空间白名单（越权 403）。
形态对齐 test_system_router.py：FastAPI + TestClient + dependency overrides。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.routers.literature_router as router_module
from server.routers.literature_router import literature
from yuxi.literature import readops

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

PAPER = {
    "rid": "doi-10.1002_adma.202407106",
    "kind": "paper",
    "doi": "10.1002/adma.202407106",
    "title": "Starch synthesis",
    "value_score": 0.0,
    "ai_value_score": None,
    "paper_rank": 0.0,
    "impact_factor": None,
    "quartile": "",
    "source_revision_id": "sir_r1",
    "updated_at": None,
}


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(literature, prefix="/api")

    user = type("U", (), {"uid": "u1"})()
    app.dependency_overrides[router_module.get_required_user] = lambda: user
    # These are response/parameter adapter tests; real source ACLs are exercised separately.
    app.dependency_overrides[router_module.authorize_literature_request] = lambda: None

    async def _asset_access(_object):
        return True

    monkeypatch.setattr(router_module, "authorize_asset", _asset_access)

    # 租户权威：固定为 7（请求参数不应影响）
    async def _tenant(_user):
        return 7

    monkeypatch.setattr(router_module, "_current_tenant", _current_tenant_stub)
    yield TestClient(app, raise_server_exceptions=False)


async def _current_tenant_stub(_user):
    return 7


def test_business_backup_route_principal_and_fail_closed(monkeypatch, client):
    from yuxi.literature import backup
    from yuxi.literature.export_vault import ExportLimitError

    async def export(tenant, uid):
        assert (tenant, uid) == (7, "u1")
        return b"private-zip"

    monkeypatch.setattr(backup, "export_business_backup", export)
    response = client.get("/api/literature/export/business-backup?tenant_id=999")
    assert response.status_code == 200 and response.content == b"private-zip"
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["cache-control"] == "no-store"

    async def limit(*args):
        raise ExportLimitError("单批安全预算")

    monkeypatch.setattr(backup, "export_business_backup", limit)
    assert client.get("/api/literature/export/business-backup").status_code == 413

    async def conflict(*args):
        raise ValueError("业务版本变更")

    monkeypatch.setattr(backup, "export_business_backup", conflict)
    assert client.get("/api/literature/export/business-backup").status_code == 409


def test_reader_link_route_preserves_product_anchor_context(monkeypatch, client):
    from yuxi.literature import management

    async def resolve(tenant, target, *, source_rid):
        assert tenant == 7 and target == "../paper/_wiki.md#摘要" and source_rid == "source"
        return {"rid": "paper", "tab": "noteL2", "variant": None, "anchor": "摘要", "ambiguous": False}

    monkeypatch.setattr(management, "resolve_reader_target", resolve)
    response = client.get(
        "/api/literature/resolve-link", params={"target": "../paper/_wiki.md#摘要", "source_rid": "source"}
    )
    assert response.status_code == 200 and response.json()["anchor"] == "摘要"


def test_graph_filter_query_contract_and_tenant(monkeypatch, client):
    from yuxi.literature import graph_service

    calls = []

    async def network(tenant_id, **kwargs):
        calls.append((tenant_id, kwargs))
        if kwargs.get("year_min") > kwargs.get("year_max"):
            raise ValueError("年份下限不能大于上限")
        return {"nodes": [], "edges": [], "meta": {"actual_nodes": 0}}

    monkeypatch.setattr(graph_service, "graph_network", network)
    response = client.get(
        "/api/literature/graph/network?tenant_id=999&year_min=2020&year_max=2025"
        "&quartiles=Q1,Q2&citation_source=wos&sort_by=year&preview=true&exclude_isolated=false"
    )
    assert response.status_code == 200
    assert calls[0][0] == 7 and calls[0][1]["quartiles"] == ["Q1", "Q2"]
    assert calls[0][1]["preview"] and not calls[0][1]["exclude_isolated"]
    assert client.get("/api/literature/graph/network?year_min=2025&year_max=2020").status_code == 422
    for params in ("min_impact_factor=nan", "min_times_cited=-1", "sort_by=secret", "year_min=999"):
        assert client.get("/api/literature/graph/network?" + params).status_code == 422


def test_history_archive_routes_use_principal_and_deliver_private_zip(monkeypatch, client):
    from yuxi.literature import history_archive

    async def listing(tenant, uid):
        assert (tenant, uid) == (7, "u1")
        return [{"archive_sha256": "a" * 64}]

    async def exporting(tenant, uid, digest):
        assert (tenant, uid) == (7, "u1")
        if digest != "a" * 64:
            raise ValueError("没有唯一已封存归档")
        return b"verified-zip"

    monkeypatch.setattr(history_archive, "list_archives", listing)
    monkeypatch.setattr(history_archive, "export_archive", exporting)
    assert client.get("/api/literature/history/archives?tenant_id=999&uid=other").status_code == 200
    response = client.get(f"/api/literature/history/archives/{'a' * 64}/download?tenant_id=999")
    assert response.content == b"verified-zip" and response.headers["content-type"] == "application/zip"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert client.get(f"/api/literature/history/archives/{'b' * 64}/download").status_code == 409


def test_list_papers_returns_tenant_scoped(monkeypatch, client):
    seen = {}

    async def fake_list(tenant_id, **kwargs):
        seen["tenant"] = tenant_id
        return {"total": 1, "page": 1, "page_size": 20, "items": [PAPER]}

    monkeypatch.setattr(readops, "list_papers", fake_list)
    resp = client.get("/api/literature/papers?tenant_id=999&kind=paper")
    assert resp.status_code == 200
    assert seen["tenant"] == 7  # 请求串 tenant_id 被忽略
    assert resp.json()["items"][0]["rid"] == PAPER["rid"]


def test_history_retry_uses_principal_and_validates_idempotency_contract(monkeypatch, client):
    from yuxi.literature import history_import

    calls = []

    async def retry(tenant_id, uid, item_id, **kwargs):
        calls.append((tenant_id, uid, item_id, kwargs))
        return {"attempt_id": 3, "status": "APPLIED", "target_ref": "card:5", "detail": "Mapped"}

    monkeypatch.setattr(history_import, "retry_history_mapping", retry)
    payload = {
        "expected_digest": "a" * 64,
        "request_id": "eaa7a8ba-2775-4cca-b3f5-2f80dd1aac31",
        "tenant_id": 999,
        "uid": "someone-else",
    }
    response = client.post("/api/literature/history/1/retry-mapping", json=payload)
    assert response.status_code == 200 and calls[0][:3] == (7, "u1", 1)
    assert (
        client.post("/api/literature/history/1/retry-mapping", json={**payload, "request_id": "not-a-uuid"}).status_code
        == 422
    )
    assert (
        client.post("/api/literature/history/1/retry-mapping", json={**payload, "expected_digest": "bad"}).status_code
        == 422
    )
    assert len(calls) == 1


def test_history_retry_hides_ownership_error_and_reports_conflict(monkeypatch, client):
    from yuxi.literature import history_import

    async def fail(*args, **kwargs):
        raise PermissionError("private history")

    monkeypatch.setattr(history_import, "retry_history_mapping", fail)
    payload = {"expected_digest": "a" * 64, "request_id": "eaa7a8ba-2775-4cca-b3f5-2f80dd1aac31"}
    response = client.post("/api/literature/history/1/retry-mapping", json=payload)
    assert response.status_code == 404 and "private history" not in response.text

    async def conflict(*args, **kwargs):
        raise ValueError("历史原件摘要已变更")

    monkeypatch.setattr(history_import, "retry_history_mapping", conflict)
    assert client.post("/api/literature/history/1/retry-mapping", json=payload).status_code == 409


def test_paper_detail_404_across_tenants(monkeypatch, client):
    async def fake_detail(tenant_id, rid):
        assert tenant_id == 7
        return None

    monkeypatch.setattr(readops, "get_paper_detail", fake_detail)
    resp = client.get("/api/literature/papers/doi-x")
    assert resp.status_code == 404


def test_render_returns_202_and_ignores_body_tenant(monkeypatch, client):
    async def fake_detail(tenant_id, rid):
        return {**PAPER, "source_revision_id": "sir_r1"}

    captured = {}

    async def fake_enqueue(*, revision_id, force=False):
        captured["revision"] = revision_id
        return {"status": "PENDING", "reused": False}

    monkeypatch.setattr(readops, "get_paper_detail", fake_detail)
    monkeypatch.setattr(router_module, "enqueue_reader_render", fake_enqueue)
    resp = client.post("/api/literature/papers/doi-x/render?tenant_id=999", json={"tenant_id": 12345})
    assert resp.status_code == 202
    assert captured["revision"] == "sir_r1"


def test_render_409_without_revision(monkeypatch, client):
    async def fake_detail(tenant_id, rid):
        return {**PAPER, "source_revision_id": None}

    monkeypatch.setattr(readops, "get_paper_detail", fake_detail)
    resp = client.post("/api/literature/papers/doi-x/render")
    assert resp.status_code == 409


def test_note_endpoint_rejects_bad_level(monkeypatch, client):
    async def fake_uri(tenant_id, rid, level):
        raise ValueError("非法笔记级别: X9")

    monkeypatch.setattr(readops, "get_note_uri", fake_uri)
    resp = client.get("/api/literature/papers/doi-x/notes/X9")
    assert resp.status_code == 422


# --- kbasset 资产解析白名单 -------------------------------------------------------


def test_asset_uri_validation_allows_own_tenant_namespaces():
    uri = "kbasset://tenants/7/documents/abc/reader_render/rev1/render.md"
    assert readops.validate_literature_asset_uri(uri, 7).startswith("tenants/7/")


def test_asset_uri_validation_rejects_cross_tenant_and_namespaces():
    cases = [
        "kbasset://tenants/9/documents/abc/reader_render/r/render.md",  # 跨租户
        "kbasset://tenants/7/documents/abc/mineru/rev/full.md",  # 命名空间不在白名单
        "https://evil/x.md",  # 非 kbasset
        "kbasset://tenants/7/../secret/x",  # 穿越
        "kbasset://tenants/abc/x/y/z",  # 租户段非数字
    ]
    for uri in cases:
        with pytest.raises(ValueError):
            readops.validate_literature_asset_uri(uri, 7)


def test_asset_endpoint_proxies_with_auth(monkeypatch, client):
    class _Minio:
        async def adownload_file(self, bucket, object_name):
            assert bucket == "knowledgebases"
            assert object_name.startswith("tenants/7/")
            return b"# rendered"

    monkeypatch.setattr(router_module, "get_minio_client", lambda: _Minio())
    resp = client.get(
        "/api/literature/assets", params={"uri": "kbasset://tenants/7/documents/a/reader_render/r/render.md"}
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert resp.content == b"# rendered"


def test_import_rejects_oversized_or_non_text_content_before_storage(monkeypatch, client):
    async def unexpected_write(*_args, **_kwargs):
        raise AssertionError("invalid input must not reach import storage")

    monkeypatch.setattr(router_module.bib_import, "import_bib", unexpected_write)
    monkeypatch.setattr(router_module.bib_import, "import_markdown_document", unexpected_write)
    response = client.post(
        "/api/literature/papers/import/bib", files={"file": ("large.bib", b"x" * (10 * 1024 * 1024 + 1))}
    )
    assert response.status_code == 413
    assert (
        client.post("/api/literature/papers/import/markdown", json={"markdown": {"unexpected": "object"}}).status_code
        == 422
    )
    response = client.post("/api/literature/papers/import/markdown", json={"markdown": "x" * (5 * 1024 * 1024 + 1)})
    assert response.status_code == 413


def test_metadata_rejects_unbounded_author_before_service(monkeypatch, client):
    async def unexpected_write(*_args, **_kwargs):
        raise AssertionError("invalid DTO must not reach metadata storage")

    monkeypatch.setattr(router_module.management, "update_metadata", unexpected_write)
    response = client.put(
        "/api/literature/papers/doi-x/metadata",
        json={
            "expected_token": "0" * 64,
            "changes": {"authors_json": ["x" * 513]},
        },
    )
    assert response.status_code == 422
