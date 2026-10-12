"""文献清单导入（PubMed / WoS / RIS / EndNote 四族官网导出格式）。

设计要点（docs/vibe/2026-10-12-literature-reference-import.md）：
- 编码显式探测：BOM（4 字节优先于 2 字节）→ UTF-8 严格 → GB18030 严格，
  全部失败即 ValueError——绝不 ``errors="replace"`` 静默乱码入库污染计量；
- 格式嗅探按内容特征（扩展名不可信）+ ``format`` 参数显式覆盖；
- 一个参数化 tagged 引擎 + 每格式一张规范（标签行/续行/记录边界/字段映射）；
  续行以 ``\\n`` 并入上一标签值，列表型字段（作者/机构/引用）按行拆分；
- 身份口径与 ``bib_import`` 完全一致：DOI→``doi-`` RID、无 DOI→
  ``nd-md5(title|year)`` 指纹（跨通道重导命中同一条）；PMID 仅列级查重，
  绝不进 ``make_rid``（字节兼容红线）；知网 %U 详情页 filename 提取 CNKI 键；
- 去重预取批量执行（大清单不做逐条 SELECT）；报告结构与 ``import_bib`` 对齐。
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, insert, or_, select
from sqlalchemy.orm import Session

from yuxi.literature.access import current_access
from yuxi.literature import bib_import
from yuxi.literature.bib_import import _bib_year, _parse_cited_dois
from yuxi.literature.identity.rid import doi_to_dirname, is_doi, make_rid, normalize_doi
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    LiteratureCitation,
    LiteraturePaper,
    LiteratureReferenceBatch,
    LiteratureReferenceMember,
    LiteratureReferenceRecord,
)

#: 单次导入记录数安全预算（对齐 journals.MAX_JOURNAL_ROWS 的失败关闭先例）。
MAX_REFERENCE_RECORDS = 50_000
MAX_REFERENCE_BYTES = 64 * 1024 * 1024

# ---------------------------------------------------------------- 编码与嗅探

#: BOM 判定必须 4 字节优先：UTF-32LE 的 \xff\xfe\x00\x00 前缀即 UTF-16LE BOM。
_BOM_CODECS: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xfe\x00\x00", "utf-32"),
    (b"\x00\x00\xfe\xff", "utf-32"),
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe", "utf-16"),
    (b"\xfe\xff", "utf-16"),
)


def decode_reference_file(data: bytes) -> tuple[str, str]:
    """清单文件解码：BOM → UTF-8 严格 → GB18030（GBK 超集）严格，失败即拒绝。"""
    for bom, codec in _BOM_CODECS:
        if data.startswith(bom):
            try:
                return data.decode(codec), codec
            except UnicodeDecodeError as exc:
                raise ValueError("文件带 BOM 但内容解码失败，请从官网重新导出") from exc
    for codec in ("utf-8", "gb18030"):
        try:
            return data.decode(codec), codec
        except UnicodeDecodeError:
            continue
    raise ValueError("无法识别文件编码（支持 UTF-8/UTF-16/UTF-32/GBK 系列），请从官网重新导出")


def detect_format(text: str) -> str:
    """内容嗅探清单格式；BibTeX 自动复用既有 bib 领域入口。"""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines and "\t" in lines[0]:
        header = {value.strip().strip('"') for value in lines[0].split("\t")}
        if {"PT", "TI"} <= header:
            return "wos-txt"
    if lines and lines[0].upper().startswith("FN ") and any(line.upper().startswith("VR ") for line in lines[1:3]):
        return "wos-txt"
    if re.search(r"^PMID- ", text, re.MULTILINE):
        return "pubmed-nbib"
    if re.search(r"^TY\s+-\s", text, re.MULTILINE):
        return "ris"
    if re.search(r"^%[^%\s]", text, re.MULTILINE):
        return "endnote"
    if re.search(r"^@\w+\{", text, re.MULTILINE):
        return "bibtex"
    raise ValueError(
        "无法识别文献清单格式（支持 PubMed/WoS CSV、MEDLINE、知网 HTML-XLS、万方 XLSX、RIS/EndNote）；"
        "仅 PMID 列表、趋势统计或摘要展示不是完整题录"
    )


# ---------------------------------------------------------------- tagged 引擎


@dataclass(frozen=True)
class TaggedSpec:
    """一种导出格式 = 一张本表；引擎共用（``_split_tagged``）。"""

    name: str  # source_main 取值
    tag_re: re.Pattern[str]  # 标签行（组1=标签，组2=值）
    starts: tuple[str, ...]  # 记录起始标签
    ends: tuple[str, ...]  # 记录结束标签
    cont_re: re.Pattern[str] | None  # 缩进续行（None 时按 cont_any 全收）
    cont_any: bool = False  # 记录内任意非标签行都是续行（RIS/EndNote 变体）
    blank_ends_record: bool = False  # 空行分隔记录（nbib/EndNote）


def _split_tagged(text: str, spec: TaggedSpec) -> list[dict[str, list[str]]]:
    """tagged 文本 → 原始记录列表（标签 → 值列表；续行以 ``\\n`` 并入上一值）。"""
    records: list[dict[str, list[str]]] = []
    current: dict[str, list[str]] | None = None
    last_tag: str | None = None
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = line.rstrip()
        if not stripped.strip():
            if spec.blank_ends_record and current is not None:
                records.append(current)
                current, last_tag = None, None
            continue
        match = spec.tag_re.match(stripped)
        if match is not None:
            tag, value = match.group(1), (match.group(2) or "").strip()
            if tag in spec.ends:
                if current is not None:
                    records.append(current)
                current, last_tag = None, None
                continue
            if tag in spec.starts:
                if current is not None:
                    records.append(current)
                current, last_tag = {}, None
            if current is None:
                continue  # 首记录前的文件头（WoS FN/VR 等）
            current.setdefault(tag, []).append(value)
            last_tag = tag
        elif current is not None and last_tag is not None and (spec.cont_any or spec.cont_re.match(stripped)):
            current[last_tag][-1] += "\n" + stripped.strip()
    if current is not None:
        records.append(current)
    return records


# ---------------------------------------------------------------- 归一化装配


def _collapse(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip()


def _first(raw: dict[str, list[str]], *tags: str) -> str:
    for tag in tags:
        values = raw.get(tag)
        if values:
            head = _collapse(values[0])
            if head:
                return head
    return ""


def _all(raw: dict[str, list[str]], *tags: str) -> list[str]:
    """列表型字段：值内按行拆（WoS 多作者/多地址走续行）、去空保序。"""
    out: list[str] = []
    for tag in tags:
        for value in raw.get(tag, ()):
            for part in value.split("\n"):
                text = _collapse(part)
                if text:
                    out.append(text)
    return out


def _split_list(values: list[str]) -> list[str]:
    """关键词等分隔字段：行内按 ;/；拆（中文导出用全角分号）。"""
    out: list[str] = []
    for value in values:
        for part in re.split(r"[;；]", value):
            part = part.strip()
            if part:
                out.append(part)
    return out


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _validated_doi(value: str) -> str:
    candidate = normalize_doi(value)
    return candidate if is_doi(candidate) else ""


def _parse_issn(value: str) -> str:
    match = re.search(r"\b\d{4}-\d{3}[\dXx]\b", value or "")
    return match.group(0) if match else ""


_LANGUAGE_MAP = {
    "eng": "en",
    "en": "en",
    "english": "en",
    "chi": "zh",
    "zh": "zh",
    "zh-cn": "zh",
    "chinese": "zh",
    "中文": "zh",
    "汉语": "zh",
    "jpn": "ja",
    "ja": "ja",
    "japanese": "ja",
    "日语": "ja",
    "fre": "fr",
    "fra": "fr",
    "fr": "fr",
    "french": "fr",
    "ger": "de",
    "deu": "de",
    "de": "de",
    "german": "de",
    "spa": "es",
    "es": "es",
    "spanish": "es",
    "kor": "ko",
    "ko": "ko",
    "korean": "ko",
    "朝鲜语": "ko",
    "rus": "ru",
    "ru": "ru",
    "russian": "ru",
}


def _normalize_language(value: str) -> str:
    key = (value or "").strip().lower().rstrip(".")
    return _LANGUAGE_MAP.get(key, key[:16])


#: 文本型类型提示（nbib PT / WoS DT / EndNote %0），按序子串匹配。
_TEXT_KIND_HINTS: tuple[tuple[str, str], ...] = (
    ("chapter", "chapter"),
    ("book", "book"),
    ("dissertation", "thesis"),
    ("thesis", "thesis"),
    ("patent", "patent"),
    ("report", "report"),
)
_RIS_KIND_HINTS: tuple[tuple[str, str], ...] = (
    ("chap", "chapter"),
    ("book", "book"),
    ("thes", "thesis"),
    ("pat", "patent"),
    ("rprt", "report"),
    ("rprt", "report"),
)
_WOS_PT_KINDS = {"J": "paper", "C": "paper", "S": "paper", "B": "book", "P": "patent"}


def _kind_from_text(value: str, hints: tuple[tuple[str, str], ...]) -> str:
    lowered = (value or "").lower()
    for token, kind in hints:
        if token in lowered:
            return kind
    return "paper"


def _nbib_record(raw: dict[str, list[str]]) -> dict[str, Any]:
    doi = ""
    for aid in [*raw.get("AID", ()), *raw.get("LID", ())]:
        text = _collapse(aid)
        if text.endswith("[doi]"):
            candidate = normalize_doi(text.removesuffix("[doi]"))
            if is_doi(candidate):
                doi = candidate
                break
    # MeSH 取主词（"Oryza/*genetics" → "Oryza"，限定符是检索面不是概念面）
    keywords = _split_list(_all(raw, "OT"))
    keywords += [text for value in _all(raw, "MH") if (text := value.split("/")[0].lstrip("*").strip())]
    return {
        "title": _first(raw, "TI"),
        "authors": _all(raw, "FAU") or _all(raw, "AU"),
        "journal": _first(raw, "JT", "TA"),
        "issn": _parse_issn(_first(raw, "IS")),
        "year": _bib_year(_first(raw, "DP", "PY")),
        "doi": doi,
        "pmid": _first(raw, "PMID"),
        "keywords": keywords,
        "abstract": _first(raw, "AB"),
        "language": _normalize_language(_first(raw, "LA")),
        "kind": _kind_from_text(_first(raw, "PT"), _TEXT_KIND_HINTS),
        # AD 续行是同一地址的折行，不是另一家机构。
        "affiliations": _dedupe([_collapse(value) for value in raw.get("AD", ()) if _collapse(value)]),
        "cited_dois": [],
        "cnki_id": "",
    }


def _wos_record(raw: dict[str, list[str]]) -> dict[str, Any]:
    affiliations: list[str] = []
    for value in raw.get("C1", ()):
        for block in value.split("\n"):
            block = re.sub(r"^\[[^\]]*\]\s*", "", block.strip()).strip(" .;,")
            if block:
                affiliations.append(block)
    kind = _kind_from_text(_first(raw, "DT"), _TEXT_KIND_HINTS)
    if kind == "paper" and not _first(raw, "DT"):
        kind = _WOS_PT_KINDS.get(_first(raw, "PT"), "paper")
    return {
        "title": _first(raw, "TI"),
        "authors": _all(raw, "AF") or _all(raw, "AU"),
        "journal": _first(raw, "SO"),
        "issn": _parse_issn(_first(raw, "SN")),
        "year": _bib_year(_first(raw, "PY")),
        "doi": _validated_doi(_first(raw, "DI")),
        "pmid": _first(raw, "PM"),
        "keywords": _split_list(_all(raw, "DE", "ID")),
        "abstract": _first(raw, "AB"),
        "language": _normalize_language(_first(raw, "LA")),
        "kind": kind,
        "affiliations": _dedupe(affiliations),
        "wos_categories": _split_list(_all(raw, "WC")),
        "research_areas": _split_list(_all(raw, "SC")),
        "cited_dois": _parse_cited_dois("\n".join(raw.get("CR", ()))),
        "cnki_id": "",
    }


def _ris_record(raw: dict[str, list[str]]) -> dict[str, Any]:
    return {
        "title": _first(raw, "TI", "T1"),
        "authors": _all(raw, "AU", "A1"),
        "journal": _first(raw, "T2", "JO", "JF"),
        "issn": _parse_issn(_first(raw, "SN")),
        "year": _bib_year(_first(raw, "PY", "Y1", "DA")),
        "doi": _validated_doi(_first(raw, "DO")),
        "pmid": "",
        "keywords": _split_list(_all(raw, "KW")),
        "abstract": _first(raw, "AB", "N2"),
        "language": _normalize_language(_first(raw, "LA")),
        "kind": _kind_from_text(_first(raw, "TY"), _RIS_KIND_HINTS),
        "affiliations": _dedupe(_all(raw, "AD")),
        "cited_dois": [],
        "cnki_id": "",
    }


_CNKI_URL_KEY_RE = re.compile(r"[?&]filename=([A-Za-z0-9]+)")


def _endnote_record(raw: dict[str, list[str]]) -> dict[str, Any]:
    cnki_id = ""
    for url in _all(raw, "U"):
        match = _CNKI_URL_KEY_RE.search(url)
        if match:
            cnki_id = match.group(1)
            break
    return {
        "title": _first(raw, "T"),
        "authors": _all(raw, "A"),
        "journal": _first(raw, "J", "B"),
        "issn": _parse_issn(_first(raw, "@")),
        "year": _bib_year(_first(raw, "D")),
        "doi": _validated_doi(_first(raw, "R")),
        "pmid": "",
        "keywords": _split_list(_all(raw, "K")),
        "abstract": _first(raw, "X"),
        "language": _normalize_language(_first(raw, "G")),
        "kind": _kind_from_text(_first(raw, "0"), _TEXT_KIND_HINTS),
        # %W 是 Database Provider，不是作者机构；只接收明确地址标签。
        "affiliations": _dedupe([_collapse(value) for value in raw.get("+", ()) if _collapse(value)]),
        "cited_dois": [],
        "cnki_id": cnki_id,
    }


@dataclass(frozen=True)
class _Format:
    spec: TaggedSpec
    build: Callable[[dict[str, list[str]]], dict[str, Any]]


FORMATS: dict[str, _Format] = {
    "pubmed-nbib": _Format(
        TaggedSpec(
            name="pubmed-nbib",
            tag_re=re.compile(r"^([A-Z][A-Z0-9]{1,3})\s*-\s?(.*)$"),
            starts=("PMID",),
            ends=(),
            cont_re=re.compile(r"^\s{4,}\S.*$"),
            blank_ends_record=True,
        ),
        _nbib_record,
    ),
    "wos-txt": _Format(
        TaggedSpec(
            name="wos-txt",
            # WoS 记录尾行 "ER" 无尾随空格，值需可选
            tag_re=re.compile(r"^([A-Z0-9]{2})(?: (.*))?$"),
            starts=("PT",),
            ends=("ER",),
            cont_re=re.compile(r"^\s{3,}\S.*$"),
        ),
        _wos_record,
    ),
    "ris": _Format(
        TaggedSpec(
            name="ris",
            # 部分导出器写 "ER  -"（无尾随空格），值侧空白可选
            tag_re=re.compile(r"^([A-Z0-9]{2})\s+-\s?(.*)$"),
            starts=("TY",),
            ends=("ER",),
            cont_re=None,
            cont_any=True,
        ),
        _ris_record,
    ),
    "endnote": _Format(
        TaggedSpec(
            name="endnote",
            # EndNote tagged：标签是 % 后单字符（%0 类型、%A 作者、%+ 机构、%? 变体…）
            tag_re=re.compile(r"^%([^%\s])\s*(.*)$"),
            starts=("0",),
            ends=(),
            cont_re=None,
            cont_any=True,
            blank_ends_record=True,
        ),
        _endnote_record,
    ),
}


def parse_references(text: str, fmt: str) -> list[dict[str, Any]]:
    """清单文本 → 归一化记录列表（字段缺失容忍：空值按 None/空列表落库）。"""
    entry = FORMATS.get(fmt)
    if entry is None:
        raise ValueError(f"不支持的清单格式: {fmt}")
    if fmt == "wos-txt" and "\t" in text.lstrip().split("\n", 1)[0]:
        return _parse_wos_table(text)
    return [entry.build(raw) for raw in _split_tagged(text, entry.spec)]


def _parse_wos_table(text: str) -> list[dict[str, Any]]:
    """WoS Tab-delimited：CSV 引擎保留引号内 tab/换行，列表单元格按分号拆。"""
    try:
        reader = csv.reader(io.StringIO(text.lstrip()), delimiter="\t", strict=True)
        header = [value.strip() for value in next(reader)]
        if not {"PT", "TI"} <= set(header) or len(set(header)) != len(header):
            raise ValueError("WoS Tab-delimited 表头需包含唯一的 PT/TI 列")
        records = []
        for cells in reader:
            if not any(value.strip() for value in cells):
                continue
            if len(cells) != len(header):
                raise ValueError(f"WoS Tab-delimited 第 {reader.line_num} 行列数与表头不一致")
            raw = {}
            for tag, value in zip(header, cells, strict=True):
                if tag == "C1":
                    # 方括号内是多个作者，不可把其分号误拆成多个机构。
                    raw[tag] = re.split(r"[;；](?![^\[]*\])", value)
                else:
                    raw[tag] = _split_list([value]) if tag in {"AU", "AF", "CR"} else [value]
            records.append(_wos_record(raw))
            if len(records) > MAX_REFERENCE_RECORDS:
                raise ValueError(f"单次导入超过 {MAX_REFERENCE_RECORDS} 条安全预算，请分批导出")
        return records
    except (csv.Error, StopIteration) as exc:
        raise ValueError("WoS Tab-delimited 引号或表头损坏，请重新导出") from exc


# ---------------------------------------------------------------- 入库


async def _prefetch_identities(
    tenant_id: int, prepared: list[dict[str, Any]], session: Session
) -> tuple[dict[str, dict[str, str]], dict[str, LiteraturePaper]]:
    """批量预取既有身份（rid/doi/pmid 三键合一查，分块防超绑定参数上限）。"""
    rids = sorted({rec["_rid"] for rec in prepared})
    dois = sorted({rec["doi"] for rec in prepared if rec["doi"]})
    pmids = sorted({rec["pmid"] for rec in prepared if rec["pmid"]})
    existing: dict[str, dict[str, str]] = {"rid": {}, "doi": {}, "pmid": {}}
    papers: dict[str, LiteraturePaper] = {}
    for start in range(0, max(len(rids), len(dois), len(pmids), 1), 1000):
        conditions = [
            column.in_(values)
            for values, column in (
                (rids[start : start + 1000], LiteraturePaper.rid),
                (dois[start : start + 1000], LiteraturePaper.doi),
                (pmids[start : start + 1000], LiteraturePaper.pmid),
            )
            if values
        ]
        if not conditions:
            break
        rows = (
            (
                await session.execute(
                    select(LiteraturePaper).where(LiteraturePaper.tenant_id == tenant_id, or_(*conditions))
                )
            )
            .scalars()
            .all()
        )
        for paper in rows:
            rid, doi, pmid = paper.rid, paper.doi, paper.pmid
            papers[rid] = paper
            for name, key in (("rid", rid), ("doi", doi), ("pmid", pmid)):
                if not key:
                    continue
                if key in existing[name] and existing[name][key] != rid:
                    raise ValueError("导入记录身份冲突，请先核对目录中的 DOI/PMID")
                existing[name][key] = rid
    return existing, papers


async def import_references(tenant_id: int, data: bytes, *, fmt: str = "auto", source_name: str = "") -> dict[str, Any]:
    """文献清单导入：解码 → 嗅探 → 解析 → 幂等入库（DOI/PMID/指纹三查链）+ WoS 引用边。"""
    if len(data) > MAX_REFERENCE_BYTES:
        raise ValueError("文献清单超过 64 MiB 安全预算，请分批导出")
    scope = current_access()
    if scope is not None and scope.tenant_id != tenant_id:
        raise ValueError("导入租户与当前访问上下文不一致")
    # Local parsing can be CPU-heavy for large CSV/XLSX; keep it off the API event loop.
    payload, format_detected, encoding = await asyncio.to_thread(parse_reference_file, data, fmt=fmt)
    if format_detected == "bibtex":
        # 新 UI 仍接收 .bib：严格解码后复用既有 BibTeX 领域入口，不改旧路由/RID。
        report = await bib_import.import_bib(tenant_id, payload, source_name=source_name)
        return {**report, "format_detected": "bibtex", "encoding": encoding}
    records = payload
    if len(records) > MAX_REFERENCE_RECORDS:
        raise ValueError(f"单次导入超过 {MAX_REFERENCE_RECORDS} 条安全预算，请分批导出")
    if sum(len(rec["cited_dois"]) for rec in records) > 200_000:
        raise ValueError("单次清单的 DOI 引用边超过 200000 条安全预算，请分批导出")
    # 有效记录底线：题名/DOI/PMID/CNKI 键至少占一（均缺即无身份无展示价值）
    for number, rec in enumerate(records, 1):
        rec["record_no"] = number
    valid = [rec for rec in records if rec["title"] or rec["doi"] or rec["pmid"] or rec["cnki_id"]]
    if not valid:
        raise ValueError("未识别到任何有效文献记录，请确认导出格式（PubMed .nbib / WoS Tab-delimited / RIS / EndNote）")
    report = {
        "source": source_name,
        "format_detected": format_detected,
        "encoding": encoding,
        "records": len(records),
        "no_identity": len(records) - len(valid),
        "papers_written": 0,
        "papers_skipped": 0,
        "citations_written": 0,
        "metadata_enriched": 0,
        "quality": {
            f"missing_{field}": sum(not rec.get(field) for rec in valid)
            for field in ("title", "year", "authors", "abstract", "keywords", "doi")
        },
    }
    # 身份计算：DOI→doi-*（与 bib 字节一致）；CNKI 键→cnki-*；否则 nd-md5(title|year) 指纹
    prepared: list[dict[str, Any]] = []
    for rec in valid:
        for field, limit in (("title", 4096), ("journal", 512), ("doi", 255), ("pmid", 32), ("abstract", 128 * 1024)):
            value = rec.get(field) or ""
            if "\x00" in value or len(value) > limit:
                raise ValueError(f"题录字段 {field} 含空字符或超过 {limit} 字符安全预算，请核对原始导出")
        for field, limit in (
            ("authors", 512),
            ("keywords", 4096),
            ("affiliations", 16384),
            ("wos_categories", 512),
            ("research_areas", 512),
            ("cited_dois", 255),
        ):
            if len(rec.get(field, [])) > 10000 or any(
                "\x00" in value or len(value) > limit for value in rec.get(field, [])
            ):
                raise ValueError(f"题录字段 {field} 含空字符或超过安全预算，请核对原始导出")
        if is_doi(rec["doi"]):
            rid = f"doi-{doi_to_dirname(rec['doi'])}"
        elif rec["cnki_id"]:
            rid = make_rid(rec["kind"], cnki=rec["cnki_id"])
        else:
            identity = f"{rec['title']}|{rec['year'] or ''}" if rec["title"] else f"pmid:{rec['pmid']}"
            fingerprint = hashlib.md5(identity.encode()).hexdigest()
            rid = make_rid(rec["kind"], fingerprint=fingerprint)
        prepared.append({**rec, "_rid": rid})

    async with pg_manager.get_async_session_context() as session:
        # 同租户清单导入串行查重；事务结束自动释放，不把并发请求挂到长队列。
        if session.get_bind().dialect.name == "postgresql":
            locked = await session.scalar(
                select(func.pg_try_advisory_xact_lock(func.hashtextextended(f"literature:references:{tenant_id}", 0)))
            )
            if not locked:
                raise ValueError("同租户文献清单正在导入，请稍后重试")
        existing, papers = await _prefetch_identities(tenant_id, prepared, session)
        created_rids: set[str] = set()
        for record_index, rec in enumerate(prepared, 1):
            rid = rec["_rid"]
            # DOI/PMID/CNKI are explicit identities; a title-year fingerprint is
            # weaker. A CSV may omit MEDLINE's terminal period and match another
            # unlabelled WoS row: never let that weak match defeat a known PMID.
            matches = {
                existing[name][key]
                for name, key in (("doi", rec["doi"]), ("pmid", rec["pmid"]))
                if key and key in existing[name]
            }
            if (rec["doi"] or rec["cnki_id"]) and rid in existing["rid"]:
                matches.add(existing["rid"][rid])
            if len(matches) > 1:
                raise ValueError(f"第 {record_index} 条有效题录身份冲突：DOI/PMID 指向不同文献，请核对后重试")
            if not matches and rid in existing["rid"]:
                candidate = papers[existing["rid"][rid]]
                if any(
                    old and new and old != new
                    for old, new in ((candidate.doi, rec["doi"]), (candidate.pmid, rec["pmid"]))
                ):
                    raise ValueError(f"第 {record_index} 条有效题录题名指纹与已有 DOI/PMID 冲突，请核对后重试")
                matches.add(candidate.rid)
            if matches:
                canonical_rid = matches.pop()
                if scope is not None and (
                    scope.tenant_id != tenant_id
                    or (canonical_rid not in scope.rids and canonical_rid not in created_rids)
                ):
                    raise ValueError("导入记录与不可访问来源冲突")
                rec["_rid"] = canonical_rid
                report["papers_skipped"] += 1
                paper = papers[canonical_rid]
                # A single matching identifier does not authorize attaching a
                # contradictory identifier, including when metadata is locked.
                if any(old and new and old != new for old, new in ((paper.doi, rec["doi"]), (paper.pmid, rec["pmid"]))):
                    raise ValueError(
                        f"第 {record_index} 条有效题录身份字段冲突：DOI/PMID 与已有文献不一致，请核对后重试"
                    )
                # Reuploading complete MEDLINE after a minimal CSV may fill
                # missing metadata; never replace populated fields or manual locks.
                enriched = False
                if not paper.meta_locked:
                    for column, value in (
                        ("title", rec["title"]),
                        ("doi", rec["doi"]),
                        ("pmid", rec["pmid"]),
                        ("authors_json", rec["authors"]),
                        ("abstract", rec["abstract"]),
                        ("journal", rec["journal"]),
                        ("issn", rec["issn"]),
                        ("pub_year", rec["year"]),
                        ("keywords_json", rec["keywords"]),
                        ("language", rec["language"]),
                        ("affiliations_json", rec["affiliations"]),
                        ("external_cited", rec.get("external_cited")),
                    ):
                        if getattr(paper, column) in (None, "", []) and value not in (None, "", []):
                            setattr(paper, column, value)
                            enriched = True
                report["metadata_enriched"] += int(enriched)
            else:
                paper = LiteraturePaper(
                    tenant_id=tenant_id,
                    rid=rid,
                    kind=rec["kind"],
                    doi=rec["doi"] or None,
                    title=rec["title"] or None,
                    pmid=rec["pmid"] or None,
                    language=rec["language"] or None,
                    source_main=format_detected,
                    authors_json=rec["authors"] or None,
                    abstract=rec["abstract"] or None,
                    journal=rec["journal"] or None,
                    issn=rec["issn"] or None,
                    pub_year=rec["year"],
                    keywords_json=rec["keywords"] or None,
                    wos_categories_json=rec.get("wos_categories") or None,
                    research_areas_json=rec.get("research_areas") or None,
                    affiliations_json=rec["affiliations"] or None,
                    external_cited=rec.get("external_cited"),
                )
                session.add(paper)
                papers[rid] = paper
                created_rids.add(rid)
                report["papers_written"] += 1
            # 本次导入内的别名也指向真实 RID；引用边不能挂到被跳过的候选 RID。
            for name, key in (("rid", rid), ("doi", rec["doi"]), ("pmid", rec["pmid"])):
                if key:
                    if name == "rid" and key in existing[name] and existing[name][key] != rec["_rid"]:
                        continue  # do not redirect an existing weak identity to a different explicit one
                    existing[name][key] = rec["_rid"]
        await session.flush()
        await _write_citation_edges(tenant_id, prepared, session, report)
        report["papers_unique"] = len({rec["_rid"] for rec in prepared})
        # Legacy trusted background callers have no user-owned upload receipt.
        uid = getattr(getattr(scope, "user", None), "uid", None)
        if uid:
            sha256 = hashlib.sha256(data).hexdigest()
            batch_id = hashlib.sha256(f"{tenant_id}:{uid}:{sha256}:{format_detected}".encode()).hexdigest()
            batch = await session.get(LiteratureReferenceBatch, batch_id)
            if batch is None:
                batch = LiteratureReferenceBatch(
                    tenant_id=tenant_id,
                    batch_id=batch_id,
                    created_by=uid,
                    source_name=(source_name.replace("\\", "/").rsplit("/", 1)[-1] or "文献清单")[:512],
                    source_sha256=sha256,
                    format=format_detected,
                    report_json=dict(report),
                )
                session.add(batch)
                await session.flush()
                await session.execute(
                    insert(LiteratureReferenceMember),
                    [
                        {"tenant_id": tenant_id, "batch_id": batch_id, "rid": rid}
                        for rid in sorted({rec["_rid"] for rec in prepared})
                    ],
                )
            if not batch.source_object:
                batch.source_object = await _store_source(tenant_id, batch_id, data)
            # Reuploading an old receipt may backfill occurrences, never its immutable report.
            if not await session.scalar(
                select(LiteratureReferenceRecord.record_no).where(
                    LiteratureReferenceRecord.batch_id == batch_id,
                    LiteratureReferenceRecord.tenant_id == tenant_id,
                ).limit(1)
            ):
                identities = {rec["record_no"]: rec["_rid"] for rec in prepared}
                rows = [
                    {
                        "tenant_id": tenant_id, "batch_id": batch_id, "record_no": rec["record_no"],
                        "rid": identities.get(rec["record_no"]),
                        "payload_json": {key: value for key, value in rec.items() if key != "cited_dois"},
                    }
                    for rec in records
                ]
                for start in range(0, len(rows), 500):
                    await session.execute(insert(LiteratureReferenceRecord), rows[start:start + 500])
            report["source_records"] = len(records)
            report["batch_id"] = batch_id
            report["source_sha256"] = sha256
        await session.commit()
    return report


async def _store_source(tenant_id: int, batch_id: str, data: bytes) -> str:
    from yuxi.storage.minio import get_minio_client

    name = f"tenants/{tenant_id}/literature/reference-sources/{batch_id}/source"
    await get_minio_client().aupload_file("knowledgebases", name, data, "application/octet-stream")
    return name


def parse_reference_file(data: bytes, *, fmt: str = "auto") -> tuple[list[dict] | str, str, str]:
    """统一文件边界，供导入和真实导出文件验收复用；绝不按后缀猜格式。"""
    from yuxi.literature.reference_tables import parse_table_file

    table = parse_table_file(data, fmt)
    if table is not None:
        return table
    text, encoding = decode_reference_file(data)
    actual = detect_format(text) if fmt == "auto" else fmt
    if actual == "bibtex":
        return text, actual, encoding
    return parse_references(text, actual), actual, encoding


async def _write_citation_edges(
    tenant_id: int, prepared: list[dict[str, Any]], session: Session, report: dict[str, Any]
) -> None:
    """WoS CR → 引用边（仅 DOI 形态；非 DOI 引用表结构不承载，明确不写）。"""
    candidates = [(rec["_rid"], doi) for rec in prepared for doi in rec["cited_dois"]]
    if not candidates:
        return
    citing_rids = sorted({rid for rid, _ in candidates})
    existing_edges: set[tuple[str, str]] = set()
    for start in range(0, len(citing_rids), 1000):
        rows = (
            await session.execute(
                select(LiteratureCitation.citing_rid, LiteratureCitation.cited_doi).where(
                    LiteratureCitation.tenant_id == tenant_id,
                    LiteratureCitation.citing_rid.in_(citing_rids[start : start + 1000]),
                )
            )
        ).all()
        existing_edges.update(rows)
    for rid, doi in candidates:
        if (rid, doi) in existing_edges:
            continue
        session.add(LiteratureCitation(tenant_id=tenant_id, citing_rid=rid, cited_doi=doi))
        existing_edges.add((rid, doi))
        report["citations_written"] += 1
