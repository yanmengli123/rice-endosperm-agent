"""Deterministic acceptance metrics for PDF table-cell extraction."""

from __future__ import annotations

import math
from typing import Any


def validate_reviewed_gold(gold: dict[str, Any]) -> None:
    """Reject drafts and empty samples before any extraction or acceptance."""
    if gold.get("schema") != "pdf-table-gold.v1":
        raise ValueError("unsupported gold schema")
    if gold.get("reviewed") is not True or not str(gold.get("reviewed_by") or "").strip():
        raise ValueError("gold requires reviewed=true and a non-empty reviewed_by; reviewed=false drafts are refused")
    if not isinstance(gold.get("cells"), list) or not gold["cells"]:
        raise ValueError("gold must contain at least one reviewed cell")


def _cell_key(cell: dict[str, Any]) -> tuple[str, int, int]:
    return (str(cell["table_id"]), int(cell["row_index"]), int(cell["column_index"]))


def evaluate_table_gold(
    *,
    expected_cells: list[dict[str, Any]],
    actual_cells: list[dict[str, Any]],
    exhaustive: bool,
) -> dict[str, Any]:
    """Compare table cells without silently normalizing scientific values.

    Coordinate matching and value matching are reported separately.  Raw values
    are compared byte-for-byte after JSON decoding so a unit, sign, decimal, or
    significance marker cannot disappear behind whitespace normalization.
    """
    if not expected_cells:
        raise ValueError("gold must contain at least one reviewed cell")
    expected = {_cell_key(cell): str(cell["raw_value"]) for cell in expected_cells}
    actual = {_cell_key(cell): str(cell["raw_value"]) for cell in actual_cells}
    if len(expected) != len(expected_cells):
        raise ValueError("gold contains duplicate table cell coordinates")
    if len(actual) != len(actual_cells):
        raise ValueError("actual extraction contains duplicate table cell coordinates")

    matched_coordinates = sorted(set(expected) & set(actual))
    exact_matches = [key for key in matched_coordinates if expected[key] == actual[key]]
    missing = sorted(set(expected) - set(actual))
    unexpected = sorted(set(actual) - set(expected)) if exhaustive else []
    mismatches = [
        {
            "table_id": key[0],
            "row_index": key[1],
            "column_index": key[2],
            "expected": expected[key],
            "actual": actual[key],
        }
        for key in matched_coordinates
        if expected[key] != actual[key]
    ]
    expected_count = len(expected)
    actual_count = len(actual)
    coordinate_recall = len(matched_coordinates) / expected_count if expected_count else 1.0
    exact_value_recall = len(exact_matches) / expected_count if expected_count else 1.0
    coordinate_precision = (
        len(matched_coordinates) / actual_count if exhaustive and actual_count else (1.0 if exhaustive else None)
    )
    return {
        "expected_cell_count": expected_count,
        "actual_cell_count": actual_count,
        "matched_coordinate_count": len(matched_coordinates),
        "exact_value_count": len(exact_matches),
        "coordinate_recall": coordinate_recall,
        "coordinate_precision": coordinate_precision,
        "exact_value_recall": exact_value_recall,
        "missing": [{"table_id": key[0], "row_index": key[1], "column_index": key[2]} for key in missing],
        "unexpected": [{"table_id": key[0], "row_index": key[1], "column_index": key[2]} for key in unexpected],
        "value_mismatches": mismatches,
    }


def table_gold_passes(
    metrics: dict[str, Any],
    *,
    minimum_coordinate_recall: float = 1.0,
    minimum_exact_value_recall: float = 1.0,
    minimum_coordinate_precision: float = 1.0,
) -> bool:
    precision = metrics.get("coordinate_precision")
    thresholds = (minimum_coordinate_recall, minimum_exact_value_recall, minimum_coordinate_precision)
    if any(not math.isfinite(value) or not 0 <= value <= 1 for value in thresholds):
        raise ValueError("acceptance thresholds must be finite numbers in [0, 1]")
    return bool(
        metrics["expected_cell_count"] > 0
        and metrics["coordinate_recall"] >= minimum_coordinate_recall
        and metrics["exact_value_recall"] >= minimum_exact_value_recall
        and (precision is None or precision >= minimum_coordinate_precision)
    )
