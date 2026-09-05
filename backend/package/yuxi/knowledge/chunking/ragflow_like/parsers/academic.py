from __future__ import annotations

import re
from typing import Any

from yuxi.knowledge.chunking.ragflow_like import nlp

_ANCHOR_PATTERN = re.compile(r"<!--\s*yuxi-evidence-anchor:(?P<id>[a-zA-Z0-9_-]+);page=(?P<page>\d+)\s*-->")
_HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_REFERENCE_HEADING = re.compile(r"^(references?|bibliography|参考文献|引用文献)$", re.IGNORECASE)
_BACK_MATTER_HEADING = re.compile(
    r"^(acknowledg(?:e)?ments?|funding|disclosures?|conflicts? of interest|author contributions?)$",
    re.IGNORECASE,
)
_REFERENCE_ENTRY = re.compile(
    r"^\s*(?:\[\d{1,3}\]|\d{1,3}[.)])\s+[A-Z][\s\S]{15,}?(?:19|20)\d{2}\b"
)
_CAPTION_PATTERN = re.compile(r"^(fig(?:ure)?\.?|table|图|表)\s*[\dA-Za-z]", re.IGNORECASE)
_FIGURE_PATTERN = re.compile(r"!\[[^\]]*\]\([^)]+\)")
ACADEMIC_CHUNKER_VERSION = "academic_scientific_v2"


def _block_type(text: str, section_path: list[str]) -> str:
    heading = section_path[-1].lower() if section_path else ""
    stripped = text.lstrip()
    if "abstract" in heading or "摘要" in heading:
        return "abstract"
    if (
        stripped.startswith("|")
        and "|" in stripped.splitlines()[0][1:]
        or re.search(r"<table\b", stripped, re.IGNORECASE)
    ):
        return "table"
    if _FIGURE_PATTERN.search(stripped):
        return "figure"
    if stripped.startswith("$$") or stripped.startswith("\\["):
        return "formula"
    if _CAPTION_PATTERN.match(stripped):
        return "caption"
    return "paragraph"


def _split_hard(text: str, hard_limit: int) -> list[str]:
    if nlp.count_tokens(text) <= hard_limit:
        return [text]
    return nlp.hard_split_by_token_limit(text, hard_limit, hard_limit_token_num=hard_limit)


def _iter_section_blocks(markdown: str) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    stack: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        body = "\n".join(buffer).strip()
        buffer.clear()
        if not body:
            return
        for paragraph in re.split(r"\n\s*\n", body):
            cleaned = paragraph.strip()
            if not cleaned:
                continue
            anchors = [match.group("id") for match in _ANCHOR_PATTERN.finditer(cleaned)]
            pages = sorted({int(match.group("page")) for match in _ANCHOR_PATTERN.finditer(cleaned)})
            cleaned = _ANCHOR_PATTERN.sub("", cleaned).strip()
            if cleaned:
                sections.append(
                    {
                        "section_path": list(stack),
                        "text": cleaned,
                        "anchor_ids": anchors,
                        "pages": pages,
                        "block_type": _block_type(cleaned, stack),
                    }
                )

    for line in markdown.splitlines():
        heading = _HEADING_PATTERN.match(line)
        if not heading:
            buffer.append(line)
            continue
        flush()
        level = len(heading.group(1))
        stack[level - 1 :] = [heading.group(2).strip()]
    flush()
    return sections


def chunk_markdown(markdown_content: str, parser_config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Section-aware scientific chunking with bounded overlap and provenance."""
    config = parser_config or {}
    target = min(max(int(config.get("chunk_token_num", 600) or 600), 200), 800)
    hard_limit = min(max(int(config.get("hard_token_limit", 900) or 900), target), 1200)
    overlap = min(max(int(config.get("overlap_token_num", 64) or 64), 0), 120)
    include_references = bool(config.get("include_references", False))
    blocks = _iter_section_blocks(markdown_content)
    chunks: list[dict[str, Any]] = []

    current: list[dict[str, Any]] = []
    current_path: list[str] = []
    reference_tail_started = False

    def emit() -> None:
        nonlocal current
        if not current:
            return
        text = "\n\n".join(block["text"] for block in current).strip()
        pages = sorted({page for block in current for page in block["pages"]})
        anchors = list(dict.fromkeys(anchor for block in current for anchor in block["anchor_ids"]))
        block_types = {block["block_type"] for block in current}
        chunks.append(
            {
                "text": text,
                "section_path": list(current_path),
                "anchor_ids": anchors,
                "pages": pages,
                "block_type": next(iter(block_types)) if len(block_types) == 1 else "mixed",
            }
        )
        if overlap <= 0:
            current = []
            return
        carry: list[dict[str, Any]] = []
        token_count = 0
        for block in reversed(current):
            block_tokens = nlp.count_tokens(block["text"])
            if carry and token_count + block_tokens > overlap:
                break
            carry.insert(0, block)
            token_count += block_tokens
            if token_count >= overlap:
                break
        current = carry

    for block in blocks:
        path = block["section_path"]
        if path and _REFERENCE_HEADING.match(path[-1].strip()) and not include_references:
            emit()
            current = []
            current_path = path
            continue
        if not include_references and reference_tail_started:
            continue
        if (
            not include_references
            and path
            and _BACK_MATTER_HEADING.match(path[-1].strip())
            and _REFERENCE_ENTRY.match(block["text"])
        ):
            # Some publishers omit a References heading after acknowledgements.
            # A numbered entry plus publication year is deliberately required so
            # ordinary numbered result lists are not mistaken for bibliography.
            emit()
            current = []
            current_path = path
            reference_tail_started = True
            continue
        if path != current_path:
            emit()
            current = []
            current_path = path

        # Tables, formulas and captions remain atomic unless the embedding hard limit requires splitting.
        if block["block_type"] in {"table", "figure", "formula", "caption"}:
            emit()
            current = []
            for part in _split_hard(block["text"], hard_limit):
                chunks.append({**block, "text": part})
            continue

        for part in _split_hard(block["text"], hard_limit):
            prospective = "\n\n".join([*(item["text"] for item in current), part])
            if current and nlp.count_tokens(prospective) > target:
                emit()
            current.append({**block, "text": part})
            if nlp.count_tokens("\n\n".join(item["text"] for item in current)) >= target:
                emit()
    emit()
    return chunks
