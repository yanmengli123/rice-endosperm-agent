from __future__ import annotations

import pytest

from yuxi.knowledge.planning.turn_execution_plan import (
    Capability,
    SourcePolicy,
    TaskIntent,
    plan_turn,
)


def test_explicit_mcp_gene_query_never_requests_document_evidence():
    plan = plan_turn(
        "你通过MCP把水稻 Wx 基因详细信息给我",
        has_knowledge_scope=True,
        configured_mcps=["bioinfo-mcp"],
        knowledge_strategy="KNOWLEDGE_FIRST",
    )

    assert plan.task.primary_intent == TaskIntent.ENTITY_PROFILE
    assert plan.source.policy == SourcePolicy.MCP_ONLY
    assert plan.required_capabilities == [Capability.GENE_RECORD_LOOKUP]
    assert plan.requires_document_retrieval is False
    assert plan.satisfiable is True


def test_explicit_mcp_without_server_fails_closed():
    plan = plan_turn("通过 MCP 查 Wx", has_knowledge_scope=True, configured_mcps=[])

    assert plan.source.policy == SourcePolicy.MCP_ONLY
    assert plan.satisfiable is False
    assert plan.error_code == "SOURCE_UNAVAILABLE"


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        (
            "这句话在原文哪一页：A sufficiently long verbatim sentence from the source document",
            TaskIntent.QUOTE_LOCATOR,
        ),
        ("Figure S8 在哪一页？", TaskIntent.FIGURE_LOCATOR),
        ("Table S2 在哪一页？", TaskIntent.TABLE_LOCATOR),
        ("这句话在原文哪一句：A sufficiently long verbatim sentence from the source", TaskIntent.QUOTE_LOCATOR),
    ],
)
def test_high_determinism_locators_are_orchestrator_owned(question: str, expected: TaskIntent):
    plan = plan_turn(question, has_knowledge_scope=True, configured_mcps=["bioinfo-mcp"])

    assert plan.task.primary_intent == expected
    assert plan.source.policy == SourcePolicy.LOCAL_DOCUMENT_ONLY
    assert plan.evidence.exact_locator_required is True
    assert plan.requires_document_retrieval is True
    assert plan.risk_class == "HIGH_DETERMINISM"


def test_locator_plus_explanation_is_compound_document_interpretation():
    plan = plan_turn(
        "Figure S8 Rice grain starch physicochemical characteristics comparison 在哪一页，具体是什么意思？",
        has_knowledge_scope=True,
    )

    assert plan.task.primary_intent == TaskIntent.DOCUMENT_INTERPRETATION
    assert plan.task.secondary_intents == [TaskIntent.QUOTE_LOCATOR]
    assert Capability.PDF_LOCATOR in plan.required_capabilities
    assert Capability.DOCUMENT_QA in plan.required_capabilities


def test_mcp_only_pdf_locator_conflict_is_rejected_before_execution():
    plan = plan_turn(
        "通过 MCP 查询 Figure S8 在论文第几页",
        has_knowledge_scope=True,
        configured_mcps=["bioinfo-mcp"],
    )

    assert plan.source.policy == SourcePolicy.MCP_ONLY
    assert plan.satisfiable is False
    assert plan.error_code == "PLAN_UNSATISFIABLE"


def test_default_knowledge_question_keeps_enterprise_evidence_route():
    plan = plan_turn(
        "Wx 如何调控直链淀粉合成？",
        has_knowledge_scope=True,
        knowledge_strategy="MODEL_DECIDES",
    )

    assert plan.source.policy == SourcePolicy.AUTO
    assert plan.requires_document_retrieval is True
    assert plan.answer.citation_policy == "VERIFIED_ONLY"


def test_no_knowledge_constraint_removes_document_evidence():
    plan = plan_turn("不要查知识库，直接解释这个概念", has_knowledge_scope=True)

    assert plan.requires_document_retrieval is False
    assert "EXPLICIT_NO_KB" in plan.reason_codes


def test_numeric_verification_without_document_scope_fails_closed():
    plan = plan_turn(
        "请核验原文中 17.2% 这个数值是否正确",
        has_knowledge_scope=False,
    )

    assert plan.satisfiable is False
    assert plan.error_code == "SOURCE_UNAVAILABLE"
    assert "NO_LOCAL_DOCUMENT_SCOPE" in plan.reason_codes


def test_hybrid_request_requires_both_provider_and_document_planes():
    plan = plan_turn(
        "先用 MCP 查 Wx，然后结合论文核验",
        has_knowledge_scope=True,
        configured_mcps=["bioinfo-mcp"],
    )

    assert plan.source.policy == SourcePolicy.HYBRID_EXPLICIT
    assert plan.task.primary_intent == TaskIntent.HYBRID_VERIFICATION
    assert plan.requires_mcp is True
    assert plan.requires_document_retrieval is True


def test_bibliography_only_output_is_buffered_for_non_document_guard():
    plan = plan_turn(
        "帮我搜索 Wx 相关论文",
        has_knowledge_scope=True,
        configured_mcps=["literature-mcp"],
    )

    assert plan.source.policy == SourcePolicy.BIBLIOGRAPHY_ONLY
    assert plan.requires_document_retrieval is False
    assert plan.requires_mcp is True
    assert plan.buffers_output is True


def test_bibliography_only_without_provider_fails_closed():
    plan = plan_turn("帮我搜索 Wx 相关论文", has_knowledge_scope=True, configured_mcps=[])

    assert plan.satisfiable is False
    assert plan.error_code == "SOURCE_UNAVAILABLE"
