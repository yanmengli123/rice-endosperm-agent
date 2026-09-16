"""文献作用域（Document Scope）：把"哪篇文献"从问题里确定性地解析成 file_ids 硬约束。

三条通道（按可靠度降序，第一条产出结果的通道即为最终结论）：

1. ``@doc`` 提及：前端用与 ``mention_utils`` 同一 token 语法插入 ``@doc:"<file_id 或文件名>"``；
2. DOI：``10.xxxx/...`` 精确匹配文件名或解析题录（``qa_report.bibliography.doi``）；
3. 文件名/标题片段：引号包裹的片段、以 ``.pdf`` 结尾的 token，与文件名 / 题录标题归一化后包含匹配。

结论三态：``RESOLVED``（唯一或显式多选 → file_ids）、``AMBIGUOUS``（同一引用命中多篇 →
candidates 只列文档身份，调用方把候选集当作硬约束交给定位器裁决）、``UNRESOLVED``（有引用
形态但范围内无匹配）、``NONE``（问题里没有文献引用）。

红线：绝不用向量相似度或模型猜"哪篇文献"；候选清单永不携带页码/图片。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select

from yuxi.storage.postgres.models_knowledge import KnowledgeFile, KnowledgeParseRevision

DOCUMENT_SCOPE_VERSION = "document_scope_v1"

SCOPE_RESOLVED = "RESOLVED"
SCOPE_AMBIGUOUS = "AMBIGUOUS"
SCOPE_UNRESOLVED = "UNRESOLVED"
SCOPE_NONE = "NONE"

CHANNEL_MENTION = "MENTION"
CHANNEL_DOI = "DOI"
CHANNEL_FILENAME = "FILENAME"

# 与 web/src/utils/mention_utils.js 的 mentionTokenRegex 同一语法（仅 doc 类型）
_DOC_MENTION = re.compile(r'@doc:(?:"((?:\\.|[^"\\])*)"|(\S+))')
_DOI = re.compile(r"\b10\.\d{4,9}/[^\s\"'<>，。；；)\]]+", re.IGNORECASE)
_QUOTED = re.compile(r"[\"“「『]([^\"”」』]{4,160})[\"”」』]")
_PDF_TOKEN = re.compile(r"[^\s\"“”「」『』]+\.pdf\b", re.IGNORECASE)
_MAX_SCOPE_FILES = 1000
_MIN_HINT_CHARS = 4


@dataclass
class DocumentScope:
    status: str = SCOPE_NONE
    channel: str | None = None
    file_ids: list[str] = field(default_factory=list)
    candidates: list[dict[str, str]] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)
    clean_question: str = ""

    @property
    def constraint_file_ids(self) -> list[str] | None:
        """交给定位器的硬约束：RESOLVED 用 file_ids；AMBIGUOUS 用候选集（让裁决器在候选内判唯一）。"""
        if self.status == SCOPE_RESOLVED and self.file_ids:
            return list(self.file_ids)
        if self.status == SCOPE_AMBIGUOUS and self.candidates:
            return [item["file_id"] for item in self.candidates]
        return None

    def public_dict(self) -> dict[str, Any]:
        return {
            "version": DOCUMENT_SCOPE_VERSION,
            "status": self.status,
            "channel": self.channel,
            "file_ids": list(self.file_ids),
            "candidates": [dict(item) for item in self.candidates],
            "hints": list(self.hints),
        }


def _unquote(value: str) -> str:
    return re.sub(r"\\([\"\\])", r"\1", value)


def extract_document_mentions(question: str) -> tuple[str, list[str]]:
    """抽出 ``@doc`` 提及值并从问题中剥离 token（剥离后的文本供定位意图/引文抽取使用）。"""
    text = str(question or "")
    values: list[str] = []
    for match in _DOC_MENTION.finditer(text):
        raw = match.group(1) if match.group(1) is not None else match.group(2)
        value = _unquote(raw or "").strip()
        if value:
            values.append(value)
    clean = re.sub(r"\s{2,}", " ", _DOC_MENTION.sub(" ", text)).strip()
    return clean, values


def extract_dois(question: str) -> list[str]:
    found: list[str] = []
    for match in _DOI.finditer(str(question or "")):
        doi = match.group(0).rstrip(".,;:)]").lower()
        if doi not in found:
            found.append(doi)
    return found


def extract_filename_hints(question: str) -> list[str]:
    """引号片段与 .pdf token；短于阈值的片段丢弃（避免 "图" 这类泛词误触发）。"""
    text = str(question or "")
    hints: list[str] = []
    for match in _PDF_TOKEN.finditer(text):
        token = match.group(0).strip()
        if token and token not in hints:
            hints.append(token)
    for match in _QUOTED.finditer(text):
        fragment = match.group(1).strip()
        if len(normalize_reference(fragment)) >= _MIN_HINT_CHARS and fragment not in hints:
            hints.append(fragment)
    return hints


def normalize_reference(text: str) -> str:
    """文件名/标题/片段归一化：小写、去扩展名、非字母数字（保留 CJK）折成单空格。"""
    lowered = str(text or "").lower()
    lowered = re.sub(r"\.pdf$", "", lowered.strip())
    lowered = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def _bibliography(qa_report: Any) -> dict[str, Any]:
    if not isinstance(qa_report, dict):
        return {}
    bibliography = qa_report.get("bibliography")
    return bibliography if isinstance(bibliography, dict) else {}


async def _scope_files(db, *, kb_ids: list[str]) -> list[dict[str, Any]]:
    scoped = [str(item) for item in kb_ids if item][:20]
    if not scoped:
        return []
    rows = (
        await db.execute(
            select(
                KnowledgeFile.file_id,
                KnowledgeFile.kb_id,
                KnowledgeFile.filename,
                KnowledgeParseRevision.qa_report,
            )
            .outerjoin(
                KnowledgeParseRevision,
                KnowledgeParseRevision.revision_id == KnowledgeFile.active_parse_revision_id,
            )
            .where(
                KnowledgeFile.kb_id.in_(scoped),
                func.coalesce(KnowledgeFile.is_folder, False) == False,  # noqa: E712 - SQL 布尔比较
            )
            .order_by(KnowledgeFile.filename)
            .limit(_MAX_SCOPE_FILES)
        )
    ).all()
    files: list[dict[str, Any]] = []
    for file_id, kb_id, filename, qa_report in rows:
        bibliography = _bibliography(qa_report)
        files.append(
            {
                "file_id": str(file_id),
                "kb_id": str(kb_id or ""),
                "filename": str(filename or ""),
                "filename_norm": normalize_reference(filename or ""),
                "title_norm": normalize_reference(bibliography.get("title") or ""),
                "doi": str(bibliography.get("doi") or "").lower(),
            }
        )
    return files


def _document(item: dict[str, Any]) -> dict[str, str]:
    return {"file_id": item["file_id"], "kb_id": item["kb_id"], "filename": item["filename"]}


def _match_mention(files: list[dict[str, Any]], value: str) -> list[dict[str, Any]]:
    exact_id = [item for item in files if item["file_id"] == value]
    if exact_id:
        return exact_id
    needle = normalize_reference(value)
    if not needle:
        return []
    exact_name = [item for item in files if item["filename_norm"] == needle or item["filename"] == value]
    if exact_name:
        return exact_name
    return [
        item
        for item in files
        if needle in item["filename_norm"] or (item["title_norm"] and needle in item["title_norm"])
    ]


def _match_doi(files: list[dict[str, Any]], doi: str) -> list[dict[str, Any]]:
    needle = doi.lower()
    # 文件名里常见的 DOI 写法把 "/" 换成 "_"（10.1002_fes3.354.pdf）
    filename_forms = {normalize_reference(needle), normalize_reference(needle.replace("/", "_"))}
    return [
        item
        for item in files
        if item["doi"] == needle or any(form and form in item["filename_norm"] for form in filename_forms)
    ]


def _match_hint(files: list[dict[str, Any]], hint: str) -> list[dict[str, Any]]:
    needle = normalize_reference(hint)
    if len(needle) < _MIN_HINT_CHARS:
        return []
    return [
        item
        for item in files
        if needle in item["filename_norm"] or (item["title_norm"] and needle in item["title_norm"])
    ]


def _strip_reference_text(text: str, hints: list[str]) -> str:
    """把已被消费为文献引用的片段（含包裹引号）从问题中剥离——否则引号里的文献名会被
    引文抽取当成"要定位的原句"，题注通道拿去做 T0–T3 比对后失败关闭（no_normalized_match）。"""
    result = str(text or "")
    for hint in sorted((item for item in hints if item), key=len, reverse=True):
        pattern = re.compile(r"[\"“「『]?" + re.escape(hint) + r"[\"”」』]?", re.IGNORECASE)
        result = pattern.sub(" ", result)
    return re.sub(r"\s{2,}", " ", result).strip()


def _conclude(channel: str, matched: dict[str, dict[str, Any]], *, hints: list[str], clean: str) -> DocumentScope:
    documents = sorted((_document(item) for item in matched.values()), key=lambda d: (d["filename"], d["file_id"]))
    if not documents:
        return DocumentScope(status=SCOPE_UNRESOLVED, channel=channel, hints=hints, clean_question=clean)
    if len(documents) == 1 or channel == CHANNEL_MENTION:
        # 显式 @ 多篇是用户主动多选，视为 RESOLVED（定位器在这几篇内裁决唯一性）
        return DocumentScope(
            status=SCOPE_RESOLVED,
            channel=channel,
            file_ids=[item["file_id"] for item in documents],
            candidates=documents,
            hints=hints,
            clean_question=clean,
        )
    return DocumentScope(
        status=SCOPE_AMBIGUOUS, channel=channel, candidates=documents, hints=hints, clean_question=clean
    )


async def resolve_document_scope(db, *, question: str, kb_ids: list[str]) -> DocumentScope:
    """问题 → 文献作用域。无引用形态时返回 NONE 且不查库。"""
    clean, mentions = extract_document_mentions(question)
    dois = extract_dois(clean)
    hints = extract_filename_hints(clean)
    if not (mentions or dois or hints):
        return DocumentScope(status=SCOPE_NONE, clean_question=clean)

    files = await _scope_files(db, kb_ids=kb_ids)

    if mentions:
        matched: dict[str, dict[str, Any]] = {}
        for value in mentions:
            hits = _match_mention(files, value)
            if len(hits) > 1:
                # 单个提及值命中多篇（如重名文件跨库）：交给用户选择，不擅自扩大范围
                return DocumentScope(
                    status=SCOPE_AMBIGUOUS,
                    channel=CHANNEL_MENTION,
                    candidates=sorted((_document(item) for item in hits), key=lambda d: (d["filename"], d["file_id"])),
                    hints=list(mentions),
                    clean_question=clean,
                )
            for item in hits:
                matched[item["file_id"]] = item
        return _conclude(CHANNEL_MENTION, matched, hints=list(mentions), clean=clean)

    if dois:
        matched = {}
        consumed: list[str] = []
        for doi in dois:
            hits = _match_doi(files, doi)
            if hits:
                consumed.append(doi)
            for item in hits:
                matched[item["file_id"]] = item
        return _conclude(CHANNEL_DOI, matched, hints=list(dois), clean=_strip_reference_text(clean, consumed))

    matched = {}
    consumed = []
    for hint in hints:
        hits = _match_hint(files, hint)
        if hits:
            consumed.append(hint)
        for item in hits:
            matched[item["file_id"]] = item
    return _conclude(CHANNEL_FILENAME, matched, hints=list(hints), clean=_strip_reference_text(clean, consumed))


__all__ = [
    "CHANNEL_DOI",
    "CHANNEL_FILENAME",
    "CHANNEL_MENTION",
    "DOCUMENT_SCOPE_VERSION",
    "DocumentScope",
    "SCOPE_AMBIGUOUS",
    "SCOPE_NONE",
    "SCOPE_RESOLVED",
    "SCOPE_UNRESOLVED",
    "extract_document_mentions",
    "extract_dois",
    "extract_filename_hints",
    "normalize_reference",
    "resolve_document_scope",
]
