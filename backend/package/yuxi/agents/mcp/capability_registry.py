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
