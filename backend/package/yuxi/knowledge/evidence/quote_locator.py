"""确定性引文定位路由（QUOTE_LOCATOR / PAGE_LOCATOR）。

「这句原文在第几页」不是生成式问答——它是确定性查询：

    用户问题 → 意图检测 → 引文抽取 → 归一化 → 锚点/证据句精确检索
    → 近似兜底 → 分区消歧 → 唯一锚点 → 页码

LLM 在该链路中**没有页码决定权**；解析结果（含 partition/anchor/页码）由
编排器写入 contract.locator_resolution，输出门禁据此把模型自写的页码统一
替换为后端权威芯片。同一问题 + 同一检索 + 同一解析版本 → 永远同一页码。

失败关闭原则：
- 意图未命中 / 引文过短 / 检索失败 / 多页命中且分区无法消歧 → 一律不产出页码；
- 图表目录行、图注清单等（``toc_line``）对定位任务天然歧义，
  ``eligible_for_locator=False``，默认从候选中剔除（第 17 页 Figure S1 事故）。
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import and_, or_, select

from yuxi.knowledge.evidence.document_partition import (
    LOCATOR_ELIGIBLE_PARTITIONS,
    PARTITION_MAIN_TEXT,
    PARTITION_SUPPORTING_INFO,
    effective_partition,
)
from yuxi.knowledge.evidence.protocol import derive_evidence_id
from yuxi.knowledge.evidence.verbatim import escape_like
from yuxi.knowledge.rendering.claim_evidence_resolver import normalize_for_match
from yuxi.storage.postgres.models_knowledge import (
    EvidenceAnchorRecord,
    EvidenceSpanRecord,
    KnowledgeFile,
    KnowledgeParseRevision,
)
from yuxi.utils import logger

LOCATOR_VERSION = "quote_locator_v2"

LOCATOR_STATUS_VERIFIED = "VERIFIED"
LOCATOR_STATUS_MULTIPLE_MATCHES = "MULTIPLE_MATCHES"
LOCATOR_STATUS_NOT_FOUND = "NOT_FOUND"
LOCATOR_STATUS_NOT_APPLICABLE = "NOT_APPLICABLE"

LOCATOR_INTENT_NONE = None
LOCATOR_KIND_QUOTE = "QUOTE_LOCATOR"
LOCATOR_KIND_PAGE = "PAGE_LOCATOR"

# 子意图（复合问题分解）：定位 + 解释等并行执行，答案必须覆盖全部子意图
SUB_INTENT_LOCATOR = "LOCATOR"
SUB_INTENT_EXPLANATION = "EXPLANATION"
SUB_INTENT_OTHER = "OTHER_QA"

ZONE_MAIN_TEXT = PARTITION_MAIN_TEXT
ZONE_SUPPORTING_INFO = PARTITION_SUPPORTING_INFO

# 意图触发词：命中即认为用户在做定位询问
_LOCATOR_KEYWORDS = re.compile(
    r"哪一页|第几页|哪个页|在哪页|出处在哪|原文在哪|位于哪|页码是多少|"
    r"which\s+page|what\s+page|where\s+in\s+the\s+(paper|article|manuscript|pdf)",
    flags=re.IGNORECASE,
)
# 解释类子意图触发词：命中即认为用户还在要求解释/说明（不允许定位路由吞掉这部分）
_EXPLANATION_KEYWORDS = re.compile(
    r"是什么意思|什么意思|是什么含义|含义|解释|说明什么|代表什么|指的是|为什么|"
    r"作用|意义|机制|如何理解|怎么理解|详[细细]说明|具体介绍|什么关系|是什么",
    flags=re.IGNORECASE,
)
_MAIN_TEXT_KEYWORDS = re.compile(r"正文|主文|main\s+text", flags=re.IGNORECASE)
_SI_KEYWORDS = re.compile(
    r"补充材料|支持信息|附属材料|supplementary|supporting\s+information|\bSI\b", flags=re.IGNORECASE
)
# 问句脚手架：从引文候选中剔除
_SCAFFOLDING_PATTERN = re.compile(
    r"这句(?:原文)?(?:出现|记载|位于)?在?论文的?正文?第几页[?？]?|"
    r"这句话(?:原文)?(?:出现|记载)?在?哪个?文献的?哪一页[?？]?|"
    r"原文出现在?论文的?正文第几页[?？]?|"
    r"这句话在哪个?文献的?哪一页[?？]?|"
    r"在原文哪一页[?？]?|出现在论文的?正文第几页[?？]?|"
    r"请给出页码[。？?]?|哪一页[?？]?|第几页[?？]?|"
    r"(?:which|what)\s+page(?:\s+[^?？]*)?[?？]?|"
    r"where\s+in\s+the\s+(?:paper|article|manuscript|pdf)(?:\s+[^?？]*)?[?？]?",
    flags=re.IGNORECASE,
)
# 引文候选：≥4 个连续拉丁词（原文句段）
_LATIN_RUN = re.compile(r"[A-Za-z][A-Za-z0-9,'’\-–—()./%\s]{24,}")
_QUOTE_MIN_NORMALIZED_CHARS = 25
_MAX_CANDIDATE_SPANS = 500
# 图注标签：句首的 "Figure S8" / "Table 2" 等，用于图注反链（caption → 正文引用处）
_CAPTION_LABEL_PATTERN = re.compile(r"^\s*((?:fig(?:ure)?|table)\s*s?\s*\d+[a-z]?)\b", flags=re.IGNORECASE)
_MAX_BACKLINKS = 3
_BACKLINK_QUOTE_CHARS = 1600
_PREFILTER_TOKEN_LIMIT = 5
_PREFILTER_STOPWORDS = {
    "about",
    "after",
    "also",
    "between",
    "could",
    "from",
    "have",
    "into",
    "paper",
    "results",
    "showed",
    "that",
    "their",
    "these",
    "this",
    "which",
    "with",
}


def decompose_question_intents(question: str) -> dict[str, Any]:
    """复合问题分解：输出子意图集合，答案必须覆盖全部子意图。

    意图歧义时宁可多答不可漏答（企业原则）：定位词 + 解释词同时命中 →
    ``compound=True``，定位走确定性 resolver，解释走正常检索生成，输出门禁
    保障定位行与引用芯片。纯函数。
    """
    source = str(question or "")
    if not _LOCATOR_KEYWORDS.search(source):
        return {
            "kind": LOCATOR_INTENT_NONE,
            "quote_text": None,
            "partition_intent": None,
            "sub_intents": [],
            "compound": False,
        }

    partition_intent = None
    has_main = bool(_MAIN_TEXT_KEYWORDS.search(source))
    has_si = bool(_SI_KEYWORDS.search(source))
    if has_main and not has_si:
        partition_intent = ZONE_MAIN_TEXT
    elif has_si and not has_main:
        partition_intent = ZONE_SUPPORTING_INFO

    remainder = _SCAFFOLDING_PATTERN.sub(" ", source)
    candidates = [match.group(0).strip() for match in _LATIN_RUN.finditer(remainder)]
    quote_text = max(candidates, key=len) if candidates else None
    if quote_text and len(normalize_for_match(quote_text)) >= _QUOTE_MIN_NORMALIZED_CHARS:
        kind = LOCATOR_KIND_QUOTE
    else:
        kind = LOCATOR_KIND_PAGE
        quote_text = None

    sub_intents = [SUB_INTENT_LOCATOR]
    if _EXPLANATION_KEYWORDS.search(source):
        sub_intents.append(SUB_INTENT_EXPLANATION)
    return {
        "kind": kind,
        "quote_text": quote_text,
        "partition_intent": partition_intent,
        "sub_intents": sub_intents,
        "compound": len(sub_intents) > 1,
    }


def detect_locator_intent(question: str) -> dict[str, Any]:
    """检测定位意图：kind（QUOTE/PAGE/NONE）、引文候选、分区意图。纯函数（兼容入口）。"""
    decomposed = decompose_question_intents(question)
    return {
        "kind": decomposed.get("kind"),
        "quote_text": decomposed.get("quote_text"),
        "partition_intent": decomposed.get("partition_intent"),
        "sub_intents": decomposed.get("sub_intents") or [],
        "compound": bool(decomposed.get("compound")),
    }


def is_toc_like(text: str, *, evidence_type: str | None = None, partition: str | None = None) -> bool:
    """Identify directory/list rows without excluding legitimate figure captions."""
    if str(evidence_type or "").casefold() == "toc_line":
        return True
    if str(partition or "").upper() in {"TOC", "FIGURE_LIST", "TABLE_LIST", "COVER", "REFERENCES"}:
        return True
    return bool(
        re.match(
            r"^\s*(?:fig(?:ure)?\.?|table)\s*s?\s*\d+.*\.{2,}\s*\d+\s*$",
            str(text or ""),
            flags=re.IGNORECASE,
        )
    )


def _prefilter_tokens(quote_norm: str) -> list[str]:
    """Bounded literal SQL prefilter; full normalization is always checked in Python."""
    values = {token for token in re.findall(r"[a-z0-9][a-z0-9-]{4,}", quote_norm) if token not in _PREFILTER_STOPWORDS}
    return sorted(values, key=lambda value: (-len(value), value))[:_PREFILTER_TOKEN_LIMIT]


async def resolve_quote_locator(db, *, question: str, kb_ids: list[str]) -> dict[str, Any]:
    """确定性引文定位：原句 → 证据句/锚点 → 分区 → 唯一页码。失败关闭。"""
    intent = detect_locator_intent(question)
    partition_intent = intent.get("partition_intent")
    if not intent.get("kind"):
        return {"status": LOCATOR_STATUS_NOT_APPLICABLE, "locator_version": LOCATOR_VERSION}
    quote_text = intent.get("quote_text")
    if intent["kind"] != LOCATOR_KIND_QUOTE or not quote_text:
        return {
            "status": LOCATOR_STATUS_NOT_APPLICABLE,
            "locator_version": LOCATOR_VERSION,
            "partition_intent": partition_intent,
            "reason": "no_extractable_quote",
        }

    quote_norm = normalize_for_match(quote_text)
    prefilter_tokens = _prefilter_tokens(quote_norm)
    if not prefilter_tokens:
        return {
            "status": LOCATOR_STATUS_NOT_FOUND,
            "locator_version": LOCATOR_VERSION,
            "partition_intent": partition_intent,
            "reason": "no_safe_prefilter_tokens",
        }
    try:
        token_filters = [
            or_(
                EvidenceSpanRecord.quote.ilike(f"%{escape_like(token)}%", escape="/"),
                EvidenceAnchorRecord.quote.ilike(f"%{escape_like(token)}%", escape="/"),
            )
            for token in prefilter_tokens
        ]
        rows = await db.execute(
            select(EvidenceSpanRecord, EvidenceAnchorRecord, KnowledgeFile, KnowledgeParseRevision)
            .join(
                EvidenceAnchorRecord,
                and_(
                    EvidenceAnchorRecord.parse_revision_id == EvidenceSpanRecord.parse_revision_id,
                    EvidenceAnchorRecord.anchor_id == EvidenceSpanRecord.anchor_id,
                ),
            )
            .join(KnowledgeFile, KnowledgeFile.file_id == EvidenceSpanRecord.file_id)
            .join(
                KnowledgeParseRevision,
                KnowledgeParseRevision.revision_id == EvidenceSpanRecord.parse_revision_id,
            )
            .where(
                EvidenceSpanRecord.kb_id.in_(list(kb_ids)[:20]),
                KnowledgeFile.active_parse_revision_id == EvidenceSpanRecord.parse_revision_id,
                KnowledgeParseRevision.file_id == EvidenceSpanRecord.file_id,
                KnowledgeParseRevision.kb_id == EvidenceSpanRecord.kb_id,
                or_(*token_filters),
            )
            .order_by(
                EvidenceSpanRecord.file_id,
                EvidenceSpanRecord.page_number,
                EvidenceSpanRecord.sentence_index,
            )
            .limit(_MAX_CANDIDATE_SPANS)
        )
        rows = rows.all()
    except Exception as exc:  # noqa: BLE001
        logger.error(f"quote locator search failed (fail-closed): {exc}")
        return {
            "status": LOCATOR_STATUS_NOT_FOUND,
            "locator_version": LOCATOR_VERSION,
            "partition_intent": partition_intent,
            "reason": "search_error",
        }

    # 双向归一化包含 + 长度比约束（近似兜底），剔除图表目录行
    candidates: list[dict[str, Any]] = []
    seen_anchors: set[tuple[str, str]] = set()
    for span, anchor, knowledge_file, revision in rows:
        anchor_key = (str(anchor.parse_revision_id), str(anchor.anchor_id))
        if not anchor.page or int(anchor.page) < 1 or anchor_key in seen_anchors:
            continue
        anchor_quote = str(anchor.quote or "")
        span_quote = str(span.quote or "")
        carrier = max((anchor_quote, span_quote), key=len)
        carrier_norm = normalize_for_match(carrier)
        if not carrier_norm:
            continue
        containment = quote_norm in carrier_norm or (
            len(quote_norm) >= 40 and _best_partial_containment(quote_norm, carrier_norm)
        )
        if not containment:
            continue
        partition = effective_partition(anchor.document_partition, page=int(anchor.page))
        if partition not in LOCATOR_ELIGIBLE_PARTITIONS:
            continue
        if is_toc_like(anchor_quote, evidence_type=span.evidence_type, partition=partition) or is_toc_like(
            span_quote, evidence_type=span.evidence_type, partition=partition
        ):
            continue  # 目录/图注清单行不参与定位
        seen_anchors.add(anchor_key)
        candidates.append(
            {
                "anchor_id": str(anchor.anchor_id),
                "span_id": str(span.span_id),
                # Public evidence identity is derived from the immutable
                # physical locator, matching yuxi.scientific-evidence.v1.
                # ``span_evidence_id`` remains internal lineage only.
                "evidence_id": derive_evidence_id(
                    source_sha256=str(revision.source_sha256),
                    page_number=int(anchor.page),
                    bbox=anchor.bbox,
                    word_start=int(anchor.word_start or 0),
                    word_end=int(anchor.word_end or anchor.word_start or 0),
                    quote_hash=str(anchor.quote_hash or ""),
                    anchor_id=str(anchor.anchor_id),
                ),
                "span_evidence_id": str(span.evidence_id),
                "parse_revision_id": str(anchor.parse_revision_id),
                "kb_id": str(span.kb_id),
                "file_id": str(span.file_id),
                "source_sha256": str(revision.source_sha256),
                "filename": str(knowledge_file.filename or ""),
                "page": int(anchor.page),
                "quote_head": re.sub(r"\s+", " ", anchor_quote.strip())[:80],
                "quote": anchor_quote[:_BACKLINK_QUOTE_CHARS],
                "zone": partition,
            }
        )

    if not candidates:
        return {
            "status": LOCATOR_STATUS_NOT_FOUND,
            "locator_version": LOCATOR_VERSION,
            "partition_intent": partition_intent,
            "reason": "no_normalized_match",
        }

    if partition_intent:
        filtered = [candidate for candidate in candidates if candidate["zone"] == partition_intent]
        if not filtered:
            return {
                "status": LOCATOR_STATUS_NOT_FOUND,
                "locator_version": LOCATOR_VERSION,
                "partition_intent": partition_intent,
                "reason": "no_match_in_requested_partition",
            }
        candidates = filtered

    physical_locations = {
        (candidate["parse_revision_id"], candidate["file_id"], candidate["page"]) for candidate in candidates
    }
    if len(physical_locations) > 1:
        return {
            "status": LOCATOR_STATUS_MULTIPLE_MATCHES,
            "locator_version": LOCATOR_VERSION,
            "partition_intent": partition_intent,
            "match_count": len(physical_locations),
            "reason": "quote_hits_multiple_physical_locations",
        }

    anchor = sorted(
        candidates,
        key=lambda item: (item["parse_revision_id"], item["file_id"], item["page"], item["anchor_id"]),
    )[0]
    resolution = {
        "status": LOCATOR_STATUS_VERIFIED,
        "locator_version": LOCATOR_VERSION,
        "partition_intent": partition_intent,
        "page": anchor["page"],
        "zone": anchor.get("zone") or ZONE_MAIN_TEXT,
        "anchor_id": anchor["anchor_id"],
        "span_id": anchor["span_id"],
        "evidence_id": anchor["evidence_id"],
        "span_evidence_id": anchor["span_evidence_id"],
        "parse_revision_id": anchor["parse_revision_id"],
        "kb_id": anchor["kb_id"],
        "file_id": anchor["file_id"],
        "source_sha256": anchor["source_sha256"],
        "quote_head": anchor["quote_head"],
        "quote": anchor.get("quote") or anchor["quote_head"],
        "filename": anchor["filename"],
    }
    # 图注反链：定位命中图注时，反查正文引用该图表的段落，为"解释"子意图提供依据
    if _CAPTION_LABEL_PATTERN.match(anchor.get("quote") or ""):
        try:
            backlinks = await resolve_caption_backlinks(
                db,
                kb_ids=kb_ids,
                caption_quote=anchor.get("quote") or "",
                exclude_anchor_id=anchor["anchor_id"],
            )
            if backlinks:
                resolution["backlinks"] = backlinks
        except Exception as exc:  # noqa: BLE001
            logger.error(f"caption backlink resolution failed (non-fatal): {exc}")
    return resolution


async def resolve_caption_backlinks(
    db,
    *,
    kb_ids: list[str],
    caption_quote: str,
    exclude_anchor_id: str | None = None,
) -> list[dict[str, Any]]:
    """图注 → 正文引用处反链：解释图注含义必须有正文依据（有理有据）。

    只取正文分区（MAIN_TEXT）中包含该图表编号、且不是图注/清单本身的锚点，
    按页序取前 N 条。查不到返回空列表（调用方明示"原文未在正文展开讨论"）。
    """
    label_match = _CAPTION_LABEL_PATTERN.match(str(caption_quote or ""))
    if not label_match:
        return []
    label = re.sub(r"\s+", " ", label_match.group(1)).strip()
    rows = (
        (
            await db.execute(
                select(EvidenceAnchorRecord, KnowledgeFile, KnowledgeParseRevision)
                .join(
                    KnowledgeParseRevision,
                    KnowledgeParseRevision.revision_id == EvidenceAnchorRecord.parse_revision_id,
                )
                .join(KnowledgeFile, KnowledgeFile.file_id == KnowledgeParseRevision.file_id)
                .where(
                    KnowledgeParseRevision.kb_id.in_(list(kb_ids)[:20]),
                    KnowledgeFile.active_parse_revision_id == EvidenceAnchorRecord.parse_revision_id,
                    EvidenceAnchorRecord.quote.ilike(f"%{escape_like(label)}%", escape="/"),
                )
                .order_by(EvidenceAnchorRecord.page.asc())
                .limit(50)
            )
        )
        .all()
    )
    backlinks: list[dict[str, Any]] = []
    for anchor, knowledge_file, revision in rows:
        quote = str(anchor.quote or "")
        if not anchor.page or int(anchor.page) < 1:
            continue
        if exclude_anchor_id and str(anchor.anchor_id) == exclude_anchor_id:
            continue
        # 排除其他图注/图表清单行：正文引用段的引文不以图表编号开头
        if _CAPTION_LABEL_PATTERN.match(quote):
            continue
        partition = effective_partition(anchor.document_partition, page=int(anchor.page))
        if partition != PARTITION_MAIN_TEXT or is_toc_like(quote, partition=partition):
            continue
        backlinks.append(
            {
                "anchor_id": str(anchor.anchor_id),
                "parse_revision_id": str(anchor.parse_revision_id),
                "kb_id": str(revision.kb_id),
                "file_id": str(revision.file_id),
                "filename": str(knowledge_file.filename or ""),
                "page": int(anchor.page),
                "zone": PARTITION_MAIN_TEXT,
                "quote": quote[:_BACKLINK_QUOTE_CHARS],
                "quote_head": re.sub(r"\s+", " ", quote.strip())[:80],
            }
        )
        if len(backlinks) >= _MAX_BACKLINKS:
            break
    return backlinks


def _best_partial_containment(quote_norm: str, carrier_norm: str) -> bool:
    """近似兜底：引文归一化后任一 ≥40 字符句段被载体包含。"""
    for sentence in re.split(r"[.!?。！？;；]", quote_norm):
        sentence = sentence.strip()
        if len(sentence) >= 40 and sentence in carrier_norm:
            return True
    return False
