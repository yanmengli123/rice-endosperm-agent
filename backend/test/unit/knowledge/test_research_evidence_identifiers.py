"""基因标识符抽取口径：MSU/RAP 位点数字段统一宽容 5-7 位（与 lexicon 同口径）。

7 位 MSU 形态（LOC_Os06g0133000）必须触发抽取与路由，RAP 5 位短形态同样保留。
"""

from __future__ import annotations

from yuxi.knowledge.research_evidence import extract_gene_identifiers


def test_seven_digit_msu_locus_is_extracted():
    assert extract_gene_identifiers("LOC_Os06g0133000 的坐标区间") == ["msu:loc_os06g0133000"]


def test_five_digit_msu_locus_still_extracted():
    assert extract_gene_identifiers("LOC_Os06g01330 的注释") == ["msu:loc_os06g01330"]


def test_rap_seven_and_five_digit_forms_extracted():
    assert extract_gene_identifiers("Os07g0842000 的坐标") == ["rap:os07g0842000"]
    assert extract_gene_identifiers("Os07g08420 是旧版 RAP 编号") == ["rap:os07g08420"]


def test_rap_pattern_does_not_slice_inside_msu_id():
    assert extract_gene_identifiers("LOC_Os06g0133000") == ["msu:loc_os06g0133000"]


def test_eight_digit_locus_is_rejected():
    assert extract_gene_identifiers("LOC_Os06g01330001 位数不对") == []


def test_multiple_identifiers_preserve_first_seen_order():
    ids = extract_gene_identifiers("比较 LOC_Os06g0133000 与 LOC_Os01g01010")
    assert ids == ["msu:loc_os06g0133000", "msu:loc_os01g01010"]
