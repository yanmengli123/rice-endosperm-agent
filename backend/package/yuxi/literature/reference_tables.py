"""官网题录表格适配：真实 WoS/PubMed CSV、知网 HTML-XLS、万方 XLSX。

只读单元格值，不执行公式/宏/HTML，不抓取外链。原始字段保留其缺失语义；
转换后复用 reference_import 的身份、ACL 和事务入口。
"""

from __future__ import annotations

import csv
import io
import re
from html.parser import HTMLParser
from zipfile import BadZipFile, ZipFile

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

MAX_TABLE_ROWS = 50_000
MAX_TABLE_COLUMNS = 256
MAX_XLSX_EXPANDED_BYTES = 128 * 1024 * 1024


def parse_table_file(data: bytes, fmt: str = "auto") -> tuple[list[dict], str, str] | None:
    """返回规范记录/实际格式/编码；非表格文件返回 None 交给既有 tagged 入口。"""
    from yuxi.literature.reference_import import decode_reference_file

    if data.startswith(b"PK\x03\x04"):
        try:
            with ZipFile(io.BytesIO(data)) as archive:
                entries = archive.infolist()
                if len(entries) > 5000 or sum(item.file_size for item in entries) > MAX_XLSX_EXPANDED_BYTES:
                    raise ValueError("Excel 解压大小超过 128 MiB 安全预算")
                if any(item.flag_bits & 1 for item in entries) or "xl/workbook.xml" not in archive.namelist():
                    raise ValueError("只支持未加密的 XLSX 文献清单，不支持普通 ZIP/宏文件")
                if any(item.filename.lower().endswith("vbaproject.bin") for item in entries):
                    raise ValueError("文献清单不接受含宏的工作簿")
            book = load_workbook(io.BytesIO(data), read_only=True, data_only=True, keep_links=False)
            records = []
            actual = None
            try:
                for sheet in book.worksheets:
                    if sheet.max_column and sheet.max_column > MAX_TABLE_COLUMNS:
                        raise ValueError("Excel 列数超过 256 列安全预算")
                    rows = sheet.iter_rows(values_only=True)
                    header = None
                    nonempty = False
                    for index, cells in enumerate(rows):
                        values = [_text(value) for value in cells]
                        if not any(values):
                            continue
                        nonempty = True
                        if header is None:
                            candidate = _table_format(values)
                            if candidate:
                                header, actual = _headers(values), candidate
                                _check_format(fmt, actual)
                            elif index >= 9:
                                raise ValueError(f"工作表 {sheet.title} 的前 10 行没有可识别文献表头")
                            continue
                        if _table_format(values):  # 官网导出可能在分组间重复表头
                            continue
                        record = _table_record(dict(zip(header, values)), actual)
                        record["source_row"] = index + 1
                        record["source_sheet"] = sheet.title
                        records.append(record)
                        if len(records) > MAX_TABLE_ROWS:
                            raise ValueError("单次表格导入超过 50000 条，请分批导出")
                    if header is None and nonempty:
                        raise ValueError(f"工作表 {sheet.title} 不是文献清单，请移除统计/说明工作表后重试")
            finally:
                book.close()
            if not actual:
                raise ValueError("Excel 没有文献清单表头（需题名、作者和刊名等字段）")
            return records, actual, "xlsx"
        except (BadZipFile, OSError, KeyError, InvalidFileException, SyntaxError) as exc:
            raise ValueError("XLSX 文件结构损坏，请重新导出") from exc
    if data.startswith(b"\xd0\xcf\x11\xe0"):
        raise ValueError("当前支持知网 HTML-XLS 和 XLSX；二进制 XLS 请在 Excel 中另存为 XLSX 再上传")
    text, encoding = decode_reference_file(data)
    if re.search(r"<html\b|<table\b", text[:4096], re.IGNORECASE):
        parser = _HtmlTable()
        parser.feed(text)
        if not parser.rows:
            raise ValueError("知网 XLS 未发现表格记录")
        actual = _table_format(parser.rows[0])
        if actual != "cnki-xls":
            raise ValueError("HTML-XLS 需包含知网导出的 Title-题名/Author-作者表头")
        _check_format(fmt, actual)
        header = _headers(parser.rows[0])
        records = []
        for number, cells in enumerate(parser.rows[1:], 2):
            if len(cells) != len(header):
                raise ValueError(f"知网 XLS 第 {number} 行列数与表头不一致")
            record = _table_record(dict(zip(header, cells, strict=True)), actual)
            record["source_row"] = number
            records.append(record)
        return records, actual, encoding
    first = text.lstrip().split("\n", 1)[0]
    if "," not in first and "\t" not in first:
        return None
    delimiter = "\t" if "\t" in first else ","
    try:
        reader = csv.reader(io.StringIO(text.lstrip()), delimiter=delimiter, strict=True)
        header = _headers(next(reader))
        actual = _table_format(header)
        if not actual:
            return None
        _check_format(fmt, actual)
        records = []
        for values in reader:
            if not any(value.strip() for value in values):
                continue
            if len(values) != len(header):
                raise ValueError(f"文献 CSV 第 {reader.line_num} 行列数与表头不一致")
            record = _table_record(dict(zip(header, values, strict=True)), actual)
            record["source_row"] = reader.line_num
            records.append(record)
            if len(records) > MAX_TABLE_ROWS:
                raise ValueError("单次表格导入超过 50000 条，请分批导出")
        return records, actual, encoding
    except (csv.Error, StopIteration) as exc:
        raise ValueError("文献 CSV 引号/表头损坏，或单元格超过安全预算，请重新导出") from exc


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def _headers(values: list[str]) -> list[str]:
    headers = [re.sub(r"\s+", " ", _text(value)).casefold() for value in values]
    named = [value for value in headers if value]
    if len(set(named)) != len(named) or len(headers) > MAX_TABLE_COLUMNS:
        raise ValueError("文献表头重复或超过 256 列安全预算")
    return headers


def _table_format(values: list[str]) -> str | None:
    keys = {re.sub(r"\s+", " ", _text(value)).casefold() for value in values}
    if {"article title", "publication type"} <= keys:
        return "wos-csv"
    if {"pmid", "title", "journal/book"} <= keys or {"pmid", "title", "journal_full"} <= keys:
        return "pubmed-csv"
    if {"title-题名", "author-作者"} <= keys:
        return "cnki-xls"
    if {"题名", "作者", "刊名"} <= keys:
        return "wanfang-xlsx"
    return None


def _check_format(requested: str, actual: str) -> None:
    if requested not in ("auto", actual):
        raise ValueError(f"指定格式 {requested} 与实际表头 {actual} 不一致，请选择自动识别")


def _table_record(values: dict[str, str], fmt: str) -> dict:
    record = _normalized_table_record(values, fmt)
    record["source_fields"] = {
        key: values.get(key, "")
        for key in (
            "ut (unique wos id)", "clc-中图分类号", "中图分类号", "addresses",
            "document type", "srcdatabase-来源库", "publication year", "year-年", "年", "pubtime-发表时间",
        )
        if values.get(key)
    }
    return record


def _normalized_table_record(values: dict[str, str], fmt: str) -> dict:
    from yuxi.literature.reference_import import (
        _bib_year,
        _collapse,
        _dedupe,
        _kind_from_text,
        _nbib_record,
        _normalize_language,
        _parse_issn,
        _split_list,
        _TEXT_KIND_HINTS,
        _validated_doi,
        _wos_record,
    )

    if fmt == "wos-csv":
        mapping = {
            "PT": "publication type",
            "TI": "article title",
            "AU": "authors",
            "AF": "author full names",
            "SO": "source title",
            "SN": "issn",
            "PY": "publication year",
            "DI": "doi",
            "PM": "pubmed id",
            "DE": "author keywords",
            "ID": "keywords plus",
            "AB": "abstract",
            "LA": "language",
            "DT": "document type",
            "C1": "addresses",
            "WC": "wos categories",
            "SC": "research areas",
            "CR": "cited references",
        }
        raw = {}
        for tag, column in mapping.items():
            value = values.get(column, "")
            if tag == "C1":
                raw[tag] = re.split(r"[;；](?![^\[]*\])", value)
            else:
                raw[tag] = _split_list([value]) if tag in {"AU", "AF", "CR"} else [value]
        result = _wos_record(raw)
        cited = values.get("times cited, wos core", "").replace(",", "")
        result["external_cited"] = int(cited) if cited.isdecimal() else None
        return result
    if fmt == "pubmed-csv":
        full_authors = values.get("authors_full", "")
        authors = _split_list([full_authors]) if full_authors else re.split(r",\s*", values.get("authors", ""))
        keywords = _split_list([values.get("keywords", "")])
        keywords += [value.split("/")[0].lstrip("*") for value in _split_list([values.get("mesh_terms", "")])]
        result = _nbib_record(
            {
                "TI": [values.get("title", "")],
                "FAU": [value.strip().rstrip(".") for value in authors if value.strip()],
                "JT": [values.get("journal/book") or values.get("journal_full") or values.get("journal", "")],
                "DP": [values.get("publication year") or values.get("year") or values.get("publication_date", "")],
                "PMID": [values.get("pmid", "")],
                "AB": [values.get("abstract", "")],
                "LA": [values.get("language", "")],
                "PT": [values.get("publication_types", "")],
                "OT": keywords,
            }
        )
        result["doi"] = _validated_doi(values.get("doi", ""))
        return result
    cnki = fmt == "cnki-xls"
    names = (
        ("title-题名", "author-作者", "source-文献来源", "year-年", "keyword-关键词", "summary-摘要", "organ-单位")
        if cnki
        else ("题名", "作者", "刊名", "年", "关键词", "摘要", "作者单位")
    )
    title, authors, journal, year, keywords, abstract, affiliations = [values.get(key, "") for key in names]
    if not year and cnki:
        year = values.get("pubtime-发表时间", "")
    split_pattern = r"[;；]" if cnki else r"[;；,，、]"
    cnki_url = values.get("url-网址", "")
    cnki_key = re.search(r"[?&]filename=([A-Za-z0-9]+)", cnki_url)
    return {
        "title": _collapse(title),
        "authors": _dedupe(
            [_collapse(value).rstrip(".") for value in re.split(split_pattern, authors) if value.strip()]
        ),
        "journal": _collapse(journal if cnki else re.split(r"[,，]", journal)[0]),
        "issn": _parse_issn(values.get("issn-国际标准刊号" if cnki else "issn", "")),
        "year": _bib_year(year),
        "doi": _validated_doi(values.get("doi-doi" if cnki else "doi", "")),
        "pmid": "",
        "keywords": _dedupe([_collapse(value) for value in re.split(split_pattern, keywords) if value.strip()]),
        "abstract": _collapse(abstract),
        "language": _normalize_language(values.get("language", "")),
        "kind": _kind_from_text(values.get("srcdatabase-来源库", ""), _TEXT_KIND_HINTS),
        "affiliations": _dedupe(
            [_collapse(value).strip(" ,") for value in re.split(r"[;；]", affiliations) if value.strip(" ,")]
        ),
        "cited_dois": [],
        "cnki_id": cnki_key.group(1) if cnki_key else "",
    }


class _HtmlTable(HTMLParser):
    """读取文本单元格而非渲染 HTML；拒绝合并单元格导致的列漂移。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.row, self.cell = [], None, None
        self.ignored = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.ignored += 1
        if self.ignored:
            return
        if tag == "tr":
            self.row = []
        if tag in {"td", "th"} and self.row is not None:
            if any(key in {"rowspan", "colspan"} and value not in {None, "1"} for key, value in attrs):
                raise ValueError("知网表格包含合并数据单元格，请重新导出平面文献清单")
            self.cell = []
        if tag == "br" and self.cell is not None:
            self.cell.append(" ")

    def handle_data(self, value):
        if not self.ignored and self.cell is not None:
            self.cell.append(value)

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.ignored = max(0, self.ignored - 1)
        if tag in {"td", "th"} and self.cell is not None:
            self.row.append("".join(self.cell).strip())
            self.cell = None
        if tag == "tr" and self.row is not None:
            if any(self.row):
                self.rows.append(self.row)
                if len(self.rows) > MAX_TABLE_ROWS + 1:
                    raise ValueError("单次表格导入超过 50000 条，请分批导出")
            self.row = None
