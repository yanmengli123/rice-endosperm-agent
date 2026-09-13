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

from yuxi.knowledge.contracts.citation_binding import CitationBindingCandidate
from yuxi.knowledge.evidence.caption_locator import (
    carrier_label_conflicts,
    extract_figure_label,
    select_quote_candidates,
)
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
# 图表编号定位：用户给出 Figure 5 / 图S8 等编号（可无引文片段），
# 走 caption span 通道 + label 硬约束（caption_locator v3）
LOCATOR_KIND_FIGURE = "FIGURE_LOCATOR"

# 子意图（复合问题分解）：定位 + 解释等并行执行，答案必须覆盖全部子意图
SUB_INTENT_LOCATOR = "LOCATOR"
SUB_INTENT_EXPLANATION = "EXPLANATION"
SUB_INTENT_OTHER = "OTHER_QA"

ZONE_MAIN_TEXT = PARTITION_MAIN_TEXT
ZONE_SUPPORTING_INFO = PARTITION_SUPPORTING_INFO

# 意图触发词：命中即认为用户在做定位询问
_LOCATOR_KEYWORDS = re.compile(
    r"哪一页|那一页|第几页|几页|哪个页|哪页|在哪页|出处在哪|原文在哪|位于哪|页码是多少|"
    r"哪一句|第几句|哪一段|第几段|哪个段落|原文.{0,8}(?:什么位置|哪里|何处)|"
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
    r"请给出页码[。？?]?|在哪个?文献那一页[?？]?|文献那一页[?？]?|"
    r"哪一页[?？]?|那一页[?？]?|几页[?？]?|第几页[?？]?|"
    r"哪一句[?？]?|第几句[?？]?|哪一段[?？]?|第几段[?？]?|哪个段落[?？]?|"
    r"原文.{0,8}(?:什么位置|哪里|何处)[?？]?|"
    r"(?:which|what)\s+page(?:\s+[^?？]*)?[?？]?|"
    r"where\s+in\s+the\s+(?:paper|article|manuscript|pdf)(?:\s+[^?？]*)?[?？]?",
    flags=re.IGNORECASE,
)
# 引文候选：≥4 个连续拉丁词（原文句段）——科研字符集升级与多候选评分
# 见 caption_locator.select_quote_candidates（v3，替代 max(len)）
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
            "figure_label": None,
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
    figure_label = extract_figure_label(remainder)
    # Caption Locator v3：多候选区分度评分替代 max(len)——统计模板段
    # （Bar, 1.0 cm / ANOVA / Tukey）即使更长也排在含基因符号的段之后。
    candidates = select_quote_candidates(remainder)
    quote_text = candidates[0] if candidates else None
    if quote_text and len(normalize_for_match(quote_text)) >= _QUOTE_MIN_NORMALIZED_CHARS:
        kind = LOCATOR_KIND_QUOTE
    elif figure_label:
        kind = LOCATOR_KIND_FIGURE
        quote_text = None
    else:
        kind = LOCATOR_KIND_PAGE
        quote_text = None

    sub_intents = [SUB_INTENT_LOCATOR]
    if _EXPLANATION_KEYWORDS.search(source):
        sub_intents.append(SUB_INTENT_EXPLANATION)
    return {
        "kind": kind,
        "quote_text": quote_text,
        "figure_label": figure_label,
        "partition_intent": partition_intent,
        "sub_intents": sub_intents,
        "compound": len(sub_intents) > 1,
    }


def detect_locator_intent(question: str) -> dict[str, Any]:
    """检测定位意图：kind（QUOTE/FIGURE/PAGE/NONE）、引文候选、图表编号、分区意图。纯函数（兼容入口）。"""
    decomposed = decompose_question_intents(question)
    return {
        "kind": decomposed.get("kind"),
        "quote_text": decomposed.get("quote_text"),
        "figure_label": decomposed.get("figure_label"),
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


def resolve_quote_locator_from_citations(
    *,
    quote_text: str,
    citations: list[dict[str, Any]],
    partition_intent: str | None = None,
    kb_id: str | None = None,
    figure_label: str | None = None,
) -> dict[str, Any]:
    """在引用池（锚点权威行，与状态模块同一行数据）内做确定性引文定位。

    单一证据集原则：答案页码与状态模块投影读同一行 citation 记录，
    结构性一致；引用池之外的相似句（如方法模板句）不可能被选中。
    失败关闭：池内无命中/多个物理位置且分区无法消歧 → 不产出页码。
    """
    locator_version = f"{LOCATOR_VERSION}+evidence_set"
    if not quote_text:
        return {
            "status": LOCATOR_STATUS_NOT_APPLICABLE,
            "locator_version": locator_version,
            "reason": "no_extractable_quote",
        }
    quote_norm = normalize_for_match(quote_text)
    if len(quote_norm) < _QUOTE_MIN_NORMALIZED_CHARS:
        return {
            "status": LOCATOR_STATUS_NOT_APPLICABLE,
            "locator_version": locator_version,
            "reason": "quote_too_short",
        }

    typed_pool = [
        CitationBindingCandidate.from_legacy_dict(citation)
        for citation in citations or []
        if isinstance(citation, dict)
    ]
    pool = [
        candidate.to_legacy_dict()
        for candidate in typed_pool
        if candidate.locator_eligible and (not kb_id or str(candidate.kb_id) == kb_id)
    ]
    if partition_intent:
        filtered = [citation for citation in pool if str(citation.get("zone")) == partition_intent]
        if not filtered:
            return {
                "status": LOCATOR_STATUS_NOT_FOUND,
                "locator_version": locator_version,
                "partition_intent": partition_intent,
                "reason": "no_match_in_requested_partition",
            }
        pool = filtered

    matched: list[dict[str, Any]] = []
    for citation in pool:
        carrier_norm = str(citation.get("_quote_norm") or "")
        if not carrier_norm:
            continue
        contained = quote_norm in carrier_norm or _best_partial_containment(quote_norm, carrier_norm)
        if not contained:
            continue
        if citation.get("toc_line"):
            continue
        # label 硬约束：引用载体句首编号与用户编号冲突（Figure 4 ≠ Figure 5）→ 剔除
        if carrier_label_conflicts(figure_label, citation.get("_quote") or citation.get("quote_head")):
            continue
        matched.append(citation)

    if not matched:
        return {
            "status": LOCATOR_STATUS_NOT_FOUND,
            "locator_version": locator_version,
            "partition_intent": partition_intent,
            "reason": "no_match_in_evidence_set",
        }

    # Deterministic locator rows were added to the same frozen evidence set
    # specifically to close a Top-K recall gap. When present, they outrank
    # semantically similar retrieval rows; full-scope duplicate detection has
    # already happened before such a row can be injected.
    seeded = [citation for citation in matched if str(citation.get("_retrieval_channel") or "") == "QUOTE_LOCATOR"]
    if seeded:
        matched = seeded

    # A page number is not a physical identity: two papers can both match on
    # page 15. Collapse only duplicates that resolve to the same active parse,
    # file and page; otherwise fail closed without leaking candidate pages.
    physical_locations = {
        (
            str(citation.get("_parse_revision_id") or ""),
            str(citation.get("file_id") or ""),
            int(citation["page_numbers"][0]),
        )
        for citation in matched
        if citation.get("page_numbers")
    }
    if not physical_locations:
        return {
            "status": LOCATOR_STATUS_NOT_FOUND,
            "locator_version": locator_version,
            "partition_intent": partition_intent,
            "reason": "matched_evidence_has_no_physical_location",
        }
    if len(physical_locations) != 1:
        return {
            "status": LOCATOR_STATUS_MULTIPLE_MATCHES,
            "locator_version": locator_version,
            "partition_intent": partition_intent,
            "match_count": len(physical_locations),
            "reason": "quote_hits_multiple_physical_locations_in_evidence_set",
        }

    best = sorted(
        matched,
        key=lambda citation: (
            0 if str(citation.get("_retrieval_channel") or "") == "QUOTE_LOCATOR" else 1,
            str(citation.get("_parse_revision_id") or ""),
            str(citation.get("file_id") or ""),
            int((citation.get("page_numbers") or [0])[0]),
            str(citation.get("_anchor_id") or ""),
            str(citation.get("ref") or ""),
        ),
    )[0]
    return {
        "status": LOCATOR_STATUS_VERIFIED,
        "locator_version": locator_version,
        "partition_intent": partition_intent,
        "page": best["page_numbers"][0],
        "zone": best.get("zone") or ZONE_MAIN_TEXT,
        "anchor_id": best.get("_anchor_id") or (best.get("anchor_ids") or [None])[0],
        "evidence_id": best.get("_physical_evidence_id") or best.get("evidence_id"),
        "retrieval_evidence_id": best.get("evidence_id"),
        "span_evidence_id": best.get("_span_evidence_id"),
        "span_id": best.get("_span_id"),
        "parse_revision_id": best.get("_parse_revision_id"),
        "index_revision_id": best.get("_index_revision_id"),
        "source_sha256": best.get("_source_sha256"),
        "kb_id": best.get("kb_id"),
        "file_id": best.get("file_id"),
        "quote_head": best.get("quote_head"),
        "quote": str(best.get("_quote") or best.get("quote_head") or "")[:800],
        "filename": best.get("filename"),
        "citation_ref": best.get("ref"),
        "backlinks": [],
    }


def _prefilter_tokens(quote_norm: str) -> list[str]:
    """Bounded literal SQL prefilter; full normalization is always checked in Python."""
    values = {token for token in re.findall(r"[a-z0-9][a-z0-9-]{4,}", quote_norm) if token not in _PREFILTER_STOPWORDS}
    return sorted(values, key=lambda value: (-len(value), value))[:_PREFILTER_TOKEN_LIMIT]


async def resolve_quote_locator(db, *, question: str, kb_ids: list[str]) -> dict[str, Any]:
    """独立全库定位（也用于补齐冻结证据集的精确召回）。

    运行时页码最终仍由 :func:`resolve_quote_locator_from_citations` 从冻结证据
    集裁决。本函数在常规/VERBATIM 召回漏掉精确原句时，把确定性命中作为一条
    正式 evidence row 注入同一集合；它不能绕开集合直接把页码写进答案。

    图表编号问题（Figure 5 / 图S8）优先走 caption span 通道（label 硬约束，
    caption_locator v3）；通道无命中时回退常规引文路径，并在候选过滤中保留
    label 硬约束——编号冲突的锚点（Figure 4 的统计模板题注）直接剔除。
    """
    intent = detect_locator_intent(question)
    partition_intent = intent.get("partition_intent")
    figure_label = intent.get("figure_label")
    if not intent.get("kind"):
        return {"status": LOCATOR_STATUS_NOT_APPLICABLE, "locator_version": LOCATOR_VERSION}
    quote_text = intent.get("quote_text")

    if figure_label:
        from yuxi.knowledge.evidence.caption_locator import resolve_figure_caption_locator

        caption_resolution = await resolve_figure_caption_locator(
            db,
            figure_label=figure_label,
            quote_text=quote_text,
            kb_ids=kb_ids,
        )
        if caption_resolution is not None:
            if caption_resolution.get("status") == "VERIFIED":
                await attach_caption_backlinks(db, kb_ids=kb_ids, resolution=caption_resolution)
            return caption_resolution
        if intent["kind"] == LOCATOR_KIND_FIGURE:
            # 编号定位但题注通道无命中：失败关闭（不回退到无编号的泛匹配）
            return {
                "status": LOCATOR_STATUS_NOT_FOUND,
                "locator_version": LOCATOR_VERSION,
                "partition_intent": partition_intent,
                "reason": "figure_label_caption_not_found_in_scope",
            }

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
        # label 硬约束：用户编号与锚点句首编号冲突（Figure 4 ≠ Figure 5）→ 剔除
        if carrier_label_conflicts(figure_label, anchor_quote):
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
                "evidence_type": str(span.evidence_type or ""),
                "parse_revision_id": str(anchor.parse_revision_id),
                "kb_id": str(span.kb_id),
                "file_id": str(span.file_id),
                "source_sha256": str(revision.source_sha256),
                "index_revision_id": str(knowledge_file.active_index_revision_id or ""),
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
        "evidence_type": anchor.get("evidence_type"),
        "parse_revision_id": anchor["parse_revision_id"],
        "kb_id": anchor["kb_id"],
        "file_id": anchor["file_id"],
        "source_sha256": anchor["source_sha256"],
        "index_revision_id": anchor["index_revision_id"],
        "quote_head": anchor["quote_head"],
        "quote": anchor.get("quote") or anchor["quote_head"],
        "filename": anchor["filename"],
    }
    # 图注反链：定位命中图注时，反查正文引用该图表的段落，为"解释"子意图提供依据
    await attach_caption_backlinks(db, kb_ids=kb_ids, resolution=resolution)
    return resolution


async def attach_caption_backlinks(db, *, kb_ids: list[str], resolution: dict[str, Any]) -> None:
    """命中图注/题注时反查正文引用段（P4 解释绑定：Figure→Caption→mentioned_by）。

    非致命：反查失败仅记录，不影响定位结论。
    """
    if _CAPTION_LABEL_PATTERN.match(resolution.get("quote") or ""):
        try:
            backlinks = await resolve_caption_backlinks(
                db,
                kb_ids=kb_ids,
                caption_quote=resolution.get("quote") or "",
                exclude_anchor_id=resolution.get("anchor_id"),
            )
            if backlinks:
                resolution["backlinks"] = backlinks
        except Exception as exc:  # noqa: BLE001
            logger.error(f"caption backlink resolution failed (non-fatal): {exc}")


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
    ).all()
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
    """近似兜底：引文归一化后任一 ≥40 字符句段被载体包含（T3 压缩包含兜底）。

    T3 解决 MinerU 存量伪影（词内被插入空格，如 ``Ye ast o f y one``）：
    句段去空白压缩后被载体压缩形式包含即视为命中。
    """
    for sentence in re.split(r"[.!?。！？;；]", quote_norm):
        sentence = sentence.strip()
        if len(sentence) >= 40 and sentence in carrier_norm:
            return True
        if len(sentence) >= 40 and sentence.replace(" ", "") in carrier_norm.replace(" ", ""):
            return True
    return False
