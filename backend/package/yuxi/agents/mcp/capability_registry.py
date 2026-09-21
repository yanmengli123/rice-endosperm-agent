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
    produces_document_evidence: bool = False
    citation_semantics: str = "DATA_PROVENANCE"
    authority_level: str = "PROVIDER_DECLARED"
    fallback_policy: str = "FAIL_CLOSED"


# Keys are original protocol tool names, not model-facing aliases. Additions
# require a reviewed server/tool contract; provider descriptions are untrusted.
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
    # Reviewed plant-genomics-mcp v1.21.0 surface. These tools retrieve
    # structured source records; synthesis tools remain data provenance and do
    # not acquire document-claim authority merely because the provider names
    # multiple databases.
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
            "go_enrichment",
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
            "analyze_locus_synth",
            "find_homologs_synth",
            "biological_context_synth",
            "consensus_homologs",
            "gene_report",
            "blast_sequence",
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
            "solr_suggest",
            "solr_search_bool",
            "mongo_find",
            "mongo_list_collections",
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
        for name in (
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
            "ricekb_source",
            "ricekb_sequence",
            "ricekb_region",
            "ricekb_genome",
        )
    },
    "ricekb_search": ToolCapabilityProfile(
        capabilities=frozenset({Capability.GENE_RECORD_LOOKUP, Capability.VERBATIM_SEARCH}),
        source_class="AUTHORITATIVE_DATABASE",
        authority_level="PRIMARY_DATABASE",
    ),
}

RICEKB_TOOL_NAMES: frozenset[str] = frozenset(name for name in _TRUSTED_PROFILES if name.startswith("ricekb_"))


def profile_for_tool(tool: Any) -> ToolCapabilityProfile | None:
    metadata = getattr(tool, "metadata", None) or {}
    original_name = str(metadata.get("mcp_tool_name") or "")
    if not original_name:
        return None
    return _TRUSTED_PROFILES.get(original_name)


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
    "profile_for_tool",
]
