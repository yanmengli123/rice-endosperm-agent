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
                "claims": [
                    {
                        "claim_id": "claim_1",
                        "subject": {"name": "Wx"},
                        "predicate": "REGULATES_PHENOTYPE",
                        "object": {"name": "amylose content"},
                        "relation_group": "FUNCTIONAL_REGULATION",
                        "evidence": [{"evidence_id": "ev_1"}],
                    }
                ],
                "evidence": [{"evidence_id": "ev_1"}],
                "completeness": {"status": "PASS"},
            },
        ),
    )

    async def handler(_prepared):
        return ModelResponse(
            result=[
                AIMessage(
                    content=(
                        "Wx 的 NCBI Gene ID 为 4340018，RAP/MSU 位点为 LOC_Os06g04200；"
                        "它编码颗粒结合型淀粉合酶，相关研究见 PMID: 12345678。"
                    )
                )
            ]
        )

    response = await KnowledgeContextMiddleware().awrap_model_call(request, handler)

    assert "12345678" not in response.result[0].content
    assert "Wx 的 NCBI Gene ID 为 4340018" in response.result[0].content
    assert "LOC_Os06g04200" in response.result[0].content
    assert "颗粒结合型淀粉合酶" in response.result[0].content
    assert "模型叙述中的引用标识未通过校验" not in response.result[0].content
    validation = response.result[0].additional_kwargs["citation_validation"]
    assert validation["source_status"] == "FAIL"
    assert validation["status"] == "PASS"
    assert validation["action"] == "IDENTIFIERS_MOVED_TO_STRUCTURED_RESULTS"


@pytest.mark.asyncio
async def test_citation_guard_preserves_multimodal_blocks_and_sanitizes_all_text_blocks():
    contract = {
        "status": "COMPLETED",
        "claims": [],
        "evidence": [],
        "completeness": {"status": "NOT_APPLICABLE"},
    }
    request = _Request(
        messages=[HumanMessage("问题")],
        context=SimpleNamespace(
            _effective_knowledge_scope={"effective_kb_ids": ["kb-a"], "retrieval_policy": {}},
            _knowledge_contract=contract,
        ),
    )
    image_block = {"type": "image_url", "image_url": {"url": "https://example.test/wx.png"}}

    async def handler(_prepared):
        return ModelResponse(
            result=[
                AIMessage(
                    content=[
                        {"type": "text", "text": "Wx 调控直链淀粉含量，PMID: 12345678。"},
                        image_block,
                        {"type": "output_text", "text": "相关 DOI: 10.1000/wx-study。"},
                    ]
                )
            ]
        )

    response = await KnowledgeContextMiddleware().awrap_model_call(request, handler)

    content = response.result[0].content
    assert "Wx 调控直链淀粉含量" in content[0]["text"]
    assert "12345678" not in content[0]["text"]
    assert content[1] == image_block
    assert "10.1000/wx-study" not in content[2]["text"]
    detected = response.result[0].additional_kwargs["citation_validation"]["detected_identifiers"]
    assert set(detected) == {"PMID", "DOI"}


@pytest.mark.asyncio
async def test_citation_only_model_output_falls_back_to_deterministic_claims():
    request = _Request(
        messages=[HumanMessage("问题")],
        context=SimpleNamespace(
            _effective_knowledge_scope={"effective_kb_ids": ["kb-a"], "retrieval_policy": {}},
            _knowledge_contract={
                "status": "COMPLETED",
                "claims": [
                    {
                        "claim_id": "claim_1",
                        "subject": {"name": "Wx"},
                        "predicate": "ENCODES",
                        "object": {"name": "granule-bound starch synthase I"},
                        "relation_group": "FUNCTIONAL_REGULATION",
                        "evidence": [{"evidence_id": "ev_1"}],
                    }
                ],
                "evidence": [{"evidence_id": "ev_1"}],
                "completeness": {"status": "NOT_APPLICABLE"},
            },
        ),
    )

    async def handler(_prepared):
        return ModelResponse(result=[AIMessage(content="PMID: 12345678")])

    response = await KnowledgeContextMiddleware().awrap_model_call(request, handler)

    content = response.result[0].content
    assert "Wx" in content
    assert "granule-bound starch synthase I" in content
    assert "12345678" not in content
    assert "拒" not in content
    assert response.result[0].additional_kwargs["citation_validation"]["action"] == "DETERMINISTIC_CLAIM_FALLBACK"
