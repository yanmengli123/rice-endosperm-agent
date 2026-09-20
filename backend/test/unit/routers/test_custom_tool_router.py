"""自定义工具路由单测：鉴权透传、DTO 严格性、异常映射（400/404/409/422/500）。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.routers.tool_router import tools
from server.utils.auth_middleware import get_admin_user, get_db
from yuxi.agents.toolkits.custom.domain import (
    CustomToolError,
    CustomToolReferenceError,
)
from yuxi.storage.postgres.models_business import User


def _build_app(*, allow_admin: bool = True) -> FastAPI:
    app = FastAPI()
    app.include_router(tools, prefix="/api")

    async def fake_db():
        return None

    async def fake_admin_user():
        if not allow_admin:
            from fastapi import HTTPException

            raise HTTPException(status_code=403, detail="需要管理员权限")
        return User(username="admin", uid="admin", password_hash="x", role="admin")

    app.dependency_overrides[get_db] = fake_db
    app.dependency_overrides[get_admin_user] = fake_admin_user
    return app


CREATE_PAYLOAD = {
    "slug": "fetch_gene_info",
    "name": "基因查询",
    "description": "按基因 ID 查询基因注释信息",
    "spec": {"base_url": "https://api.example.com", "path": "/v1/genes/{{gene_id}}", "method": "GET"},
    "args_schema": {"type": "object", "properties": {"gene_id": {"type": "string"}}},
}


def test_create_custom_tool_ok(monkeypatch):
    captured = {}

    async def fake_create(db, **kwargs):
        captured.update(kwargs)
        return {"slug": kwargs["slug"], "lifecycle_status": "DRAFT", "enabled": False}

    monkeypatch.setattr("server.routers.tool_router.create_custom_tool", fake_create)
    client = TestClient(_build_app())
    resp = client.post("/api/system/tools", json=CREATE_PAYLOAD)
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["success"] is True
    assert payload["data"]["lifecycle_status"] == "DRAFT"
    assert captured["slug"] == "fetch_gene_info"
    assert captured["tenant_id"] == 1  # db=None stub → 默认租户 + uid=admin


def test_create_custom_tool_contract_error_maps_400(monkeypatch):
    async def fake_create(db, **kwargs):
        raise CustomToolError("标识 fetch_gene_info 与内置工具冲突，请更换")

    monkeypatch.setattr("server.routers.tool_router.create_custom_tool", fake_create)
    client = TestClient(_build_app())
    resp = client.post("/api/system/tools", json=CREATE_PAYLOAD)
    assert resp.status_code == 400
    assert "内置工具" in resp.json()["detail"]


def test_create_custom_tool_rejects_unknown_fields():
    client = TestClient(_build_app())
    resp = client.post("/api/system/tools", json={**CREATE_PAYLOAD, "exec_code": "print(1)"})
    assert resp.status_code == 422  # extra=forbid：在线代码面永远拒绝


def test_delete_custom_tool_reference_maps_409(monkeypatch):
    async def fake_delete(db, *, tenant_id, slug):
        raise CustomToolReferenceError("工具 x 仍被 1 个智能体引用（lab-assistant）")

    monkeypatch.setattr("server.routers.tool_router.delete_custom_tool", fake_delete)
    client = TestClient(_build_app())
    resp = client.delete("/api/system/tools/custom/x")
    assert resp.status_code == 409
    assert "lab-assistant" in resp.json()["detail"]


def test_missing_tool_maps_404(monkeypatch):
    async def fake_get(db, *, tenant_id, slug):
        return None

    monkeypatch.setattr("server.routers.tool_router.get_custom_tool", fake_get)
    client = TestClient(_build_app())
    resp = client.get("/api/system/tools/custom/missing")
    assert resp.status_code == 404


def test_status_requires_ready_maps_400(monkeypatch):
    async def fake_set_enabled(db, *, tenant_id, uid, slug, enabled):
        raise CustomToolError("仅测连通过（READY）的自定义工具可启用；请先执行测连")

    monkeypatch.setattr("server.routers.tool_router.set_custom_tool_enabled", fake_set_enabled)
    client = TestClient(_build_app())
    resp = client.put("/api/system/tools/custom/x/status", json={"enabled": True})
    assert resp.status_code == 400


def test_list_tools_merges_builtin_and_custom(monkeypatch):
    async def fake_catalog(db, *, tenant_id, category=None):
        assert tenant_id == 1
        return [
            {"slug": "present_artifacts", "name": "呈现产物", "category": "buildin"},
            {"slug": "fetch_gene_info", "name": "基因查询", "category": "custom", "source": "custom"},
        ]

    monkeypatch.setattr("server.routers.tool_router.build_merged_tool_catalog", fake_catalog)
    client = TestClient(_build_app())
    resp = client.get("/api/system/tools")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert [item["slug"] for item in data] == ["present_artifacts", "fetch_gene_info"]


def test_import_route_maps_contract_error(monkeypatch):
    async def fake_import(db, **kwargs):
        raise CustomToolError("文档缺少 openapi/swagger 版本字段，不是 OpenAPI 文档")

    monkeypatch.setattr("server.routers.tool_router.import_openapi_tools", fake_import)
    client = TestClient(_build_app())
    resp = client.post("/api/system/tools/import", json={"document": {"foo": 1}, "base_url": "https://x.com"})
    assert resp.status_code == 400


def test_non_admin_rejected():
    client = TestClient(_build_app(allow_admin=False))
    resp = client.post("/api/system/tools", json=CREATE_PAYLOAD)
    assert resp.status_code == 403
