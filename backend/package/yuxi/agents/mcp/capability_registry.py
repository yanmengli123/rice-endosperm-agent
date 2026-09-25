"""Trusted server-side capability profiles for MCP tools.

MCP descriptions and annotations are provider-controlled and therefore never
grant citation/page authority.  Only this registry may classify a tool for a
source-constrained turn.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from yuxi.knowledge.evidence.dimensions import ClaimClass
from yuxi.knowledge.planning.turn_execution_plan import Capability, EvidenceLevel


@dataclass(frozen=True)
class ToolCapabilityProfile:
    capabilities: frozenset[Capability]
    source_class: str
    answer_eligible: bool = True
    produces_document_evidence: bool = False
    citation_semantics: str = "DATA_PROVENANCE"
    authority_level: str = "PROVIDER_DECLARED"
    fallback_policy: str = "FAIL_CLOSED"
    # --- ADR-0006 企业级来源注册字段（服务端裁决的输入，提供方不可自填）---
    # 提供方与其 API/数据版本：可复现裁决的前提（ADR-0005 验收第 1 条）
    provider: str = ""
    provider_version: str = ""
    # 该操作可支持的 Claim 类型闭集（取值于 ClaimClass；空 = 不支撑任何 Claim，
    # 仅发现/候选用途）。PXD 项目元数据只能声明 DATASET_METADATA，不得冒充实验结论。
    supported_claim_types: frozenset[ClaimClass] = frozenset()
    # 最高可满足的证据义务；None = 不具备任何答案证据义务
    max_evidence_obligation: EvidenceLevel | None = None
    # 许可/再分发约束（如 Europe PMC 仅 OA 子集、Gramene 待法务复核）
    license_scope: str = ""
    # 失败语义：上游超时/5xx/空结果分别映射到哪个 AuthorityOutcome，
    # 防止 UNAVAILABLE 被静默降级成 MISS
    failure_semantics: str = ""
    # 出口预算归属（NCBI 按 api_key × egress_ip × tool 核算；共享代理据此限流）
    egress_class: str = ""


# Keys are original protocol tool names, not model-facing aliases. Additions
# require a reviewed server/tool contract; provider descriptions are untrusted.

# Vendored ricekb gateway 脚本（docker/mcp/ricekb/ricekb_mcp.py@gateway-2.2.0）
# 声明的全部工具（与其 TOOL_NAMES 精确对齐）。profile 覆盖不变量
# （test_ricekb_builtin：vendored ↔ registry 1:1）以此为唯一真源。
_RICEKB_VENDORED_TOOLS: tuple[str, ...] = (
    "ricekb_resolve",
    "ricekb_entity",
    "ricekb_compare",
    "ricekb_annotations",
    "ricekb_support",
    "ricekb_evidence",
    "ricekb_references",
    "ricekb_regulators",
    "ricekb_targets",
    "ricekb_candidates",
    "ricekb_search",
    "ricekb_source",
    "ricekb_sequence",
    "ricekb_region",
    "ricekb_genome",
)

# 包内确定性档案装配器（内置 MCP "ricekb-profile"，模块
# yuxi.agents.mcp.ricekb_profile，Yuxi 自有代码）：与 vendored 行级工具同 gateway
# 契约、同严格权威口径，但不是 vendored 脚本声明的工具，故不参与上面的覆盖不变量。
_RICEKB_PROFILE_TOOLS: tuple[str, ...] = ("ricekb_gene_profile",)

# ricekb_search 额外携带 VERBATIM_SEARCH 能力，单列定义（不并入严格行级块）。
_RICEKB_VERBATIM_TOOLS: tuple[str, ...] = ("ricekb_search",)

_TRUSTED_PROFILES: dict[str, ToolCapabilityProfile] = {
    "plant_gene_lookup": ToolCapabilityProfile(
        capabilities=frozenset({Capability.GENE_RECORD_LOOKUP}),
        source_class="AUTHORITATIVE_DATABASE",
        authority_level="PRIMARY_DATABASE",
    ),
    "gene_lookup": ToolCapabilityProfile(
        capabilities=frozenset({Capability.GENE_RECORD_LOOKUP}),
        source_class="STRUCTURED_DATABASE",
    ),
    "get_gene_info": ToolCapabilityProfile(
        capabilities=frozenset({Capability.GENE_RECORD_LOOKUP}),
        source_class="STRUCTURED_DATABASE",
    ),
    "search_literature": ToolCapabilityProfile(
        capabilities=frozenset({Capability.BIBLIOGRAPHIC_SEARCH}),
        source_class="BIBLIOGRAPHY",
        citation_semantics="BIBLIOGRAPHIC_PROVENANCE",
    ),
    # NCBI Datasets（官方基因档案）：符号→Entrez Gene→精确记录链已编入工具本身。
    # 只支撑数据库档案事实（E1），不得用于机制结论。
    **{
        name: ToolCapabilityProfile(
            capabilities=frozenset({Capability.GENE_RECORD_LOOKUP, Capability.OFFICIAL_LINK_LOOKUP}),
            source_class="AUTHORITATIVE_DATABASE",
            authority_level="PRIMARY_DATABASE",
            provider="NCBI Datasets",
            provider_version="v2 REST / datasets CLI 18.37.0 (pinned, sha256-verified)",
            supported_claim_types=frozenset({ClaimClass.RECORD_FACT}),
            max_evidence_obligation=EvidenceLevel.DATA_PROVENANCE,
            license_scope="PUBLIC_WITH_TOOL_IDENTITY",
            failure_semantics="MISS_ON_EMPTY_RECORDS;UNAVAILABLE_ON_5XX_OR_TIMEOUT",
            egress_class="ncbi",
        )
        for name in (
            "ncbi_datasets_gene_report_rest",
            "ncbi_datasets_gene_summary_cli",
            "ncbi_datasets_gene_package_cli",
        )
    },
    **{
        name: ToolCapabilityProfile(
            capabilities=frozenset({Capability.GENE_RECORD_LOOKUP}),
            source_class="AUTHORITATIVE_DATABASE",
            authority_level="PRIMARY_DATABASE",
            provider="UniProtKB",
            provider_version="REST current (field-projected responses)",
            supported_claim_types=frozenset({ClaimClass.RECORD_FACT}),
            max_evidence_obligation=EvidenceLevel.DATA_PROVENANCE,
            license_scope="CC_BY_4_0",
            failure_semantics="MISS_ON_404;UNAVAILABLE_ON_5XX_OR_TIMEOUT",
            egress_class="uniprot",
        )
        for name in ("uniprot_entry_rest", "uniprot_search_rest")
    },
    # 服务端确定性计算工具：无外部出口，非证据来源
    **{
        name: ToolCapabilityProfile(
            capabilities=frozenset({Capability.GENE_RECORD_LOOKUP}),
            source_class="AUTHORITATIVE_DATABASE",
            authority_level="PRIMARY_DATABASE",
            provider="yuxi-internal",
            provider_version="deterministic",
            supported_claim_types=frozenset({ClaimClass.RECORD_FACT}),
            max_evidence_obligation=EvidenceLevel.DATA_PROVENANCE,
            egress_class="none",
        )
        for name in ("verify_genomic_interval", "compute_delta")
    },
    # Europe PMC 题录：只支撑书目事实（E2），不是论文结论的证据
    **{
        name: ToolCapabilityProfile(
            capabilities=frozenset({Capability.BIBLIOGRAPHIC_SEARCH}),
            source_class="BIBLIOGRAPHY",
            citation_semantics="BIBLIOGRAPHIC_PROVENANCE",
            authority_level="PRIMARY_DATABASE",
            provider="Europe PMC",
            provider_version="REST v6+ (core metadata)",
            supported_claim_types=frozenset({ClaimClass.BIBLIOGRAPHIC_FACT}),
            max_evidence_obligation=EvidenceLevel.BIBLIOGRAPHIC,
            license_scope="OA_SUBSET_ONLY",
            failure_semantics="MISS_ON_NO_HIT;UNAVAILABLE_ON_5XX_OR_TIMEOUT",
            egress_class="europepmc",
        )
        for name in ("europe_pmc_search_rest", "europe_pmc_article_rest")
    },
    # 发现型工具：只产出候选，永不直接支撑答案 Claim
    **{
        name: ToolCapabilityProfile(
            capabilities=frozenset({Capability.DATASET_LOOKUP, Capability.GENERIC_MCP}),
            source_class="DISCOVERY",
            answer_eligible=False,
            authority_level="DISCOVERY_ONLY",
            provider="NCBI E-Utilities / EBI PRIDE",
            provider_version="esearch v2 / pride ws archive v2",
            supported_claim_types=frozenset(),
            max_evidence_obligation=None,
            license_scope="PUBLIC_WITH_TOOL_IDENTITY",
            failure_semantics="CANDIDATE_ONLY;UNAVAILABLE_ON_5XX_OR_TIMEOUT",
            egress_class="ncbi+ebi-pride",
        )
        for name in ("ncbi_eutils_search_rest", "pride_search_projects_rest")
    },
    # NCBI E-Utilities 精确记录（esummary/efetch）：数字 ID 精确核验，E1
    **{
        name: ToolCapabilityProfile(
            capabilities=frozenset({Capability.DATASET_LOOKUP}),
            source_class="STRUCTURED_DATABASE",
            authority_level="PRIMARY_DATABASE",
            provider="NCBI E-Utilities",
            provider_version="esummary/efetch v2 (gene db allowlisted)",
            supported_claim_types=frozenset({ClaimClass.RECORD_FACT}),
            max_evidence_obligation=EvidenceLevel.DATA_PROVENANCE,
            license_scope="PUBLIC_WITH_TOOL_IDENTITY",
            failure_semantics="MISS_ON_EMPTY;UNAVAILABLE_ON_5XX_OR_TIMEOUT",
            egress_class="ncbi",
        )
        for name in ("ncbi_eutils_summary_rest", "ncbi_eutils_fetch_rest")
    },
    # PRIDE 项目/文件元数据：只能证明「项目记录存在及其字段」，
    # 不能单凭 PXD 记录证明任何 PTM / 实验结论（磷酸化须 MS run 级核验）
    **{
        name: ToolCapabilityProfile(
            capabilities=frozenset({Capability.DATASET_LOOKUP}),
            source_class="STRUCTURED_DATABASE",
            authority_level="PRIMARY_DATABASE",
            provider="EBI PRIDE Archive",
            provider_version="ws archive v2 (HAL links, domain-allowlisted)",
            supported_claim_types=frozenset({ClaimClass.DATASET_METADATA}),
            max_evidence_obligation=EvidenceLevel.DATA_PROVENANCE,
            license_scope="PUBLIC_METADATA",
            failure_semantics="MISS_ON_404;UNAVAILABLE_ON_5XX_OR_TIMEOUT",
            egress_class="ebi-pride",
        )
        for name in ("pride_project_rest", "pride_project_files_rest")
    },
    # OA XML 段落候选：只产出 XML 定位候选（QUOTE_CANDIDATE_ONLY），
    # 须本地解析/对齐后方可升级为证据单元；不编页码
    "europe_pmc_oa_passages_rest": ToolCapabilityProfile(
        capabilities=frozenset({Capability.DOCUMENT_QA, Capability.GENERIC_MCP}),
        source_class="DISCOVERY",
        answer_eligible=False,
        authority_level="QUOTE_CANDIDATE_ONLY",
        provider="Europe PMC",
        provider_version="fullTextXML OA subset",
        supported_claim_types=frozenset(),
        max_evidence_obligation=None,
        license_scope="OA_SUBSET_ONLY",
        failure_semantics="NON_OA_IS_MISS_NOT_UNAVAILABLE;UNAVAILABLE_ON_5XX_OR_TIMEOUT",
        egress_class="europepmc",
    ),
    # Reviewed plant-genomics-mcp v1.21.0 surface (commit ddd223f). Only exact
    # retrieval tools that return structured source records earn a capability.
    # Synthesis/aggregate tools (analyze_locus_synth, find_homologs_synth,
    # biological_context_synth, consensus_homologs, gene_report) and semantically
    # mismatched analysis tools (go_enrichment, blast_sequence) are intentionally
    # ABSENT: without a profile they are filtered out of source-constrained turns
    # and can never satisfy a source obligation. Per-tool review decisions:
    # docker/mcp/plant-genomics/TOOL_CONTRACT_REVIEW.md.
    **{
        name: ToolCapabilityProfile(
            capabilities=frozenset({Capability.GENE_RECORD_LOOKUP}),
            source_class="STRUCTURED_DATABASE",
            authority_level="CROSS_SOURCE_AGGREGATOR",
        )
        for name in (
            "ensembl_plants_lookup_locus",
            "get_gene_xrefs",
            "get_sequence",
            "ensembl_region_query",
            "phytozome_lookup_locus",
            "resolve_locus_to_uniprot",
            "locus_go_annotations",
            "locus_plant_ontology",
            "gramene_homologs",
            "kegg_pathways",
            "bar_gene_summary",
            "bar_efp_expression",
            "bar_aiv_interactions",
            "string_interactions",
            "tair_locus_info",
            "plantcyc_locus_info",
            "alphafold_structure",
            "experimental_structures",
            "tf_binding_motifs",
            "jaspar_motif",
            "experimental_interactions",
            "locus_gene_rifs",
            "interpro_domains",
            "locus_variants",
            "vep_annotate",
            "panther_family",
            "orthodb_orthologs",
            "aragwas_associations",
            "arabidopsis_natural_variation",
            "batch_ensembl_plants_lookup_locus",
            "batch_get_gene_xrefs",
            "batch_phytozome_lookup_locus",
            "batch_resolve_locus_to_uniprot",
            "batch_locus_go_annotations",
            "batch_gramene_homologs",
            "batch_kegg_pathways",
            "batch_bar_gene_summary",
            "batch_bar_aiv_interactions",
            "batch_string_interactions",
            "atted_coexpression",
            "batch_atted_coexpression",
        )
    },
    **{
        name: ToolCapabilityProfile(
            capabilities=frozenset({Capability.BIBLIOGRAPHIC_SEARCH}),
            source_class="BIBLIOGRAPHY",
            citation_semantics="BIBLIOGRAPHIC_PROVENANCE",
            authority_level="CROSS_SOURCE_AGGREGATOR",
        )
        for name in ("locus_literature", "batch_locus_literature")
    },
    # warelab/gramene-mcp pinned commit b42afce. Prompt names are intentionally
    # absent: only protocol tools can satisfy source obligations.
    **{
        name: ToolCapabilityProfile(
            capabilities=frozenset({Capability.GENE_RECORD_LOOKUP}),
            source_class="STRUCTURED_DATABASE",
            authority_level="DOMAIN_DATABASE",
        )
        for name in (
            "solr_search",
            "solr_search_bool",
            "mongo_find",
            "mongo_lookup_by_ids",
            "solr_graph",
            "kb_relations",
            "genes_in_region",
            "expression_for_genes",
            "vep_for_gene",
        )
    },
    "pubmed_for_genes": ToolCapabilityProfile(
        capabilities=frozenset({Capability.BIBLIOGRAPHIC_SEARCH}),
        source_class="BIBLIOGRAPHY",
        citation_semantics="BIBLIOGRAPHIC_PROVENANCE",
        authority_level="DOMAIN_DATABASE",
    ),
    # Rice Source KB (builtin MCP "ricekb"): every tool returns rows from the 34
    # lossless MSU / Oryzabase / RAP-DB source tables with row-level provenance
    # (source, schema, table, row_ref, content_sha256, import_run_id). The three
    # sequence tools return the same 34-table snapshot's FASTA/GFF records
    # (bases + sequence_sha256, 1-based inclusive intervals); they are source
    # records too, not a separate evidence plane. Citations are data provenance,
    # never bibliographic; the gateway's machine states
    # (FOUND / NO_EVIDENCE / ...) are authoritative and must not be reinterpreted.
    **{
        name: ToolCapabilityProfile(
            capabilities=frozenset({Capability.GENE_RECORD_LOOKUP}),
            source_class="AUTHORITATIVE_DATABASE",
            authority_level="PRIMARY_DATABASE",
        )
        for name in (*_RICEKB_VENDORED_TOOLS, *_RICEKB_PROFILE_TOOLS)
        if name not in _RICEKB_VERBATIM_TOOLS
    },
    "ricekb_search": ToolCapabilityProfile(
        capabilities=frozenset({Capability.GENE_RECORD_LOOKUP, Capability.VERBATIM_SEARCH}),
        source_class="AUTHORITATIVE_DATABASE",
        authority_level="PRIMARY_DATABASE",
    ),
    "ricekb_sequence": ToolCapabilityProfile(
        capabilities=frozenset({Capability.GENE_RECORD_LOOKUP, Capability.SEQUENCE_LOOKUP}),
        source_class="AUTHORITATIVE_DATABASE",
        authority_level="PRIMARY_DATABASE",
    ),
}

_DATA_AGGREGATOR_DISCOVERY = ToolCapabilityProfile(
    capabilities=frozenset({Capability.DATASET_LOOKUP, Capability.GENERIC_MCP}),
    source_class="DISCOVERY",
    answer_eligible=False,
    authority_level="DISCOVERY_ONLY",
    provider="data-aggregator-mcp (musharna)",
    provider_version="pinned upstream",
    supported_claim_types=frozenset(),
    max_evidence_obligation=None,
    license_scope="DISCOVERY_ONLY_NO_ANSWER",
    failure_semantics="CANDIDATE_ONLY;SERVER_FS_DOWNLOADS_NOT_USER_PATHS",
    egress_class="aggregator",
)
_DATA_AGGREGATOR_TOOLS = frozenset({"search", "resolve", "relate", "list_sources"})

RICEKB_TOOL_NAMES: frozenset[str] = frozenset(_RICEKB_VENDORED_TOOLS)

# 服务器级限定 profile：通用上游工具名（search/resolve/...）只在其评审过的
# 服务器内受信；命中该表的服务器实行 fail-closed（未列名工具一律无 profile），
# 不再回落到全局裸名表，防止跨服务器裸名撞车。
_BIO_MCP_STRUCTURED_LOOKUP = ToolCapabilityProfile(
    capabilities=frozenset({Capability.GENE_RECORD_LOOKUP, Capability.GENERIC_MCP}),
    source_class="STRUCTURED_DATABASE",
    authority_level="CROSS_SOURCE_AGGREGATOR",
)
#: bio-mcp 评审过的精确检索工具（实测返回结构化记录）；intelligent_analyze 等
#: 综合类与 blast_search 等分析类工具刻意不列（无 profile → 受控轮被滤除）。
_BIO_MCP_TOOLS = frozenset(
    {
        "plant_gene_lookup",
        "plant_species_list",
        "ensembl_gene_lookup",
        "uniprot_annotate",
        "protein_domains",
        "pdb_structure_summary",
        "alphafold_structure",
        "pubmed_search",
        "pride_project",
        "pride_search",
        "geo_dataset_search",
        "sra_search",
        "bioproject_search",
        "taxonomy_lookup",
        "hgnc_gene_symbol",
        "hgnc_search",
    }
)
_SERVER_SCOPED_PROFILES: dict[str, dict[str, ToolCapabilityProfile]] = {
    "data-aggregator": {name: _DATA_AGGREGATOR_DISCOVERY for name in sorted(_DATA_AGGREGATOR_TOOLS)},
    "bio-mcp": {name: _BIO_MCP_STRUCTURED_LOOKUP for name in sorted(_BIO_MCP_TOOLS)},
}


def profile_for_tool(tool: Any) -> ToolCapabilityProfile | None:
    metadata = getattr(tool, "metadata", None) or {}
    original_name = str(metadata.get("mcp_tool_name") or "")
    if not original_name:
        return None
    return profile_for_server_tool(str(metadata.get("server") or ""), original_name)


def profile_for_server_tool(server_slug: str, name: str) -> ToolCapabilityProfile | None:
    """Generic upstream tool names are trusted only within their reviewed server.

    命中服务器级限定表时 fail-closed：该服务器上未列名的工具一律无 profile，
    不回落全局裸名表；未限定的服务器按全局受信表解析。
    """
    scoped = _SERVER_SCOPED_PROFILES.get(str(server_slug or ""))
    if scoped is not None:
        return scoped.get(str(name or ""))
    return _TRUSTED_PROFILES.get(str(name or ""))


def profile_for_protocol_name(name: str) -> ToolCapabilityProfile | None:
    return _TRUSTED_PROFILES.get(str(name or ""))


def is_mcp_tool(tool: Any) -> bool:
    metadata = getattr(tool, "metadata", None) or {}
    return bool(metadata.get("mcp_tool_name") and metadata.get("server"))


__all__ = [
    "RICEKB_TOOL_NAMES",
    "ToolCapabilityProfile",
    "is_mcp_tool",
    "profile_for_protocol_name",
    "profile_for_server_tool",
    "profile_for_tool",
]
