from types import SimpleNamespace

import pytest

from yuxi.agents.middlewares.knowledge_context import KnowledgeContextMiddleware
from yuxi.knowledge.planning.turn_execution_plan import plan_turn


class _Request:
    def __init__(self, *, context, tools, system_message=None, messages=None):
        self.runtime = SimpleNamespace(context=context)
        self.tools = tools
        self.system_message = system_message
        self.messages = messages or []

    def override(self, **values):
        return _Request(
            context=self.runtime.context,
            tools=values.get("tools", self.tools),
            system_message=values.get("system_message", self.system_message),
            messages=values.get("messages", self.messages),
        )


def _tool(name, *, mcp_name=None):
    metadata = {"server": "bioinfo-mcp", "mcp_tool_name": mcp_name} if mcp_name else {}
    return SimpleNamespace(name=name, metadata=metadata)


@pytest.mark.asyncio
async def test_mcp_only_exposes_only_capability_matched_mcp_tool():
    plan = plan_turn(
        "通过 MCP 查 Wx 基因详细信息",
        has_knowledge_scope=True,
        configured_mcps=["bioinfo-mcp"],
    )
    context = SimpleNamespace(
        _turn_execution_plan=plan.public_dict(),
        _effective_knowledge_scope={"effective_kb_ids": ["kb-a"]},
        _knowledge_contract=None,
    )
    request = _Request(
        context=context,
        tools=[
            _tool("query_knowledge_scope"),
            _tool("plant_gene_lookup", mcp_name="plant_gene_lookup"),
            _tool("unknown_mcp", mcp_name="unknown_mcp"),
            _tool("tavily_search"),
        ],
    )
    captured = {}

    async def handler(prepared):
        captured["system_message"] = prepared.system_message
        return [tool.name for tool in prepared.tools]

    result = await KnowledgeContextMiddleware().awrap_model_call(request, handler)

    assert result == ["plant_gene_lookup"]
    assert "AUTHORITATIVE_TURN_EXECUTION_PLAN" in captured["system_message"].text
    assert "AUTHORITATIVE_RUN_KNOWLEDGE_SCOPE" not in captured["system_message"].text


@pytest.mark.asyncio
async def test_frozen_document_plan_exposes_no_model_driven_evidence_tools():
    plan = plan_turn("Figure S8 在哪一页？", has_knowledge_scope=True)
    context = SimpleNamespace(
        _turn_execution_plan=plan.public_dict(),
        _effective_knowledge_scope={"effective_kb_ids": ["kb-a"], "retrieval_policy": {}},
        _knowledge_contract={"status": "COMPLETED", "claims": [], "evidence": []},
    )
    request = _Request(
        context=context,
        tools=[_tool("grep_evidence"), _tool("deepen_evidence"), _tool("query_kb")],
    )

    async def handler(prepared):
        return [tool.name for tool in prepared.tools]

    assert await KnowledgeContextMiddleware().awrap_model_call(request, handler) == []
