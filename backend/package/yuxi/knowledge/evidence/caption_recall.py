"""Deterministic recall of direct Figure/Table captions in an explicit document scope.

Vector retrieval is optimized for explanatory prose and can miss the most direct
caption (for example Figure 2 for a single-mutant phenotype) while returning
later comparison figures.  This auxiliary channel is intentionally narrow:

* it runs only when the user explicitly scoped one or more files and asked for
  figure/table evidence;
* it reads caption spans from each file's active parse revision and tenant;
* bilingual scientific concept overlap ranks candidates without an LLM;
* only the best, high-confidence captions are prepended to the frozen evidence
  set.  They still pass the normal anchor/citation/semantic release gates.

It does not guess a figure number and never searches outside the requested
documents.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select

from yuxi.storage.postgres.models_knowledge import EvidenceSpanRecord, KnowledgeFile

CAPTION_RECALL_VERSION = "caption_recall_v1"
MAX_RECALLED_CAPTIONS = 3

_FIGURE_EVIDENCE_INTENT = re.compile(
    r"图注|表注|哪个图|哪张图|哪些图|哪(?:个|张)表|注明.{0,12}(?:图|表)|"
    r"(?:figure|fig\.?|table)\s+(?:caption|evidence)|caption",
    re.IGNORECASE,
)


def _patterns(*values: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(value, re.IGNORECASE) for value in values)


_CONCEPTS: dict[str, tuple[int, tuple[re.Pattern[str], ...]]] = {
    "crispr": (3, _patterns(r"\bCRISPR(?:/Cas9)?\b", r"基因编辑")),
    "mutant": (2, _patterns(r"突变(?:体|系)", r"敲除", r"\bmutants?\b", r"\bknockout\b")),
    "phenotype": (2, _patterns(r"表型", r"\bphenotyp(?:e|ic)\b")),
    "grain_length": (4, _patterns(r"粒长", r"\bgrain\s+length\b", r"\blonger\s+grains?\b")),
    "chalkiness": (4, _patterns(r"垩白", r"腹白", r"\bchalk(?:y|iness)\b", r"white[- ]belly")),
    "grain_size": (2, _patterns(r"粒型", r"籽粒大小", r"\bgrain\s+(?:size|shape)\b")),
    "microscopy": (3, _patterns(r"电镜", r"\bSEM\b", r"\bTEM\b", r"microscop")),
    "starch": (3, _patterns(r"淀粉", r"\bstarch\b")),
    "physicochemical": (3, _patterns(r"理化", r"直链淀粉", r"糊化", r"physicochemical", r"amylose")),
    "rna_seq": (3, _patterns(r"转录组", r"RNA[- ]?seq", r"transcriptom")),
    "venn": (3, _patterns(r"韦恩", r"\bVenn\b")),
    "kegg": (3, _patterns(r"\bKEGG\b")),
    "go": (3, _patterns(r"(?:^|[^A-Za-z])GO(?:[^A-Za-z]|$)", r"Gene Ontology")),
    "heat_stress": (4, _patterns(r"热胁迫", r"高温", r"heat\s+stress")),
}

_IDENTIFIER = re.compile(r"\b(?:Os)?[A-Za-z]{2,}[-]?[A-Za-z0-9]{1,12}\b")
_IDENTIFIER_STOP = {"figure", "table", "crispr", "cas9", "mutant", "rice", "grain", "caption"}


def _has(patterns: tuple[re.Pattern[str], ...], text: str) -> bool:
    return any(pattern.search(text) for pattern in patterns)


def caption_recall_score(question: str, caption: str) -> int:
    """Return a conservative bilingual relevance score for one caption."""
    query = str(question or "")
    candidate = str(caption or "")
    score = sum(
        weight for weight, patterns in _CONCEPTS.values() if _has(patterns, query) and _has(patterns, candidate)
    )
    query_ids = {value.casefold() for value in _IDENTIFIER.findall(query) if value.casefold() not in _IDENTIFIER_STOP}
    caption_ids = {
        value.casefold() for value in _IDENTIFIER.findall(candidate) if value.casefold() not in _IDENTIFIER_STOP
    }
    score += min(6, 2 * len(query_ids & caption_ids))

    query_is_mutant = _has(_CONCEPTS["mutant"][1], query)
    if query_is_mutant and re.search(r"过表达|overexpress", candidate, re.IGNORECASE):
        score -= 6
    if query_is_mutant and not re.search(r"双突变|double\s+mutant|组合突变", query, re.IGNORECASE):
        if re.search(r"double\s+mutant|\bOs[A-Za-z0-9-]+\s*\+\s*Os", candidate, re.IGNORECASE):
            score -= 2
    if re.search(r"补充|supporting|supplement", query, re.IGNORECASE) and re.match(
        r"\s*(?:Figure|Fig\.?)\s*S\d+", candidate, re.IGNORECASE
    ):
        score += 2
    return score


def rank_caption_candidates(
    question: str,
    candidates: list[EvidenceSpanRecord],
    *,
    limit: int = MAX_RECALLED_CAPTIONS,
) -> list[EvidenceSpanRecord]:
    ranked = sorted(
        ((caption_recall_score(question, str(span.quote or "")), span) for span in candidates),
        key=lambda item: (-item[0], int(item[1].sentence_index or 0), str(item[1].span_id or "")),
    )
    if not ranked or ranked[0][0] < 3:
        return []
    floor = max(3, ranked[0][0] - 3)
    return [span for score, span in ranked if score >= floor][: max(1, min(int(limit), MAX_RECALLED_CAPTIONS))]


def _evidence_row(span: EvidenceSpanRecord) -> dict[str, Any]:
    anchor_id = str(span.anchor_id or "")
    return {
        "evidence_id": str(span.evidence_id),
        "source_type": "DOCUMENT",
        "evidence_status": "SUPPORTING",
        "retrieval_channel": "CAPTION_RECALL",
        "kb_id": str(span.kb_id),
        "file_id": str(span.file_id),
        "chunk_id": str(span.span_id),
        "span_id": str(span.span_id),
        "anchor_id": anchor_id or None,
        "anchor_ids": [anchor_id] if anchor_id else [],
        "page_number": span.page_number,
        "parse_revision_id": str(span.parse_revision_id),
        "document_partition": str(span.document_partition or "UNKNOWN"),
        "partition_confidence": float(span.partition_confidence or 0.0),
        "evidence_type": "caption",
        "span_evidence_type": "caption",
        "container_label": span.container_label,
        "evidence_quote": str(span.quote or ""),
        "claim_eligible": False,
        "match_tier": "DIRECT_CAPTION_RECALL",
        "matched_value": span.container_label,
    }


async def recall_scoped_caption_evidence(
    db,
    *,
    tenant_id: int,
    kb_ids: list[str],
    file_ids: list[str],
    question: str,
    existing_evidence_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Recall direct captions from active revisions inside an explicit scope."""
    if not file_ids or not kb_ids or not _FIGURE_EVIDENCE_INTENT.search(str(question or "")):
        return []
    rows = list(
        (
            await db.execute(
                select(EvidenceSpanRecord)
                .join(KnowledgeFile, KnowledgeFile.file_id == EvidenceSpanRecord.file_id)
                .where(
                    EvidenceSpanRecord.tenant_id == int(tenant_id),
                    EvidenceSpanRecord.kb_id.in_(kb_ids[:20]),
                    EvidenceSpanRecord.file_id.in_(file_ids[:20]),
                    EvidenceSpanRecord.evidence_type == "caption",
                    KnowledgeFile.active_parse_revision_id == EvidenceSpanRecord.parse_revision_id,
                )
                .order_by(EvidenceSpanRecord.file_id, EvidenceSpanRecord.sentence_index)
                .limit(512)
            )
        )
        .scalars()
        .all()
    )
    existing = {str(value) for value in (existing_evidence_ids or set()) if value}
    candidates = [span for span in rows if str(span.evidence_id) not in existing]
    return [_evidence_row(span) for span in rank_caption_candidates(question, candidates)]


__all__ = [
    "CAPTION_RECALL_VERSION",
    "caption_recall_score",
    "rank_caption_candidates",
    "recall_scoped_caption_evidence",
]
