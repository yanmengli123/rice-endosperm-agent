"""Figure Ingestor（R-P2 / v4）：解析入库时构建持久化图表资产指纹索引。

职责：把 MinerU 视觉块（image/figure/chart）转成版本化的 FigureEntity +
FigureAsset 记录——下载 MinIO 图片字节，计算 SHA256、感知哈希、尺寸与
panel 变体指纹，使上传图片可通过 V0（字节一致）/V1（感知哈希）确定性定位，
不依赖视觉模型。重解析时随 parse_revision 级联重建，不原地修改旧解析事实。

v4（多 panel 聚合，ADR-0004 §11）：MinerU 常把一张多 panel 图拆成十几块，只有带
题注的那一块能绑到编号——"问 Figure 2 只出一个 panel、截图别的 panel 找不到出处"。
v4 在**同一页**把无 label 的相邻视觉块并入最近的有 label 图组（块间空隙 ≤
``_GROUP_GAP_POINTS``），题注绑**图组**而不是单块；每块按 bbox IoU/包含关系配到
视觉锚点（不再只靠"题注文本一致"）；有原 PDF 时按图组 bbox 并集渲染一张合成整图作为
``role=primary`` 资产（截图整张图时 V1/V1G 才有对象可比）；题注里的 panel 字母数与图组块数
差距过大记 ``partial_figure_suspected``。所有规则确定性、无模型参与。

失败语义：指纹计算失败（对象缺失/解码失败）不阻断入库——资产行保留
（label/题注/页码仍可用），指纹字段留空并在 summary 中计数，绝不编造；合成整图
渲染/上传失败只计数，不影响 panel 资产。
"""

from __future__ import annotations

import asyncio
import math
import posixpath
import re
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import delete

from yuxi.knowledge.evidence.caption_locator import canonical_figure_label, extract_figure_label
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

FIGURE_INGESTOR_VERSION = "figure_ingestor_v4"

# 与 ScientificPdfPipeline engine_params 一致（图片上传的目标 bucket）
FIGURE_ASSET_BUCKET = "knowledgebases"
_MAX_ASSETS_PER_REVISION = 200

# 同页视觉块聚合：块间空隙 ≤ 36pt（≈0.5 英寸）视为同一图组的相邻 panel；
# 有 label 图组之间永不合并（同页 Figure 3 / Figure 4 各自成组）
_GROUP_GAP_POINTS = 36.0
_ANCHOR_MIN_IOU = 0.5
_ANCHOR_MIN_CONTAINMENT = 0.8
# 合成整图：并集区域相对页面面积下限（过滤坐标空间异常的 bbox）与渲染尺寸上限
_SYNTHETIC_MIN_PAGE_RATIO = 0.02
_SYNTHETIC_MAX_SIDE_PX = 2400
_PANEL_LETTER = re.compile(r"\(([a-zA-Z])\)")

ROLE_PRIMARY = "primary"
ROLE_PANEL = "panel"

PdfBytesLoader = Callable[[], Awaitable[bytes | None]]


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

    from yuxi.knowledge.pdf_evidence.asset_paths import revision_image_prefix  # 惰性：避免与 pipeline 循环导入

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


# ---- 几何：bbox 工具（PDF 点，左上原点）----


def _bbox(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(item) for item in value)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(item) for item in (x0, y0, x1, y1)):
        return None
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1, y1)


def _bbox_gap(first: tuple[float, ...], second: tuple[float, ...]) -> float:
    """两个 bbox 的最小间隙（重叠为 0；并排看水平间隙，上下看垂直间隙）。"""
    dx = max(0.0, max(first[0], second[0]) - min(first[2], second[2]))
    dy = max(0.0, max(first[1], second[1]) - min(first[3], second[3]))
    return math.hypot(dx, dy)


def _bbox_area(box: tuple[float, ...]) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _bbox_iou(first: tuple[float, ...], second: tuple[float, ...]) -> float:
    ix0, iy0 = max(first[0], second[0]), max(first[1], second[1])
    ix1, iy1 = min(first[2], second[2]), min(first[3], second[3])
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    union = _bbox_area(first) + _bbox_area(second) - inter
    return inter / union if union > 0 else 0.0


def _bbox_containment(first: tuple[float, ...], second: tuple[float, ...]) -> float:
    """交集占较小 bbox 的比例（一块落在锚点区域内 / 锚点落在块内）。"""
    ix0, iy0 = max(first[0], second[0]), max(first[1], second[1])
    ix1, iy1 = min(first[2], second[2]), min(first[3], second[3])
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    smaller = min(_bbox_area(first), _bbox_area(second))
    return inter / smaller if smaller > 0 else 0.0


def _union_bbox(boxes: list[tuple[float, ...]]) -> tuple[float, float, float, float] | None:
    boxes = [box for box in boxes if box]
    if not boxes:
        return None
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _anchor_bbox(anchor: Any) -> tuple[float, float, float, float] | None:
    box = _bbox(getattr(anchor, "bbox", None))
    if box is not None:
        return box
    for fragment in getattr(anchor, "fragments", None) or []:
        if isinstance(fragment, dict):
            box = _bbox(fragment.get("bbox"))
            if box is not None:
                return box
    return None


# ---- 分组：同页无 label 块并入最近的有 label 图组，其余按邻接聚类 ----


def _reading_order(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def key(block: dict[str, Any]) -> tuple:
        box = block["bbox"]
        if box is None:
            return (block["page"], 1, 0.0, 0.0, block["img_path"])
        return (block["page"], 0, round(box[1] / 24.0), box[0], block["img_path"])

    return sorted(blocks, key=key)


def _min_gap_to_members(block: dict[str, Any], members: list[dict[str, Any]]) -> float | None:
    if block["bbox"] is None:
        return None
    gaps = [
        _bbox_gap(block["bbox"], member["bbox"])
        for member in members
        if member["bbox"] is not None and member["page"] == block["page"]
    ]
    return min(gaps) if gaps else None


def group_visual_blocks(blocks: list[dict[str, Any]]) -> list[tuple[str | None, list[dict[str, Any]]]]:
    """视觉块 → 图组列表 [(label_key | None, members)]（确定性，纯函数）。

    - 有 label 的块按规范编号聚合（同编号跨页也归一组，与 v3 一致）；
    - 无 label 的块：并入**同页**最近的有 label 图组（空隙 ≤ 阈值；迭代传递，允许链式相邻）；
    - 仍无归属的块：同页按邻接聚类，每簇一个无 label 实体（v3 是每块一个实体）。
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    unlabeled: list[dict[str, Any]] = []
    for block in blocks:
        if block["label_key"]:
            groups.setdefault(block["label_key"], []).append(block)
        else:
            unlabeled.append(block)

    changed = True
    while changed and unlabeled and groups:
        changed = False
        remaining: list[dict[str, Any]] = []
        for block in unlabeled:
            best_key: str | None = None
            best_gap: float | None = None
            for key, members in groups.items():
                gap = _min_gap_to_members(block, members)
                if gap is not None and gap <= _GROUP_GAP_POINTS and (best_gap is None or gap < best_gap):
                    best_key, best_gap = key, gap
            if best_key is not None:
                groups[best_key].append(block)
                block["merged_into_label"] = True
                changed = True
            else:
                remaining.append(block)
        unlabeled = remaining

    # 无归属块：同页邻接聚类（并查集）
    clusters: list[list[dict[str, Any]]] = []
    for block in _reading_order(unlabeled):
        target: list[dict[str, Any]] | None = None
        for cluster in clusters:
            gap = _min_gap_to_members(block, cluster)
            if gap is not None and gap <= _GROUP_GAP_POINTS:
                target = cluster
                break
        if target is None:
            clusters.append([block])
        else:
            target.append(block)

    ordered: list[tuple[str | None, list[dict[str, Any]]]] = [
        (key, _reading_order(members)) for key, members in groups.items()
    ]
    ordered.extend((None, _reading_order(cluster)) for cluster in clusters)
    return ordered


def _panel_letter_count(caption: str) -> int:
    return len({match.lower() for match in _PANEL_LETTER.findall(str(caption or ""))})


# ---- 合成整图（有原 PDF 时按图组 bbox 并集渲染）----


def _render_region_png(pdf_bytes: bytes, page_index: int, region: tuple[float, float, float, float]) -> bytes | None:
    import fitz  # PyMuPDF

    with fitz.open(stream=pdf_bytes, filetype="pdf") as document:
        if page_index < 0 or page_index >= document.page_count:
            return None
        page = document[page_index]
        page_rect = page.rect
        clip = fitz.Rect(*region) & page_rect
        if clip.is_empty or clip.width < 20 or clip.height < 20:
            return None
        if clip.get_area() < page_rect.get_area() * _SYNTHETIC_MIN_PAGE_RATIO:
            return None
        zoom = min(2.0, _SYNTHETIC_MAX_SIDE_PX / max(clip.width, clip.height))
        pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip, alpha=False)
        return pixmap.tobytes("png")


def _synthetic_object_name(revision: Any, entity_key: str, digest: str) -> str:
    from yuxi.knowledge.pdf_evidence.asset_paths import revision_image_prefix  # 惰性：避免与 pipeline 循环导入

    prefix = revision_image_prefix(
        tenant_id=int(revision.tenant_id),
        source_sha256=str(revision.source_sha256),
        revision_id=str(revision.revision_id),
    )
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", entity_key).strip("_") or "figure"
    return f"{prefix}/{digest[:24]}-synthetic_{safe}.png"


async def persist_figure_index(
    session,
    *,
    revision: Any,
    article_assets: list[dict[str, Any]],
    spans: list[Any],
    anchors: list[Any] | None = None,
    pdf_bytes_loader: PdfBytesLoader | None = None,
) -> dict[str, Any]:
    """为一个 parse revision 构建 figure_entities / figure_assets（幂等重试）。

    返回 summary（计入 qa_report.figure_index）：实体/资产/指纹命中/聚合/合成计数。
    ``pdf_bytes_loader`` 提供原 PDF 字节时，多块图组额外渲染合成整图作为 primary 资产。
    """
    revision_id = str(revision.revision_id)
    await session.execute(delete(FigureAssetRecord).where(FigureAssetRecord.parse_revision_id == revision_id))
    await session.execute(delete(FigureEntityRecord).where(FigureEntityRecord.parse_revision_id == revision_id))

    visual_assets = [
        asset
        for asset in (article_assets or [])
        if isinstance(asset, dict) and str(asset.get("kind") or "") == "figure" and int(asset.get("page") or 0) >= 1
    ][:_MAX_ASSETS_PER_REVISION]
    summary: dict[str, Any] = {
        "version": FIGURE_INGESTOR_VERSION,
        "entities": 0,
        "assets": 0,
        "anchored_assets": 0,
        "fingerprinted": 0,
        "fingerprint_misses": 0,
        "locator_ready_assets": 0,
        "repaired_caption_spans": 0,
        "merged_unlabeled_blocks": 0,
        "synthetic_primary": 0,
        "synthetic_failures": 0,
        "partial_figure_suspected": 0,
    }
    if not visual_assets:
        return summary

    caption_spans = [span for span in spans or [] if str(span.evidence_type) == "caption"]
    span_by_label: dict[str, Any] = {}
    for span in caption_spans:
        label_key = canonical_figure_label(span.container_label or "")
        if label_key:
            span_by_label.setdefault(label_key, span)

    # 视觉块锚点：v4 先按 bbox（IoU / 包含）配对，再回退 v3 的"页码 + 题注文本一致"与同页唯一
    image_anchors: list[Any] = [
        anchor for anchor in anchors or [] if str(getattr(anchor, "anchor_type", "")) in {"image", "figure", "chart"}
    ]
    anchor_by_identity: dict[tuple[int, str], Any] = {}
    anchor_by_page: dict[int, list[Any]] = {}
    for anchor in image_anchors:
        anchor_by_identity.setdefault((int(anchor.page or 0), str(anchor.quote or "").strip()), anchor)
        anchor_by_page.setdefault(int(anchor.page or 0), []).append(anchor)
    paired_anchor_ids: set[str] = set()

    def pair_anchor(block: dict[str, Any]) -> Any | None:
        page_anchors = anchor_by_page.get(block["page"], [])
        if block["bbox"] is not None:
            best, best_score = None, 0.0
            for anchor in page_anchors:
                anchor_box = _anchor_bbox(anchor)
                if anchor_box is None:
                    continue
                iou = _bbox_iou(block["bbox"], anchor_box)
                containment = _bbox_containment(block["bbox"], anchor_box)
                score = max(
                    iou if iou >= _ANCHOR_MIN_IOU else 0.0,
                    containment if containment >= _ANCHOR_MIN_CONTAINMENT else 0.0,
                )
                if score > best_score:
                    best, best_score = anchor, score
            if best is not None:
                return best
        matched = anchor_by_identity.get((block["page"], block["caption"].strip()))
        if matched is not None:
            return matched
        unpaired = [item for item in page_anchors if str(item.anchor_id) not in paired_anchor_ids]
        return unpaired[0] if len(unpaired) == 1 else None

    object_by_name = await _resolve_asset_objects(revision)
    minio_client = None
    pdf_bytes: bytes | None = None
    pdf_loaded = False

    async def load_pdf() -> bytes | None:
        nonlocal pdf_bytes, pdf_loaded
        if pdf_loaded:
            return pdf_bytes
        pdf_loaded = True
        if pdf_bytes_loader is None:
            return None
        try:
            pdf_bytes = await pdf_bytes_loader()
        except Exception as exc:  # noqa: BLE001 - 合成整图是增强，不阻断
            logger.warning(f"figure ingestor: source pdf unavailable (synthetic whole skipped): {exc}")
            pdf_bytes = None
        return pdf_bytes

    blocks: list[dict[str, Any]] = []
    for asset in visual_assets:
        caption = str(asset.get("caption") or "")
        page = int(asset.get("page") or 0)
        page_index = asset.get("page_index")
        blocks.append(
            {
                "asset": asset,
                "caption": caption,
                "label_key": _entity_key_for_caption(caption),
                "page": page,
                "page_index": int(page_index) if isinstance(page_index, int) and page_index >= 0 else page - 1,
                "bbox": _bbox(asset.get("bbox")),
                "img_path": str(asset.get("img_path") or ""),
                "block_id": str(asset.get("block_id") or ""),
            }
        )

    groups = group_visual_blocks(blocks)
    summary["entities"] = len(groups)
    summary["merged_unlabeled_blocks"] = sum(1 for block in blocks if block.get("merged_into_label"))
    tenant_id = int(revision.tenant_id)

    for label_key, members in groups:
        captioned = [block for block in members if block["caption"]]
        labeled = [block for block in members if block["label_key"]]
        head = labeled[0] if labeled else (captioned[0] if captioned else members[0])
        caption = head["caption"]
        entity_key = label_key or f"asset:{head['img_path'] or head['block_id']}"
        span = span_by_label.get(label_key) if label_key else None
        entity_row = FigureEntityRecord(
            tenant_id=tenant_id,
            parse_revision_id=revision_id,
            kb_id=str(revision.kb_id),
            file_id=str(revision.file_id),
            source_sha256=str(revision.source_sha256),
            index_revision_id="",
            pipeline_version=str(getattr(revision, "pipeline_version", "") or ""),
            entity_key=entity_key,
            container_label=extract_figure_label(caption) if caption else None,
            caption=caption or None,
            caption_page=int(head["page"] or 0) or None,
            caption_anchor_id=str(span.anchor_id) if span is not None and span.anchor_id else None,
            caption_span_id=str(span.span_id) if span is not None else None,
            caption_span_evidence_id=str(span.evidence_id) if span is not None else None,
            document_partition=str(span.document_partition if span is not None else "UNKNOWN"),
            association_method="span_linkage" if span is not None and span.anchor_id else "block_pairing",
            asset_count=len(members),
        )
        session.add(entity_row)
        await session.flush()

        member_rows: list[FigureAssetRecord] = []
        member_anchor_ids: list[str] = []
        largest_index = max(
            range(len(members)),
            key=lambda index: _bbox_area(members[index]["bbox"]) if members[index]["bbox"] else 0.0,
        )
        for group_index, block in enumerate(members):
            summary["assets"] += 1
            img_path = block["img_path"]
            page = block["page"]
            matched_anchor = pair_anchor(block)
            member_anchor_ids.append(str(getattr(matched_anchor, "anchor_id", "") or "") if matched_anchor else "")
            if matched_anchor is not None:
                paired_anchor_ids.add(str(matched_anchor.anchor_id))
                summary["anchored_assets"] += 1
                if (
                    span is not None
                    and not span.anchor_id
                    and block["label_key"]
                    and int(head["page"] or 0) == int(matched_anchor.page or 0)
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
            asset_row = FigureAssetRecord(
                tenant_id=tenant_id,
                entity_id=entity_row.id,
                parse_revision_id=revision_id,
                kb_id=str(revision.kb_id),
                asset_key=img_path or block["block_id"],
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
                bbox=list(block["bbox"]) if block["bbox"] else block["asset"].get("bbox"),
                page=page,
                ocr_text=None,
                # 单块图组即 primary；多块图组先让面积最大的块当 primary，合成整图成功后再让位
                role=ROLE_PRIMARY if group_index == largest_index else ROLE_PANEL,
                group_index=group_index,
                panel_label="",
            )
            session.add(asset_row)
            member_rows.append(asset_row)

        # 多块图组：渲染合成整图作为 primary（有原 PDF 且并集几何合理时）；失败只计数
        if len(members) > 1 and label_key:
            synthetic_row = await _persist_synthetic_whole(
                session,
                revision=revision,
                entity_row=entity_row,
                entity_key=entity_key,
                members=members,
                anchor_id=member_anchor_ids[largest_index] or next((item for item in member_anchor_ids if item), ""),
                load_pdf=load_pdf,
                summary=summary,
            )
            if synthetic_row is not None:
                member_rows[largest_index].role = ROLE_PANEL
                entity_row.asset_count = len(members) + 1
        letters = _panel_letter_count(caption)
        if label_key and letters >= 2 and len(members) < letters:
            summary["partial_figure_suspected"] += 1
    return summary


async def _persist_synthetic_whole(
    session,
    *,
    revision: Any,
    entity_row: FigureEntityRecord,
    entity_key: str,
    members: list[dict[str, Any]],
    anchor_id: str,
    load_pdf: Callable[[], Awaitable[bytes | None]],
    summary: dict[str, Any],
) -> FigureAssetRecord | None:
    pages = {block["page"] for block in members}
    if len(pages) != 1:
        return None
    region = _union_bbox([block["bbox"] for block in members if block["bbox"]])
    if region is None:
        return None
    pdf_bytes = await load_pdf()
    if not pdf_bytes:
        return None
    page_index = members[0]["page_index"]
    try:
        png = await asyncio.to_thread(_render_region_png, pdf_bytes, page_index, region)
        if not png:
            return None
        digest = compute_asset_digest(png)
        object_name = _synthetic_object_name(revision, entity_key, digest)
        from yuxi.storage.minio.client import get_minio_client

        await get_minio_client().aupload_file(FIGURE_ASSET_BUCKET, object_name, png, content_type="image/png")
        width, height, mime = image_dimensions(png)
        row = FigureAssetRecord(
            tenant_id=int(revision.tenant_id),
            entity_id=entity_row.id,
            parse_revision_id=str(revision.revision_id),
            kb_id=str(revision.kb_id),
            asset_key=f"synthetic:{entity_key}",
            img_path="",
            anchor_id=anchor_id or "",
            object_bucket=FIGURE_ASSET_BUCKET,
            object_name=object_name,
            asset_sha256=digest,
            asset_phash=compute_phash(png) or "",
            panel_phashes=compute_panel_phashes(png),
            mime=mime,
            width=width,
            height=height,
            bbox=list(region),
            page=members[0]["page"],
            ocr_text=None,
            role=ROLE_PRIMARY,
            group_index=-1,
            panel_label="",
        )
        session.add(row)
        summary["assets"] += 1
        summary["fingerprinted"] += 1
        if anchor_id:
            summary["anchored_assets"] += 1
            summary["locator_ready_assets"] += 1
        summary["synthetic_primary"] += 1
        return row
    except Exception as exc:  # noqa: BLE001 - 合成整图失败不阻断 panel 资产
        logger.warning(f"figure ingestor: synthetic whole failed for {entity_key}: {exc}")
        summary["synthetic_failures"] += 1
        return None


__all__ = [
    "FIGURE_ASSET_BUCKET",
    "FIGURE_INGESTOR_VERSION",
    "ROLE_PANEL",
    "ROLE_PRIMARY",
    "group_visual_blocks",
    "persist_figure_index",
]
