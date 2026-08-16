from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from langchain.agents.middleware import ModelResponse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from yuxi.agents.middlewares.evidence_validation import (
    EvidenceValidationMiddleware,
    _disable_streaming_for_validation,
    validate_answer_evidence,
)


def _evidence(status: str = "STRICT", *, conflict: bool = False) -> dict:
    return {"evidence_id": "ev_rice_1", "evidence_status": status, "conflict": conflict}


def test_validator_requires_local_citation_for_scientific_claim():
    report = validate_answer_evidence("OsFIE1 调控水稻胚乳发育。", [_evidence()])
    assert report["valid"] is False
    assert "MISSING_EVIDENCE_CITATION" in report["violations"]
    assert "CLAIM_WITHOUT_LOCAL_EVIDENCE" in report["violations"]


def test_validator_enforces_candidate_and_conflict_language():
    candidate = validate_answer_evidence("OsFIE1 调控该过程（ev_rice_1）。", [_evidence("CANDIDATE")])
    conflict = validate_answer_evidence("OsFIE1 调控该过程（ev_rice_1）。", [_evidence(conflict=True)])
    assert "CANDIDATE_NOT_LABELED:ev_rice_1" in candidate["violations"]
    assert "CONFLICT_NOT_DISCLOSED:ev_rice_1" in conflict["violations"]


def test_validator_accepts_labeled_candidate_with_citation():
    report = validate_answer_evidence("候选证据提示 OsFIE1 可能调控该过程（ev_rice_1）。", [_evidence("CANDIDATE")])
    assert report["valid"] is True


def test_validator_does_not_treat_general_software_advice_as_scientific_claim():
    report = validate_answer_evidence("这个配置会影响前端构建性能。", None)
    assert report["valid"] is True


def test_non_domain_question_keeps_model_streaming():
    model = SimpleNamespace(model_copy=lambda **_kwargs: pytest.fail("普通问答不应关闭流式输出"))
    request = SimpleNamespace(
        model=model,
        messages=[HumanMessage(content="怎么优化前端构建？")],
        runtime=SimpleNamespace(context=SimpleNamespace(_effective_knowledge_scope={"scope_version": 2})),
    )
    assert _disable_streaming_for_validation(request) is request


@pytest.mark.asyncio
async def test_middleware_blocks_unreferenced_scientific_answer():
    middleware = EvidenceValidationMiddleware()
    request = SimpleNamespace(
        model=None,
        messages=[
            HumanMessage(content="OsFIE1 有什么功能？"),
            ToolMessage(
                name="query_knowledge_scope",
                tool_call_id="call-1",
                content=json.dumps({"evidence": [_evidence()]}, ensure_ascii=False),
            ),
        ],
        runtime=SimpleNamespace(context=SimpleNamespace(_effective_knowledge_scope={"scope_version": 2})),
    )

    async def handler(_request):
        return ModelResponse(result=[AIMessage(content="OsFIE1 调控水稻胚乳发育。")])

    response = await middleware.awrap_model_call(request, handler)
    assert "已阻止输出" in response.result[-1].content
    assert response.result[-1].additional_kwargs["knowledge_claim_validation"]["valid"] is False
