"""断尾清理 + 生长素/KEGG 严格分离 + 三态条件回归锁（2026-09-27 收尾）。"""

from __future__ import annotations

import pytest

from yuxi.knowledge.rendering.citation_channel import _clean_truncated_fragments
from yuxi.knowledge.validation.figure_claim_validator import validate_figure_claims
from yuxi.knowledge.validation.figure_semantic_gate import run_semantic_gate

pytestmark = [pytest.mark.unit]


# ---- 断尾清理 ----


def test_truncated_fragment_removed():
    """删句后残片"为主结论中"被清理。"""
    text = "前面正常句子。\n这些图为主结论中\n\n下一段正常。"
    cleaned = _clean_truncated_fragments(text)
    assert "为主结论中" not in cleaned
    assert "前面正常句子。" in cleaned and "下一段正常。" in cleaned


def test_short_properly_terminated_kept():
    """正常短句（以句读结尾）不被清理。"""
    for keep in ("好。", "如图 5。", "结论：正常。", "详见下文；"):
        assert _clean_truncated_fragments(keep) == keep


def test_headings_tables_codefence_not_cleaned():
    """标题/表格/代码块行不清理。"""
    for keep in ("# 短标题", "| a | b |", "```python\n```"):
        assert _clean_truncated_fragments(keep) == keep


def test_list_item_marker_not_cleaned_twice():
    """列表标记行由 _drop_orphan_list_markers 处理，本函数不重复删。"""
    assert _clean_truncated_fragments("- ") == "- "


# ---- 生长素/KEGG 严格分离 ----


def _kegg_registry():
    return {
        "figure s18": {
            "kind": "figure",
            "label": "Figure S18",
            "source": "caption",
            "quote_head": "Figure S18 ZH11_vs_myb73-35 KEGG barplot of differentially metabolites",
            "_quote": "Figure S18 ZH11_vs_myb73-35 KEGG barplot of differentially metabolites",
            "anchor_id": "ea_s18",
            "evidence_id": "ev_s18",
            "kb_id": "kb",
            "file_id": "f",
            "revision_id": "r",
            "page": 18,
            "citation_ref": "E1",
        }
    }


def _kegg_citations():
    return [
        {
            "ref": "E1",
            "_quote": "Figure S18 ZH11_vs_myb73-35 KEGG barplot of differentially metabolites",
            "quote_head": "Figure S18 KEGG barplot",
            "locatable": True,
        }
    ]


def test_auxin_claim_on_kegg_figure_rejected():
    """KEEG 图上声称生长素结论 → CAPTION_SEMANTIC_MISMATCH。"""
    cleaned, report = validate_figure_claims(
        "Figure S18 展示了生长素通路相关代谢物分析，支持 OsMYB73 调控生长素合成。",
        registry=_kegg_registry(),
        citations=_kegg_citations(),
    )
    assert report["status"] in ("DEGRADED", "UNSUPPORTED")
    assert any(c["verdict"] == "UNSUPPORTED" for c in report.get("claims", []))


def test_kegg_only_description_passes():
    """纯 KEGG 描述（无生长素）→ PASS。"""
    cleaned, report = validate_figure_claims(
        "Figure S18 展示了差异代谢物的 KEGG 通路分类柱状图。",
        registry=_kegg_registry(),
        citations=_kegg_citations(),
    )
    assert report["status"] == "PASS"
    assert "KEGG" in cleaned


# ---- 三态条件检查回归锁 ----


def test_condition_mismatch_table1_no_heat():
    bad = "野生型 ZH11 在正常条件下直链淀粉含量为 18.4%，热胁迫后下降至 14.9%。"
    vs = run_semantic_gate(bad, available_conditions=set())
    assert any(v.reason_code == "CONDITION_MISMATCH" for v in vs)


def test_condition_ok_table2_with_heat():
    vs = run_semantic_gate(
        "野生型 ZH11 直链淀粉含量为 18.4%。",
        available_conditions={"control", "heat"},
    )
    assert not any(v.reason_code == "CONDITION_MISMATCH" for v in vs)


def test_condition_none_no_judgment():
    vs = run_semantic_gate(
        "野生型 ZH11 在正常条件下直链淀粉含量为 18.4%，热胁迫后下降至 14.9%。",
        available_conditions=None,
    )
    assert not any(v.verdict.value == "UNSUPPORTED" and v.reason_code == "CONDITION_MISMATCH" for v in vs)
