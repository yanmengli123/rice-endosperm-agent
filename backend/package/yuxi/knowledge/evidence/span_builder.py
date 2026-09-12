"""Evidence Span 与多字段词法索引构建（P2-10/P2-11）。

确定性、幂等：同一 (parse_revision_id, article) 输入得到同一套 span/词法行，
解析器升级重建 revision 后新旧依据 evidence_id 比对即得回归结论。

- ``build_evidence_spans``：切分 → 写入 ``evidence_spans``（级联 revision）；
- ``build_lexical_index``：从 anchors + spans 提取
  scientific_identifiers / numeric_tokens / citation_ids / figure_table_labels
  写入 ``scientific_lexical_index``（owner_type=anchor|span）。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.knowledge.evidence.sentence_splitter import SPLITTER_VERSION, split_evidence_units
from yuxi.storage.postgres.models_knowledge import (
    EvidenceSpanRecord,
    KnowledgeParseRevision,
    ScientificLexicalIndexRecord,
)

EVIDENCE_SPAN_BUILDER_VERSION = "1.0"

# 科研标识符：OsMYB73 / LOC_Os01g01010 / RAP 形态 等
_IDENTIFIER_PATTERN = re.compile(r"(?<![A-Za-z])(?:Os[A-Z][A-Za-z0-9]+|LOC_Os\w+|Os\d+g\w+)\b")
# 数值区间/带单位量值：如 115-164 aa、0.3 mg/L、45% 等
_NUMERIC_PATTERN = re.compile(
    r"\d+(?:[.,]\d+)?(?:\s*[-–~]\s*\d+(?:[.,]\d+)?)?\s*(?:%|％|倍|mg/L|mM|µM|μM|kb|bp|aa|氨基酸|cM|MB|DAF)?",
    re.IGNORECASE,
)
# 引文：DOI / PMID
_CITATION_PATTERN = re.compile(r"(?:doi[:：]?\s*)?(10\.\d{4,}/[^\s;，。]+)|PMID[:：]?\s*(\d+)", re.IGNORECASE)
# 图/表引用标签
_FIGURE_TABLE_PATTERN = re.compile(r"(?:Figure|Fig\.?|Table|图|表)\s*([0-9]+[A-Za-z]?)", re.IGNORECASE)


def normalize_numeric(value: str) -> str:
    """数值区间归一为检索键：\"115-164 aa\" → \"115:164\"。"""
    compact = re.sub(r"[\s,]", "", str(value)).replace("－", "-").replace("–", "-").replace("~", "-")
    parts = re.split(r"-", compact)
    if len(parts) >= 2 and all(part[:1].isdigit() for part in parts[:2]):
        first = re.match(r"\d+(?:[.,]\d+)?", parts[0])
        second = re.match(r"\d+(?:[.,]\d+)?", parts[1])
        if first and second:
            return f"{first.group(0)}:{second.group(0)}"
    number = re.match(r"\d+(?:[.,]\d+)?", compact)
    return number.group(0) if number else compact


def extract_lexical_rows(*, owner_type: str, owner_id: str, quote: str) -> list[dict[str, str]]:
    """从单条 quote 确定性提取词法行（供单测与批量构建复用）。

    行内按 (lex_type, lex_value_folded) 去重；数值项剔除被标识符吸收的
    尾部数字（如 OsMYB73 的 73 不算数值）。
    """
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def push(lex_type: str, lex_value: str, lex_value_folded: str) -> None:
        key = (lex_type, lex_value_folded)
        if key in seen:
            return
        seen.add(key)
        rows.append({"lex_type": lex_type, "lex_value": lex_value, "lex_value_folded": lex_value_folded})

    identifiers = list(dict.fromkeys(_IDENTIFIER_PATTERN.findall(quote)))
    for value in identifiers:
        push("identifier", value, value.casefold())

    identifier_tails = {value.casefold() for value in identifiers}
    for value in _NUMERIC_PATTERN.findall(quote):
        if not value or not value.strip():
            continue
        folded = normalize_numeric(value)
        if not (folded.isdigit() or ":" in folded):
            continue
        # 数值不能是某个标识符的一部分（OsMYB73 的 73）
        if any(folded in tail for tail in identifier_tails if tail):
            continue
        push("numeric", value, folded)
    for doi, pmid in _CITATION_PATTERN.findall(quote):
        if doi:
            push("citation", f"doi:{doi}", f"doi:{doi.lower()}")
        if pmid:
            push("citation", f"PMID:{pmid}", f"pmid:{pmid}")
    for label in _FIGURE_TABLE_PATTERN.findall(quote):
        push("figure_table", label, label.lower())
    return rows


def _span_evidence_id(*, parse_revision_id: str, sentence_index: int, quote_hash: str) -> str:
    """Span 证据身份：同一解析正文同一句 → 同一 id；正文变化 → 新 id。"""
    digest = hashlib.sha256(
        f"yuxi-evidence-span|v1|{parse_revision_id}|s{sentence_index}|{quote_hash}".encode()
    ).hexdigest()[:20]
    return f"evs_{digest}"


async def build_evidence_spans(
    db: AsyncSession,
    *,
    revision: KnowledgeParseRevision,
    anchors: list[Any],
    markdown_body: str,
) -> dict[str, Any]:
    """切分并写入 evidence_spans；幂等（先清后写，同事务）。"""
    units = split_evidence_units(anchors=anchors, markdown_body=markdown_body)
    anchor_by_id = {str(getattr(a, "anchor_id", "") or ""): a for a in anchors}

    await db.execute(delete(EvidenceSpanRecord).where(EvidenceSpanRecord.parse_revision_id == revision.revision_id))
    rows: list[EvidenceSpanRecord] = []
    for unit in units:
        anchor = anchor_by_id.get(unit.anchor_id) if unit.anchor_id else None
        quote_hash = hashlib.sha256(unit.quote.encode("utf-8")).hexdigest()
        start_char = _find_in_chunk(unit.quote, markdown_body)
        page_number = int(getattr(anchor, "page", 0) or 0) if anchor else None
        evidence_id = _span_evidence_id(
            parse_revision_id=revision.revision_id,
            sentence_index=unit.sentence_index,
            quote_hash=quote_hash,
        )
        rows.append(
            EvidenceSpanRecord(
                tenant_id=revision.tenant_id,
                parse_revision_id=revision.revision_id,
                kb_id=revision.kb_id,
                file_id=revision.file_id,
                span_id=f"es_{revision.revision_id}_{unit.sentence_index}_{quote_hash[:8]}",
                anchor_id=unit.anchor_id,
                sentence_index=unit.sentence_index,
                quote=unit.quote,
                quote_hash=quote_hash,
                start_char=start_char,
                end_char=(start_char + len(unit.quote) if start_char is not None else None),
                page_number=page_number if page_number and page_number >= 1 else None,
                evidence_type=unit.evidence_type,
                document_partition=str(getattr(anchor, "document_partition", "UNKNOWN") or "UNKNOWN"),
                partition_confidence=float(getattr(anchor, "partition_confidence", 0.0) or 0.0),
                evidence_id=evidence_id,
                container_label=unit.container_label,
                row_key=unit.row_key,
                metadata_json={
                    "splitter_version": SPLITTER_VERSION,
                    "builder_version": EVIDENCE_SPAN_BUILDER_VERSION,
                    "anchor_locatable": bool(getattr(anchor, "locatable", False)) if anchor else False,
                },
            )
        )
    if rows:
        db.add_all(rows)
    summary = {"total": len(rows), "types": _type_counts(rows), "version": EVIDENCE_SPAN_BUILDER_VERSION}
    await db.flush()
    return summary


async def build_lexical_index(
    db: AsyncSession,
    *,
    revision: KnowledgeParseRevision,
    anchors: list[Any],
    spans: list[EvidenceSpanRecord],
) -> dict[str, Any]:
    """从 anchors + spans 构建词法倒排；幂等（先清后写）。"""
    await db.execute(
        delete(ScientificLexicalIndexRecord).where(
            ScientificLexicalIndexRecord.parse_revision_id == revision.revision_id
        )
    )

    rows: list[ScientificLexicalIndexRecord] = []
    seen: set[tuple[str, str, str]] = set()

    def add(owner_type: str, owner_id: str, quote: str, *, extra_meta: dict[str, Any] | None = None) -> None:
        for row in extract_lexical_rows(owner_type=owner_type, owner_id=owner_id, quote=quote):
            key = (owner_id, row["lex_type"], row["lex_value_folded"])
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                ScientificLexicalIndexRecord(
                    tenant_id=revision.tenant_id,
                    parse_revision_id=revision.revision_id,
                    kb_id=revision.kb_id,
                    file_id=revision.file_id,
                    owner_type=owner_type,
                    owner_id=owner_id,
                    lex_type=row["lex_type"],
                    lex_value=row["lex_value"],
                    lex_value_folded=row["lex_value_folded"],
                    metadata_json={**row, **(extra_meta or {})},
                )
            )

    for anchor in anchors:
        quote = str(getattr(anchor, "quote", "") or "").strip()
        if quote:
            add("anchor", str(getattr(anchor, "anchor_id", "") or ""), quote)

    for span in spans:
        add("span", span.span_id, span.quote, extra_meta={"evidence_type": span.evidence_type})

    if rows:
        db.add_all(rows)
    summary = {"total": len(rows), "types": _lex_type_counts(rows), "version": EVIDENCE_SPAN_BUILDER_VERSION}
    await db.flush()
    return summary


def _find_in_chunk(quote: str, corpus: str) -> int | None:
    pos = corpus.find(quote)
    return pos if pos >= 0 else None


def _type_counts(rows: list[EvidenceSpanRecord]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.evidence_type] = counts.get(row.evidence_type, 0) + 1
    return counts


def _lex_type_counts(rows: list[ScientificLexicalIndexRecord]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.lex_type] = counts.get(row.lex_type, 0) + 1
    return counts
