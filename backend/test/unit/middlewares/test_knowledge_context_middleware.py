from types import SimpleNamespace

import pytest
from langchain.agents.middleware import ModelResponse
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from yuxi.agents.middlewares.knowledge_context import KnowledgeContextMiddleware


class _Request:
    def __init__(self, *, messages, context, system_message=None, tools=None):
        self.messages = messages
        self.runtime = SimpleNamespace(context=context)
        self.system_message = system_message
        self.tools = tools or []

    def override(self, **values):
        return _Request(
            messages=values.get("messages", self.messages),
            context=self.runtime.context,
            system_message=values.get("system_message", self.system_message),
            tools=values.get("tools", self.tools),
        )


@pytest.mark.asyncio
async def test_authoritative_scope_marks_stale_assistant_runtime_state_non_authoritative():
    request = _Request(
        messages=[HumanMessage("问题"), AIMessage("当前会话没有挂载知识库。")],
        context=SimpleNamespace(
            _effective_knowledge_scope={
                "scope_id": "scope_default_qa",
                "scope_version": 8,
                "knowledge_strategy": "KNOWLEDGE_FIRST",
                "effective_kb_ids": ["kb-a"],
                "retrieval_mode": "KB_ONLY",
                "allow_web": False,
            },
            _knowledge_contract=None,
        ),
        system_message=SystemMessage("base"),
    )
    captured = {}

    async def handler(prepared):
        captured["request"] = prepared
        return "ok"

    result = await KnowledgeContextMiddleware().awrap_model_call(request, handler)

    assert result == "ok"
    assert "AUTHORITATIVE_RUN_KNOWLEDGE_SCOPE" in captured["request"].system_message.text
    assert "NON_AUTHORITATIVE" in captured["request"].messages[1].content


@pytest.mark.asyncio
async def test_contract_hides_repeated_unified_retrieval_tool():
    tools = [
        SimpleNamespace(name="query_knowledge_scope"),
        SimpleNamespace(name="query_kb"),
        SimpleNamespace(name="deepen_evidence"),
    ]
    request = _Request(
        messages=[
            HumanMessage("问题"),
            ToolMessage(
                content='{"evidence":[{"evidence_quote":"full raw quote"}]}',
                tool_call_id="kr_1",
                name="query_knowledge_scope",
                additional_kwargs={"knowledge_first": True},
            ),
        ],
        context=SimpleNamespace(
            _effective_knowledge_scope={"effective_kb_ids": ["kb-a"], "retrieval_policy": {}},
            _knowledge_contract={
                "retrieval_id": "kr_1",
                "status": "COMPLETED",
                "claims": [],
                "evidence": [{"evidence_quote": "full raw quote"}],
            },
        ),
        tools=tools,
    )

    captured = {}

    async def handler(prepared):
        captured["messages"] = prepared.messages
        return [tool.name for tool in prepared.tools]

    assert await KnowledgeContextMiddleware().awrap_model_call(request, handler) == ["deepen_evidence"]
    assert "full raw quote" not in captured["messages"][1].content


@pytest.mark.asyncio
async def test_narrative_identifier_is_replaced_by_citation_guard():
    request = _Request(
        messages=[HumanMessage("问题")],
        context=SimpleNamespace(
            _effective_knowledge_scope={"effective_kb_ids": ["kb-a"], "retrieval_policy": {}},
            _knowledge_contract={
                "retrieval_id": "kr_1",
                "status": "COMPLETED",
                "claims": [{"claim_id": "claim_1"}],
                "evidence": [{"evidence_id": "ev_1"}],
                "completeness": {"status": "PASS"},
            },
        ),
    )

    async def handler(_prepared):
        return ModelResponse(result=[AIMessage(content="模型自行写了 PMID: 12345678")])

    response = await KnowledgeContextMiddleware().awrap_model_call(request, handler)

    assert "12345678" not in response.result[0].content
    assert response.result[0].additional_kwargs["citation_validation"]["status"] == "FAIL"
