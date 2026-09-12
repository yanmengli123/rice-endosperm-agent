"""P2-10/P2-11 单测：证据单元切分、span 构建与词法索引提取。"""

from __future__ import annotations


import pytest

from yuxi.knowledge.evidence.sentence_splitter import (
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


def test_splitter_detects_caption_table_row_sentence_and_formula():
    anchors = [
        _FakeAnchor("a1", "OsMYB73 affects grain size by regulating storage substances accumulation."),
        _FakeAnchor("a2", "Table 1\nSummary of grain chalkiness metrics."),
        _FakeAnchor("a3", "| OsMYB73 | 115-164 | SANT |"),
    ]
    markdown = (
        "Figure 2\nExpression profiles of OsMYB73 across endosperm stages.\n"
        "| Test | WT | mutant |\n"
        "| 45% | 30% | 55% |"
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
        "OsMYB73 / LOC_Os01g01010 spans 115-164 aa; doi:10.1111/pbi.14558; "
        "PMID: 35084453; see Figure 2A and Table 1."
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
