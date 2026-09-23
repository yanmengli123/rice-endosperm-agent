"""Source Registry 企业级字段 golden（ADR-0006 §3 验收）。"""

from yuxi.agents.mcp.capability_registry import (
    profile_for_protocol_name,
    profile_for_server_tool,
)
from yuxi.knowledge.evidence.dimensions import ClaimClass
from yuxi.knowledge.planning.turn_execution_plan import EvidenceLevel

_OFFICIAL_SOURCE_TOOLS = (
    "ncbi_datasets_gene_report_rest",
    "ncbi_datasets_gene_summary_cli",
    "ncbi_datasets_gene_package_cli",
    "uniprot_entry_rest",
    "uniprot_search_rest",
    "europe_pmc_search_rest",
    "europe_pmc_article_rest",
    "ncbi_eutils_summary_rest",
    "ncbi_eutils_fetch_rest",
    "pride_project_rest",
    "pride_project_files_rest",
)


def test_official_source_profiles_carry_full_registration_metadata():
    for name in _OFFICIAL_SOURCE_TOOLS:
        profile = profile_for_protocol_name(name)
        assert profile is not None, name
        assert profile.provider, name
        assert profile.provider_version, name
        assert profile.supported_claim_types, name
        assert profile.max_evidence_obligation is not None, name
        assert profile.failure_semantics, name
        assert profile.egress_class, name


def test_pride_project_metadata_cannot_support_ptm_claims():
    """PXD 记录只能证明项目元数据，不能单凭它证明「Wx 被磷酸化」。"""
    for name in ("pride_project_rest", "pride_project_files_rest"):
        profile = profile_for_protocol_name(name)
        assert profile.supported_claim_types == frozenset({ClaimClass.DATASET_METADATA})
        assert profile.max_evidence_obligation == EvidenceLevel.DATA_PROVENANCE
        assert ClaimClass.PTM_MODIFICATION not in profile.supported_claim_types


def test_ncbi_uniprot_profiles_support_record_facts_only_at_e1():
    for name in ("ncbi_datasets_gene_report_rest", "uniprot_entry_rest"):
        profile = profile_for_protocol_name(name)
        assert profile.supported_claim_types == frozenset({ClaimClass.RECORD_FACT})
        assert profile.max_evidence_obligation == EvidenceLevel.DATA_PROVENANCE


def test_europe_pmc_bibliography_caps_at_e2_and_declares_oa_subset_license():
    profile = profile_for_protocol_name("europe_pmc_search_rest")
    assert profile.supported_claim_types == frozenset({ClaimClass.BIBLIOGRAPHIC_FACT})
    assert profile.max_evidence_obligation == EvidenceLevel.BIBLIOGRAPHIC
    assert profile.license_scope == "OA_SUBSET_ONLY"


def test_discovery_only_tools_support_no_claims():
    for name in ("ncbi_eutils_search_rest", "pride_search_projects_rest", "europe_pmc_oa_passages_rest"):
        profile = profile_for_protocol_name(name)
        assert profile.answer_eligible is False
        assert profile.supported_claim_types == frozenset()
        assert profile.max_evidence_obligation is None


def test_server_scoped_profiles_are_fail_closed():
    # data-aggregator 的四个发现工具在服务器限定内受信
    profile = profile_for_server_tool("data-aggregator", "search")
    assert profile is not None
    assert profile.answer_eligible is False
    assert profile.egress_class == "aggregator"
    # 同一服务器上未列名工具：fail-closed，不回落全局裸名表
    assert profile_for_server_tool("data-aggregator", "fetch") is None
    assert profile_for_server_tool("data-aggregator", "ricekb_resolve") is None
    # 裸工具名在其他服务器上不得冒领 data-aggregator 的发现级 profile
    assert profile_for_server_tool("other-server", "search") is None


def test_existing_profiles_keep_prior_classification():
    profile = profile_for_protocol_name("ricekb_resolve")
    assert profile is not None
    assert profile.authority_level == "PRIMARY_DATABASE"
    assert profile.answer_eligible is True
