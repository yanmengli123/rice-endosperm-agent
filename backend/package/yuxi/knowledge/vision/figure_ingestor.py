"""Figure Ingestor（R-P2）：解析入库时构建持久化图表资产指纹索引。

职责：把 MinerU 视觉块（image/figure/chart）转成版本化的 FigureEntity +
FigureAsset 记录——下载 MinIO 图片字节，计算 SHA256、感知哈希、尺寸与
panel 变体指纹，使上传图片可通过 V0（字节一致）/V1（感知哈希）确定性定位，
不依赖视觉模型。重解析时随 parse_revision 级联重建，不原地修改旧解析事实。

失败语义：指纹计算失败（对象缺失/解码失败）不阻断入库——资产行保留
（label/题注/页码仍可用），指纹字段留空并在 summary 中计数，绝不编造。
"""

from __future__ import annotations

import posixpath
from typing import Any

from sqlalchemy import delete

from yuxi.knowledge.evidence.caption_locator import canonical_figure_label, extract_figure_label
from yuxi.knowledge.pdf_evidence.asset_paths import revision_image_prefix
from yuxi.knowledge.rendering.claim_evidence_resolver import normalize_for_match
from yuxi.knowledge.vision.phash import (
    compute_asset_digest,
    compute_panel_phashes,
    compute_phash,
    image_dimensions,
)
from yuxi.storage.postgres.models_knowledge import (
    FigureAssetRecord,
    FigureEntityRecord,
)
from yuxi.utils import logger

FIGURE_INGESTOR_VERSION = "figure_ingestor_v3"

# 与 ScientificPdfPipeline engine_params 一致（图片上传的目标 bucket）
FIGURE_ASSET_BUCKET = "knowledgebases"
_MAX_ASSETS_PER_REVISION = 200


def _entity_key_for_caption(caption: str) -> str | None:
    label = canonical_figure_label(extract_figure_label(caption) or "")
    return label


def _caption_matches_visual_anchor(caption: str, anchor_quote: str) -> bool:
    """Strictly validate a caption-to-visual-anchor lineage repair.

    MinerU occasionally prefixes a chart caption with a panel marker such as
    ``(i)`` in the physical anchor while the Markdown caption omits it.  The
    figure label must still agree and the normalized full caption must contain
    the other side; shared statistical boilerplate alone is never sufficient.
    """
    caption_label = _entity_key_for_caption(caption)
    anchor_label = _entity_key_for_caption(anchor_quote)
    if not caption_label or caption_label != anchor_label:
        return False
    caption_norm = normalize_for_match(caption)
    anchor_norm = normalize_for_match(anchor_quote)
    if min(len(caption_norm), len(anchor_norm)) < 40:
        return False
    return caption_norm == anchor_norm or caption_norm in anchor_norm or anchor_norm in caption_norm


async def _resolve_asset_objects(revision: Any) -> dict[str, str]:
    """版本专属前缀列表 → {safe_name: object_name}。"""
    from yuxi.storage.minio.client import get_minio_client

    client = get_minio_client()
    prefix = revision_image_prefix(
        tenant_id=int(revision.tenant_id),
        source_sha256=str(revision.source_sha256),
        revision_id=str(revision.revision_id),
    )
    try:
        names = await client.alist_object_names_by_prefix(FIGURE_ASSET_BUCKET, f"{prefix}/")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"figure ingestor: asset prefix list failed (fingerprints skipped): {exc}")
        return {}
    mapping: dict[str, list[str]] = {}
    for object_name in names:
        safe_name = posixpath.basename(object_name)
        # 对象名形如 {prefix}/{digest[:24]}-{safe_name}
        marker = safe_name.find("-")
        if marker > 0:
            safe_name = safe_name[marker + 1 :]
        mapping.setdefault(safe_name, []).append(object_name)
    return {name: candidates[0] for name, candidates in mapping.items() if len(candidates) == 1}


async def persist_figure_index(
    session,
    *,
    revision: Any,
    article_assets: list[dict[str, Any]],
    spans: list[Any],
    anchors: list[Any] | None = None,
) -> dict[str, Any]:
    """为一个 parse revision 构建 figure_entities / figure_assets（幂等重试）。

    返回 summary（计入 qa_report.figure_index）：实体/资产/指纹命中计数。
    """
    revision_id = str(revision.revision_id)
    await session.execute(delete(FigureAssetRecord).where(FigureAssetRecord.parse_revision_id == revision_id))
    await session.execute(delete(FigureEntityRecord).where(FigureEntityRecord.parse_revision_id == revision_id))

    visual_assets = [
        asset
        for asset in (article_assets or [])
        if isinstance(asset, dict) and str(asset.get("kind") or "") == "figure" and int(asset.get("page") or 0) >= 1
    ][:_MAX_ASSETS_PER_REVISION]
    if not visual_assets:
        return {
            "version": FIGURE_INGESTOR_VERSION,
            "entities": 0,
            "assets": 0,
            "anchored_assets": 0,
            "fingerprinted": 0,
            "fingerprint_misses": 0,
            "locator_ready_assets": 0,
            "repaired_caption_spans": 0,
        }

    caption_spans = [span for span in spans or [] if str(span.evidence_type) == "caption"]
    span_by_label: dict[str, Any] = {}
    for span in caption_spans:
        label_key = canonical_figure_label(span.container_label or "")
        if label_key:
            span_by_label.setdefault(label_key, span)

    # 视觉块锚点（页码 + 题注文本一致）回填资产 anchor_id：确定性命中后
    # 由该锚点行构建绑定的物理血统
    anchor_by_identity: dict[tuple[int, str], Any] = {}
    for anchor in anchors or []:
        if str(getattr(anchor, "anchor_type", "")) in {"image", "figure", "chart"}:
            anchor_by_identity.setdefault(
                (int(anchor.page or 0), str(anchor.quote or "").strip()),
                anchor,
            )
    anchor_by_page: dict[int, list[Any]] = {}
    for (page, _quote), anchor in anchor_by_identity.items():
        anchor_by_page.setdefault(page, []).append(anchor)
    paired_anchor_ids: set[str] = set()

    object_by_name = await _resolve_asset_objects(revision)
    minio_client = None

    # 同一图表编号的多个视觉块聚合为一个实体；无编号块按 img_path 独立成实体
    entities: dict[str, dict[str, Any]] = {}
    for asset in visual_assets:
        caption = str(asset.get("caption") or "")
        label_key = _entity_key_for_caption(caption)
        key = label_key or f"asset:{asset.get('img_path') or asset.get('block_id')}"
        entity = entities.setdefault(
            key,
            {
                "entity_key": key,
                "container_label": extract_figure_label(caption) if caption else None,
                "caption": caption,
                "caption_page": int(asset.get("page") or 0),
                "span": span_by_label.get(label_key) if label_key else None,
                "assets": [],
            },
        )
        if not entity["caption"] and caption:
            entity["caption"] = caption
            entity["caption_page"] = int(asset.get("page") or 0)
            entity["container_label"] = extract_figure_label(caption)
            entity["span"] = span_by_label.get(label_key) if label_key else None
        entity["assets"].append(asset)

    tenant_id = int(revision.tenant_id)
    summary = {
        "version": FIGURE_INGESTOR_VERSION,
        "entities": len(entities),
        "assets": 0,
        "anchored_assets": 0,
        "fingerprinted": 0,
        "fingerprint_misses": 0,
        "locator_ready_assets": 0,
        "repaired_caption_spans": 0,
    }
    for entity_data in entities.values():
        span = entity_data["span"]
        entity_row = FigureEntityRecord(
            tenant_id=tenant_id,
            parse_revision_id=revision_id,
            kb_id=str(revision.kb_id),
            file_id=str(revision.file_id),
            source_sha256=str(revision.source_sha256),
            index_revision_id="",
            pipeline_version=str(getattr(revision, "pipeline_version", "") or ""),
            entity_key=entity_data["entity_key"],
            container_label=entity_data["container_label"],
            caption=entity_data["caption"] or None,
            caption_page=int(entity_data["caption_page"] or 0) or None,
            caption_anchor_id=str(span.anchor_id) if span is not None and span.anchor_id else None,
            caption_span_id=str(span.span_id) if span is not None else None,
            caption_span_evidence_id=str(span.evidence_id) if span is not None else None,
            document_partition=str(span.document_partition if span is not None else "UNKNOWN"),
            association_method="span_linkage" if span is not None and span.anchor_id else "block_pairing",
            asset_count=len(entity_data["assets"]),
        )
        session.add(entity_row)
        await session.flush()

        for asset in entity_data["assets"]:
            summary["assets"] += 1
            img_path = str(asset.get("img_path") or "")
            page = int(asset.get("page") or 0)
            caption = str(asset.get("caption") or "")
            matched_anchor = anchor_by_identity.get((page, caption.strip()))
            if matched_anchor is None:
                page_anchors = [
                    item for item in anchor_by_page.get(page, []) if str(item.anchor_id) not in paired_anchor_ids
                ]
                if len(page_anchors) == 1:
                    matched_anchor = page_anchors[0]
            if matched_anchor is not None:
                paired_anchor_ids.add(str(matched_anchor.anchor_id))
                summary["anchored_assets"] += 1
                if (
                    span is not None
                    and not span.anchor_id
                    and int(entity_data["caption_page"] or 0) == int(matched_anchor.page or 0)
                    and _caption_matches_visual_anchor(str(span.quote or ""), str(matched_anchor.quote or ""))
                ):
                    # The visual anchor and caption span describe the same complete
                    # numbered caption on the same physical page. Repair the missing
                    # span lineage before activation so the caption locator can use
                    # the authoritative caption channel instead of a discussion hit.
                    span.anchor_id = str(matched_anchor.anchor_id)
                    span.page_number = int(matched_anchor.page)
                    span.document_partition = str(matched_anchor.document_partition or "UNKNOWN")
                    span.partition_confidence = float(matched_anchor.partition_confidence or 0.0)
                    span.metadata_json = {
                        **dict(span.metadata_json or {}),
                        "anchor_link_repair": "figure_asset_same_page_full_caption",
                        "anchor_locatable": bool(matched_anchor.locatable),
                    }
                    entity_row.caption_anchor_id = str(matched_anchor.anchor_id)
                    entity_row.document_partition = str(matched_anchor.document_partition or "UNKNOWN")
                    entity_row.association_method = "asset_caption_linkage"
                    summary["repaired_caption_spans"] += 1
            safe_name = posixpath.basename(img_path) if img_path else ""
            object_name = object_by_name.get(safe_name, "") if safe_name else ""
            asset_sha = ""
            asset_phash = ""
            panel_phashes: dict[str, str] = {}
            width = height = 0
            mime = ""
            if object_name:
                try:
                    from yuxi.storage.minio.client import get_minio_client

                    minio_client = minio_client or get_minio_client()
                    data = await minio_client.adownload_file(FIGURE_ASSET_BUCKET, object_name)
                    asset_sha = compute_asset_digest(data)
                    asset_phash = compute_phash(data) or ""
                    panel_phashes = compute_panel_phashes(data)
                    width, height, mime = image_dimensions(data)
                except Exception as exc:  # noqa: BLE001 - 指纹缺失不阻断入库
                    logger.warning(f"figure ingestor: asset fingerprint failed for {img_path}: {exc}")
            if asset_sha:
                summary["fingerprinted"] += 1
                if matched_anchor is not None:
                    summary["locator_ready_assets"] += 1
            else:
                summary["fingerprint_misses"] += 1
            session.add(
                FigureAssetRecord(
                    tenant_id=tenant_id,
                    entity_id=entity_row.id,
                    parse_revision_id=revision_id,
                    kb_id=str(revision.kb_id),
                    asset_key=img_path or str(asset.get("block_id") or ""),
                    img_path=img_path,
                    anchor_id=str(getattr(matched_anchor, "anchor_id", "") or "") if matched_anchor else "",
                    object_bucket=FIGURE_ASSET_BUCKET if object_name else "",
                    object_name=object_name,
                    asset_sha256=asset_sha,
                    asset_phash=asset_phash,
                    panel_phashes=panel_phashes,
                    mime=mime,
                    width=width,
                    height=height,
                    bbox=asset.get("bbox"),
                    page=page,
                    ocr_text=None,
                )
            )
    return summary


__all__ = [
    "FIGURE_ASSET_BUCKET",
    "FIGURE_INGESTOR_VERSION",
    "persist_figure_index",
]
