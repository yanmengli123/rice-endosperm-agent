"""实际导出语法与未信任文件边界；不把用户研究数据作为仓库 fixtures。"""

from __future__ import annotations

import io
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from openpyxl import Workbook

from yuxi.literature import reference_import, reference_tables

pytestmark = pytest.mark.unit


def xlsx(rows, extra_sheet=None):
    book = Workbook()
    sheet = book.active
    for row in rows:
        sheet.append(row)
    if extra_sheet:
        book.create_sheet("说明").append([extra_sheet])
    stream = io.BytesIO()
    book.save(stream)
    return stream.getvalue()


def test_wos_long_csv_quoted_authors_addresses_and_zero_citations():
    raw = (
        "Publication Type,Article Title,Author Full Names,Source Title,Publication Year,DOI,"
        'Addresses,"Times Cited, WoS Core",Abstract\n'
        'J,"Rice, endosperm","Li, Wei; Wang, Mei",Rice Science,2024,10.1000/rice,'
        '"[Li, Wei; Wang, Mei] Univ A, China; Univ B, USA",0,"line 1\nline 2"\n'
    )
    records, fmt, encoding = reference_import.parse_reference_file(raw.encode())
    assert (fmt, encoding, len(records)) == ("wos-csv", "utf-8", 1)
    paper = records[0]
    assert paper["authors"] == ["Li, Wei", "Wang, Mei"]
    assert paper["affiliations"] == ["Univ A, China", "Univ B, USA"]
    assert paper["year"] == 2024 and paper["external_cited"] == 0
    assert paper["abstract"] == "line 1 line 2"


def test_pubmed_csv_and_enriched_csv_map_identity_without_guessing_absent_fields():
    original = (
        b"PMID,Title,Authors,Journal/Book,Publication Year,DOI\n"
        b'12345,Rice,"Li W, Wang M.",Rice Science,2024,10.1000/rice\n'
    )
    [paper], fmt, _ = reference_import.parse_reference_file(original)
    assert fmt == "pubmed-csv" and paper["pmid"] == "12345"
    assert paper["authors"] == ["Li W", "Wang M"]
    assert paper["abstract"] == "" and paper["language"] == ""
    enriched = (
        b"pmid,title,authors_full,journal_full,year,abstract,mesh_terms\n"
        b'12345,Rice,"Li, Wei; Wang, Mei",Rice Science,2024,Full abstract,Oryza/*genetics\n'
    )
    [paper], _, _ = reference_import.parse_reference_file(enriched)
    assert paper["authors"] == ["Li, Wei", "Wang, Mei"]
    assert paper["abstract"] == "Full abstract" and paper["keywords"] == ["Oryza"]


def test_cnki_html_xls_gbk_header_date_fallback_and_script_not_executed():
    html = (
        "<html><table><tr><th>Title-题名</th><th>Author-作者</th><th>PubTime-发表时间</th><th>Summary-摘要</th></tr>"
        "<tr><td>水稻 &amp; 胚乳</td><td>张伟; 李明</td><td>2026-06-11 11:12</td>"
        "<td><script>private()</script>研究<br>方法</td></tr></table></html>"
    )
    [paper], fmt, codec = reference_import.parse_reference_file(html.encode("gb18030"))
    assert fmt == "cnki-xls" and codec == "gb18030"
    assert paper["title"] == "水稻 & 胚乳" and paper["authors"] == ["张伟", "李明"]
    assert paper["year"] == 2026 and paper["abstract"] == "研究 方法"


def test_wanfang_xlsx_skips_export_group_banner_and_reads_all_rows():
    data = xlsx(
        [
            ["期刊"],
            [None, "序号", "作者", "题名", "刊名", "年", "关键词", "DOI"],
            [None, 1, "张晖.", "水稻胚乳", "现代农业科技,XIANDAI,2008(22)", 2008, "水稻, 胚乳, 基因", "10.1000/rice"],
            [None, 2, "李明, 王梅", "另一篇", "Rice", 2025, "发育; 调控", ""],
        ]
    )
    papers, fmt, encoding = reference_import.parse_reference_file(data)
    assert (len(papers), fmt, encoding) == (2, "wanfang-xlsx", "xlsx")
    assert papers[0]["journal"] == "现代农业科技" and papers[0]["authors"] == ["张晖"]
    assert papers[0]["keywords"] == ["水稻", "胚乳", "基因"]
    assert papers[1]["authors"] == ["李明", "王梅"]


@pytest.mark.parametrize("data", [b"Year,Count\n2024,99\n", b"12345\n67890\n"])
def test_non_catalog_statistics_or_pmid_only_file_is_rejected(data):
    with pytest.raises(ValueError, match="无法识别"):
        reference_import.parse_reference_file(data)


def test_mismatch_ragged_columns_duplicate_headers_and_xlsx_budget(monkeypatch):
    data = b"PMID,Title,Authors,Journal/Book\n12345,Rice,Li W,Rice\n"
    with pytest.raises(ValueError, match="不一致"):
        reference_import.parse_reference_file(data, fmt="wos-csv")
    with pytest.raises(ValueError, match="列数"):
        reference_import.parse_reference_file(data + b"67890,Rice\n")
    with pytest.raises(ValueError, match="表头重复"):
        reference_import.parse_reference_file(b"PMID,Title,Title,Journal/Book\n")
    bad = xlsx([["题名", "作者", "刊名"], ["Rice", "Li", "Rice"]], extra_sheet="不是题录")
    with pytest.raises(ValueError, match="不是文献清单"):
        reference_import.parse_reference_file(bad)
    monkeypatch.setattr(reference_tables, "MAX_XLSX_EXPANDED_BYTES", 1)
    with pytest.raises(ValueError, match="解压大小"):
        reference_import.parse_reference_file(bad)


def test_plain_zip_biff_and_formulas_are_not_executed():
    stream = io.BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        archive.writestr("data.txt", "not a workbook")
    with pytest.raises(ValueError, match="普通 ZIP"):
        reference_import.parse_reference_file(stream.getvalue())
    with pytest.raises(ValueError, match="二进制 XLS"):
        reference_import.parse_reference_file(b"\xd0\xcf\x11\xe0fake")
    records, _, _ = reference_import.parse_reference_file(
        xlsx([["题名", "作者", "刊名"], ['=HYPERLINK("https://invalid")', "Li", "Rice"]])
    )
    assert records[0]["title"] == ""  # no cached formula value, no execution/network


async def test_invalid_metadata_fails_before_any_database_write(monkeypatch):
    def unexpected():
        raise AssertionError("Untrusted invalid input must not open a transaction")

    monkeypatch.setattr(reference_import.pg_manager, "get_async_session_context", unexpected)
    for title in (b"x" * 4097, b"Rice\x00paper"):
        with pytest.raises(ValueError, match="安全预算"):
            await reference_import.import_references(1, b"PMID- 12345\nTI  - " + title + b"\n")
    with pytest.raises(ValueError, match="authors.*安全预算"):
        await reference_import.import_references(1, b"PMID- 12345\nTI  - Rice\nFAU - Name\x00bad\n")
