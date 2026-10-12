import pytest

from yuxi.knowledge.pdf_evidence.table_gold import evaluate_table_gold, table_gold_passes


def _cell(table_id: str, row: int, column: int, value: str) -> dict:
    return {"table_id": table_id, "row_index": row, "column_index": column, "raw_value": value}


def test_table_gold_separates_coordinate_and_exact_value_failures():
    metrics = evaluate_table_gold(
        expected_cells=[_cell("Table 1", 0, 0, "18.4%"), _cell("Table 1", 0, 1, "14.9%")],
        actual_cells=[_cell("Table 1", 0, 0, "18.4"), _cell("Table 1", 1, 1, "14.9%")],
        exhaustive=True,
    )

    assert metrics["coordinate_recall"] == 0.5
    assert metrics["coordinate_precision"] == 0.5
    assert metrics["exact_value_recall"] == 0.0
    assert metrics["value_mismatches"][0]["expected"] == "18.4%"
    assert table_gold_passes(metrics) is False


def test_non_exhaustive_gold_does_not_claim_precision():
    metrics = evaluate_table_gold(
        expected_cells=[_cell("Table S2", 2, 3, "71.5 °C")],
        actual_cells=[_cell("Table S2", 2, 3, "71.5 °C"), _cell("Table S2", 4, 5, "extra")],
        exhaustive=False,
    )

    assert metrics["coordinate_precision"] is None
    assert metrics["exact_value_recall"] == 1.0
    assert table_gold_passes(metrics) is True


def test_duplicate_gold_coordinates_are_rejected():
    duplicate = [_cell("Table 1", 0, 0, "A"), _cell("Table 1", 0, 0, "B")]
    with pytest.raises(ValueError, match="duplicate"):
        evaluate_table_gold(expected_cells=duplicate, actual_cells=[], exhaustive=True)


def test_empty_gold_cannot_report_perfect_acceptance():
    with pytest.raises(ValueError, match="at least one"):
        evaluate_table_gold(expected_cells=[], actual_cells=[], exhaustive=True)


@pytest.mark.parametrize("threshold", [-0.1, 1.1, float("nan"), float("inf")])
def test_invalid_threshold_cannot_bypass_acceptance(threshold):
    metrics = evaluate_table_gold(expected_cells=[_cell("t1", 0, 0, "A")], actual_cells=[], exhaustive=True)
    with pytest.raises(ValueError, match="thresholds"):
        table_gold_passes(metrics, minimum_exact_value_recall=threshold)


def test_physical_gold_verifier_refuses_unreviewed_input_before_database(tmp_path):
    import asyncio
    import json
    from argparse import Namespace

    from scripts.verify_pdf_table_gold import verify

    path = tmp_path / "gold.json"
    path.write_text(json.dumps({"schema": "pdf-table-gold.v1", "reviewed": False}), encoding="utf-8")
    with pytest.raises(ValueError, match="reviewed=true"):
        asyncio.run(verify(Namespace(gold=str(path), kb_id="kb", file_id="file")))


def test_byte_exact_comparison_catches_unit_and_sign_drift():
    """科学值逐字节比对：单位/符号/小数位漂移不得被空白归一化吞掉。"""
    metrics = evaluate_table_gold(
        expected_cells=[
            _cell("t1", 0, 0, "18.4% ± 0.3"),
            _cell("t1", 0, 1, "-2.1"),
            _cell("t1", 0, 2, "71.5 °C"),
        ],
        actual_cells=[
            _cell("t1", 0, 0, "18.4%±0.3"),
            _cell("t1", 0, 1, "2.1"),
            _cell("t1", 0, 2, "71.5°C"),
        ],
        exhaustive=True,
    )

    assert metrics["matched_coordinate_count"] == 3
    assert metrics["exact_value_count"] == 0
    assert len(metrics["value_mismatches"]) == 3
    assert table_gold_passes(metrics) is False


def test_unreviewed_draft_gold_is_refused_by_verify_gate(tmp_path, monkeypatch):
    """草案（reviewed=false）不得当金标使用——防自我循环验证（RC→GA §② 红线）。"""
    import asyncio
    import json
    from argparse import Namespace

    import scripts.draft_pdf_table_gold as draft_tool

    gold = {
        "schema": "pdf-table-gold.v1",
        "kb_id": "kb",
        "file_id": "file",
        "source_sha256": "a" * 64,
        "caption": "Table 1 demo",
        "reviewed": False,
        "reviewed_by": None,
        "cells": [_cell("table1", 0, 0, "v")],
    }
    path = tmp_path / "gold.json"
    path.write_text(json.dumps(gold, ensure_ascii=False), encoding="utf-8")

    async def no_extraction(**_kwargs):
        raise AssertionError("未复核金标必须在提取前被拒绝")

    monkeypatch.setattr(draft_tool, "_extract_cells", no_extraction)
    with pytest.raises(SystemExit, match="reviewed=false"):
        asyncio.run(draft_tool.verify(Namespace(gold=str(path), output=str(tmp_path / "report.json"))))
