"""自定义工具装配/执行层单测：模板替换、URL 渲染、SSRF 拒绝、StructuredTool 装配。"""

from __future__ import annotations

import httpx
import pytest

from yuxi.agents.toolkits.custom.adapter import (
    build_custom_tool,
    execute_custom_http_tool,
    render_url_and_query,
    substitute_template,
)
from yuxi.agents.toolkits.custom.domain import CustomToolError


def _patch_dns(monkeypatch):
    async def _noop(url, *, allow_insecure_http=None):
        return url

    monkeypatch.setattr("yuxi.agents.toolkits.custom.adapter.validate_remote_url_dns", _noop)


def _mock_client(handler):
    def factory():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False, trust_env=False)

    return factory


RECORD = {
    "slug": "fetch_gene_info",
    "tenant_id": 1,
    "tool_type": "http",
    "description": "按基因 ID 查询信息",
    "data_access_level": "PUBLIC",
    "args_schema": {
        "type": "object",
        "properties": {
            "gene_id": {"type": "string", "description": "基因 ID"},
            "with_seq": {"type": "boolean", "description": "是否返回序列"},
        },
        "required": ["gene_id"],
    },
    "spec": {
        "base_url": "https://api.example.com",
        "path": "/v1/genes/{{gene_id}}",
        "method": "GET",
        "headers": {"X-Client": "yuxi"},
        "query": {"seq": "1"},
        "body_params": [],
        "timeout_s": 5,
        "response": {"max_text_length": 200},
    },
}


def test_substitute_template_quotes_and_validates():
    assert substitute_template("/v1/{{gene_id}}", {"gene_id": "Os01g01"}, section="path") == "/v1/Os01g01"
    assert substitute_template("/v1/{{gene_id}}", {"gene_id": "a b/c"}, section="path") == "/v1/a%20b%2Fc"
    assert substitute_template("q={{with_seq}}", {"with_seq": True}, section="query") == "q=true"
    with pytest.raises(CustomToolError):
        substitute_template("/v1/{{gene_id}}", {}, section="path")
    with pytest.raises(CustomToolError):
        substitute_template("/v1/{{gene_id}}", {"gene_id": ["x"]}, section="path")


def test_render_url_and_query():
    path, query = render_url_and_query(RECORD["spec"], {"gene_id": "Os01g01"})
    assert path == "/v1/genes/Os01g01"
    assert query == {"seq": "1"}
    path, query = render_url_and_query({**RECORD["spec"], "query": {"q": "{{gene_id}}"}}, {"gene_id": "Os01g01"})
    assert query == {"q": "Os01g01"}


async def test_execute_custom_http_tool_get(monkeypatch):
    _patch_dns(monkeypatch)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, text='{"name": "Os01g01"}')

    result = await execute_custom_http_tool(RECORD, {"gene_id": "Os01g01"}, client_factory=_mock_client(handler))
    assert result["status_code"] == 200
    assert result["text"] == '{"name": "Os01g01"}'
    assert captured["method"] == "GET"
    assert captured["url"].startswith("https://api.example.com/v1/genes/Os01g01")
    assert captured["url"].endswith("?seq=1")
    assert captured["headers"].get("x-client") == "yuxi"


async def test_execute_custom_http_tool_post_body_and_auth(monkeypatch):
    _patch_dns(monkeypatch)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = request.read()
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(201, text="created")

    record = {
        **RECORD,
        "auth": ("bearer", "s3cr3t-token", {}),
        "spec": {
            **RECORD["spec"],
            "method": "POST",
            "path": "/v1/genes",
            "query": {},
            "body_params": ["gene_id"],
        },
    }
    result = await execute_custom_http_tool(record, {"gene_id": "Os01g01"}, client_factory=_mock_client(handler))
    assert result["status_code"] == 201
    assert b'"gene_id":"Os01g01"' in captured["json"]
    assert captured["auth"] == "Bearer s3cr3t-token"


async def test_execute_custom_http_tool_truncates_long_response(monkeypatch):
    _patch_dns(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="x" * 1000)

    result = await execute_custom_http_tool(RECORD, {"gene_id": "g"}, client_factory=_mock_client(handler))
    assert result["text"].startswith("xxx")
    assert "已截断" in result["text"]
    assert len(result["text"]) < 400


async def test_execute_rejects_private_base_url_without_dns(monkeypatch):
    # 静态校验在 DNS 之前：私网字面量必须被拒，且无需任何网络 I/O
    from yuxi.agents.mcp.security import McpSecurityError

    with pytest.raises(McpSecurityError):
        await execute_custom_http_tool(
            {**RECORD, "spec": {**RECORD["spec"], "base_url": "https://127.0.0.1:8443"}},
            {"gene_id": "g"},
        )


async def test_build_custom_tool_ainvoke(monkeypatch):
    async def fake_execute(record, args, *, client_factory=None):
        assert record["slug"] == "fetch_gene_info"
        assert args == {"gene_id": "Os01g01"}
        return {"status_code": 200, "text": "ok", "duration_ms": 3}

    monkeypatch.setattr("yuxi.agents.toolkits.custom.adapter.execute_custom_http_tool", fake_execute)
    tool = build_custom_tool(RECORD)
    assert tool.name == "fetch_gene_info"
    assert tool.metadata["custom_tool"] == "fetch_gene_info"
    # langchain 会把 dict schema 归一为扁平字段或 {"properties": ...}，两种形态都接受
    args = tool.args
    assert "gene_id" in args.get("properties", args)
    text = await tool.ainvoke({"gene_id": "Os01g01"})
    assert text == "ok"
