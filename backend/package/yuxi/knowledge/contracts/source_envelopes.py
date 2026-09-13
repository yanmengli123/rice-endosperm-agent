"""Typed, non-interchangeable result planes for external sources."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class McpDataEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["mcp-data-envelope.v1"] = "mcp-data-envelope.v1"
    plane: Literal["MCP_DATA"] = "MCP_DATA"
    stable_tool_id: str
    server: str
    capability: str
    source_class: str
    produces_document_evidence: Literal[False] = False
    citation_semantics: str = "DATA_PROVENANCE"
    payload: dict[str, Any] = Field(default_factory=dict)


class BibliographicEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["bibliographic-envelope.v1"] = "bibliographic-envelope.v1"
    plane: Literal["BIBLIOGRAPHY"] = "BIBLIOGRAPHY"
    stable_tool_id: str
    server: str
    capability: Literal["BIBLIOGRAPHIC_SEARCH"] = "BIBLIOGRAPHIC_SEARCH"
    citation_semantics: Literal["BIBLIOGRAPHIC_PROVENANCE"] = "BIBLIOGRAPHIC_PROVENANCE"
    records: list[dict[str, Any]] = Field(default_factory=list)
    produces_page_locator: Literal[False] = False


class ArtifactEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["artifact-envelope.v1"] = "artifact-envelope.v1"
    plane: Literal["ARTIFACT"] = "ARTIFACT"
    artifacts: list[dict[str, Any]] = Field(default_factory=list)


__all__ = ["ArtifactEnvelope", "BibliographicEnvelope", "McpDataEnvelope"]
