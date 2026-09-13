"""Strongly typed internal citation binding shared by producer and resolver."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CitationBindingCandidate(BaseModel):
    """One citation proposal and its complete physical lineage.

    Public answer context is produced separately by ``public_view``. The
    internal fields are required for deterministic locator decisions and must
    never be reconstructed from model text.
    """

    model_config = ConfigDict(extra="forbid")

    ref: str
    retrieval_evidence_id: str | None = None
    physical_evidence_id: str | None = None
    span_evidence_id: str | None = None
    kb_id: str | None = None
    file_id: str | None = None
    filename: str = ""
    zone: str = "MAIN_TEXT"
    page_numbers: list[int] = Field(default_factory=list)
    quote_head: str = ""
    anchor_id: str | None = None
    span_id: str | None = None
    parse_revision_id: str | None = None
    index_revision_id: str | None = None
    source_sha256: str | None = None
    retrieval_channel: str | None = None
    quote: str = ""
    quote_norm: str = ""
    locatable: bool = False
    toc_line: bool = False
    secondary_of: str | None = None
    lineage_dropped: int = 0

    @property
    def locator_eligible(self) -> bool:
        return bool(
            self.locatable
            and not self.toc_line
            and self.quote_norm
            and self.kb_id
            and self.file_id
            and self.parse_revision_id
            and self.index_revision_id
            and self.anchor_id
            and self.span_id
            and self.span_evidence_id
            and self.source_sha256
            and self.physical_evidence_id
            and self.page_numbers
        )

    def public_view(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "evidence_id": self.retrieval_evidence_id,
            "kb_id": self.kb_id,
            "file_id": self.file_id,
            "filename": self.filename,
            "zone": self.zone,
            "quote_head": self.quote_head,
            "locatable": self.locatable,
            "toc_line": self.toc_line,
            "secondary_of": self.secondary_of,
        }

    def to_legacy_dict(self) -> dict[str, Any]:
        """Compatibility adapter for the existing renderer during migration."""
        return {
            **self.public_view(),
            "page_numbers": self.page_numbers,
            "primary_page": self.page_numbers[0] if self.page_numbers else None,
            "anchor_ids": [self.anchor_id] if self.anchor_id else [],
            "_anchor_id": self.anchor_id,
            "_span_id": self.span_id,
            "_span_evidence_id": self.span_evidence_id,
            "_physical_evidence_id": self.physical_evidence_id,
            "_parse_revision_id": self.parse_revision_id,
            "_index_revision_id": self.index_revision_id,
            "_source_sha256": self.source_sha256,
            "_retrieval_channel": self.retrieval_channel,
            "_quote": self.quote,
            "_quote_norm": self.quote_norm,
            "_lineage_dropped": self.lineage_dropped,
        }

    @classmethod
    def from_legacy_dict(cls, row: dict[str, Any]) -> CitationBindingCandidate:
        pages = [int(value) for value in row.get("page_numbers") or [] if int(value) >= 1]
        anchors = [str(value) for value in row.get("anchor_ids") or [] if value]
        return cls(
            ref=str(row.get("ref") or ""),
            retrieval_evidence_id=row.get("evidence_id"),
            physical_evidence_id=row.get("_physical_evidence_id") or row.get("evidence_id"),
            span_evidence_id=row.get("_span_evidence_id"),
            kb_id=row.get("kb_id"),
            file_id=row.get("file_id"),
            filename=str(row.get("filename") or ""),
            zone=str(row.get("zone") or "MAIN_TEXT"),
            page_numbers=pages,
            quote_head=str(row.get("quote_head") or ""),
            anchor_id=row.get("_anchor_id") or (anchors[0] if anchors else None),
            span_id=row.get("_span_id"),
            parse_revision_id=row.get("_parse_revision_id"),
            index_revision_id=row.get("_index_revision_id"),
            source_sha256=row.get("_source_sha256"),
            retrieval_channel=row.get("_retrieval_channel"),
            quote=str(row.get("_quote") or row.get("quote_head") or ""),
            quote_norm=str(row.get("_quote_norm") or ""),
            locatable=bool(row.get("locatable")),
            toc_line=bool(row.get("toc_line")),
            secondary_of=row.get("secondary_of"),
            lineage_dropped=int(row.get("_lineage_dropped") or 0),
        )


__all__ = ["CitationBindingCandidate"]
