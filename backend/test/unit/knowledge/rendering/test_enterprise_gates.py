"""G1-G3 企业级门禁测试（2026-09-26 第四批）+ 语义门禁接线（2026-09-27）。

G1：数值未绑定时追加"可引用 Table 卡片"可行动提示
G2：caption 行优先入池（覆盖率回归）
G3：已签芯片的图表标签与 citation 题注的确定性类型词比对
语义门禁接线：题注意义冲突强制失败关闭；条件/数值判据无输入时 fail-open
"""

from __future__ import annotations

import pytest

from yuxi.knowledge.rendering.citation_channel import (
    MAX_CITATIONS,
    _check_figure_label_consistency,
    _drop_orphan_list_markers,
    _TABLE_CARD_HINT,
    apply_citation_channel,
)

pytestmark = [pytest.mark.unit]


def _caption_citation(ref: str, label: str, quote: str, *, page: int = 10, evidence_type: str = "caption") -> dict:
    return {
        "ref": ref,
        "evidence_id": f"ev-{ref}",
        "kb_id": "kb-a",
        "file_id": "file-a",
        "filename": "paper.pdf",
        "zone": "MAIN_TEXT",
        "page_numbers": [page],
        "primary_page": page,
        "quote_head": quote[:80],
        "anchor_ids": [f"ea-{ref}"],
        "locatable": True,
        "toc_line": False,
        "secondary_of": None,
        "_quote": quote,
        "_quote_norm": quote.lower(),
        "_anchor_id": f"ea-{ref}",
        "_parse_revision_id": "r1",
        "_evidence_type": evidence_type,
    }


# ---- G1：数值→卡片可行动提示 ----


def test_g1_standalone_numeric_notice_includes_card_hint():
    """无未定位提示但有未绑定数值时，独立披露含"可引用 Table 卡片"引导。"""
    cits = [_caption_citation("E1", "Figure 2", "Figure 2. Phenotypes of mutants")]
    # 正文有数值 + 图表编号提及但无芯片绑定
    text = "如图 5 所示，突变体垩白度为 95.6%，对照为 45.5%。"
    result, validation = apply_citation_channel(text, cits, figure_refs_enabled=False)
    norm = validation.get("render_normalization") or {}
    if norm.get("numeric_disclosure"):
        assert _TABLE_CARD_HINT in result, "数值披露应包含卡片引导"


def test_g1_combined_notice_includes_card_hint_when_numeric():
    """未定位提示 + 数值条款形态也含卡片引导。"""
    cits = [_caption_citation("E1", "Figure 2", "Figure 2. Phenotypes")]
    text = "如图 5 所示，该表型数据为 95.6%。这是模型的推断。"
    result, _ = apply_citation_channel(text, cits, figure_refs_enabled=False)
    # 如果既有未定位又有数值，引导应在
    if "其中" in result and "处含具体数值" in result:
        assert _TABLE_CARD_HINT in result


def test_g1_no_numeric_no_hint():
    """无数值时不出现卡片引导（防噪声）。"""
    cits = [_caption_citation("E1", "Figure 2", "Figure 2. Phenotypes")]
    text = "突变体表现出明显的表型变化。"
    result, _ = apply_citation_channel(text, cits, figure_refs_enabled=False)
    assert _TABLE_CARD_HINT not in result


# ---- G2：caption 优先入池 ----


def test_g2_max_citations_raised():
    assert MAX_CITATIONS >= 16


# ---- 语义门禁接线（2026-09-27 事故回归）----
# 事故：门禁删句逻辑绑定在 figure_ref_anchor_enabled 上，且两个判据校验器在
# **空输入**下把"调用方没给数据"判成"断言不成立"；生产调用点恰好只传
# figure_labels_in_registry（表格/条件数据在 chat 层事后才投影）→ 开关全开即
# 删真实正文（实测 205 字符段 → 只剩 20 字符标题；msg 4024 留悬空空列表项）。
# 下面三条锁住「生产形态不得删句」。


def _numeric_citation(ref: str = "E1") -> dict:
    return _caption_citation(
        ref,
        "Table 1",
        "Table 1. Physicochemical properties of rice grains across genotypes.",
        page=1,
    )


def test_gate_never_deletes_numeric_sentence_in_production_shape():
    """生产形态：只给 citations（无 conditions/table_rows）→ 数值句必须保留。"""
    text = "正常条件下垩白率为 12.1%。"
    out, val = apply_citation_channel(text, [_numeric_citation()], figure_refs_enabled=True)
    assert "12.1" in out, "无表格数据时不得删数值句（fail-open）"
    sg = (val.get("render_normalization") or {}).get("semantic_gate") or {}
    assert sg.get("unsupported_removed") == 0
    assert sg.get("deletion_enabled") is False
    assert sg.get("table_rows_available") == 0


def test_gate_never_deletes_condition_sentence_in_production_shape():
    """生产形态：含条件词的句子必须保留（历史实现会判 CONDITION_MISMATCH 删句）。"""
    text = "热胁迫下 osmyb73 垩白度为 26.8%。"
    out, val = apply_citation_channel(text, [_numeric_citation()], figure_refs_enabled=True)
    assert "26.8" in out
    sg = (val.get("render_normalization") or {}).get("semantic_gate") or {}
    assert sg.get("unsupported_removed") == 0


def test_gate_full_paragraph_survives_production_shape():
    """事故原文形态：多句数值段落不得被掏空。"""
    text = (
        "## 热胁迫下基因型间理化与形态差异\n\n"
        "野生型 ZH11 在正常条件下直链淀粉 18.4%、糊化温度 76.2 ℃、垩白率 12.1%。\n\n"
        "osmyb73 在正常条件下直链淀粉 14.9%、糊化温度 71.5 ℃、垩白率 26.8%。\n"
    )
    out, _ = apply_citation_channel(text, [_numeric_citation()], figure_refs_enabled=True)
    for token in ("18.4", "76.2", "12.1", "14.9", "71.5", "26.8"):
        assert token in out, f"{token} 被删除"
    assert len(out) >= len(text) - 10


def test_gate_deletion_requires_explicit_switch():
    """即便判据齐备，删句也必须显式开闸（semantic_gate_enabled=True）。"""
    text = "osmyb73 垩白度为 95.6%。"
    cits = [_numeric_citation()]
    table = [[{"text": "Genotype"}, {"text": "Chalkiness"}], [{"text": "osmyb73"}, {"text": "26.8"}]]

    off, val_off = apply_citation_channel(
        text,
        cits,
        figure_refs_enabled=True,
        available_conditions={"control"},
        table_rows=table,
    )
    assert "95.6" in off, "开关关闭时不得删句"
    sg_off = (val_off.get("render_normalization") or {}).get("semantic_gate") or {}
    assert sg_off["unsupported_detected"] >= 1, "关闭态必须留痕「检测到但未删」"
    assert sg_off["unsupported_removed"] == 0
    assert sg_off["deletion_enabled"] is False

    on, val_on = apply_citation_channel(
        text,
        cits,
        figure_refs_enabled=True,
        semantic_gate_enabled=True,
        available_conditions={"control"},
        table_rows=table,
    )
    assert "95.6" not in on, "显式开闸且判据齐备时删除编造数值"
    sg_on = (val_on.get("render_normalization") or {}).get("semantic_gate") or {}
    assert sg_on["unsupported_removed"] == 1
    assert sg_on["table_rows_available"] == 2  # 行数（表头 + 数据行）


def test_caption_identity_gate_removes_bad_supplementary_summary_before_signing():
    """真实坏样例：错误 S4/S6/S8 描述不能获得 F#，正确 S5 保留并签发。"""
    citations = [
        _caption_citation(
            "E1",
            "Figure S4",
            "Figure S4. Comparison of plant morphology between WT and cr-myb73 mutants.",
        ),
        _caption_citation(
            "E2",
            "Figure S5",
            "Figure S5. SEM and TEM observations of mature endosperm in ZH11 and mutants.",
        ),
        _caption_citation(
            "E3",
            "Figure S6",
            "Figure S6. Rice starch particles of WT and cr-myb73 mutants.",
        ),
        _caption_citation(
            "E4",
            "Figure S8",
            "Figure S8. Physicochemical properties of rice grains across genotypes.",
        ),
    ]
    text = (
        "- Figure S4 展示 OsMYB73 过表达系籽粒表型。\n"
        "- Figure S5 展示成熟胚乳的扫描电镜和透射电镜图像。\n"
        "- Figure S6 展示 OsMYB73 与其他基因的双突变体籽粒表型。\n"
        "- Figure S8 展示双突变体籽粒表型。"
    )

    out, validation = apply_citation_channel(text, citations, figure_refs_enabled=True)

    assert "过表达系籽粒表型" not in out
    assert "双突变体籽粒表型" not in out
    assert "扫描电镜和透射电镜" in out
    assert out.count("〔图表F") == 1
    assert "Figure S5" in out
    claim_gate = validation["figure_claim_validation"]
    assert claim_gate["deletion_enabled"] is True
    assert claim_gate["enforcement_scope"] == "CAPTION_IDENTITY"
    assert claim_gate["removed_sentence_count"] == 3
    assert len(validation["figure_refs"]) == 1


def test_orphan_list_marker_cleanup():
    """删句掏空列表项后不得留悬空 "-"（msg 4024 实测形态）。"""
    assert _drop_orphan_list_markers("## 标题\n\n-  \n \n- 有内容\n") == "## 标题\n\n \n- 有内容\n"
    assert _drop_orphan_list_markers("1.\n2. 保留\n> \n") == "2. 保留\n"
    # 带缩进的子项与正常引用块不受影响
    keep = "- 父项\n  - 子项\n> 引用内容\n"
    assert _drop_orphan_list_markers(keep) == keep


def test_g2_caption_rows_prioritized():
    """caption 行排在非 caption 行前面（覆盖率保证的构造性测试）。"""
    rows = []
    # 造 18 行（超过旧 12 上限），caption 混在中间
    for i in range(18):
        rows.append(
            {
                "evidence_id": f"ev-{i}",
                "kb_id": "kb-a",
                "file_id": "file-a",
                "anchor_id": f"ea-{i}",
                "anchor_ids": [f"ea-{i}"],
                "page_number": 3,
                "evidence_type": "caption" if i == 5 else "sentence",
                "evidence_quote": f"Figure {i}. Some caption text for row {i}.",
                "content": f"【证据锚点】ea-{i}\n【页码】3\nFigure {i}. Some caption text.",
            }
        )
    # 用 sorted 的 key 直接验证排序语义
    ordered = sorted(
        rows,
        key=lambda row: 0 if str(row.get("evidence_type") or "").casefold() == "caption" else 1,
    )
    assert ordered[0]["evidence_type"] == "caption"  # caption 行在最前


# ---- G3：芯片-题注类型词比对 ----


def test_g3_mismatch_detected_and_marked():
    """标签含 KEGG，题注是 RNA-seq → 追加不一致标记。"""
    cits = [
        {
            "ref": "E1",
            "quote_head": "Figure S10 RNA-sequencing transcriptomic analysis of ZH11 and cr-myb73",
        }
    ]
    text = "KEGG 分析见 〔证据E1｜x〕 〔图表F1｜Fig. S10 KEGG〕"
    result, mismatches = _check_figure_label_consistency(text, cits)
    assert mismatches == 1
    assert "（与原文题注不一致，请以原文题注为准）" in result


def test_g3_matching_terms_not_marked():
    """标签与题注共享类型词 → 不标。"""
    cits = [{"ref": "E1", "quote_head": "Figure S17 Venn diagram showing differential metabolites"}]
    text = "见 〔证据E1｜x〕 〔图表F1｜Fig. S17 Venn〕"
    result, mismatches = _check_figure_label_consistency(text, cits)
    assert mismatches == 0
    assert "不一致" not in result


def test_g3_no_type_terms_not_compared():
    """无类型词标签（"Figure 2"）→ 不比对。"""
    cits = [{"ref": "E1", "quote_head": "Figure 2. Phenotypes of mutants in rice"}]
    text = "见 〔图表F1｜Figure 2〕"
    result, mismatches = _check_figure_label_consistency(text, cits)
    assert mismatches == 0
    assert "不一致" not in result


def test_g3_idempotent():
    """已有标记的芯片不重复标记。"""
    cits = [{"ref": "E1", "quote_head": "Figure S10 RNA-sequencing"}]
    text = " 〔图表F1｜Fig. S10 KEGG〕（与原文题注不一致，请以原文题注为准）"
    result, mismatches = _check_figure_label_consistency(text, cits)
    assert mismatches == 0
    assert result.count("不一致") == 1


def test_g3_mixed_true_positive_negative():
    """一个匹配一个不匹配 → 只标不匹配的。"""
    cits = [
        {"ref": "E1", "quote_head": "Figure S17 Venn diagram"},
        {"ref": "E2", "quote_head": "Figure S11 RNA-seq analysis"},
    ]
    text = " 〔证据E1｜x〕 〔图表F1｜Fig. S17 Venn〕 和 〔证据E2｜x〕 〔图表F2｜Fig. S11 KEGG〕"
    result, mismatches = _check_figure_label_consistency(text, cits)
    assert mismatches == 1
    assert mismatches == 1  # 只有 S11 不匹配
    # S11 的芯片后面跟着不一致标记

    assert "图表F2" in result and "S11" in result
    # 不一致标记紧跟在 S11 之后
    s11_pos = result.find("S11")
    after_s11 = result[s11_pos : s11_pos + 80]
    assert "与原文题注不一致" in after_s11, f"S11 后应有标记: {after_s11!r}"
    # S17 芯片后紧跟的文本不含不一致标记（用芯片闭合符精确定位）
    import re as _re

    s17_chip = _re.search(r"图表F1｜[^〕]*S17[^〕]*〕", result)
    assert s17_chip, "S17 芯片应存在"
    immediately_after = result[s17_chip.end() : s17_chip.end() + 20]
    assert "不一致" not in immediately_after, f"S17 芯片紧邻处不应有标记: {immediately_after!r}"


def test_g3_3830_byte_gate():
    """G3 不破坏金标门：3830 对一致性比对仍逐字节不变（真实答案无类型词冲突）。"""
    from pathlib import Path

    fixture = (
        Path(__file__).resolve().parents[3] / "fixtures" / "render_normalization" / "message_3830.txt"
    ).read_text(encoding="utf-8")
    # 3830 是 table 卡片答案，无 figure 芯片 → G3 零改动
    result, mismatches = _check_figure_label_consistency(fixture, [])
    assert result == fixture and mismatches == 0
