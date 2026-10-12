"""Modality-specific quality, with explicit denominators and comparable versions."""

from __future__ import annotations

QUALITY_VECTOR_VERSION = "pdf-quality-vector.v1"


def quality_vector(physical: dict, anchors: list[dict], audit: dict) -> dict:
    by_id = {a["anchor_id"]: a for a in anchors}
    units = [
        u
        for u in physical["evidence_units"]
        if u["evidence_type"] == "sentence"
        and by_id[u["anchor_id"]].get("document_partition") != "REFERENCES"
        and by_id[u["anchor_id"]].get("anchor_type") != "figure_ocr"
    ]
    cells = [c for t in physical["tables"] for c in t["cells"] if c["raw_value"]]
    assets = physical["assets"]

    def ratio(count, total):
        return count / total if total else None

    return {
        "version": QUALITY_VECTOR_VERSION,
        "denominator": "source-occurrences.v2",
        "eligible_text_units": len(units),
        "eligible_cells": len(cells),
        "eligible_visual_regions": len(assets),
        "source_binding_coverage": ratio(sum(bool(u["anchor_id"]) for u in units), len(units)),
        "text_precise_locator_coverage": ratio(sum(u["locator"]["status"] == "VERIFIED" for u in units), len(units)),
        "figure_region_locator_coverage": ratio(sum(bool(a.get("region_id")) for a in assets), len(assets)),
        "figure_ocr_ready_coverage": ratio(
            sum(a.get("locator_metadata", {}).get("ocr", {}).get("status") == "READY" for a in assets), len(assets)
        ),
        "table_cell_locator_coverage": ratio(
            sum(c.get("metadata", {}).get("locator_status") == "VERIFIED" for c in cells), len(cells)
        ),
        "raw_block_acceptance": ratio(audit.get("accepted_blocks", 0), audit.get("raw_blocks", 0)),
        "extraction_audit": audit,
        "ocr": physical["ocr_report"],
    }


def quality_regressions(previous: dict, candidate: dict) -> list[str]:
    old = previous.get("quality_vector") or {}
    new = candidate.get("quality_vector") or {}
    if old.get("version") != new.get("version") or old.get("denominator") != new.get("denominator"):
        return []
    regressions = []
    for name in (
        "source_binding_coverage",
        "text_precise_locator_coverage",
        "figure_region_locator_coverage",
        "figure_ocr_ready_coverage",
        "table_cell_locator_coverage",
        "raw_block_acceptance",
    ):
        a, b = old.get(name), new.get(name)
        if a is not None and (b is None or b + 1e-6 < a):
            regressions.append(name)
    return regressions
