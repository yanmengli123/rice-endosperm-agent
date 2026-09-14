"""Figure Image Locator：上传原图 → FigureEntity → VerifiedLocatorBinding。

R-P2 起图片索引是**持久化的资产指纹索引**（figure_entities + figure_assets，
入库时由 Figure Ingestor 计算 SHA256/pHash/尺寸/panel 变体指纹），旧解析
版本无持久化行时回退锚点读取投影。

裁决顺序（确定性优先，VLM 最后——视觉模型只描述图片，永不决定页码）：

1. **V0_EXACT_ASSET_SHA**：上传字节 SHA256 与库内资产完全一致 → 直接物理
   绑定，不需要视觉模型。
2. **V1_STRONG_PHASH**：感知哈希强匹配（距离 ≤ 8，整图或 panel 变体）且
   范围内物理唯一 → 绑定并报告 panel_key（用户只上传 (c) 子图仍绑定父图）。
3. **V1_LOCAL_FEATURE_GEOMETRY**：pHash 未决时，对有界候选执行 ORB +
   RANSAC 几何一致性验证，覆盖缩放、重编码和轻度裁切截图。
4. **V2_VISUAL_CONSTRAINTS**：观察编号 + 可见文本/实体/题注片段 ≥2 信号。
5. **V3_MULTI_SIGNAL_HARD_CONSTRAINTS**：无编号但文本+实体多信号且物理唯一。
6. **V4_SEMANTIC_ONLY**：仅语义相似 → Candidate only，永不发布页码。

失败语义：确定性层未决且观察不可用 → ``VISION_PROVIDER_UNAVAILABLE``（显式
暴露，不静默退化为普通问答）；多物理位置 → ``MULTIPLE_MATCHES`` 不发布候选页码。
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from sqlalchemy import select

from yuxi.knowledge.evidence.protocol import derive_evidence_id
from yuxi.knowledge.vision.phash import PHASH_STRONG_DISTANCE, phash_hamming_distance
from yuxi.knowledge.vision.visual_observation import VisualObservationEnvelope
from yuxi.storage.postgres.models_knowledge import (
    EvidenceAnchorRecord,
    EvidenceSpanRecord,
    FigureAssetRecord,
    FigureEntityRecord,
    KnowledgeFile,
    KnowledgeParseRevision,
)
from yuxi.utils import logger

FIGURE_IMAGE_LOCATOR_VERSION = "figure_image_locator_v3"

TIER_V0_EXACT_ASSET_SHA = "V0_EXACT_ASSET_SHA"
TIER_V1_STRONG_PHASH_LABEL = "V1_STRONG_PHASH"
TIER_V1_LOCAL_FEATURE_GEOMETRY = "V1_LOCAL_FEATURE_GEOMETRY"
TIER_V2_VISUAL_CONSTRAINTS = "V2_VISUAL_CONSTRAINTS"
TIER_V3_MULTI_SIGNAL_HARD_CONSTRAINTS = "V3_MULTI_SIGNAL_HARD_CONSTRAINTS"
TIER_V4_SEMANTIC_ONLY = "V4_SEMANTIC_ONLY"

# 可见文本片段命中题注的最小归一化长度（防止 2-3 字符子串误命中）
_VISIBLE_TEXT_MIN_CHARS = 6
_LOCAL_FEATURE_PREFILTER_LIMIT = 12
_LOCAL_FEATURE_DOWNLOAD_CONCURRENCY = 4

# MinerU 视觉块锚点类型（v3 起含 chart）
_IMAGE_ANCHOR_TYPES = ("image", "figure", "chart")


def _norm(text: str) -> str:
    from yuxi.knowledge.rendering.claim_evidence_resolver import normalize_for_match

    return normalize_for_match(text)


def _label_key(label: str | None) -> str | None:
    from yuxi.knowledge.evidence.caption_locator import canonical_figure_label

    return canonical_figure_label(label)


async def build_figure_index(db, *, kb_ids: list[str]) -> list[dict[str, Any]]:
    """查询期 figure index：持久化资产索引优先，旧版本回退锚点读取投影。"""
    entities = await _persisted_figure_index(db, kb_ids=kb_ids)
    if entities:
        return entities
    return await _anchor_figure_index(db, kb_ids=kb_ids)


async def _persisted_figure_index(db, *, kb_ids: list[str]) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(FigureEntityRecord, FigureAssetRecord, KnowledgeFile)
            .join(FigureAssetRecord, FigureAssetRecord.entity_id == FigureEntityRecord.id)
            .join(KnowledgeFile, KnowledgeFile.file_id == FigureEntityRecord.file_id)
            .where(
                FigureEntityRecord.kb_id.in_(list(kb_ids)[:20]),
                KnowledgeFile.kb_id == FigureEntityRecord.kb_id,
                KnowledgeFile.active_parse_revision_id == FigureEntityRecord.parse_revision_id,
            )
            .order_by(FigureEntityRecord.parse_revision_id, FigureEntityRecord.entity_key)
            .limit(600)
        )
    ).all()
    grouped: dict[int, dict[str, Any]] = {}
    for entity, asset, knowledge_file in rows:
        projection = grouped.setdefault(
            entity.id,
            {
                "entity_key": entity.entity_key,
                "container_label": entity.container_label,
                "caption": entity.caption or "",
                "caption_norm": _norm(entity.caption or ""),
                "caption_page": entity.caption_page,
                "kb_id": entity.kb_id,
                "file_id": entity.file_id,
                "filename": str(knowledge_file.filename or ""),
                "source_sha256": entity.source_sha256,
                "parse_revision_id": entity.parse_revision_id,
                "index_revision_id": str(knowledge_file.active_index_revision_id or ""),
                "span_id": entity.caption_span_id,
                "span_evidence_id": entity.caption_span_evidence_id,
                "zone": entity.document_partition or "MAIN_TEXT",
                "assets": [],
            },
        )
        projection["assets"].append(
            {
                "anchor_id": asset.anchor_id or None,
                "page": int(asset.page),
                "bbox": asset.bbox,
                "asset_digest": asset.asset_sha256 or None,
                "asset_phash": asset.asset_phash or None,
                "panel_phashes": dict(asset.panel_phashes or {}),
                "object_bucket": asset.object_bucket,
                "object_name": asset.object_name,
                "img_path": asset.img_path,
                "width": asset.width,
                "height": asset.height,
            }
        )
    return list(grouped.values())


async def _anchor_figure_index(db, *, kb_ids: list[str]) -> list[dict[str, Any]]:
    """旧解析版本回退：视觉锚点 + caption span 的读取投影（无资产指纹）。"""
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
                KnowledgeFile.kb_id == KnowledgeParseRevision.kb_id,
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
    if not rows:
        return []
    revision_ids = {str(anchor.parse_revision_id) for anchor, _, _ in rows}
    spans = (
        (
            await db.execute(
                select(EvidenceSpanRecord).where(
                    EvidenceSpanRecord.parse_revision_id.in_(revision_ids),
                    EvidenceSpanRecord.evidence_type == "caption",
                )
            )
        )
        .scalars()
        .all()
    )
    caption_span_by_anchor: dict[tuple[str, str], Any] = {}
    for span in spans:
        caption_span_by_anchor.setdefault((str(span.parse_revision_id), str(span.anchor_id)), span)

    entities: list[dict[str, Any]] = []
    seen_anchors: set[tuple[str, str]] = set()
    for anchor, knowledge_file, _revision in rows:
        anchor_key = (str(anchor.parse_revision_id), str(anchor.anchor_id))
        if anchor_key in seen_anchors or not anchor.page or int(anchor.page) < 1:
            continue
        seen_anchors.add(anchor_key)
        quote = str(anchor.quote or "")
        span = caption_span_by_anchor.get(anchor_key)
        entities.append(
            {
                "entity_key": _label_key(span.container_label if span is not None else quote)
                or f"asset:{anchor.anchor_id}",
                "container_label": (span.container_label if span is not None else None),
                "caption": quote,
                "caption_norm": _norm(quote),
                "caption_page": int(span.page_number) if span is not None and span.page_number else int(anchor.page),
                "kb_id": None,
                "file_id": str(knowledge_file.file_id),
                "filename": str(knowledge_file.filename or ""),
                "source_sha256": "",
                "parse_revision_id": str(anchor.parse_revision_id),
                "index_revision_id": str(knowledge_file.active_index_revision_id or ""),
                "span_id": str(span.span_id) if span is not None else None,
                "span_evidence_id": str(span.evidence_id) if span is not None else None,
                "zone": str(span.document_partition if span is not None else anchor.document_partition or "MAIN_TEXT"),
                "assets": [
                    {
                        "anchor_id": str(anchor.anchor_id),
                        "page": int(anchor.page),
                        "bbox": anchor.bbox,
                        "asset_digest": None,
                        "asset_phash": None,
                        "panel_phashes": {},
                        "img_path": "",
                        "width": 0,
                        "height": 0,
                    }
                ],
            }
        )
    return entities


def _physical_location(entity: dict[str, Any], asset: dict[str, Any]) -> tuple[str, str, int]:
    return (str(entity["parse_revision_id"]), str(entity["file_id"]), int(asset["page"]))


def _best_phash_match(uploaded_phash: str | None, asset: dict[str, Any]) -> tuple[str | None, int]:
    """整图 + panel 变体的最强感知哈希匹配；返回 (panel_key, 距离) 或 (None, -1)。"""
    if not uploaded_phash:
        return (None, -1)
    best_key: str | None = None
    best_distance = PHASH_STRONG_DISTANCE + 1
    whole = asset.get("asset_phash")
    if whole:
        distance = phash_hamming_distance(uploaded_phash, whole)
        if distance is not None and distance < best_distance:
            best_key, best_distance = "whole", distance
    for panel_key, panel_hash in (asset.get("panel_phashes") or {}).items():
        distance = phash_hamming_distance(uploaded_phash, panel_hash)
        if distance is not None and distance < best_distance:
            best_key, best_distance = panel_key, distance
    return (best_key, best_distance)


def adjudicate_deterministic_match(
    candidates: list[dict[str, Any]],
    *,
    image_asset_digest: str | None = None,
    image_phash: str | None = None,
) -> dict[str, Any]:
    """确定性裁决（V0/V1，零 LLM、零观察）：唯一 → VERIFIED，多位置 → 歧义。

    V0 字节一致是最强信号，单独即可发布；V1 感知哈希强匹配要求范围内物理
    唯一（缩放/压缩/裁剪 panel 均可命中，但同图多处出现时失败关闭）。
    """
    sha_hits: list[tuple[dict[str, Any], dict[str, Any]]] = []
    if image_asset_digest:
        for entity in candidates:
            for asset in entity.get("assets") or []:
                if asset.get("asset_digest") == image_asset_digest:
                    sha_hits.append((entity, asset))
    if sha_hits:
        locations = {_physical_location(entity, asset) for entity, asset in sha_hits}
        if len(locations) != 1:
            return {
                "status": "MULTIPLE_MATCHES",
                "locator_kind": "FIGURE_IMAGE",
                "match_count": len(locations),
                "reason": "exact_asset_hits_multiple_physical_locations",
            }
        entity, asset = sha_hits[0]
        return {
            "status": "VERIFIED",
            "tier": TIER_V0_EXACT_ASSET_SHA,
            "entity": entity,
            "asset": asset,
            "panel_key": "whole",
            "signals": {"asset_sha_exact": True, "phash_distance": None, "panel_key": "whole"},
        }

    phash_hits: list[tuple[dict[str, Any], dict[str, Any], str, int]] = []
    for entity in candidates:
        for asset in entity.get("assets") or []:
            panel_key, distance = _best_phash_match(image_phash, asset)
            if panel_key is not None and distance <= PHASH_STRONG_DISTANCE:
                phash_hits.append((entity, asset, panel_key, distance))
    if phash_hits:
        locations = {_physical_location(entity, asset) for entity, asset, _panel, _distance in phash_hits}
        if len(locations) != 1:
            return {
                "status": "MULTIPLE_MATCHES",
                "locator_kind": "FIGURE_IMAGE",
                "match_count": len(locations),
                "reason": "phash_hits_multiple_physical_locations",
            }
        entity, asset, panel_key, distance = phash_hits[0]
        return {
            "status": "VERIFIED",
            "tier": TIER_V1_STRONG_PHASH_LABEL,
            "entity": entity,
            "asset": asset,
            "panel_key": panel_key,
            "signals": {"asset_sha_exact": False, "phash_distance": distance, "panel_key": panel_key},
        }
    return {"status": "DETERMINISTIC_UNRESOLVED", "locator_kind": "FIGURE_IMAGE"}


def _phash_prefilter_distance(uploaded_phash: str | None, asset: dict[str, Any]) -> int:
    distances: list[int] = []
    for value in [asset.get("asset_phash"), *(asset.get("panel_phashes") or {}).values()]:
        distance = phash_hamming_distance(uploaded_phash, value)
        if distance is not None:
            distances.append(distance)
    return min(distances) if distances else 10_000


async def adjudicate_local_feature_match(
    candidates: list[dict[str, Any]],
    *,
    image_bytes: bytes,
    image_phash: str | None,
) -> dict[str, Any]:
    """Bounded ORB + homography fallback for resized/cropped screenshots.

    pHash is used only to cap internal MinIO reads; it is not part of the publish
    gate.  Local geometry must independently pass and be physically unique.
    """
    from yuxi.knowledge.vision.local_features import match_local_feature_geometry
    from yuxi.storage.minio.client import get_minio_client

    ranked: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    for entity in candidates:
        for asset in entity.get("assets") or []:
            if not asset.get("object_name") or not asset.get("anchor_id"):
                continue
            ranked.append((_phash_prefilter_distance(image_phash, asset), entity, asset))
    ranked.sort(key=lambda item: (item[0], str(item[1].get("entity_key") or ""), str(item[2].get("img_path") or "")))
    ranked = ranked[:_LOCAL_FEATURE_PREFILTER_LIMIT]
    if not ranked:
        return {"status": "DETERMINISTIC_UNRESOLVED", "locator_kind": "FIGURE_IMAGE"}

    client = get_minio_client()
    semaphore = asyncio.Semaphore(_LOCAL_FEATURE_DOWNLOAD_CONCURRENCY)

    async def evaluate(item):
        phash_distance, entity, asset = item
        try:
            async with semaphore:
                candidate_bytes = await client.adownload_file(
                    str(asset.get("object_bucket") or "knowledgebases"),
                    str(asset["object_name"]),
                )
            metrics = await asyncio.to_thread(match_local_feature_geometry, image_bytes, candidate_bytes)
            return entity, asset, phash_distance, metrics
        except Exception as exc:  # noqa: BLE001 - one missing asset cannot widen authority
            logger.warning(f"figure local-feature candidate unavailable: {asset.get('object_name')}: {exc}")
            return entity, asset, phash_distance, {"strong": False}

    evaluated = await asyncio.gather(*(evaluate(item) for item in ranked))
    strong = [item for item in evaluated if bool(item[3].get("strong"))]
    if not strong:
        return {"status": "DETERMINISTIC_UNRESOLVED", "locator_kind": "FIGURE_IMAGE"}
    locations = {_physical_location(entity, asset) for entity, asset, _distance, _metrics in strong}
    if len(locations) != 1:
        return {
            "status": "MULTIPLE_MATCHES",
            "locator_kind": "FIGURE_IMAGE",
            "match_count": len(locations),
            "reason": "local_feature_hits_multiple_physical_locations",
        }
    entity, asset, phash_distance, metrics = max(
        strong,
        key=lambda item: (
            int(item[3].get("inliers") or 0),
            float(item[3].get("inlier_ratio") or 0.0),
            float(item[3].get("query_coverage") or 0.0),
        ),
    )
    return {
        "status": "VERIFIED",
        "tier": TIER_V1_LOCAL_FEATURE_GEOMETRY,
        "entity": entity,
        "asset": asset,
        "panel_key": "geometry",
        "signals": {"phash_distance": phash_distance, "local_feature_geometry": metrics},
    }


def _text_signal(fragments: list[str], caption_norm: str) -> int:
    hits = 0
    for fragment in fragments or []:
        fragment_norm = _norm(str(fragment))
        if len(fragment_norm) >= _VISIBLE_TEXT_MIN_CHARS and fragment_norm in caption_norm:
            hits += 1
    return hits


def adjudicate_figure_candidates(
    observation: VisualObservationEnvelope,
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """观察信号裁决（V2/V3）：编号硬约束 + 双信号最低。纯函数。

    V0/V1（资产指纹）只存在于确定性层（adjudicate_deterministic_match）；
    观察层的编号是模型读图所得，本身不构成资产级确定性，必须叠加文本/
    实体信号且物理唯一才可发布页码。
    """
    observed_label_key = _label_key(observation.figure_label)
    text_fragments = [str(item) for item in observation.visible_text or []]
    entity_fragments = [str(item) for item in observation.visible_entities or []]
    caption_fragments = [str(item) for item in observation.caption_fragments or []]

    scored: list[dict[str, Any]] = []
    for entity in candidates:
        candidate_label_key = _label_key(entity.get("container_label"))
        # 编号硬约束：观察编号与候选编号都明确且不同 → REJECT
        if observed_label_key and candidate_label_key and observed_label_key != candidate_label_key:
            continue
        caption_norm = str(entity.get("caption_norm") or "")
        assets = entity.get("assets") or []
        if not caption_norm or not assets:
            continue
        label_match = bool(observed_label_key and candidate_label_key == observed_label_key)
        visible_text_hits = _text_signal(text_fragments, caption_norm)
        entity_hits = _text_signal(entity_fragments, caption_norm)
        caption_hits = _text_signal(caption_fragments, caption_norm)

        if label_match and (visible_text_hits + entity_hits + caption_hits) >= 2:
            tier = TIER_V2_VISUAL_CONSTRAINTS
        elif not observed_label_key and visible_text_hits >= 1 and entity_hits >= 1:
            tier = TIER_V3_MULTI_SIGNAL_HARD_CONSTRAINTS
        else:
            continue  # V4 semantic only：候选宽召回，不参与发布

        scored.append(
            {
                "entity": entity,
                "asset": assets[0],
                "tier": tier,
                "panel_key": "whole",
                "signals": {
                    "label_match": label_match,
                    "visible_text_hits": visible_text_hits,
                    "entity_hits": entity_hits,
                    "caption_hits": caption_hits,
                    "phash_distance": None,
                    "panel_key": "whole",
                },
            }
        )

    if not scored:
        return {
            "status": "NOT_FOUND",
            "locator_kind": "FIGURE_IMAGE",
            "reason": "no_figure_candidate_satisfies_two_signal_minimum",
        }
    locations = {_physical_location(item["entity"], item["asset"]) for item in scored}
    if len(locations) != 1:
        return {
            "status": "MULTIPLE_MATCHES",
            "locator_kind": "FIGURE_IMAGE",
            "match_count": len(locations),
            "reason": "figure_match_ambiguous",
        }
    tier_rank = {TIER_V2_VISUAL_CONSTRAINTS: 0, TIER_V3_MULTI_SIGNAL_HARD_CONSTRAINTS: 1}
    best = sorted(scored, key=lambda item: (tier_rank[item["tier"]], str(item["entity"]["entity_key"])))[0]
    return {"status": "VERIFIED", **best}


async def _materialize_resolution(db, adjudication: dict[str, Any]) -> dict[str, Any]:
    """把裁决结果落成 locator_resolution：锚点行提供物理血统（evidence_id 等）。"""
    entity = adjudication["entity"]
    asset = adjudication["asset"]
    anchor = (
        (
            (
                await db.execute(
                    select(EvidenceAnchorRecord).where(
                        EvidenceAnchorRecord.parse_revision_id == entity["parse_revision_id"],
                        EvidenceAnchorRecord.anchor_id == str(asset.get("anchor_id") or ""),
                    )
                )
            )
            .scalars()
            .first()
        )
        if asset.get("anchor_id")
        else None
    )
    if anchor is None:
        return {
            "status": "NOT_FOUND",
            "locator_version": FIGURE_IMAGE_LOCATOR_VERSION,
            "locator_kind": "FIGURE_IMAGE",
            "reason": "figure_asset_anchor_unavailable",
        }
    span_evidence_id = entity.get("span_evidence_id")
    span_id = entity.get("span_id")
    if not span_evidence_id:
        span = (
            (
                await db.execute(
                    select(EvidenceSpanRecord).where(
                        EvidenceSpanRecord.parse_revision_id == entity["parse_revision_id"],
                        EvidenceSpanRecord.anchor_id == str(anchor.anchor_id),
                        EvidenceSpanRecord.evidence_type == "caption",
                    )
                )
            )
            .scalars()
            .first()
        )
        span_evidence_id = str(span.evidence_id) if span is not None else None
        span_id = str(span.span_id) if span is not None else None
    asset_page = int(anchor.page)
    caption_page = int(entity.get("caption_page") or asset_page)
    quote = str(anchor.quote or "")
    return {
        "status": "VERIFIED",
        "locator_version": FIGURE_IMAGE_LOCATOR_VERSION,
        "locator_kind": "FIGURE_IMAGE",
        "match_tier": adjudication["tier"],
        # 页码语义（v2）：图片问页 → asset 页；题注问页 → caption 页；跨页时
        # 两个页码都携带，display 由入口意图决定（本入口 display = asset 页）
        "page": asset_page,
        "asset_page": asset_page,
        "caption_page": caption_page,
        "source_page_index": asset_page - 1,
        "zone": entity.get("zone") or "MAIN_TEXT",
        "anchor_id": str(anchor.anchor_id),
        "span_id": span_id,
        "evidence_id": derive_evidence_id(
            source_sha256=str(entity.get("source_sha256") or ""),
            page_number=asset_page,
            bbox=anchor.bbox,
            word_start=int(anchor.word_start or 0),
            word_end=int(anchor.word_end or anchor.word_start or 0),
            quote_hash=str(anchor.quote_hash or ""),
            anchor_id=str(anchor.anchor_id),
        ),
        "span_evidence_id": span_evidence_id,
        "evidence_type": "caption" if span_evidence_id else "image",
        "container_label": entity.get("container_label"),
        "parse_revision_id": entity["parse_revision_id"],
        "kb_id": entity.get("kb_id"),
        "file_id": entity["file_id"],
        "source_sha256": entity.get("source_sha256") or None,
        "index_revision_id": entity.get("index_revision_id"),
        "quote_head": re.sub(r"\s+", " ", quote.strip())[:80],
        "quote": quote[:1600],
        "filename": entity.get("filename"),
        "visual_signals": adjudication.get("signals") or {},
        "panel_match": adjudication.get("panel_key") or None,
    }


async def resolve_figure_image_locator(
    db,
    *,
    kb_ids: list[str],
    image_bytes: bytes | None = None,
    observation: VisualObservationEnvelope | None = None,
) -> dict[str, Any]:
    """图片定位入口：确定性指纹（V0/V1）优先，视觉观察（V2/V3）最后。

    确定性层命中即返回（未配置视觉模型也能完成 SHA/pHash 定位）；两层都
    未决时按观察可用性返回显式失败原因——绝不静默退化为自由回答页码。
    """
    candidates = await build_figure_index(db, kb_ids=kb_ids)
    if not candidates:
        return {
            "status": "NOT_FOUND",
            "locator_version": FIGURE_IMAGE_LOCATOR_VERSION,
            "locator_kind": "FIGURE_IMAGE",
            "reason": "figure_index_empty_in_scope",
        }
    try:
        if image_bytes:
            from yuxi.knowledge.vision.phash import compute_asset_digest, compute_phash

            image_phash = compute_phash(image_bytes)
            deterministic = adjudicate_deterministic_match(
                candidates,
                image_asset_digest=compute_asset_digest(image_bytes),
                image_phash=image_phash,
            )
            if deterministic.get("status") in {"VERIFIED", "MULTIPLE_MATCHES"}:
                if deterministic.get("status") == "MULTIPLE_MATCHES":
                    return {**deterministic, "locator_version": FIGURE_IMAGE_LOCATOR_VERSION}
                return await _materialize_resolution(db, deterministic)
            local_feature = await adjudicate_local_feature_match(
                candidates,
                image_bytes=image_bytes,
                image_phash=image_phash,
            )
            if local_feature.get("status") in {"VERIFIED", "MULTIPLE_MATCHES"}:
                if local_feature.get("status") == "MULTIPLE_MATCHES":
                    return {**local_feature, "locator_version": FIGURE_IMAGE_LOCATOR_VERSION}
                return await _materialize_resolution(db, local_feature)
        if observation is None:
            # 显式暴露视觉通道不可用（不静默退化、不冒充定位）
            return {
                "status": "NOT_FOUND",
                "locator_version": FIGURE_IMAGE_LOCATOR_VERSION,
                "locator_kind": "FIGURE_IMAGE",
                "reason": "VISION_PROVIDER_UNAVAILABLE",
            }
        adjudication = adjudicate_figure_candidates(observation, candidates)
        if adjudication.get("status") != "VERIFIED":
            return {**adjudication, "locator_version": FIGURE_IMAGE_LOCATOR_VERSION}
        return await _materialize_resolution(db, adjudication)
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
    "TIER_V1_LOCAL_FEATURE_GEOMETRY",
    "TIER_V2_VISUAL_CONSTRAINTS",
    "TIER_V3_MULTI_SIGNAL_HARD_CONSTRAINTS",
    "TIER_V4_SEMANTIC_ONLY",
    "adjudicate_deterministic_match",
    "adjudicate_local_feature_match",
    "adjudicate_figure_candidates",
    "build_figure_index",
    "resolve_figure_image_locator",
]
