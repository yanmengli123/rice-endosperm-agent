"""句级抽取单元：检索块 → LLM 抽取窗口（双表示的第二表示）。

检索 chunk 服务于召回（300–600 token），关系抽取需要自包含的小单元：
每个自然句作一次主句，前后各带一句同段语境。只有主句允许抽取，语境句
仅供指代消解——这是防止 LLM 过度推断的结构性保证，也让每条关系的
evidence_quote 精确到句。

确定性、无模型、不增删原文字符（G2 逐字门禁依赖 main_text 与 LLM
看到的文本完全一致）。句切复用 ``knowledge/evidence/sentence_splitter``。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from yuxi.knowledge.evidence.sentence_splitter import split_sentences

EXTRACTION_UNIT_VERSION = "sentence_window_v1"

# chunk 内容前缀的 provenance 标记行（【章节】【页码】【证据锚点】【文献】【标识符】【证据级别】等）
_PROVENANCE_LINE = re.compile(r"^\s*【[^】]{1,32}】")
# 不抽取的 Markdown 行：标题、代码围栏、HTML 注释、图片、表格分隔线
_SKIP_LINE = re.compile(r"^\s*(?:#|```|<!--|!\[|\|?\s*:?-{3,})")
# 图/表题注行自成一句：句切器会保护 "Fig." 的缩写点，不能与相邻行合并后再切
_CAPTION_LINE = re.compile(r"^(?:Figure|Fig\.?|Table|图|表)\s*[0-9]+[A-Za-z]?", re.IGNORECASE)
_MIN_MAIN_CHARS = 8


@dataclass(frozen=True)
class ExtractionWindow:
    index: int
    main_text: str
    context_before: str = ""
    context_after: str = ""
    paragraph_index: int = 0

    @property
    def full_text(self) -> str:
        return " ".join(part for part in (self.context_before, self.main_text, self.context_after) if part)


def strip_provenance_prefix(text: str) -> str:
    """剥离 chunk 开头的 provenance 标记行；正文中间出现的标记行同样剥离（不进入抽取）。"""
    kept = [line for line in text.splitlines() if not _PROVENANCE_LINE.match(line)]
    return "\n".join(kept)


def split_paragraph_sentences(text: str) -> list[list[str]]:
    """段落 → 句子列表。段以空行分隔；段内多行合并后按句切；题注行自成一句；跳过标题/代码/图片/表格线。"""
    paragraphs: list[list[str]] = []
    current_sentences: list[str] = []
    pending_lines: list[str] = []

    def flush_lines() -> None:
        if not pending_lines:
            return
        merged = " ".join(pending_lines)
        current_sentences.extend(sentence.strip() for sentence in split_sentences(merged) if sentence.strip())
        pending_lines.clear()

    def flush_paragraph() -> None:
        flush_lines()
        if current_sentences:
            paragraphs.append(list(current_sentences))
            current_sentences.clear()

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or _SKIP_LINE.match(line):
            flush_paragraph()
            continue
        if _CAPTION_LINE.match(line):
            flush_lines()
            current_sentences.append(line)
            continue
        pending_lines.append(line)
    flush_paragraph()
    return paragraphs


def build_extraction_windows(text: str, *, context_sentences: int = 1) -> list[ExtractionWindow]:
    """构造句窗：每句作主句，前后各取同段 ``context_sentences`` 句语境。

    过短主句（< 8 字符，如编号、残句）跳过但仍可作为邻句语境。
    """
    windows: list[ExtractionWindow] = []
    body = strip_provenance_prefix(text)
    for paragraph_index, sentences in enumerate(split_paragraph_sentences(body)):
        for position, sentence in enumerate(sentences):
            if len(sentence) < _MIN_MAIN_CHARS:
                continue
            before = sentences[max(0, position - context_sentences) : position]
            after = sentences[position + 1 : position + 1 + context_sentences]
            windows.append(
                ExtractionWindow(
                    index=len(windows),
                    main_text=sentence,
                    context_before=" ".join(before),
                    context_after=" ".join(after),
                    paragraph_index=paragraph_index,
                )
            )
    return windows
