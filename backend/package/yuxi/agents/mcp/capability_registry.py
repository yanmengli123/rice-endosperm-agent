"""Trusted server-side capability profiles for MCP tools.

MCP descriptions and annotations are provider-controlled and therefore never
grant citation/page authority.  Only this registry may classify a tool for a
source-constrained turn.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from yuxi.knowledge.planning.turn_execution_plan import Capability


@dataclass(frozen=True)
class ToolCapabilityProfile:
    capabilities: frozenset[Capability]
    source_class: str
    answer_eligible: bool = True
    produces_document_evidence: bool = False
    citation_semantics: str = "DATA_PROVENANCE"
    authority_level: str = "PROVIDER_DECLARED"
    fallback_policy: str = "FAIL_CLOSED"


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
    **{
        name: ToolCapabilityProfile(
            capabilities=frozenset({Capability.GENE_RECORD_LOOKUP}),
            source_class="AUTHORITATIVE_DATABASE",
            authority_level="PRIMARY_DATABASE",
        )
        for name in (
            "ncbi_datasets_gene_report_rest",
            "ncbi_datasets_gene_summary_cli",
            "ncbi_datasets_gene_package_cli",
            "uniprot_entry_rest",
            "uniprot_search_rest",
            "verify_genomic_interval",
            "compute_delta",
        )
    },
    **{
        name: ToolCapabilityProfile(
            capabilities=frozenset({Capability.BIBLIOGRAPHIC_SEARCH}),
            source_class="BIBLIOGRAPHY",
            citation_semantics="BIBLIOGRAPHIC_PROVENANCE",
            authority_level="PRIMARY_DATABASE",
        )
        for name in ("europe_pmc_search_rest", "europe_pmc_article_rest")
    },
    **{
        name: ToolCapabilityProfile(
            capabilities=frozenset({Capability.DATASET_LOOKUP, Capability.GENERIC_MCP}),
            source_class="DISCOVERY",
            answer_eligible=False,
            authority_level="DISCOVERY_ONLY",
        )
        for name in ("ncbi_eutils_search_rest", "pride_search_projects_rest")
    },
    **{
        name: ToolCapabilityProfile(
            capabilities=frozenset({Capability.DATASET_LOOKUP}),
            source_class="STRUCTURED_DATABASE",
            authority_level="PRIMARY_DATABASE",
        )
        for name in (
            "ncbi_eutils_summary_rest",
            "ncbi_eutils_fetch_rest",
            "pride_project_rest",
            "pride_project_files_rest",
        )
    },
    "europe_pmc_oa_passages_rest": ToolCapabilityProfile(
        capabilities=frozenset({Capability.DOCUMENT_QA, Capability.GENERIC_MCP}),
        source_class="DISCOVERY",
        answer_eligible=False,
        authority_level="QUOTE_CANDIDATE_ONLY",
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
}

_DATA_AGGREGATOR_DISCOVERY = ToolCapabilityProfile(
    capabilities=frozenset({Capability.DATASET_LOOKUP, Capability.GENERIC_MCP}),
    source_class="DISCOVERY",
    answer_eligible=False,
    authority_level="DISCOVERY_ONLY",
)
_DATA_AGGREGATOR_TOOLS = frozenset({"search", "resolve", "relate", "list_sources"})

RICEKB_TOOL_NAMES: frozenset[str] = frozenset(_RICEKB_VENDORED_TOOLS)


def profile_for_tool(tool: Any) -> ToolCapabilityProfile | None:
    metadata = getattr(tool, "metadata", None) or {}
    original_name = str(metadata.get("mcp_tool_name") or "")
    if not original_name:
        return None
    return profile_for_server_tool(str(metadata.get("server") or ""), original_name)


def profile_for_server_tool(server_slug: str, name: str) -> ToolCapabilityProfile | None:
    """Generic upstream tool names are trusted only within their reviewed server."""
    if server_slug == "data-aggregator" and name in _DATA_AGGREGATOR_TOOLS:
        return _DATA_AGGREGATOR_DISCOVERY
    return _TRUSTED_PROFILES.get(name)


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
