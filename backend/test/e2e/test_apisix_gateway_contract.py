"""APISIX 网关契约断言（桌面端适配 Phase 0）。

两层验证：
1. 配置层（总是执行）：解析 ``docker/apisix/apisix.yaml``，断言桌面端依赖的
   路由与请求体闭集字段都已显式声明——网关无 catch-all，漏一条即线上 404。
2. 实链路层（经网关才执行）：对 ``E2E_GATEWAY_URL``（默认本机 :9088）发起
   未授权请求，断言关键路由返回上游鉴权 401 而非网关 404。网关不可达时跳过，
   避免无网关环境误报；但一旦可达，404 就是失败。
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

# 配置断言需要读到仓库内的 apisix.yaml；api 容器只挂载 package/server/test，
# 读取不到时跳过（在宿主机或挂载了完整仓库的环境必须执行）。
APISIX_YAML = (
    Path(os.getenv("APISIX_YAML_PATH", ""))
    if os.getenv("APISIX_YAML_PATH")
    else (Path(__file__).resolve().parents[3] / "docker" / "apisix" / "apisix.yaml")
)
GATEWAY_URL = os.getenv("E2E_GATEWAY_URL", "http://127.0.0.1:9088").rstrip("/")

# 桌面端 yuxi.rs 实际调用的、且必须经网关放行的路由（uri, methods）。
REQUIRED_ROUTES: dict[str, set[str]] = {
    "/api/chat/attachments/tmp": {"POST"},
    "/api/chat/attachments/tmp/parse": {"POST"},
    "/api/chat/thread/:thread_id/attachments/confirm": {"POST"},
    "/api/user/quota": {"GET"},
    "/api/user/usage": {"GET"},
    "/api/auth/sessions": {"GET"},
    "/api/auth/sessions/:family_id": {"DELETE"},
    # 协议能力快照（公开）：连接阶段 fail-fast 的前置兼容判断。
    "/api/agent/protocol": {"GET"},
    # 会话问答 HTML 导出（导出为自包含文件保存到本地）。
    "/api/chat/thread/:thread_id/export": {"GET"},
    # 单条回答 HTML 导出（回答末尾「导出」按钮）。
    "/api/chat/thread/:thread_id/messages/:message_id/export": {"GET"},
    # 线程产物下载/预览与保存到工作区（MCP 物化产物、序列交付物、附件）。
    "/api/chat/thread/:thread_id/artifacts/*": {"GET"},
    "/api/chat/thread/:thread_id/artifacts/save": {"POST"},
    # 证据平面（citation_ready v2 图卡取图）：仅 GET，鉴权与归属由上游承担。
    "/api/knowledge/databases/:kb_id/documents/:file_id/revisions/:revision_id/assets/*": {"GET"},
}


def _load_routes() -> list[dict]:
    if not APISIX_YAML.exists():
        pytest.skip(f"读不到 {APISIX_YAML}（当前环境未挂载完整仓库）；请在仓库根目录环境执行")
    yaml = pytest.importorskip("yaml")

    data = yaml.safe_load(open(APISIX_YAML, encoding="utf-8"))
    assert isinstance(data, dict) and "routes" in data, "apisix.yaml 结构异常"
    return list(data["routes"])


def _run_create_schema(routes: list[dict]) -> dict:
    route = next(r for r in routes if r["uri"] == "/api/agent/runs")
    return route["plugins"]["request-validation"]["body_schema"]


def test_gateway_declares_all_desktop_required_routes() -> None:
    routes = _load_routes()
    declared = {(r["uri"], frozenset(m.upper() for m in r.get("methods", []))) for r in routes}
    for uri, methods in REQUIRED_ROUTES.items():
        matches = [m for (u, m) in declared if u == uri]
        assert matches, f"网关缺少路由 {uri}"
        missing = methods - set(matches[0])
        assert not missing, f"{uri} 缺少方法 {missing}"


def test_run_create_meta_allows_attachment_file_ids_but_stays_closed() -> None:
    schema = _run_create_schema(_load_routes())
    meta = schema["properties"]["meta"]
    field = meta["properties"]["attachment_file_ids"]
    assert field["type"] == "array"
    assert field["items"]["type"] == "string"
    assert meta["additionalProperties"] is False, "meta 闭集被打开会放行任意字段"


def test_run_create_top_level_allows_resume_fields_but_stays_closed() -> None:
    schema = _run_create_schema(_load_routes())
    assert "resume" in schema["properties"], "顶层缺 resume，中断恢复会被网关 400"
    assert "created_by_run_id" in schema["properties"]
    assert schema["additionalProperties"] is False


def test_attachment_upload_uses_long_timeout_upstream() -> None:
    routes = _load_routes()
    upload = next(r for r in routes if r["uri"] == "/api/chat/attachments/tmp")
    assert upload["upstream_id"] == "yuxi-backend-attachment", "上传必须走长超时 upstream"
    # multipart 上传不得做 JSON body 校验，否则请求被 400 拒绝
    assert "request-validation" not in upload["plugins"], "multipart 上传禁止 body 校验"


def test_gateway_has_no_catch_all_route() -> None:
    routes = _load_routes()
    for route in routes:
        uri = route["uri"]
        assert "*" not in uri or uri.count("*") == 1 and uri.endswith("/*"), f"可疑通配路由 {uri}"
        assert uri != "/*", "禁止 catch-all：所有路由必须显式声明"


def test_run_create_gateway_schema_matches_pydantic_model() -> None:
    """网关闭集 schema 与服务端 pydantic 模型的漂移检查（单一真源守护）。

    网关 schema 手写在 apisix.yaml，与服务端 AgentRunCreate 是两份声明；
    字段集合漂移（一边加了字段另一边没加）会让请求被网关 400 或被上游 422，
    这里强制两者保持一致。
    """
    routes = _load_routes()
    schema = _run_create_schema(routes)
    try:
        from server.routers.agent_router import AgentRunCreate
    except ImportError:  # pragma: no cover - 容器外环境无 server 包
        pytest.skip("当前环境导入不了 server.routers.agent_router")

    model_fields = set(AgentRunCreate.model_fields)
    gateway_fields = set(schema["properties"])
    assert gateway_fields == model_fields, (
        f"网关与 pydantic 字段漂移：仅网关有 {gateway_fields - model_fields}，"
        f"仅 pydantic 有 {model_fields - gateway_fields}"
    )
    # 网关必填是 pydantic 必填（agent_slug/thread_id）+ 网关治理要求（meta）。
    assert set(schema["required"]) == {"agent_slug", "thread_id", "meta"}


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_live_agent_protocol_endpoint_reachable_without_auth() -> None:
    """经网关实测：协议能力端点公开可达，桌面端登录前即可做版本兼容判断。"""
    try:
        async with httpx.AsyncClient(base_url=GATEWAY_URL, timeout=10.0) as client:
            response = await client.get("/api/agent/protocol")
            assert response.status_code == 200, (
                f"/api/agent/protocol 经网关返回 {response.status_code}："
                "apisix.yaml 未放行或容器未重建（改配置必须 force-recreate）"
            )
            body = response.json()
            assert body["service"] == "yuxi"
            assert body["protocol_version"]
            assert isinstance(body["capabilities"], list)
    except httpx.ConnectError:
        pytest.skip(f"网关不可达：{GATEWAY_URL}")


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_live_knowledge_asset_route_hits_upstream_auth_not_404() -> None:
    """经网关实测：图卡资产路由未带凭证应得上游 401，网关级 404 说明路由未放行。

    上游对已鉴权但缺失/越权的资产刻意返回 404（防枚举），因此本探测只断言
    「不是网关 404」且落在鉴权拒绝区间。
    """
    try:
        async with httpx.AsyncClient(base_url=GATEWAY_URL, timeout=10.0) as client:
            response = await client.get(
                "/api/knowledge/databases/probe-kb/documents/probe-file"
                "/revisions/probe-rev/assets/probe-figure.png"
            )
            assert response.status_code != 404, (
                "知识库资产路由经网关 404：apisix.yaml 未放行或容器未重建（改配置必须 force-recreate）"
            )
            assert response.status_code in (401, 403), (
                f"未带凭证的资产请求意外状态 {response.status_code}"
            )
    except httpx.ConnectError:
        pytest.skip(f"网关不可达：{GATEWAY_URL}")


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_live_attachment_routes_hit_upstream_auth_not_404() -> None:
    """经网关实测：未带凭证的附件请求应得 401（上游鉴权），404 说明路由未放行。"""
    try:
        async with httpx.AsyncClient(base_url=GATEWAY_URL, timeout=10.0) as client:
            for path in (
                "/api/chat/attachments/tmp",
                "/api/chat/attachments/tmp/parse",
                "/api/chat/thread/probe-thread-id/attachments/confirm",
            ):
                response = await client.post(path, json={})
                assert response.status_code != 404, (
                    f"{path} 经网关 404：apisix.yaml 未放行或容器未重建（改配置必须 force-recreate）"
                )
                assert response.status_code in (400, 401, 422), f"{path} 意外状态 {response.status_code}"
    except httpx.ConnectError:
        pytest.skip(f"网关不可达：{GATEWAY_URL}")
