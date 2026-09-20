"""VERBATIM（GREP）确定性证据检索通道（P0/P1/P2）。

在 ``evidence_spans``（解析平面不可变的句级证据单元）上做三层确定性字面量
检索，为精确信号（标识符/数值区间/DOI/PMID/图表编号/引号原文片段）提供
不被语义检索分词埋没的召回保底：

- L1 ``TYPED_KEY``：``scientific_lexical_index`` 四类词法倒排等值查询
  （ingest 期由 span_builder 构建；检索键复用 ``extract_lexical_rows``，
  与索引侧归一化零漂移）；
- L2 ``EXACT_SUBSTRING``：``quote`` 上 ``ILIKE '%pattern%'``（PG 侧由
  ``ix_evidence_spans_quote_trgm`` pg_trgm GIN 索引加速）；
- L3 ``TOKEN_GAP``：多 token 短语在 L2 未整句命中时的逐 token ILIKE AND
  兜底，容忍换行/连字符等排版差异。

安全契约：查询只接受字面量（``%``/``_``/``/`` 由 :func:`escape_like` 转义，
不向调用方开放任意正则，结构上免疫 ReDoS）；模式数/长度受限；租户隔离与
在役版本过滤（``tenant_id`` + ``active_parse_revision_id``）在 SQL 谓词里
强制，不依赖调用方传参正确。本通道只产 context_evidence
（``claim_eligible=False``），Claim 仍只能出自 canonical 图谱。
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.knowledge.evidence.span_builder import extract_lexical_rows
from yuxi.storage.postgres.models_knowledge import (
    EvidenceSpanRecord,
    KnowledgeFile,
    ScientificLexicalIndexRecord,
)

VERBATIM_CHANNEL_VERSION = "verbatim_v1"

MAX_VERBATIM_HITS = 12
MAX_VERBATIM_PATTERNS = 4
MIN_VERBATIM_PATTERN_CHARS = 3  # pg_trgm 对 <3 字符模式退化为顺序扫描，直接拒绝
MAX_VERBATIM_PATTERN_CHARS = 128
MAX_TOKEN_GAP_TOKENS = 6

# match_tier → 确定性命中基础分（叠加跨通道 lexical 校准与 gateway 的
# +0.04 精确命中加成；分数量级刻意低于语义满分，只保证进候选池）
TIER_BASE_SCORE = {"TYPED_KEY": 0.78, "EXACT_SUBSTRING": 0.72, "TOKEN_GAP": 0.68}
TIER_RANK = {"TYPED_KEY": 0, "EXACT_SUBSTRING": 1, "TOKEN_GAP": 2}

# 引号包裹片段（用户复述原文时最可靠的字面信号）
_QUOTED_PHRASE_PATTERN = re.compile(r"[“\"]([^“”\"]{3,120})[”\"]")
# 标识符样长 token（≥8 字符字母数字混合，如 LOC_Os01g01010.1、OsMYB73-like）
_IDENTIFIER_LIKE_PATTERN = re.compile(r"\b[A-Za-z][A-Za-z0-9][A-Za-z0-9._/\-]{6,126}\b")
# DOI/PMID/URL 由 L1 citation 类型化键覆盖，先从文本剥离再抽 token，
# 避免 DOI 内部片段（如 10.1111/pbi.14558 里的 "pbi.14558"）误入 L2 模式
_CITATION_STRIP_PATTERN = re.compile(r"(?:10\.\d{4,}/[^\s;，。]+)|(?:https?://\S+)|(?:PMID[:：]?\s*\d+)", re.IGNORECASE)


def escape_like(value: str) -> str:
    """转义 ILIKE 通配符（转义符统一用 ``/``，与 ``escape="/"`` 配对使用）。

    与 ``entity_resolver`` 踩过的 ``%``/``_`` 通配符放大命中面是同一类坑，
    本通道从第一天就在边界转义。
    """
    text = str(value or "")
    return text.replace("/", "//").replace("%", "/%").replace("_", "/_")


def extract_verbatim_patterns(question: str) -> list[str]:
    """从问题文本确定性抽取 L2 字面量模式：引号片段 + 标识符样长 token。

    有序去重、长度窗口过滤、上限 :data:`MAX_VERBATIM_PATTERNS`。
    """
    text = _CITATION_STRIP_PATTERN.sub(" ", str(question or ""))
    candidates: list[str] = []
    candidates.extend(phrase.strip() for phrase in _QUOTED_PHRASE_PATTERN.findall(text))
    for token in _IDENTIFIER_LIKE_PATTERN.findall(text):
        candidates.append(token)
    seen: set[str] = set()
    patterns: list[str] = []
    for candidate in candidates:
        value = re.sub(r"\s+", " ", candidate).strip()
        if not (MIN_VERBATIM_PATTERN_CHARS <= len(value) <= MAX_VERBATIM_PATTERN_CHARS):
            continue
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        patterns.append(value)
        if len(patterns) >= MAX_VERBATIM_PATTERNS:
            break
    return patterns


def sanitize_verbatim_patterns(patterns: list[str] | None) -> list[str]:
    """外部传入模式（如 grep_evidence 工具入参）的同一套边界收口。"""
    seen: set[str] = set()
    output: list[str] = []
    for pattern in patterns or []:
        value = re.sub(r"\s+", " ", str(pattern or "")).strip()
        if not (MIN_VERBATIM_PATTERN_CHARS <= len(value) <= MAX_VERBATIM_PATTERN_CHARS):
            continue
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        output.append(value)
        if len(output) >= MAX_VERBATIM_PATTERNS:
            break
    return output


def typed_keywords(question: str) -> list[tuple[str, str]]:
    """L1 类型化检索键：直接复用索引侧提取器，保证折叠规则一致。"""
    rows = extract_lexical_rows(owner_type="query", owner_id="query", quote=str(question or ""))
    return [(row["lex_type"], row["lex_value_folded"]) for row in rows]


def _span_row(span: EvidenceSpanRecord, *, match_tier: str, matched_value: str) -> dict[str, Any]:
    return {
        "span_id": span.span_id,
        "evidence_id": span.evidence_id,
        "anchor_id": span.anchor_id,
        "page_number": span.page_number,
        "sentence_index": span.sentence_index,
        "evidence_type": span.evidence_type,
        "container_label": span.container_label,
        "row_key": span.row_key,
        "quote": span.quote,
        "kb_id": span.kb_id,
        "file_id": span.file_id,
        "parse_revision_id": span.parse_revision_id,
        "document_partition": span.document_partition,
        "partition_confidence": span.partition_confidence,
        "match_tier": match_tier,
        "matched_value": matched_value,
    }


def _scope_filters(tenant_id: int, kb_ids: list[str], file_ids: list[str] | None = None) -> list[Any]:
    """租户 + 成员库 + 在役解析版本三重过滤（SQL 内强制）；``file_ids`` 为文献硬约束。"""
    filters = [
        EvidenceSpanRecord.tenant_id == int(tenant_id),
        EvidenceSpanRecord.kb_id.in_(kb_ids),
        KnowledgeFile.active_parse_revision_id == EvidenceSpanRecord.parse_revision_id,
    ]
    if file_ids:
        filters.append(EvidenceSpanRecord.file_id.in_(file_ids))
    return filters


def _collect(
    collected: dict[str, dict[str, Any]],
    rows: list[dict[str, Any]],
    *,
    limit: int,
) -> None:
    # 层序即优先级（L1 → L2 → L3）：同 span 首次入库的 tier 最优，后续跳过。
    for row in rows:
        if row["span_id"] in collected:
            continue
        collected[row["span_id"]] = row
        if len(collected) >= limit:
            return


async def query_verbatim_evidence(
    db: AsyncSession,
    *,
    tenant_id: int,
    kb_ids: list[str],
    question: str,
    patterns: list[str] | None = None,
    file_ids: list[str] | None = None,
    limit: int = MAX_VERBATIM_HITS,
) -> dict[str, Any]:
    """在冻结成员库的 evidence_spans 上执行 L1/L2/L3 三层字面量检索。

    返回 ``{"spans": [...], "hit_counts": {typed_key/exact_substring/token_gap}}``；
    span 行带 ``match_tier``/``matched_value`` 供 gateway 归一化与审计。
    """
    kb_ids = [str(kb_id).strip() for kb_id in kb_ids if str(kb_id).strip()]
    limit = max(1, min(int(limit), MAX_VERBATIM_HITS))
    hit_counts = {"typed_key": 0, "exact_substring": 0, "token_gap": 0}
    if tenant_id is None or not kb_ids:
        return {"spans": [], "hit_counts": hit_counts}

    keywords = typed_keywords(question)
    literal_patterns = sanitize_verbatim_patterns(
        patterns if patterns is not None else extract_verbatim_patterns(question)
    )
    if not keywords and not literal_patterns:
        return {"spans": [], "hit_counts": hit_counts}

    collected: dict[str, dict[str, Any]] = {}

    # L0：整模式子串预收集（字面精确优先，占据前排配额；L2 仍跑完整逻辑并
    # 回填 matched_patterns）。泛词法命中不得挤掉字面精确命中（定位引文场景）。
    for pattern in literal_patterns:
        stmt = (
            select(EvidenceSpanRecord)
            .join(KnowledgeFile, KnowledgeFile.file_id == EvidenceSpanRecord.file_id)
            .where(
                EvidenceSpanRecord.quote.ilike(f"%{escape_like(pattern)}%", escape="/"),
                *_scope_filters(tenant_id, kb_ids, file_ids),
            )
            .order_by(EvidenceSpanRecord.sentence_index)
            .limit(limit)
        )
        rows = (await db.execute(stmt)).scalars().all()
        if rows:
            matched_patterns_l0 = pattern
            _collect(
                collected,
                [_span_row(span, match_tier="EXACT_SUBSTRING", matched_value=matched_patterns_l0) for span in rows],
                limit=limit,
            )

    # L1：类型化等值（词法倒排；owner_id==span_id 修复原 lexical_hints 的
    # 仅按 revision join 导致返回同 revision 任意 span 的缺陷）
    for lex_type, folded in keywords:
        if len(collected) >= limit:
            break
        stmt = (
            select(EvidenceSpanRecord)
            .join(
                ScientificLexicalIndexRecord,
                and_(
                    ScientificLexicalIndexRecord.parse_revision_id == EvidenceSpanRecord.parse_revision_id,
                    ScientificLexicalIndexRecord.owner_type == "span",
                    ScientificLexicalIndexRecord.owner_id == EvidenceSpanRecord.span_id,
                ),
            )
            .join(KnowledgeFile, KnowledgeFile.file_id == EvidenceSpanRecord.file_id)
            .where(
                ScientificLexicalIndexRecord.tenant_id == int(tenant_id),
                ScientificLexicalIndexRecord.kb_id.in_(kb_ids),
                ScientificLexicalIndexRecord.lex_type == lex_type,
                ScientificLexicalIndexRecord.lex_value_folded == folded,
                *_scope_filters(tenant_id, kb_ids, file_ids),
            )
            .order_by(EvidenceSpanRecord.sentence_index)
            .limit(limit - len(collected))
        )
        rows = (await db.execute(stmt)).scalars().all()
        _collect(
            collected,
            [_span_row(span, match_tier="TYPED_KEY", matched_value=f"{lex_type}:{folded}") for span in rows],
            limit=limit,
        )
    hit_counts["typed_key"] = sum(1 for row in collected.values() if row["match_tier"] == "TYPED_KEY")

    # L2：整模式子串（ILIKE + pg_trgm GIN；PG 之外方言由 SQLAlchemy 翻译）
    matched_patterns: set[str] = set()
    if literal_patterns and len(collected) < limit:
        like_filters = [
            EvidenceSpanRecord.quote.ilike(f"%{escape_like(pattern)}%", escape="/") for pattern in literal_patterns
        ]
        stmt = (
            select(EvidenceSpanRecord)
            .join(KnowledgeFile, KnowledgeFile.file_id == EvidenceSpanRecord.file_id)
            .where(or_(*like_filters), *_scope_filters(tenant_id, kb_ids, file_ids))
            .order_by(EvidenceSpanRecord.sentence_index)
            .limit(limit)
        )
        rows = (await db.execute(stmt)).scalars().all()
        for pattern in literal_patterns:
            if any(pattern.casefold() in span.quote.casefold() for span in rows):
                matched_patterns.add(pattern)
        _collect(
            collected,
            [
                _span_row(
                    span,
                    match_tier="EXACT_SUBSTRING",
                    matched_value=_first_matching_pattern(span, literal_patterns),
                )
                for span in rows
            ],
            limit=limit,
        )

    # L3：多 token 短语的 token-gap 兜底（逐 token ILIKE AND；不开放任意正则）
    if literal_patterns and len(collected) < limit:
        for pattern in literal_patterns:
            if len(collected) >= limit:
                break
            if pattern in matched_patterns:
                continue
            tokens = [token for token in re.split(r"\s+", pattern) if len(token) >= MIN_VERBATIM_PATTERN_CHARS]
            if len(tokens) < 2:
                continue
            like_filters = [
                EvidenceSpanRecord.quote.ilike(f"%{escape_like(token)}%", escape="/")
                for token in tokens[:MAX_TOKEN_GAP_TOKENS]
            ]
            stmt = (
                select(EvidenceSpanRecord)
                .join(KnowledgeFile, KnowledgeFile.file_id == EvidenceSpanRecord.file_id)
                .where(and_(*like_filters), *_scope_filters(tenant_id, kb_ids, file_ids))
                .order_by(EvidenceSpanRecord.sentence_index)
                .limit(limit - len(collected))
            )
            rows = (await db.execute(stmt)).scalars().all()
            _collect(
                collected,
                [_span_row(span, match_tier="TOKEN_GAP", matched_value=pattern) for span in rows],
                limit=limit,
            )
    hit_counts["exact_substring"] = sum(1 for row in collected.values() if row["match_tier"] == "EXACT_SUBSTRING")
    hit_counts["token_gap"] = sum(1 for row in collected.values() if row["match_tier"] == "TOKEN_GAP")

    spans = sorted(
        collected.values(),
        key=lambda row: (TIER_RANK[row["match_tier"]], row.get("sentence_index") or 0),
    )
    return {"spans": spans, "hit_counts": hit_counts}


def _first_matching_pattern(span: EvidenceSpanRecord, patterns: list[str]) -> str:
    folded = span.quote.casefold()
    for pattern in patterns:
        if pattern.casefold() in folded:
            return pattern
    return patterns[0] if patterns else ""
