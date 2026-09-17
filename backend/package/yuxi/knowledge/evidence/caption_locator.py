"""Caption Locator v3：科研题注确定性定位（label 硬约束 + T0-T3 分级匹配）。

2026-09 Figure 5 事故根因：引文抽取取 ``max(candidates, key=len)``，而科研题注
尾部常带跨图几乎相同的统计模板句（``Bar, 1.0 cm`` / ``ANOVA`` / ``Tukey`` /
``P < 0.05``），「最长」反而「辨识度最低」，导致 Figure 5 的问句命中 Figure 4
的题注。v3 的三条规则：

1. **区分度评分替代最长优先**：
   ``score = figure_label_signal + rare_entity_signal + gene_combination_signal
   + numeric_constraint_signal + information_content - boilerplate_penalty``
2. **Figure label 是硬约束，不是加权项**：用户明确给出 Figure 5 时，label 为
   Figure 4 的候选无论文本多匹配都直接 REJECT（`label_conflicts`）。
3. **T0-T3 分级匹配**：模糊匹配永远不能单独发布页码；T2/T3 必须同时满足
   科研硬约束（图表编号、标识符、数字/区间）才可 VERIFIED。

归一化从「Latin regex」升级为科研字符集（± < > = 希腊字母、上下标等是
高价值语义，不是异常字符）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, or_, select

from yuxi.knowledge.rendering.claim_evidence_resolver import (
    extract_hard_constraints,
    normalize_for_match,
)
from yuxi.storage.postgres.models_knowledge import (
    EvidenceAnchorRecord,
    EvidenceSpanRecord,
    KnowledgeFile,
    KnowledgeParseRevision,
)

CAPTION_LOCATOR_VERSION = "caption_locator_v3"

# 匹配分级（从强到弱）；模糊层级必须叠加硬约束才可发布页码
TIER_T0_RAW_EXACT = "T0_RAW_EXACT"
TIER_T1_CANONICAL_EXACT = "T1_CANONICAL_EXACT"
TIER_LABEL_CONTAINER_EXACT = "LABEL_CONTAINER_EXACT"
TIER_T2_TOKEN_HARD_CONSTRAINTS = "T2_TOKEN_HARD_CONSTRAINTS"
TIER_T3_WHITESPACE_COMPRESSED = "T3_WHITESPACE_COMPRESSED"

# ---- Figure/Table 编号：抽取 + 规范键 + 硬约束 ----

# panel 字母只在与数字直接相邻时才算编号组成部分（"Figure 5A"）；带空格的
# "Figure 7 A schematic..." 中 A 是英文单词开头，不得把编号误归一成 figure 7a
# 限定词：Supplementary/Supplemental Figure N → figure sN（与 "Figure S8" 写法共用规范键）；
# Extended Data Fig. N → figure edN。不扩展前，主图 Figure 2 与 Supplementary Figure 2 会
# 归一成同一键，跨文献/同文献都会被误判为 MULTIPLE。
_LABEL_QUALIFIER = r"(?:(supplementa(?:ry|l)|extended\s+data|ext\.?\s*data)\s+)?"
_LABEL_PATTERN = re.compile(
    _LABEL_QUALIFIER + r"(?:fig(?:ure)?s?\.?|table|图|表)\s*[sS]?\s*\d{1,3}(?:[A-Za-z](?![A-Za-z0-9]))?",
    re.IGNORECASE,
)
_LABEL_FULL = re.compile(
    _LABEL_QUALIFIER + r"(fig(?:ure)?s?|table|图|表)\.?\s*([sS])?\.?\s*(\d{1,3})(?:([A-Za-z])(?![A-Za-z0-9]))?",
    re.IGNORECASE,
)


def canonical_figure_label(label: str | None) -> str | None:
    """归一图表编号为规范键：Fig.5 / figure S8 / 图5A → figure 5 / figure s8 / figure 5a；
    Supplementary Figure 2 → figure s2；Extended Data Fig. 1 → figure ed1。"""
    match = _LABEL_FULL.fullmatch(re.sub(r"\s+", " ", str(label or "").strip()))
    if not match:
        return None
    qualifier, kind, suffix, number, panel = match.groups()
    kind_key = "table" if kind.lower().startswith("tab") or kind == "表" else "figure"
    prefix = ""
    if qualifier:
        prefix = "ed" if qualifier.lower().startswith("ext") else "s"
    suffix_key = (suffix or "").lower()
    if prefix == "s" and suffix_key == "s":
        suffix_key = ""
    return f"{kind_key} {prefix}{suffix_key}{number}{(panel or '').lower()}"


def label_number(label_key: str | None) -> str | None:
    """规范键中的编号数字（"figure s12" → "12"），供 SQL 预过滤使用。"""
    match = re.search(r"\d{1,3}", str(label_key or ""))
    return match.group(0) if match else None


def extract_figure_label(text: str | None) -> str | None:
    """抽取文本中第一个图表编号（原文片段；题注句首即自身编号）。"""
    match = _LABEL_PATTERN.search(str(text or ""))
    return re.sub(r"\s+", " ", match.group(0)).strip() if match else None


def label_conflicts(input_label: str | None, candidate_label: str | None) -> bool:
    """label 硬约束：双方编号都明确且规范键不同 → 冲突（REJECT，绝不降权）。"""
    input_key = canonical_figure_label(input_label)
    candidate_key = canonical_figure_label(candidate_label)
    if input_key is None or candidate_key is None:
        return False
    return input_key != candidate_key


def carrier_label_conflicts(input_label: str | None, carrier_text: str | None) -> bool:
    """载体（题注/锚点引文）句首编号与输入 label 冲突 → 候选剔除。"""
    if not input_label:
        return False
    return label_conflicts(input_label, extract_figure_label(carrier_text))


# ---- T0-T3 分级匹配 ----


def _token_covered(quote_norm: str, carrier_norm: str) -> bool:
    quote_tokens = [token for token in quote_norm.split() if token]
    if not quote_tokens:
        return False
    carrier_tokens = set(carrier_norm.split())
    return all(token in carrier_tokens for token in quote_tokens)


def hard_constraints_satisfied(quote_text: str, carrier_norm: str) -> bool:
    """T2/T3 的硬约束：图表编号、基因/缩写标识符、数字/区间必须全部出现在载体中。"""
    hard = extract_hard_constraints(quote_text)
    if not hard["numbers"] and not hard["identifiers"]:
        return False  # 无约束时不启用模糊层级（由 T0/T1 或词面重叠层裁决）
    for number in hard["numbers"]:
        if number not in carrier_norm:
            return False
    for identifier in hard["identifiers"]:
        if normalize_for_match(identifier) not in carrier_norm:
            return False
    return True


def match_tier(quote: str | None, carrier: str | None) -> str | None:
    """确定性分级匹配：返回最高匹配层级；None = 不匹配。纯函数。

    T0 原文相等 → T1 规范化（NFKC/连字/破折号/空白）相等或包含 →
    T2 词集覆盖 + 硬约束全满足 → T3 去空白压缩包含（MinerU 存量伪影，
    如 ``Ye ast o f y one``）。T2/T3 单独不足以发布页码——调用方还必须
    验证物理唯一性。
    """
    quote_text = str(quote or "").strip()
    carrier_text = str(carrier or "").strip()
    if not quote_text or not carrier_text:
        return None
    if quote_text == carrier_text:
        return TIER_T0_RAW_EXACT
    quote_norm = normalize_for_match(quote_text)
    carrier_norm = normalize_for_match(carrier_text)
    if not quote_norm or not carrier_norm:
        return None
    if quote_norm == carrier_norm or quote_norm in carrier_norm:
        return TIER_T1_CANONICAL_EXACT
    if _token_covered(quote_norm, carrier_norm) and hard_constraints_satisfied(quote_text, carrier_norm):
        return TIER_T2_TOKEN_HARD_CONSTRAINTS
    if len(quote_norm) >= 25 and quote_norm.replace(" ", "") in carrier_norm.replace(" ", ""):
        if hard_constraints_satisfied(quote_text, carrier_norm.replace(" ", "")):
            return TIER_T3_WHITESPACE_COMPRESSED
    return None


# ---- 区分度评分（替代 max(len)）与多候选选择 ----

# 统计模板句：跨图几乎逐字重复，辨识度最低
_BOILERPLATE_PATTERNS = (
    re.compile(r"\bbar[s]?\s*[,=]?\s*\d*\.?\d*\s*(?:cm|mm|µm|μm|um)\b", re.IGNORECASE),
    re.compile(r"\bscale\s+bars?\b", re.IGNORECASE),
    re.compile(r"\blowercase\s+letters?\s+indicate\b", re.IGNORECASE),
    re.compile(r"\berror\s+bars?\s+(?:indicate|represent|show)\b", re.IGNORECASE),
    re.compile(r"\b(?:ANOVA|Tukey|Duncan|LSD\s+test|Student'?s?\s*t[- ]?test)\b", re.IGNORECASE),
    re.compile(r"\bP\s*[<=>]\s*0?\.\d+\b", re.IGNORECASE),
    re.compile(r"\bmeans?\s*[±+\-]\s*(?:SD|SE|SEM)\b", re.IGNORECASE),
    re.compile(r"\bsignificant(?:ly)?\s+different\b", re.IGNORECASE),
)
_BOILERPLATE_PENALTY = 3.0
_GENE_LIKE = re.compile(r"\b(?=[A-Za-z]{2,}\d)[A-Za-z][A-Za-z0-9-]{2,}\b")
_ACRONYM = re.compile(r"\b[A-Z]{3,}\b")
_NUMBER_RANGE = re.compile(r"\b\d+\s*[-–]\s*\d+\b")


def score_caption_text(text: str | None) -> float:
    """区分度评分：基因符号/编号组合加分，统计模板句重罚。纯函数。

    分值用于候选排序（同一问题里的多个拉丁段、同一 label 下的多个题注
    句），不单独决定 VERIFIED——那是 match_tier + 物理唯一性的职责。
    """
    source = str(text or "")
    if not source.strip():
        return 0.0
    score = 0.0
    if extract_figure_label(source):
        score += 1.0
    identifiers = set(_GENE_LIKE.findall(source)) | {
        match.group(0) for match in _ACRONYM.finditer(source) if not match.group(0).isalpha() or True
    }
    identifiers.discard("")
    score += min(1.5 * len(identifiers), 3.0)
    if len(identifiers) >= 2:
        score += 0.5  # 基因组合（OsMYB73 + OsNF-YB1）是强区分信号
    if _NUMBER_RANGE.search(source):
        score += 0.5  # 数值区间（115-164 aa）约束强
    token_count = len([token for token in re.findall(r"[A-Za-z0-9]+", source)])
    score += min(token_count / 20.0, 2.0)  # 信息量
    boilerplate_hits = sum(1 for pattern in _BOILERPLATE_PATTERNS if pattern.search(source))
    score -= min(_BOILERPLATE_PENALTY * boilerplate_hits, 6.0)
    return round(score, 3)


# 科研字符集：± < > = ≈ 希腊字母、上下标、% + 是高价值语义，不是异常字符
_LATIN_RUN = re.compile(
    r"[A-Za-z\u0370-\u03ff]"
    r"[A-Za-z0-9\u0370-\u03ff,'\u2019\-–—()./%\s±×÷≈≤≥<>~°µ²³+\u2070-\u209f]{24,}"
)


def extract_latin_runs(text: str | None) -> list[str]:
    """抽取问题中全部拉丁/希腊连续段（科研字符集），供多候选评分。"""
    return [match.group(0).strip() for match in _LATIN_RUN.finditer(str(text or ""))]


def select_quote_candidates(text: str | None, *, min_normalized_chars: int = 25) -> list[str]:
    """按区分度排序的引文候选（替代 ``max(candidates, key=len)``）。

    返回降序列表；调用方取首位为 quote 候选。统计模板段即使更长也排在
    含基因符号/编号组合的段之后。
    """
    candidates = [run for run in extract_latin_runs(text) if len(normalize_for_match(run)) >= min_normalized_chars]
    return sorted(candidates, key=lambda run: (score_caption_text(run), len(run)), reverse=True)


# ---- 题注通道：label 硬约束下的 caption span 定位 ----


async def _scan_caption_rows(db, *, figure_label: str, kb_ids: list[str], file_ids: list[str] | None = None):
    """label 硬过滤后的题注候选行（span/anchor/file/revision 四元组）。

    统一施加：编号规范键一致、页码合法、span/锚点跨源页码一致、分区可定位、
    非目录/清单行。文本入口与视觉桥接（FigureCaptionQuery）共用本扫描。
    ``file_ids`` 为"哪篇文献"的硬约束（@doc 提及 / DOI / 文件名解析得到），非空时只扫这些文件。
    """
    from yuxi.knowledge.evidence.document_partition import (
        LOCATOR_ELIGIBLE_PARTITIONS,
        effective_partition,
    )
    from yuxi.knowledge.evidence.quote_locator import is_toc_like
    from yuxi.knowledge.evidence.verbatim import escape_like

    label_key = canonical_figure_label(figure_label)
    if not label_key:
        return []
    # SQL 预过滤按编号数字：容器标签 "Fig. 2" / "Figure 2" / "图2" 写法不同但数字一致，
    # 原先按输入原文 ilike 会把 "Fig. 2" 问 "Figure 2" 的题注整个漏掉；规范键相等在 Python 层严格判定
    number = label_number(label_key) or figure_label
    conditions = [
        EvidenceSpanRecord.kb_id.in_(list(kb_ids)[:20]),
        EvidenceSpanRecord.evidence_type == "caption",
        EvidenceSpanRecord.container_label.isnot(None),
        KnowledgeFile.active_parse_revision_id == EvidenceSpanRecord.parse_revision_id,
        KnowledgeParseRevision.file_id == EvidenceSpanRecord.file_id,
        KnowledgeParseRevision.kb_id == EvidenceSpanRecord.kb_id,
        or_(
            EvidenceSpanRecord.container_label.ilike(f"%{escape_like(number)}%", escape="/"),
            EvidenceSpanRecord.quote.ilike(f"%{escape_like(figure_label)}%", escape="/"),
        ),
    ]
    if file_ids:
        conditions.append(EvidenceSpanRecord.file_id.in_([str(item) for item in file_ids][:50]))
    rows = (
        await db.execute(
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
            .where(*conditions)
            .order_by(
                EvidenceSpanRecord.file_id,
                EvidenceSpanRecord.page_number,
                EvidenceSpanRecord.sentence_index,
            )
            .limit(500)
        )
    ).all()

    scanned: list[tuple[Any, Any, Any, Any]] = []
    seen_anchors: set[tuple[str, str]] = set()
    for span, anchor, knowledge_file, revision in rows:
        # label 硬约束：container_label 规范键必须与输入一致（Figure 4 永不冒充 Figure 5）
        if canonical_figure_label(span.container_label) != label_key:
            continue
        if not anchor.page or int(anchor.page) < 1:
            continue
        # 跨源页码一致性守卫：caption span 页（Markdown 层）与锚点页（MinerU 物理层）
        # 不一致说明页归属可疑（MinerU 误归属），不发布该候选
        if span.page_number is not None and int(span.page_number) >= 1 and int(span.page_number) != int(anchor.page):
            continue
        anchor_key = (str(anchor.parse_revision_id), str(anchor.anchor_id))
        if anchor_key in seen_anchors:
            continue
        anchor_quote = str(anchor.quote or "")
        partition = effective_partition(anchor.document_partition, page=int(anchor.page))
        if partition not in LOCATOR_ELIGIBLE_PARTITIONS:
            continue
        if is_toc_like(anchor_quote, evidence_type=span.evidence_type, partition=partition) or is_toc_like(
            str(span.quote or ""), evidence_type=span.evidence_type, partition=partition
        ):
            continue
        # 资格收口（Invariant 5）：页眉/页脚/running head 页码再准也不作回答证据。
        # footer-caption 存量兼容（Figure 4 事故）：题注位于页面底部被 MinerU 归为
        # footer，语义角色仍是 caption——caption_layout_exception 全条件放行。
        from yuxi.knowledge.evidence.anchor_eligibility import (
            anchor_answer_eligible,
            caption_layout_exception,
        )

        if not anchor_answer_eligible(anchor) and not caption_layout_exception(span, anchor):
            continue
        seen_anchors.add(anchor_key)
        scanned.append((span, anchor, knowledge_file, revision))
    return scanned


def _candidate_from_row(span, anchor, knowledge_file, revision, *, tier: str | None) -> dict[str, Any]:
    from yuxi.knowledge.evidence.protocol import derive_evidence_id

    anchor_quote = str(anchor.quote or "")
    return {
        "anchor_id": str(anchor.anchor_id),
        "span_id": str(span.span_id),
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
        "index_revision_id": str(knowledge_file.active_index_revision_id or ""),
        "filename": str(knowledge_file.filename or ""),
        "page": int(anchor.page),
        "quote_head": re.sub(r"\s+", " ", anchor_quote.strip())[:80],
        "quote": anchor_quote[:1600],
        "zone": effective_partition_of(anchor),
        "tier": tier or TIER_LABEL_CONTAINER_EXACT,
    }


def effective_partition_of(anchor) -> str:
    from yuxi.knowledge.evidence.document_partition import effective_partition

    return effective_partition(anchor.document_partition, page=int(anchor.page))


_TIER_RANK = {
    TIER_T0_RAW_EXACT: 0,
    TIER_T1_CANONICAL_EXACT: 1,
    TIER_LABEL_CONTAINER_EXACT: 2,
    TIER_T2_TOKEN_HARD_CONSTRAINTS: 3,
    TIER_T3_WHITESPACE_COMPRESSED: 4,
}


def _best_verbatim_tier(verbatim_segments, carrier_quote: str) -> str | None:
    """可见文本在题注载体上的最高**可发布**匹配层级。

    只放行 T0/T1（逐字相等 / 规范化包含）。T2/T3 允许 VLM 概述文本以次优
    形态通过，对「逐字观察」类输入不可信，本接合口一律不放行（返回 None）。
    """
    for segment in verbatim_segments:
        tier = match_tier(segment, carrier_quote)
        if tier in (TIER_T0_RAW_EXACT, TIER_T1_CANONICAL_EXACT):
            return tier
    return None


def _entities_in_caption(visible_entities, caption_quote: str) -> bool:
    """可见实体须全部以规范化形式出现在真实题注中（辅助消歧，不作独立发布条件）。"""
    norm = normalize_for_match(caption_quote)
    return bool(visible_entities) and all(
        normalize_for_match(str(entity)) in norm for entity in visible_entities if str(entity)
    )


@dataclass(frozen=True)
class FigureCaptionQuery:
    """结构化题注定位查询（P0-C 视觉桥接入口）。

    只允许确定性入参；模型自述的页码/文件/anchor 物理上不在本契约内：
    - ``canonical_label``：观察得出的图表编号（如 ``Figure 1``）——label 硬过滤；
    - ``verbatim_segments``：图中逐字照抄的可见文本——T0/T1 验证（可发布信号）；
    - ``visible_entities``：图中可见标识符——仅在题注中精确出现且物理唯一时协助消歧。
    ``inferred_caption_fragments`` 属推测字段，不进入本契约（仅审计展示）。
    """

    canonical_label: str
    verbatim_segments: tuple[str, ...] = ()
    visible_entities: tuple[str, ...] = ()
    source: str = "VISUAL_OBSERVATION"


async def resolve_caption_bridge(
    db, *, query: FigureCaptionQuery, kb_ids: list[str], file_ids: list[str] | None = None
) -> dict[str, Any] | None:
    """观察 → 题注通道确定性接合（P0-C）。

    图片定位指纹（V0/V1/V1G）未命中且观察给出编号时，由本桥接把观察结果
    转交互题注通道裁决（label 硬过滤 + T0/T1 + 实体消歧 + 物理唯一）；页码
    只可能从 caption anchor.page 出来。失败返回 None（调用方失败关闭）。
    """
    scanned = await _scan_caption_rows(db, figure_label=query.canonical_label, kb_ids=kb_ids, file_ids=file_ids)
    if not scanned:
        return None
    candidates: list[dict[str, Any]] = []
    for span, anchor, knowledge_file, revision in scanned:
        anchor_quote = str(anchor.quote or "")
        verbatim_tier = _best_verbatim_tier(query.verbatim_segments, anchor_quote)
        if verbatim_tier is not None:
            tier = verbatim_tier
        elif query.visible_entities and _entities_in_caption(query.visible_entities, anchor_quote):
            tier = TIER_LABEL_CONTAINER_EXACT
        else:
            continue
        candidates.append(_candidate_from_row(span, anchor, knowledge_file, revision, tier=tier))
    return _adjudicate_caption_candidates(candidates, container_label=query.canonical_label)


def _adjudicate_caption_candidates(candidates: list[dict[str, Any]], *, container_label: str) -> dict[str, Any] | None:
    """物理唯一性 + 区分度排序 → VERIFIED / MULTIPLE_MATCHES / None。"""
    if not candidates:
        return None
    physical_locations = {
        # G7：唯一性键含 anchor_id——同页两个同编号候选（跨块题注/上下双栏）
        # 不被静默合并，也无法互相冒充唯一页码
        (candidate["parse_revision_id"], candidate["file_id"], candidate["page"], candidate["anchor_id"])
        for candidate in candidates
    }
    if len(physical_locations) > 1:
        # 候选文献清单：只列文档身份（file_id/kb_id/filename），不带任何页码——供用户 @ 指定
        # 文献后重试；同一文献内多处命中时清单只有一个文档，调用方不提示"选文献"
        documents: dict[str, dict[str, Any]] = {}
        for candidate in candidates:
            file_id = str(candidate.get("file_id") or "")
            if not file_id:
                continue
            documents.setdefault(
                file_id,
                {
                    "file_id": file_id,
                    "kb_id": str(candidate.get("kb_id") or ""),
                    "filename": str(candidate.get("filename") or ""),
                },
            )
        return {
            "status": "MULTIPLE_MATCHES",
            "locator_version": CAPTION_LOCATOR_VERSION,
            "locator_kind": "FIGURE_CAPTION",
            "match_count": len(physical_locations),
            "reason": "figure_label_hits_multiple_physical_locations",
            "candidate_documents": sorted(documents.values(), key=lambda item: (item["filename"], item["file_id"])),
        }
    best = sorted(
        candidates,
        key=lambda item: (
            _TIER_RANK.get(item["tier"], 3),
            -score_caption_text(item["quote"]),
            item["anchor_id"],
        ),
    )[0]
    return {
        "status": "VERIFIED",
        "locator_version": CAPTION_LOCATOR_VERSION,
        "locator_kind": "FIGURE_CAPTION",
        "match_tier": best["tier"],
        "page": best["page"],
        "zone": best["zone"],
        "anchor_id": best["anchor_id"],
        "span_id": best["span_id"],
        "evidence_id": best["evidence_id"],
        "span_evidence_id": best["span_evidence_id"],
        "evidence_type": "caption",
        "container_label": container_label,
        "parse_revision_id": best["parse_revision_id"],
        "kb_id": best["kb_id"],
        "file_id": best["file_id"],
        "source_sha256": best["source_sha256"],
        "index_revision_id": best["index_revision_id"],
        "quote_head": best["quote_head"],
        "quote": best["quote"],
        "filename": best["filename"],
    }


async def search_captions_by_observation(
    db,
    *,
    observation,
    kb_ids: list[str],
    file_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """P1 降级：用 VLM 观察的可见实体/文字在题注语料做确定性文本搜索。

    返回命中的候选 caption（含编号与文件身份），**不带页码**——这些结果仅用于
    "可能来自 Figure N"的提示与用户引导，永不参与页码/文献发布（只有 VERIFIED
    Binding 才能发页码）。观察是 Observation 不是 Authority。
    """
    from yuxi.knowledge.evidence.verbatim import escape_like

    entity_fragments = [str(item) for item in (observation.visible_entities or []) if len(str(item).strip()) >= 3][:8]
    text_fragments = [str(item) for item in (observation.visible_text or []) if len(str(item).strip()) >= 6][:6]
    fragments = entity_fragments + text_fragments
    if not fragments:
        return []

    conditions = [
        EvidenceSpanRecord.kb_id.in_(list(kb_ids)[:20]),
        EvidenceSpanRecord.evidence_type == "caption",
        EvidenceSpanRecord.container_label.isnot(None),
        KnowledgeFile.active_parse_revision_id == EvidenceSpanRecord.parse_revision_id,
        KnowledgeParseRevision.file_id == EvidenceSpanRecord.file_id,
        KnowledgeParseRevision.kb_id == EvidenceSpanRecord.kb_id,
    ]
    if file_ids:
        conditions.append(EvidenceSpanRecord.file_id.in_([str(item) for item in file_ids][:50]))
    rows = (
        await db.execute(
            select(EvidenceSpanRecord, KnowledgeFile)
            .join(KnowledgeFile, KnowledgeFile.file_id == EvidenceSpanRecord.file_id)
            .join(
                KnowledgeParseRevision,
                KnowledgeParseRevision.revision_id == EvidenceSpanRecord.parse_revision_id,
            )
            .where(
                *conditions,
                or_(
                    *[EvidenceSpanRecord.quote.ilike(f"%{escape_like(frag)}%", escape="/") for frag in fragments[:6]],
                ),
            )
            .order_by(EvidenceSpanRecord.file_id, EvidenceSpanRecord.page_number)
            .limit(50)
        )
    ).all()

    scored: dict[tuple[str, str], dict[str, Any]] = {}
    for span, knowledge_file in rows:
        caption_norm = normalize_for_match(str(span.quote or ""))
        hits = sum(1 for frag in fragments if normalize_for_match(frag) in caption_norm)
        if hits == 0:
            continue
        key = (str(span.file_id), str(span.container_label or ""))
        entry = scored.setdefault(
            key,
            {
                "file_id": str(span.file_id),
                "kb_id": str(span.kb_id or ""),
                "filename": str(knowledge_file.filename or ""),
                "figure_label": str(span.container_label or ""),
                "caption_head": re.sub(r"\s+", " ", str(span.quote or ""))[:120],
                "signal_hits": 0,
            },
        )
        entry["signal_hits"] = max(entry["signal_hits"], hits)
    return sorted(scored.values(), key=lambda item: (-item["signal_hits"], item["figure_label"]))[:5]


async def resolve_figure_caption_locator(
    db,
    *,
    figure_label: str,
    quote_text: str | None = None,
    kb_ids: list[str],
    file_ids: list[str] | None = None,
) -> dict[str, Any] | None:
    """题注通道定位（文本/编号入口，兼容旧接口）。

    返回与 :func:`yuxi.knowledge.evidence.quote_locator.resolve_quote_locator`
    同形的 resolution dict（含 locator_kind=FIGURE_CAPTION 与 match_tier）；
    范围内不存在该 label 的题注时返回 None（调用方回退常规引文路径）。
    失败关闭：同 label 多个物理位置且无法消歧 → MULTIPLE_MATCHES，无候选页码
    （但携带 candidate_documents 供用户 @ 指定文献）。``file_ids`` 为文献硬约束。
    """
    scanned = await _scan_caption_rows(db, figure_label=figure_label, kb_ids=kb_ids, file_ids=file_ids)
    candidates: list[dict[str, Any]] = []
    for span, anchor, knowledge_file, revision in scanned:
        anchor_quote = str(anchor.quote or "")
        tier = match_tier(quote_text, anchor_quote) if quote_text else None
        if quote_text and tier is None:
            continue  # 有引文片段时必须匹配上题注载体（T0-T3 之一）
        candidates.append(_candidate_from_row(span, anchor, knowledge_file, revision, tier=tier))
    return _adjudicate_caption_candidates(candidates, container_label=figure_label)


__all__ = [
    "CAPTION_LOCATOR_VERSION",
    "FigureCaptionQuery",
    "TIER_LABEL_CONTAINER_EXACT",
    "TIER_T0_RAW_EXACT",
    "TIER_T1_CANONICAL_EXACT",
    "TIER_T2_TOKEN_HARD_CONSTRAINTS",
    "TIER_T3_WHITESPACE_COMPRESSED",
    "canonical_figure_label",
    "carrier_label_conflicts",
    "extract_figure_label",
    "extract_latin_runs",
    "hard_constraints_satisfied",
    "label_conflicts",
    "label_number",
    "match_tier",
    "resolve_caption_bridge",
    "resolve_figure_caption_locator",
    "score_caption_text",
    "search_captions_by_observation",
    "select_quote_candidates",
]
