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

SPLITTER_VERSION = "1.4"

# 句边界：全角 。！？ 无歧义直接切；ASCII .!? 需后随空白/闭合引号括号或文末
# （小数 "7.0"、URL 因后随非空白不会被切）。1.1 起纳入 ASCII 句号。
_SENTENCE_END = re.compile(r"(?<=[。！？])|(?<=[.!?])(?=[“”\"'）)\]》\s]|$)")
# 图/表标题行：Figure/Fig./Table/图/表 + 编号（字面量，非浮点/范围）
# 必须支持补充材料编号的 S 前缀（Figure S21 / Supplementary Figure 2 /
# 图S5）：此前只认「关键词 + 直接数字」，导致所有补充图题注行被判成普通
# sentence、container_label=None → 既进不了 figure_entities、也拿不到权威
# 芯片，正文引用 S 图只能落 no_registry_match（2026-09-26 Q3 实测：S21/S22
# 描述错误且全文无芯片）。此处与 caption_locator._LABEL_PATTERN 的限定词
# 口径对齐（Supplementary/Supplemental/Extended Data）。
_CAPTION_START = re.compile(
    r"^(?:(?:supplementa(?:ry|l)|extended\s+data|ext\.?\s*data)\s+)?"
    r"(?:Figure|Fig\.?|Table|图|表)\.?\s*([sS])?\.?\s*([0-9]+[A-Za-z]?)",
    re.IGNORECASE,
)
_CAPTION_ANY = re.compile(
    r"(?:(?:supplementa(?:ry|l)|extended\s+data|ext\.?\s*data)\s+)?"
    r"(?:Figure|Fig\.?|Table|图|表)\.?\s*([sS])?\.?\s*([0-9]+[A-Za-z]?)",
    re.IGNORECASE,
)
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
        anchor_id = str(getattr(anchor, "anchor_id", "") or "")
        # Some PDF extractors concatenate consecutive supplementary captions
        # into one physical anchor ("Figure S5 ... Figure S6 ...").  Treating
        # that blob as S5 poisons both identities.  Split only when the quote
        # starts with a caption and another label begins after a sentence
        # boundary; every child retains the same physical anchor/page lineage.
        parts = split_caption_sequence(quote)
        seen_quotes.add(quote)
        for part in parts:
            seen_quotes.add(part)
            evidence_type, container_label, row_key = _classify_quote(part)
            units.append(
                EvidenceUnit(
                    evidence_type=evidence_type,
                    quote=part,
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


def split_caption_sequence(text: str) -> list[str]:
    """Split a parser-merged sequence of captions without losing characters.

    A mid-sentence reference ("as shown in Figure 2") is never a split point:
    the first label must start the quote and later labels must follow terminal
    punctuation.  This keeps ordinary multi-sentence captions intact while
    separating the observed S5/S6 and S9/S10 extraction artefacts.
    """
    source = str(text or "").strip()
    if not source or _CAPTION_START.match(source) is None:
        return [source] if source else []
    starts = [0]
    for match in list(_CAPTION_ANY.finditer(source))[1:]:
        prefix = source[: match.start()].rstrip()
        if prefix and prefix[-1] in ".!?。！？;；":
            starts.append(match.start())
    if len(starts) == 1:
        return [source]
    starts.append(len(source))
    return [source[starts[index] : starts[index + 1]].strip() for index in range(len(starts) - 1)]


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
            for part in split_caption_sequence(line):
                if part not in seen_quotes:
                    part_match = _CAPTION_START.match(part)
                    units.append(("caption", part, part_match.group(0) if part_match else None, None))
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
