from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from yuxi.knowledge.pdf_evidence.contracts import EvidenceAnchor, EvidenceFragment, ParserArtifact
from yuxi.knowledge.pdf_evidence.geometry import geometry_by_page, mineru_bbox_to_pdf_points

MINERU_LAYOUT_ADAPTER_VERSION = "mineru_layout_v3"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())


def _join_text(value: Any) -> str:
    if isinstance(value, list):
        parts = [_join_text(item) for item in value]
        return " ".join(part for part in parts if part)
    if isinstance(value, dict):
        return _join_text(value.get("text") or value.get("content") or "")
    return str(value or "")


def _artifact_payload(artifacts: list[ParserArtifact], kind: str) -> Any | None:
    artifact = next((item for item in artifacts if item.kind == kind), None)
    if artifact is None:
        expected_filename = {
            "mineru_content_list": "content_list.json",
            "mineru_content_list_v2": "content_list_v2.json",
        }.get(kind)
        if expected_filename:
            artifact = next(
                (item for item in artifacts if item.filename.casefold().endswith(expected_filename)),
                None,
            )
    if artifact is None:
        return None
    try:
        return json.loads(artifact.content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _bbox(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        left, top, right, bottom = (float(item) for item in value)
    except (TypeError, ValueError):
        return None
    if right <= left or bottom <= top:
        return None
    return (left, top, right, bottom)


# 视觉块类型：MinerU 对 image/figure/chart 的产物形状不同（chart 携带
# chart_caption），但它们都是"有物理 bbox 的视觉证据"，即使文本为空也不能丢弃
# （图片字节与指纹由 Figure Ingestor 经 img_path 回收，见 fragments.source_path）。
_VISUAL_BLOCK_TYPES = {"image", "figure", "chart"}


def _block_text(block: dict[str, Any]) -> str:
    block_type = str(block.get("type") or block.get("block_type") or "text").lower()
    candidates: list[Any] = []
    if block_type == "table":
        candidates.extend((block.get("table_caption"), block.get("table_body"), block.get("text")))
    elif block_type == "chart":
        # 2026-09 Figure 5 事故：真实 MinerU 输出中图表常为 type="chart" 且
        # 题注在 chart_caption 字段——此前适配器不认识该形态，整块被丢弃，
        # Figure 5 永远无法生成物理锚点。
        candidates.extend((block.get("chart_caption"), block.get("caption"), block.get("text")))
    elif block_type in {"image", "figure"}:
        candidates.extend((block.get("image_caption"), block.get("text")))
    else:
        candidates.extend((block.get("text"), block.get("content")))
    return _normalize(" ".join(_join_text(item) for item in candidates if _join_text(item)))


def _flatten_v2_pages(payload: Any) -> list[dict[str, Any]]:
    """Normalize MinerU's nested content_list_v2 into the flat v1 contract."""
    if not isinstance(payload, list):
        return []
    rows: list[dict[str, Any]] = []
    for fallback_page, page in enumerate(payload):
        if not isinstance(page, dict):
            continue
        page_index = int(page.get("page_idx", page.get("page_index", fallback_page)))
        blocks = page.get("blocks") or page.get("para_blocks") or page.get("content") or []
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if isinstance(block, dict):
                rows.append({**block, "page_idx": page_index})
    return rows


def extract_mineru_blocks(
    artifacts: list[ParserArtifact],
    page_geometry: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return ordered, locatable semantic blocks from immutable MinerU output."""
    payload = _artifact_payload(artifacts, "mineru_content_list")
    rows = payload if isinstance(payload, list) else []
    if not rows:
        rows = _flatten_v2_pages(_artifact_payload(artifacts, "mineru_content_list_v2"))

    normalized: list[dict[str, Any]] = []
    geometry = geometry_by_page(page_geometry or [])
    for order, raw in enumerate(rows):
        if not isinstance(raw, dict):
            continue
        try:
            page_index = int(raw.get("page_idx", raw.get("page_index")))
        except (TypeError, ValueError):
            continue
        source_bbox = _bbox(raw.get("bbox"))
        text = _block_text(raw)
        block_type = str(raw.get("type") or raw.get("block_type") or "text").lower()
        if page_index < 0 or source_bbox is None or (not text and block_type not in _VISUAL_BLOCK_TYPES | {"table"}):
            continue
        bbox = source_bbox
        coordinate_space = "mineru_1000"
        if geometry:
            page = geometry.get(page_index)
            if page is None:
                continue
            converted = mineru_bbox_to_pdf_points(source_bbox, page)
            if converted is None:
                continue
            bbox = converted
            coordinate_space = "pdf_points"
        block_id = f"mineru:{page_index}:{order}"
        normalized.append(
            {
                "page_index": page_index,
                "page": page_index + 1,
                "bbox": bbox,
                "source_bbox": source_bbox,
                "coordinate_space": coordinate_space,
                "text": text,
                "text_hash": _digest(text),
                "block_type": block_type,
                "order": order,
                "block_id": block_id,
                "source_path": raw.get("img_path") or raw.get("image_path"),
                "raw": raw,
            }
        )
    return normalized


def build_mineru_anchors(
    source_sha256: str,
    artifacts: list[ParserArtifact],
    *,
    page_geometry: list[dict[str, Any]] | None = None,
    blocks: list[dict[str, Any]] | None = None,
) -> list[EvidenceAnchor]:
    anchors: list[EvidenceAnchor] = []
    source_blocks = blocks if blocks is not None else extract_mineru_blocks(artifacts, page_geometry)
    for block in source_blocks:
        quote = str(block["text"])
        fragment = EvidenceFragment(
            page_index=int(block["page_index"]),
            bbox=block["bbox"],
            coordinate_space=str(block.get("coordinate_space") or "mineru_1000"),
            text=quote,
            source_block_id=str(block.get("block_id") or ""),
            source_path=str(block.get("source_path") or ""),
        )
        if not quote and block["block_type"] == "table":
            continuation_index = next(
                (
                    index
                    for index in range(len(anchors) - 1, -1, -1)
                    if anchors[index].anchor_type == "table"
                    and fragment.page_index - anchors[index].fragments[-1].page_index == 1
                ),
                None,
            )
            if continuation_index is not None:
                previous = anchors[continuation_index]
                anchors[continuation_index] = EvidenceAnchor(
                    **{
                        **previous.to_dict(),
                        "fragments": (*previous.fragments, fragment),
                    }
                )
            continue
        if not quote:
            continue
        quote_hash = _digest(quote)
        identity = "|".join(
            (
                source_sha256,
                MINERU_LAYOUT_ADAPTER_VERSION,
                str(block["page_index"]),
                str(block["order"]),
                ",".join(f"{value:.2f}" for value in block["bbox"]),
                quote_hash,
            )
        )
        anchors.append(
            EvidenceAnchor(
                anchor_id=f"ea_{_digest(identity)[:40]}",
                page=int(block["page"]),
                bbox=block["bbox"],
                word_start=0,
                word_end=0,
                quote=quote,
                quote_hash=quote_hash,
                prefix_hash=_digest(""),
                suffix_hash=_digest(""),
                fragments=(fragment,),
                anchor_type=str(block["block_type"]),
                locator_quality="HIGH",
                confidence=1.0,
                locatable=True,
                source="mineru",
            )
        )
    return anchors
