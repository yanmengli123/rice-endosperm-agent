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
