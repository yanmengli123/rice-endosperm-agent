"""MCP 工具 schema 透传回归测试。

背景缺陷：langchain-mcp-adapters 0.2.x 返回的 ``args_schema`` 是原始
JSON-schema **dict**（非 pydantic 类），MCP 分层重构中描述符层用
``isinstance(args_model, type)`` 判断导致 dict 被丢弃，所有工具退化为
通用 ``arguments: dict`` 兜底 schema——模型按真实字段名（如 ``symbol``）
传参时全部被校验拒绝，bio-mcp 四个基因查询工具因此集体报"参数校验失败"。
"""

from __future__ import annotations

from types import SimpleNamespace

from yuxi.agents.mcp.host import LegacyLangChainHost
from yuxi.agents.mcp.langchain_adapter import build_mcp_base_tool

DICT_SCHEMA = {
    "type": "object",
    "properties": {
        "symbol": {"type": "string", "description": "gene symbol"},
        "species": {"type": "string", "description": "plant species"},
    },
    "required": ["symbol"],
}


def _fake_host():
    """绕过单例：给 build_mcp_base_tool 注入不发起 IO 的 host 工厂。"""

    class _Host:
        async def call_tool(self, slug, config, name, arguments):
            return SimpleNamespace(text="ok", to_dict=lambda: {"text": "ok"})

    return _Host


def test_descriptor_keeps_dict_args_schema():
    raw = SimpleNamespace(
        name="plant_gene_lookup",
        description="lookup plant gene",
        args_schema=DICT_SCHEMA,
        metadata={"annotations": {}},
    )
    descriptor = LegacyLangChainHost()._descriptor_from_raw("bio-mcp", raw)
    assert descriptor.args_model == DICT_SCHEMA


def test_descriptor_none_schema_still_falls_back():
    raw = SimpleNamespace(name="no_schema_tool", description="d", args_schema=None, metadata={})
    descriptor = LegacyLangChainHost()._descriptor_from_raw("bio-mcp", raw)
    assert descriptor.args_model is None


def test_assembled_tool_uses_dict_schema_not_fallback():
    raw = SimpleNamespace(
        name="plant_gene_lookup",
        description="lookup plant gene",
        args_schema=DICT_SCHEMA,
        metadata={"annotations": {}},
    )
    descriptor = LegacyLangChainHost()._descriptor_from_raw("bio-mcp", raw)
    import yuxi.agents.mcp.langchain_adapter as adapter

    original = adapter._host_resolver
    adapter.set_host_resolver(_fake_host())
    try:
        tool = build_mcp_base_tool(descriptor, {"transport": "stdio"})
    finally:
        adapter.set_host_resolver(original)
    # 装配后的工具必须携带真实 schema（模型才能看到 symbol 字段），
    # 而不是退化为通用 arguments:dict 兜底。
    assert tool.args_schema == DICT_SCHEMA
    schema = tool.args_schema if isinstance(tool.args_schema, dict) else tool.args_schema.model_json_schema()
    assert "symbol" in schema.get("properties", {})
    assert schema.get("required") == ["symbol"]
