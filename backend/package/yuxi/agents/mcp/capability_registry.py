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
}


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


__all__ = ["ToolCapabilityProfile", "is_mcp_tool", "profile_for_protocol_name", "profile_for_tool"]
