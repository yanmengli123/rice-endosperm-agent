"""Output guards for non-document source planes."""

from __future__ import annotations

import re

from yuxi.knowledge.rendering.authority_markers import authority_marker_pattern

_EVIDENCE_CHIP = authority_marker_pattern()
_EVIDENCE_REF = re.compile(r"\[E\d{1,3}\]")
_ANCHOR_ID = re.compile(r"\b(?:ea|ev|evs)_[0-9a-f]{12,64}\b", re.I)
_PAGE = re.compile(r"第\s*\d{1,4}\s*页|\bp\.\s*\d{1,4}\b|\bpages?\s+\d{1,4}\b", re.I)
_REFERENCE_BLOCK = re.compile(r"\n*【证据引用】[^\n]*\n(?:-\s*E\d+[^\n]*(?:\n|$))*", re.IGNORECASE)


def guard_non_document_source_answer(text: str) -> tuple[str, dict[str, int | str]]:
    """Remove document-evidence affordances from MCP/data-only answers."""
    source = str(text or "")
    counts = {
        "evidence_chips_removed": len(_EVIDENCE_CHIP.findall(source)),
        "evidence_refs_removed": len(_EVIDENCE_REF.findall(source)),
        "anchor_ids_removed": len(_ANCHOR_ID.findall(source)),
        "pdf_pages_removed": len(_PAGE.findall(source)),
        "reference_blocks_removed": len(_REFERENCE_BLOCK.findall(source)),
    }
    guarded = _REFERENCE_BLOCK.sub("", source)
    guarded = _EVIDENCE_CHIP.sub("", guarded)
    guarded = _EVIDENCE_REF.sub("", guarded)
    guarded = _ANCHOR_ID.sub("", guarded)
    guarded = _PAGE.sub("（当前数据来源不提供 PDF 物理页码）", guarded)
    guarded = re.sub(r"[ \t]+\n", "\n", guarded)
    guarded = re.sub(r"\n{3,}", "\n\n", guarded).strip()
    return guarded, {
        "schema_version": "non-document-source-guard.v1",
        **counts,
    }


__all__ = ["guard_non_document_source_answer"]
