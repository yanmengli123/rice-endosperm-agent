"""P2-10/P2-11 单测：证据单元切分、span 构建与词法索引提取。"""

from __future__ import annotations


import pytest

from yuxi.knowledge.evidence.sentence_splitter import (
    _classify_quote,
    split_evidence_units,
)
from yuxi.knowledge.evidence.span_builder import (
    extract_lexical_rows,
    normalize_numeric,
)

pytestmark = [pytest.mark.unit]


class _FakeAnchor:
    def __init__(self, anchor_id, quote, page=1):
        self.anchor_id = anchor_id
        self.quote = quote
        self.page = page


# ---- 补充材料图表题注（2026-09-26 修复）----
# 事故：_CAPTION_START 只认「关键词 + 直接数字」，Figure S21 / Table S2 /
# Supplementary Figure 3 全部落成普通 sentence、container_label=None →
# 进不了 figure_entities、正文引用 S 图只能 no_registry_match。


def test_splitter_recognizes_supplementary_figure_captions():
    evidence_type, container_label, row_key = _classify_quote(
        "Figure S21 Rice OsISA2 spatiotemporal expression pattern and "
        "CRISPR/Cas9-mediated target mutagenesis of OsISA2-mutant brown rice grains."
    )
    assert evidence_type == "caption"
    assert container_label == "Figure S21"
    assert row_key is None


def test_splitter_recognizes_supplementary_table_and_qualifier_forms():
    for quote, expected in (
        ("Table S2 Absolute values of all detected fatty acid components.", "Table S2"),
        ("Supplementary Figure 3 Primer sequences used in this study.", "Supplementary Figure 3"),
        ("Supplemental Figure 1 Schematic of the vector.", "Supplemental Figure 1"),
        ("图S5 野生型与突变体胚乳细胞电镜观察。", "图S5"),
        ("Fig. S7 Metabolic heat map of the double mutants.", "Fig. S7"),
    ):
        evidence_type, container_label, _row_key = _classify_quote(quote)
        assert evidence_type == "caption", quote
        assert container_label == expected, quote


def test_supplementary_caption_label_canonicalizes_to_registry_key():
    # 题注 span 的 container_label 必须能被 canonical_figure_label 归一成
    # figure sN 注册键，否则签发阶段仍会 no_registry_match（闭环校验）
    from yuxi.knowledge.evidence.caption_locator import canonical_figure_label

    _type, container_label, _row = _classify_quote("Figure S22 Screening binding motifs of OsMYB73.")
    assert canonical_figure_label(container_label) == "figure s22"


def test_main_figure_and_plain_sentence_unchanged():
    # 回归：主图题注判定不受影响
    assert _classify_quote("Figure 5 CRISPR/Cas9 mediated target mutagenesis.")[:2] == (
        "caption",
        "Figure 5",
    )
    # 普通句子仍不是题注（不得因 S 前缀放宽而误判）
    assert _classify_quote("This sentence merely mentions figure quality.")[0] == "sentence"


def test_splitter_separates_parser_merged_supplementary_captions():
    merged = (
        "Figure S5 SEM and TEM observations of mature endosperm in ZH11 and cr-myb73 mutants. "
        "Figure S6 Rice starch particles of WT and cr-myb73 mutants."
    )
    units = split_evidence_units(anchors=[_FakeAnchor("a-merged", merged, page=17)], markdown_body="")
    assert [(unit.container_label, unit.anchor_id) for unit in units] == [
        ("Figure S5", "a-merged"),
        ("Figure S6", "a-merged"),
    ]
    assert units[0].quote.endswith("mutants.")
    assert units[1].quote.startswith("Figure S6")


def test_splitter_does_not_split_mid_sentence_figure_reference():
    quote = "Figure S5 SEM observations are compared with Figure S6 in the following analysis."
    units = split_evidence_units(anchors=[_FakeAnchor("a1", quote)], markdown_body="")
    assert len(units) == 1
    assert units[0].container_label == "Figure S5"


def test_splitter_detects_caption_table_row_sentence_and_formula():
    anchors = [
        _FakeAnchor("a1", "OsMYB73 affects grain size by regulating storage substances accumulation."),
        _FakeAnchor("a2", "Table 1\nSummary of grain chalkiness metrics."),
        _FakeAnchor("a3", "| OsMYB73 | 115-164 | SANT |"),
    ]
    markdown = (
        "Figure 2\nExpression profiles of OsMYB73 across endosperm stages.\n| Test | WT | mutant |\n| 45% | 30% | 55% |"
    )
    units = split_evidence_units(anchors=anchors, markdown_body=markdown)
    by_line = {unit.quote: unit for unit in units}
    assert "OsMYB73 affects grain size by regulating storage substances accumulation." in by_line
    assert units[0].evidence_type == "sentence"
    # anchor quote 含表格行首字符 → table_row
    assert any(u.evidence_type == "table_row" and u.row_key == "OsMYB73" for u in units)
    # caption 识别
    assert any(u.evidence_type == "caption" and u.container_label == "Figure 2" for u in units)
    assert any(u.evidence_type == "caption" and u.container_label == "Table 1" for u in units)
    # 去重：同一句只保留一个单元
    same_quote = [u for u in units if u.quote == units[0].quote]
    assert len(same_quote) == 1


def test_splitter_protects_doi_in_sentence():
    anchors = [_FakeAnchor("a1", "See doi:10.1111/pbi.14558 for details.")]
    units = split_evidence_units(anchors=anchors, markdown_body="")
    assert len(units) == 1
    assert "doi:10.1111/pbi.14558" in units[0].quote


def test_normalize_numeric_range():
    assert normalize_numeric("115-164 aa") == "115:164"
    assert normalize_numeric("0.3 mg/L") == "0.3"
    assert normalize_numeric("45%") == "45"


def test_extract_lexical_rows_identifiers_numeric_citation_figure():
    quote = (
        "OsMYB73 / LOC_Os01g01010 spans 115-164 aa; doi:10.1111/pbi.14558; PMID: 35084453; see Figure 2A and Table 1."
    )
    rows = extract_lexical_rows(owner_type="span", owner_id="es_1", quote=quote)
    types = {row["lex_type"] for row in rows}
    assert "identifier" in types
    assert "numeric" in types
    assert "citation" in types
    assert "figure_table" in types
    values = {row["lex_value_folded"] for row in rows if row["lex_type"] == "citation"}
    assert "doi:10.1111/pbi.14558" in values
    assert "pmid:35084453" in values
    figs = {row["lex_value"] for row in rows if row["lex_type"] == "figure_table"}
    assert "2A" in figs and "1" in figs


def test_extract_lexical_rows_is_deterministic_and_dedupes():
    quote = "OsMYB73 OsMYB73 located at 115-164 and 167-215 aa."
    first = extract_lexical_rows(owner_type="span", owner_id="es_1", quote=quote)
    second = extract_lexical_rows(owner_type="span", owner_id="es_1", quote=quote)
    assert first == second
    ids = [(row["lex_type"], row["lex_value_folded"]) for row in first]
    assert len(ids) == len(set(ids))
