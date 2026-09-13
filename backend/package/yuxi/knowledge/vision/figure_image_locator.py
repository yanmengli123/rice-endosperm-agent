"""Figure Image Locator：上传原图 → FigureEntity → VerifiedLocatorBinding（P2）。

定位入口 ``FIGURE_IMAGE``：用户上传图片问「这个图片在哪篇论文哪一页」。
执行分两段——Stage A 定位（本模块，确定性裁决）与 Stage B 解释（检索证据，
不得反向修改 Stage A 的定位）。

裁决阶梯（Candidate recall 可以宽，page publication 必须极严）：

- V0_EXACT_ASSET_SHA：资产字节摘要完全一致（需 figure_index 持久化资产指纹；
  当前锚点库尚未携带资产摘要，该层预留，指纹入库后自动启用）。
- V1_STRONG_PHASH_LABEL：pHash 强匹配（距离 ≤ 8）**且**图表编号一致（双信号）。
- V2_VISUAL_CONSTRAINTS：编号 + 可见文本/实体/题注片段约束 ≥2 个独立信号。
- V3_MULTI_SIGNAL_HARD_CONSTRAINTS：无编号但可见文本+实体多信号且物理唯一。
- V4_SEMANTIC_ONLY：仅语义相似 → Candidate only，**永不发布页码**。

硬不变量：视觉模型只产出 VisualObservationEnvelope（schema 无 page 字段）；
provider 不可用 / 观察缺失 / 信号不足 / 物理不唯一 → 一律失败关闭
（FIGURE_MATCH_AMBIGUOUS / NOT_FOUND），绝不回退到「模型自由回答页码」。
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select

from yuxi.knowledge.evidence.protocol import derive_evidence_id
from yuxi.knowledge.vision.phash import PHASH_STRONG_DISTANCE, phash_hamming_distance
from yuxi.knowledge.vision.visual_observation import VisualObservationEnvelope
from yuxi.storage.postgres.models_knowledge import (
    EvidenceAnchorRecord,
    EvidenceSpanRecord,
    KnowledgeFile,
    KnowledgeParseRevision,
)
from yuxi.utils import logger

FIGURE_IMAGE_LOCATOR_VERSION = "figure_image_locator_v1"

TIER_V0_EXACT_ASSET_SHA = "V0_EXACT_ASSET_SHA"
TIER_V1_STRONG_PHASH_LABEL = "V1_STRONG_PHASH_LABEL"
TIER_V2_VISUAL_CONSTRAINTS = "V2_VISUAL_CONSTRAINTS"
TIER_V3_MULTI_SIGNAL_HARD_CONSTRAINTS = "V3_MULTI_SIGNAL_HARD_CONSTRAINTS"
TIER_V4_SEMANTIC_ONLY = "V4_SEMANTIC_ONLY"

# 可见文本片段命中题注的最小归一化长度（防止 2-3 字符子串误命中）
_VISIBLE_TEXT_MIN_CHARS = 6

# MinerU image block 的锚点类型（mineru_layout anchor_type = block_type）
_IMAGE_ANCHOR_TYPES = ("image", "figure")


async def build_figure_index(db, *, kb_ids: list[str]) -> list[dict[str, Any]]:
    """从持久化证据行构建 FigureEntity 投影（读取投影，不落新表）。

    实体 = image/figure 锚点（物理 bbox + 页码）+ 同锚 caption span
    （container_label 细化）。资产摘要/pHash 字段在 figure_index artifact
    持久化指纹后自动参与 V0/V1 层（当前为 None → 阶梯从 V2 起步）。
    """
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
                EvidenceAnchorRecord.anchor_type.in_(_IMAGE_ANCHOR_TYPES),
                EvidenceAnchorRecord.page >= 1,
            )
            .order_by(
                EvidenceAnchorRecord.parse_revision_id,
                EvidenceAnchorRecord.page,
            )
            .limit(500)
        )
    ).all()
    anchor_keys = [(str(anchor.parse_revision_id), str(anchor.anchor_id)) for anchor, _, _ in rows]
    spans = (
        (
            await db.execute(
                select(EvidenceSpanRecord).where(
                    EvidenceSpanRecord.parse_revision_id.in_({key[0] for key in anchor_keys}),
                    EvidenceSpanRecord.evidence_type == "caption",
                )
            )
        )
        .scalars()
        .all()
        if anchor_keys
        else []
    )
    caption_span_by_anchor: dict[tuple[str, str], Any] = {}
    for span in spans:
        caption_span_by_anchor.setdefault((str(span.parse_revision_id), str(span.anchor_id)), span)

    entities: list[dict[str, Any]] = []
    for anchor, knowledge_file, revision in rows:
        quote = str(anchor.quote or "")
        page = int(anchor.page)
        span = caption_span_by_anchor.get((str(anchor.parse_revision_id), str(anchor.anchor_id)))
        entities.append(
            {
                "anchor_id": str(anchor.anchor_id),
                "span_id": str(span.span_id) if span is not None else None,
                "span_evidence_id": str(span.evidence_id) if span is not None else None,
                "evidence_id": derive_evidence_id(
                    source_sha256=str(revision.source_sha256),
                    page_number=page,
                    bbox=anchor.bbox,
                    word_start=int(anchor.word_start or 0),
                    word_end=int(anchor.word_end or anchor.word_start or 0),
                    quote_hash=str(anchor.quote_hash or ""),
                    anchor_id=str(anchor.anchor_id),
                ),
                "parse_revision_id": str(anchor.parse_revision_id),
                "index_revision_id": str(knowledge_file.active_index_revision_id or ""),
                "kb_id": str(revision.kb_id),
                "file_id": str(revision.file_id),
                "source_sha256": str(revision.source_sha256),
                "filename": str(knowledge_file.filename or ""),
                "page": page,
                "zone": str(span.document_partition if span is not None else anchor.document_partition or "MAIN_TEXT"),
                "container_label": str(span.container_label) if span is not None and span.container_label else None,
                "caption": quote,
                "caption_norm": _norm(quote),
                "asset_digest": None,  # figure_index artifact 持久化后启用 V0
                "asset_phash": None,  # figure_index artifact 持久化后启用 V1
            }
        )
    return entities


def _norm(text: str) -> str:
    from yuxi.knowledge.rendering.claim_evidence_resolver import normalize_for_match

    return normalize_for_match(text)


def _label_key(label: str | None) -> str | None:
    from yuxi.knowledge.evidence.caption_locator import canonical_figure_label

    return canonical_figure_label(label)


def _text_signal(fragments: list[str], caption_norm: str) -> int:
    """可见文本片段命中题注的信号计数（每个 ≥6 归一化字符的独立片段算一档）。"""
    hits = 0
    for fragment in fragments or []:
        fragment_norm = _norm(str(fragment))
        if len(fragment_norm) >= _VISIBLE_TEXT_MIN_CHARS and fragment_norm in caption_norm:
            hits += 1
    return hits


def adjudicate_figure_candidates(
    observation: VisualObservationEnvelope,
    candidates: list[dict[str, Any]],
    *,
    image_asset_digest: str | None = None,
    image_phash: str | None = None,
) -> dict[str, Any]:
    """确定性裁决：观察信号 × 候选 FigureEntity → 唯一绑定或失败关闭。纯函数。

    返回与 quote locator 同形的 resolution dict（match_tier=Vx）。V4 语义相似
    候选永远不出页码；任何层级都要求物理唯一，否则 MULTIPLE_MATCHES。
    """
    observed_label_key = _label_key(observation.figure_label)
    text_fragments = [str(item) for item in observation.visible_text or []]
    entity_fragments = [str(item) for item in observation.visible_entities or []]
    caption_fragments = [str(item) for item in observation.caption_fragments or []]

    scored: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate_label_key = _label_key(candidate.get("container_label"))
        # 编号硬约束：观察编号与候选编号都明确且不同 → REJECT（同 P1 不变量）
        if observed_label_key and candidate_label_key and observed_label_key != candidate_label_key:
            continue
        caption_norm = str(candidate.get("caption_norm") or "")
        if not caption_norm:
            continue

        label_match = bool(observed_label_key and candidate_label_key == observed_label_key)
        visible_text_hits = _text_signal(text_fragments, caption_norm)
        entity_hits = _text_signal(entity_fragments, caption_norm)
        caption_hits = _text_signal(caption_fragments, caption_norm)
        phash_distance = phash_hamming_distance(image_phash, candidate.get("asset_phash"))
        asset_exact = bool(image_asset_digest and candidate.get("asset_digest") == image_asset_digest)

        # 阶梯判定（宽召回、严发布）
        if asset_exact:
            tier = TIER_V0_EXACT_ASSET_SHA
        elif phash_distance is not None and phash_distance <= PHASH_STRONG_DISTANCE and label_match:
            tier = TIER_V1_STRONG_PHASH_LABEL
        elif label_match and (visible_text_hits + entity_hits + caption_hits) >= 2:
            tier = TIER_V2_VISUAL_CONSTRAINTS
        elif not observed_label_key and visible_text_hits >= 1 and entity_hits >= 1:
            tier = TIER_V3_MULTI_SIGNAL_HARD_CONSTRAINTS
        else:
            continue  # V4 semantic only：候选宽召回，不参与发布

        scored.append(
            {
                "candidate": candidate,
                "tier": tier,
                "signals": {
                    "label_match": label_match,
                    "visible_text_hits": visible_text_hits,
                    "entity_hits": entity_hits,
                    "caption_hits": caption_hits,
                    "phash_distance": phash_distance,
                    "asset_exact": asset_exact,
                },
            }
        )

    if not scored:
        return {
            "status": "NOT_FOUND",
            "locator_version": FIGURE_IMAGE_LOCATOR_VERSION,
            "locator_kind": "FIGURE_IMAGE",
            "reason": "no_figure_candidate_satisfies_two_signal_minimum",
        }
    physical_locations = {
        (
            str(item["candidate"]["parse_revision_id"]),
            str(item["candidate"]["file_id"]),
            int(item["candidate"]["page"]),
        )
        for item in scored
    }
    if len(physical_locations) != 1:
        return {
            "status": "MULTIPLE_MATCHES",
            "locator_version": FIGURE_IMAGE_LOCATOR_VERSION,
            "locator_kind": "FIGURE_IMAGE",
            "match_count": len(physical_locations),
            "reason": "figure_match_ambiguous",
        }
    tier_rank = {
        TIER_V0_EXACT_ASSET_SHA: 0,
        TIER_V1_STRONG_PHASH_LABEL: 1,
        TIER_V2_VISUAL_CONSTRAINTS: 2,
        TIER_V3_MULTI_SIGNAL_HARD_CONSTRAINTS: 3,
    }
    best = sorted(scored, key=lambda item: (tier_rank[item["tier"]], str(item["candidate"]["anchor_id"])))[0]
    candidate = best["candidate"]
    return {
        "status": "VERIFIED",
        "locator_version": FIGURE_IMAGE_LOCATOR_VERSION,
        "locator_kind": "FIGURE_IMAGE",
        "match_tier": best["tier"],
        "page": int(candidate["page"]),
        "zone": candidate.get("zone") or "MAIN_TEXT",
        "anchor_id": candidate["anchor_id"],
        "span_id": candidate.get("span_id"),
        "evidence_id": candidate["evidence_id"],
        "span_evidence_id": candidate.get("span_evidence_id"),
        "evidence_type": "caption" if candidate.get("span_evidence_id") else "image",
        "container_label": candidate.get("container_label") or observation.figure_label,
        "parse_revision_id": candidate["parse_revision_id"],
        "kb_id": candidate["kb_id"],
        "file_id": candidate["file_id"],
        "source_sha256": candidate["source_sha256"],
        "index_revision_id": candidate["index_revision_id"],
        "quote_head": re.sub(r"\s+", " ", str(candidate.get("caption") or "").strip())[:80],
        "quote": str(candidate.get("caption") or "")[:1600],
        "filename": candidate["filename"],
        "visual_signals": best["signals"],
    }


async def resolve_figure_image_locator(
    db,
    *,
    observation: VisualObservationEnvelope | None,
    kb_ids: list[str],
    image_asset_digest: str | None = None,
    image_phash: str | None = None,
) -> dict[str, Any]:
    """图片定位入口：构建 figure index → 确定性裁决（观察缺失即失败关闭）。"""
    candidates = await build_figure_index(db, kb_ids=kb_ids)
    if not candidates:
        return {
            "status": "NOT_FOUND",
            "locator_version": FIGURE_IMAGE_LOCATOR_VERSION,
            "locator_kind": "FIGURE_IMAGE",
            "reason": "figure_index_empty_in_scope",
        }
    if observation is None:
        # 视觉 provider 不可用 / 观察非法：绝不回退到自由回答页码
        return {
            "status": "NOT_FOUND",
            "locator_version": FIGURE_IMAGE_LOCATOR_VERSION,
            "locator_kind": "FIGURE_IMAGE",
            "reason": "vision_observation_unavailable",
        }
    try:
        return adjudicate_figure_candidates(
            observation,
            candidates,
            image_asset_digest=image_asset_digest,
            image_phash=image_phash,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(f"figure image locator failed (fail-closed): {exc}")
        return {
            "status": "NOT_FOUND",
            "locator_version": FIGURE_IMAGE_LOCATOR_VERSION,
            "locator_kind": "FIGURE_IMAGE",
            "reason": "figure_adjudication_error",
        }


__all__ = [
    "FIGURE_IMAGE_LOCATOR_VERSION",
    "TIER_V0_EXACT_ASSET_SHA",
    "TIER_V1_STRONG_PHASH_LABEL",
    "TIER_V2_VISUAL_CONSTRAINTS",
    "TIER_V3_MULTI_SIGNAL_HARD_CONSTRAINTS",
    "TIER_V4_SEMANTIC_ONLY",
    "adjudicate_figure_candidates",
    "build_figure_index",
    "resolve_figure_image_locator",
]
