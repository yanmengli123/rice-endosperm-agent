from __future__ import annotations

import pytest

from yuxi.knowledge.planning.turn_execution_plan import (
    Capability,
    EvidenceLevel,
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
    assert plan.answer.mode == "MCP_VALUE_ONLY"
    assert plan.requires_document_retrieval is False
    assert plan.satisfiable is True


def test_dataset_question_requires_official_record_and_buffers_answer():
    plan = plan_turn(
        "通过 MCP 查水稻胚乳磷酸化组数据集",
        has_knowledge_scope=False,
        configured_mcps=["gene-authority", "data-aggregator"],
    )

    assert plan.task.primary_intent == TaskIntent.DATASET_DISCOVERY
    assert plan.required_capabilities == [Capability.DATASET_LOOKUP]
    assert SourceClass.DISCOVERY in plan.source.allowed_sources
    assert plan.requires_mcp is True
    assert plan.buffers_output is True
    assert plan.evidence.level == EvidenceLevel.DATA_PROVENANCE


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
    assert plan.answer.citation_policy == "VERIFIED_CLAIMS_ONLY"


def test_no_knowledge_constraint_removes_document_evidence():
    plan = plan_turn("不要查知识库，直接解释这个概念", has_knowledge_scope=True)

    assert plan.requires_document_retrieval is False
    assert plan.evidence.level == EvidenceLevel.NONE
    assert plan.satisfiable is True
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
    assert plan.evidence.level == EvidenceLevel.BIBLIOGRAPHIC


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


def test_evidence_level_matrix_is_explicit_and_auditable():
    cases = [
        ("你好", False, [], EvidenceLevel.NONE, "NONE"),
        (
            "通过 MCP 查询 Wx 的源记录",
            True,
            ["ricekb"],
            EvidenceLevel.DATA_PROVENANCE,
            "DATA_SOURCES",
        ),
        (
            "帮我搜索 Wx 相关论文",
            True,
            ["literature-mcp"],
            EvidenceLevel.BIBLIOGRAPHIC,
            "REFERENCES",
        ),
        (
            "Wx 如何调控直链淀粉合成？",
            True,
            [],
            EvidenceLevel.CLAIM_EVIDENCE,
            "EVIDENCE",
        ),
        (
            "Figure S8 在哪一页？",
            True,
            [],
            EvidenceLevel.VERBATIM_LOCATOR,
            "ORIGINAL_TEXT",
        ),
    ]
    for question, has_scope, mcps, level, section in cases:
        plan = plan_turn(question, has_knowledge_scope=has_scope, configured_mcps=mcps)
        assert plan.evidence.level == level
        assert plan.answer.evidence_level == level
        assert plan.answer.source_section == section


def test_glossary_lookup_uses_canonical_record_not_document_quote():
    plan = plan_turn("术语PCR含义是什么", has_knowledge_scope=True)

    assert plan.task.primary_intent == TaskIntent.GLOSSARY_LOOKUP
    assert plan.evidence.level == EvidenceLevel.DATA_PROVENANCE
    assert plan.required_capabilities == [Capability.CANONICAL_LOOKUP]
    assert plan.requires_document_retrieval is False
    assert SourceClass.CANONICAL_RECORD in plan.source.allowed_sources


@pytest.mark.parametrize("question", ["OASIS 是什么缩写", "Wx 是什么", "PCR是什么意思"])
def test_short_definition_questions_route_to_glossary_before_gene_like(question):
    plan = plan_turn(question, has_knowledge_scope=True, configured_mcps=["ricekb"])

    assert plan.task.primary_intent == TaskIntent.GLOSSARY_LOOKUP
    assert plan.task.target_type == "TERM"
    assert plan.required_capabilities == [Capability.CANONICAL_LOOKUP]


def test_glossary_without_scope_remains_satisfiable_for_unavailable_outcome():
    plan = plan_turn("OASIS 是什么缩写", has_knowledge_scope=False)

    assert plan.satisfiable is True


def test_leading_combine_phrase_routes_to_hybrid_verification():
    plan = plan_turn(
        "结合知识库和 MCP 验证 Os01g0100100 的功能与论文依据",
        has_knowledge_scope=True,
        configured_mcps=["ricekb"],
    )

    assert plan.task.primary_intent == TaskIntent.HYBRID_VERIFICATION
    assert plan.source.policy == SourcePolicy.HYBRID_EXPLICIT
    assert [item.claim_id for item in plan.claim_obligations] == ["claim:database", "claim:literature"]
    assert SourceClass.CANONICAL_RECORD in plan.source.allowed_sources


# ── 服务器级意图解析（P1-A）──────────────────────────────────────


def test_named_configured_server_binds_to_plan():
    plan = plan_turn(
        "通过 BioMCP 查 Wx 基因信息",
        has_knowledge_scope=True,
        configured_mcps=["bio-mcp", "ricekb"],
        known_mcps=["bio-mcp", "ricekb"],
    )

    assert plan.source.policy == SourcePolicy.MCP_ONLY
    assert plan.required_server == "bio-mcp"
    assert plan.required_server_missing is None
    assert "EXPLICIT_MCP_SERVER_BOUND" in plan.reason_codes
    assert plan.satisfiable is True


@pytest.mark.parametrize(
    ("question", "server"),
    [
        ("从 NCBI 查询 Wx 的官方记录", "gene-authority"),
        ("用 UniProt 获取 Wx 蛋白信息", "gene-authority"),
        ("通过 Gramene 查询 Os06g0133000", "gramene"),
        ("使用 Plant Genomics MCP 获取 Wx 注释", "plant-genomics"),
        ("通过 Research Data Discovery 搜索水稻数据集", "data-aggregator"),
        ("查询 Wx 的 CDS 序列", "ricekb"),
        ("查询水稻 Wx 的详细基因档案", "ricekb-profile"),
    ],
)
def test_fixed_source_words_route_to_one_server(question: str, server: str):
    configured = ["gene-authority", "gramene", "plant-genomics", "data-aggregator", "ricekb", "ricekb-profile"]
    plan = plan_turn(question, has_knowledge_scope=False, configured_mcps=configured, known_mcps=configured)

    assert plan.source.policy == SourcePolicy.MCP_ONLY
    assert plan.required_server == server
    assert "FIXED_MCP_SOURCE_ROUTE" in plan.reason_codes
    assert plan.answer.mode == "MCP_VALUE_ONLY"


def test_at_mcp_mention_routes_even_after_control_token_is_removed():
    plan = plan_turn(
        "查询 Wx 的官方记录",
        has_knowledge_scope=False,
        configured_mcps=["gene-authority", "ricekb"],
        known_mcps=["gene-authority", "ricekb"],
        mentioned_mcp_slugs=["gene-authority"],
    )

    assert plan.source.policy == SourcePolicy.MCP_ONLY
    assert plan.required_server == "gene-authority"
    assert "MENTION_MCP_SERVER_BOUND" in plan.reason_codes


def test_named_unbound_builtin_server_fails_explicitly_without_silent_substitution():
    plan = plan_turn(
        "通过 BioMCP 查 Wx 基因信息",
        has_knowledge_scope=True,
        configured_mcps=["ricekb"],
        known_mcps=["bio-mcp", "ricekb"],
    )

    assert plan.required_server is None
    assert plan.required_server_missing == "bio-mcp"
    assert plan.satisfiable is False
    assert plan.error_code == "MCP_SERVER_NOT_CONFIGURED"
    assert "MCP_SERVER_NOT_CONFIGURED" in plan.reason_codes


# ── 六问 golden：非 MCP 题永不触发 MCP 义务（MCP 混乱回归锁）─────────────


def test_skills_glossary_and_locator_questions_never_require_mcp():
    skills_q = plan_turn(
        "你有哪些skills",
        has_knowledge_scope=True,
        configured_mcps=["ricekb"],
        known_mcps=["bio-mcp", "ricekb"],
    )
    assert skills_q.source.policy == SourcePolicy.AUTO
    assert skills_q.requires_mcp is False
    assert skills_q.required_server is None
    assert skills_q.required_server_missing is None

    glossary_q = plan_turn(
        "Oryza sativa是什么意思",
        has_knowledge_scope=True,
        configured_mcps=["ricekb"],
        known_mcps=["bio-mcp", "ricekb"],
    )
    assert glossary_q.task.primary_intent == TaskIntent.GLOSSARY_LOOKUP
    assert glossary_q.requires_mcp is False

    locator_q = plan_turn(
        "Figure 4 在哪一页，是什么意思",
        has_knowledge_scope=True,
        configured_mcps=["ricekb"],
        known_mcps=["bio-mcp", "ricekb"],
    )
    assert locator_q.source.policy == SourcePolicy.LOCAL_DOCUMENT_ONLY
    assert locator_q.evidence.level == EvidenceLevel.VERBATIM_LOCATOR
    assert locator_q.requires_mcp is False


def test_plain_mcp_rice_gene_query_uses_configured_rice_authority():
    plan = plan_turn(
        "通过 MCP 查 Wx 基因信息",
        has_knowledge_scope=True,
        configured_mcps=["ricekb"],
        known_mcps=["bio-mcp", "ricekb"],
    )

    assert plan.required_server == "ricekb"
    assert plan.required_server_missing is None
    assert plan.satisfiable is True


def test_plain_mcp_wx_prefers_profile_assembler_when_configured():
    plan = plan_turn(
        "通过 MCP 查 Wx",
        has_knowledge_scope=False,
        configured_mcps=["ricekb", "ricekb-profile"],
        known_mcps=["ricekb", "ricekb-profile"],
    )

    assert plan.required_server == "ricekb-profile"
    assert plan.answer.mode == "MCP_VALUE_ONLY"


def test_server_name_without_usage_intent_does_not_bind():
    plan = plan_turn(
        "BioMCP 是一个生物信息学 MCP 服务器吗",
        has_knowledge_scope=True,
        configured_mcps=["ricekb"],
        known_mcps=["bio-mcp", "ricekb"],
    )

    assert plan.required_server is None
    assert plan.required_server_missing is None


# ── 基因 ID 口径统一（P1-D）：7 位 MSU/RAP 形态触发路由 ──────────


def test_seven_digit_msu_locus_routes_to_rice_source_mcp():
    for scope in (False, True):
        plan = plan_turn("LOC_Os06g0133000 是什么基因？", has_knowledge_scope=scope, configured_mcps=["ricekb"])
        assert "RICE_SOURCE_IDENTIFIER_ROUTING" in plan.reason_codes
        assert SourceClass.STRUCTURED_DATABASE in plan.source.allowed_sources
        assert Capability.GENE_RECORD_LOOKUP in plan.required_capabilities
        assert plan.requires_mcp is True
        assert plan.satisfiable is True


def test_seven_digit_rap_locus_also_routes():
    plan = plan_turn("Os07g0842000 的注释有哪些", has_knowledge_scope=False, configured_mcps=["ricekb"])
    assert "RICE_SOURCE_IDENTIFIER_ROUTING" in plan.reason_codes
