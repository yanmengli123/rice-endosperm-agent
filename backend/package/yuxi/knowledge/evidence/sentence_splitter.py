"""确定性句子/证据单元切分（P2-10）。

不调用模型：纯规则、可审计、跨解析版本稳定。覆盖三类证据单元：

- ``sentence``：自然句（中英混合，尊重引号内的句号、DOI 点）；
- ``table_row``：表格行（单行文本，即便长度超过一句）；
- ``caption``：图/表的标题行（Figure/Table/图/表 起始行）。

输出 ``(evidence_type, quote, container_label, row_key, sentence_index)``，
由 :mod:`span_builder` 组装为 ``EvidenceSpanRecord``。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

SPLITTER_VERSION = "1.2"

# 句边界：全角 。！？ 无歧义直接切；ASCII .!? 需后随空白/闭合引号括号或文末
# （小数 "7.0"、URL 因后随非空白不会被切）。1.1 起纳入 ASCII 句号。
_SENTENCE_END = re.compile(r"(?<=[。！？])|(?<=[.!?])(?=[“”\"'）)\]》\s]|$)")
# 图/表标题行：Figure/Fig./Table/图/表 + 编号（字面量，非浮点/范围）
_CAPTION_START = re.compile(r"^(?:Figure|Fig\.?|Table|图|表)\s*([0-9]+[A-Za-z]?)", re.IGNORECASE)
# 表格行：以管线或首列单元开头的单行（如 "| OsMYB73 | 115-164 |"）
_TABLE_ROW = re.compile(r"^\s*\|?\s*[^\s|]+\s*\|")
_DOI_IN_SENTENCE = re.compile(r"doi[:：]?\s*\S+", re.IGNORECASE)
# 句内缩写与人名首字母（"Fig. 5" / "et al. (2020)" / "J. Liu"）：其后的点不是句界
_ABBREVIATION_DOT = re.compile(
    r"\b(?i:Figs?|Tabs?|Eqs?|Refs?|Nos?|et al|e\.g|i\.e|vs|cf|ca|approx|resp|spp?|Dr|Prof|Suppl)\.|\b[A-Z]\.(?=\s)"
)
_PROTECTED_DOT = "\u2024"


@dataclass(frozen=True)
class EvidenceUnit:
    """一个可引用的证据单元（尚未落库）。"""

    evidence_type: str
    quote: str
    container_label: str | None = None
    row_key: str | None = None
    sentence_index: int = 0
    anchor_id: str | None = None


def split_evidence_units(
    *,
    anchors: list[Any],
    markdown_body: str,
) -> list[EvidenceUnit]:
    """从锚点上下文和正文生成确定性证据单元。

    策略（确定性、无模型）：
    1. 优先以 anchors 的 quote 为单元（它们的物理定位已验证）；
    2. 对未覆盖正文（表格/图表标题等），用行切分补 caption/table_row。

    保证同一 (parse_revision_id, quote) 输入得到相同输出。
    """
    units: list[EvidenceUnit] = []
    seen_quotes: set[str] = set()
    index = 0
    for anchor in anchors:
        quote = str(getattr(anchor, "quote", "") or "").strip()
        if not quote or quote in seen_quotes:
            continue
        seen_quotes.add(quote)
        anchor_id = str(getattr(anchor, "anchor_id", "") or "")
        evidence_type, container_label, row_key = _classify_quote(quote)
        units.append(
            EvidenceUnit(
                evidence_type=evidence_type,
                quote=quote,
                container_label=container_label,
                row_key=row_key,
                sentence_index=index,
                anchor_id=anchor_id,
            )
        )
        index += 1

    body_units = _split_body_lines(markdown_body or "", seen_quotes)
    for unit in body_units:
        units.append(
            EvidenceUnit(
                evidence_type=unit[0],
                quote=unit[1],
                container_label=unit[2],
                row_key=unit[3],
                sentence_index=index,
                anchor_id=None,
            )
        )
        index += 1
    return units


def _classify_quote(quote: str) -> tuple[str, str | None, str | None]:
    """返回 (evidence_type, container_label, row_key)。

    题注判定只看编号形态（句首 Figure/Table/图/表 + 编号），不看长度：
    2026-09 Figure 5 事故中含基因名/统计方法/比例尺的长题注（>300 字符）
    被长度上限降级为普通句子，caption span 因此拿不到 container_label，
    题注定位通道整条失效。
    """
    caption_match = _CAPTION_START.match(quote)
    if caption_match:
        return "caption", caption_match.group(0), None
    if _TABLE_ROW.match(quote):
        cells = [cell.strip() for cell in quote.strip(" |").split("|")]
        first = cells[0] if cells else None
        return "table_row", None, first or None
    # 含公式/空格密集的单行优先 formula
    if _looks_like_formula(quote):
        return "formula", None, None
    return "sentence", None, None


def _looks_like_formula(quote: str) -> bool:
    if len(quote) > 200:
        return False
    # 公式特征：下标/上标连续，或低密度字母夹杂符号
    if re.search(r"[A-Za-z]_\{[^}]{1,12}\}|\^\{[^}]{1,12}\}", quote):
        return True
    if re.search(r"(?:\+=|→|\s-\s|\b{yield|σ|Δ|αβγ)\b", quote, re.IGNORECASE):
        return True
    return False


def _split_body_lines(markdown_body: str, seen_quotes: set[str]) -> list[tuple[str, str, str | None, str | None]]:
    """正文行切分：caption/table_row 单行成单元，其余按句切。"""
    units: list[tuple[str, str, str | None, str | None]] = []
    for raw_line in markdown_body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "```", "<!--", "![")):
            continue
        caption_match = _CAPTION_START.match(line)
        if caption_match:
            if line not in seen_quotes:
                units.append(("caption", line, caption_match.group(0), None))
            continue
        if _TABLE_ROW.match(line):
            if line not in seen_quotes:
                cells = [cell.strip() for cell in line.strip(" |").split("|")]
                units.append(("table_row", line, None, cells[0] if cells else None))
            continue
        for sentence in split_sentences(line):
            sentence = sentence.strip()
            if sentence and sentence not in seen_quotes:
                units.append(("sentence", sentence, None, None))
    return units


def split_sentences(text: str) -> list[str]:
    """按中英句边界切分；保护 DOI 与缩写中的点。不增删字符，仅在边界处切开。"""
    protected = _DOI_IN_SENTENCE.sub(lambda m: m.group(0).replace(".", _PROTECTED_DOT), text)
    protected = _ABBREVIATION_DOT.sub(lambda m: m.group(0).replace(".", _PROTECTED_DOT), protected)
    parts = _SENTENCE_END.split(protected)
    return [part.replace(_PROTECTED_DOT, ".") for part in parts if part.strip()]
