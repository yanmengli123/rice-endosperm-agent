from __future__ import annotations

import pytest

from yuxi.knowledge.planning.turn_execution_plan import (
    Capability,
    SourceClass,
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


# --- Rice Source KB identifier routing (builtin MCP "ricekb") ---------------


@pytest.mark.parametrize(
    "question",
    [
        "LOC_Os06g01210 是什么基因？",
        "Os06g0101600 的注释有哪些",
        "RAP:Os01g0100100 在三个源库里的记录一致吗",
        "转录本 Os06t0101600-01 对应哪个 MSU model？LOC_Os06g01210.1 吗",
    ],
)
def test_rice_identifier_admits_structured_database_when_ricekb_configured(question):
    for scope in (False, True):
        plan = plan_turn(question, has_knowledge_scope=scope, configured_mcps=["ricekb"])
        assert plan.source.policy == SourcePolicy.AUTO
        assert SourceClass.STRUCTURED_DATABASE in plan.source.allowed_sources
        assert SourceClass.STRUCTURED_DATABASE not in plan.source.forbidden_sources
        assert Capability.GENE_RECORD_LOOKUP in plan.required_capabilities
        assert plan.task.target_type == "GENE"
        assert "RICE_SOURCE_IDENTIFIER_ROUTING" in plan.reason_codes
        assert plan.satisfiable is True
    # Knowledge sources keep whatever AUTO already granted.
    scoped = plan_turn(question, has_knowledge_scope=True, configured_mcps=["ricekb"])
    assert SourceClass.LOCAL_DOCUMENT in scoped.source.allowed_sources


def test_rice_identifier_routing_requires_ricekb_to_be_configured():
    plan = plan_turn("LOC_Os06g01210 是什么基因？", has_knowledge_scope=False, configured_mcps=["bioinfo-mcp"])
    assert SourceClass.STRUCTURED_DATABASE not in plan.source.allowed_sources
    assert "RICE_SOURCE_IDENTIFIER_ROUTING" not in plan.reason_codes


def test_bare_symbol_or_unrelated_text_does_not_trigger_rice_routing():
    for question in (
        "Wx 基因对应哪个 RAP locus？",
        "OsMADS3 的功能",
        "Os13g0100100 不存在的染色体",
        "LOC_Os06g0121 位数不对",
    ):
        plan = plan_turn(question, has_knowledge_scope=False, configured_mcps=["ricekb"])
        assert "RICE_SOURCE_IDENTIFIER_ROUTING" not in plan.reason_codes, question
        assert SourceClass.STRUCTURED_DATABASE not in plan.source.allowed_sources, question


def test_explicit_no_mcp_veto_beats_rice_identifier_routing():
    plan = plan_turn("不要调用 MCP，LOC_Os06g01210 是什么基因？", has_knowledge_scope=True, configured_mcps=["ricekb"])
    assert SourceClass.STRUCTURED_DATABASE in plan.source.forbidden_sources
    assert SourceClass.STRUCTURED_DATABASE not in plan.source.allowed_sources
    assert "RICE_SOURCE_IDENTIFIER_ROUTING" not in plan.reason_codes


def test_explicit_mcp_phrasing_with_rice_identifier_keeps_mcp_only():
    plan = plan_turn("通过 MCP 查询 LOC_Os06g01210 是什么基因？", has_knowledge_scope=True, configured_mcps=["ricekb"])
    assert plan.source.policy == SourcePolicy.MCP_ONLY
    assert plan.source.allowed_sources == [SourceClass.STRUCTURED_DATABASE]
    assert plan.required_capabilities == [Capability.GENE_RECORD_LOOKUP]
    assert "RICE_SOURCE_IDENTIFIER_ROUTING" not in plan.reason_codes
