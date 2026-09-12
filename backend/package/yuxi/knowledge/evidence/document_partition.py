"""Deterministic document-partition classification for scientific PDF evidence.

Partition is ingestion metadata, not an answer-time page heuristic.  Downstream
locators may use the page threshold only for legacy rows whose persisted value is
``UNKNOWN``.
"""

from __future__ import annotations

import re
from typing import Any

PARTITION_UNKNOWN = "UNKNOWN"
PARTITION_MAIN_TEXT = "MAIN_TEXT"
PARTITION_SUPPORTING_INFO = "SUPPORTING_INFO"
PARTITION_REFERENCES = "REFERENCES"
PARTITION_APPENDIX = "APPENDIX"
PARTITION_TOC = "TOC"
PARTITION_FIGURE_LIST = "FIGURE_LIST"
PARTITION_TABLE_LIST = "TABLE_LIST"
PARTITION_COVER = "COVER"

LOCATOR_ELIGIBLE_PARTITIONS = {
    PARTITION_MAIN_TEXT,
    PARTITION_SUPPORTING_INFO,
    PARTITION_APPENDIX,
}

_SI_HEADING = re.compile(
    r"^\s*(?:support(?:ing)?|supplementary)\s+(?:information|materials?|data)\b",
    flags=re.IGNORECASE,
)
_REFERENCES_HEADING = re.compile(r"^\s*(?:references|bibliography)\s*$", flags=re.IGNORECASE)
_APPENDIX_HEADING = re.compile(r"^\s*(?:appendix|appendices)(?:\s+[A-Z0-9]+)?\s*$", flags=re.IGNORECASE)
_TOC_HEADING = re.compile(r"^\s*(?:table\s+of\s+contents|contents)\s*$", flags=re.IGNORECASE)
_FIGURE_LIST_HEADING = re.compile(r"^\s*list\s+of\s+figures\s*$", flags=re.IGNORECASE)
_TABLE_LIST_HEADING = re.compile(r"^\s*list\s+of\s+tables\s*$", flags=re.IGNORECASE)


def heading_partition(text: Any) -> str | None:
    """Return a partition transition for a short section heading, if any."""
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not value or len(value) > 180:
        return None
    for pattern, partition in (
        (_SI_HEADING, PARTITION_SUPPORTING_INFO),
        (_REFERENCES_HEADING, PARTITION_REFERENCES),
        (_APPENDIX_HEADING, PARTITION_APPENDIX),
        (_TOC_HEADING, PARTITION_TOC),
        (_FIGURE_LIST_HEADING, PARTITION_FIGURE_LIST),
        (_TABLE_LIST_HEADING, PARTITION_TABLE_LIST),
    ):
        if pattern.match(value):
            return partition
    return None


def classify_anchor_partitions(anchors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach persisted partition metadata to anchors in physical reading order.

    A new SI heading is allowed to supersede an earlier References section because
    publishers commonly append Supporting Information after the main article's
    references in the same PDF.
    """
    ordered = sorted(
        enumerate(anchors or []),
        key=lambda pair: (
            int(pair[1].get("page") or 0),
            int(pair[1].get("word_start") or 0),
            pair[0],
        ),
    )
    current = PARTITION_MAIN_TEXT
    classified: dict[int, dict[str, Any]] = {}
    for original_index, anchor in ordered:
        transition = heading_partition(anchor.get("quote"))
        if transition:
            current = transition
        classified[original_index] = {
            **anchor,
            "document_partition": current,
            "partition_confidence": 1.0 if transition else 0.9,
        }
    return [classified[index] for index in range(len(anchors or []))]


def effective_partition(value: Any, *, page: int | None = None, si_start_page: int | None = None) -> str:
    """Read persisted partition, with a clearly bounded legacy fallback."""
    partition = str(value or PARTITION_UNKNOWN).strip().upper()
    if partition != PARTITION_UNKNOWN:
        return partition
    if si_start_page is not None and page is not None and int(page) >= int(si_start_page):
        return PARTITION_SUPPORTING_INFO
    return PARTITION_MAIN_TEXT
