from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

EvidenceCapability = Literal[
    "INDEXED_FULL",
    "INDEXED_CONTENT_ONLY",
    "INDEXED_TEXT_ONLY",
    "REJECTED",
]


@dataclass(frozen=True)
class EvidenceAnchor:
    anchor_id: str
    page: int
    bbox: tuple[float, float, float, float]
    word_start: int
    word_end: int
    quote: str
    quote_hash: str
    prefix_hash: str
    suffix_hash: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class NativePdfSnapshot:
    pdf_sha256: str
    page_count: int
    pages: list[dict[str, Any]]
    anchors: list[EvidenceAnchor]
    quality: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["anchors"] = [anchor.to_dict() for anchor in self.anchors]
        return data


@dataclass(frozen=True)
class ParserArtifact:
    kind: str
    filename: str
    content: bytes
    content_type: str


@dataclass
class UnifiedArticle:
    schema_version: str
    source_sha256: str
    title: str
    markdown: str
    metadata: dict[str, Any]
    sections: list[dict[str, Any]]
    references: list[dict[str, Any]]
    citation_mentions: list[dict[str, Any]]
    anchors: list[dict[str, Any]]
    parser_provenance: dict[str, Any]
    assets: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PipelineResult:
    capability: EvidenceCapability
    unified_article: UnifiedArticle | None
    qa_report: dict[str, Any]
    artifacts: list[ParserArtifact]
    annotated_markdown: str
    parser_fingerprint: str
