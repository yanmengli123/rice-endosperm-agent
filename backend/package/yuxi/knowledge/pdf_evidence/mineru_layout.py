from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any

from yuxi.knowledge.pdf_evidence.contracts import EvidenceAnchor, EvidenceFragment, ParserArtifact
from yuxi.knowledge.pdf_evidence.geometry import geometry_by_page, mineru_bbox_to_pdf_points
from yuxi.knowledge.pdf_evidence.text_geometry import normalize_with_map

MINERU_LAYOUT_ADAPTER_VERSION = "mineru_layout_v5"


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
        if isinstance(page, list):
            for block in page:
                if isinstance(block, dict):
                    content = block.get("content") or {}
                    if isinstance(content, dict):
                        content = {
                            **content,
                            "table_body": content.get("html"),
                            "img_path": (content.get("image_source") or {}).get("path")
                            if isinstance(content.get("image_source"), dict)
                            else content.get("image_source"),
                        }
                        rows.append({**content, **block, "page_idx": fallback_page})
                    else:
                        rows.append({**block, "page_idx": fallback_page})
            continue
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


def _line_text(line: dict) -> str:
    return "".join(
        f"${span.get('content', '')}$" if span.get("type") == "inline_equation" else str(span.get("content") or "")
        for span in line.get("spans", [])
    )


def _pairing_text(value: str) -> str:
    # Compare producer representations, without changing stored source text.
    return re.sub(r"\s+", "", normalize_with_map(unicodedata.normalize("NFKC", value)).text)


def _recover_text_fragments(blocks: list[dict], artifacts: list[ParserArtifact], geometry: dict) -> None:
    """Recover merged paragraph members from MiddleJson's original regions.

    A cross-page line must identify exactly one preprocessed region by BOTH
    its producer text and rectangle. Page order/proximity alone is insufficient.
    """
    middle = _artifact_payload(artifacts, "mineru_layout")
    pages = middle.get("pdf_info", []) if isinstance(middle, dict) else []
    if not pages or not geometry:
        return
    originals = []

    def converted(box, page_index):
        if page_index not in geometry or not _bbox(box):
            return None
        size = pages[page_index].get("page_size")
        if not size or len(size) != 2 or min(size) <= 0:
            return None
        return mineru_bbox_to_pdf_points(
            tuple(float(v) * 1000 / size[i % 2] for i, v in enumerate(box)), geometry[page_index]
        )

    def close(a, b):
        return a is not None and b is not None and max(abs(x - y) for x, y in zip(a, b, strict=True)) <= 2

    for page_index, page in enumerate(pages):
        for original in page.get("preproc_blocks", []):
            if original.get("type") not in {"text", "title", "ref_text", "list"}:
                continue
            text = " ".join(_line_text(line) for line in original.get("lines", []))
            box = converted(original.get("bbox"), page_index)
            if box and text:
                originals.append({"page_index": page_index, "bbox": box, "text": text})
    for block in list(blocks):
        if block["block_type"] in _VISUAL_BLOCK_TYPES and block["text"]:
            page_index = block["page_index"]
            candidates = [
                para
                for para in pages[page_index].get("para_blocks", [])
                if para.get("type") in _VISUAL_BLOCK_TYPES
                and close(converted(para.get("bbox"), page_index), block["bbox"])
                and _pairing_text(
                    " ".join(
                        _line_text(line)
                        for child in para.get("blocks", [])
                        if child.get("type", "").endswith(("_caption", "_footnote"))
                        for line in child.get("lines", [])
                    )
                )
                == _pairing_text(block["text"])
            ]
            if len(candidates) != 1:
                continue
            fragments = [{**block, "source_block_id": block["block_id"], "text": ""}]
            for child in candidates[0].get("blocks", []):
                if not child.get("type", "").endswith(("_caption", "_footnote")):
                    continue
                text = " ".join(_line_text(line) for line in child.get("lines", []))
                box = converted(child.get("bbox"), page_index)
                if not box or not text:
                    continue
                region_block = {
                    "page_index": page_index,
                    "page": page_index + 1,
                    "bbox": box,
                    "source_bbox": child["bbox"],
                    "coordinate_space": "pdf_points",
                    "text": text,
                    "text_hash": _digest(text),
                    "block_type": "caption",
                    "order": len(blocks),
                    "block_id": f"mineru:caption:{page_index}:{len(blocks)}",
                    "source_path": None,
                    "raw": child,
                    "source_region_only": True,
                }
                blocks.append(region_block)
                fragments.append({**region_block, "source_block_id": region_block["block_id"]})
            block["physical_fragments"] = fragments
            continue
        if block["block_type"] not in {"text", "ref_text", "title", "list"} or not block["text"]:
            continue
        page_index = block["page_index"]
        if page_index >= len(pages):
            continue
        candidates = [
            para
            for para in pages[page_index].get("para_blocks", [])
            if close(converted(para.get("bbox"), page_index), block["bbox"])
            and _pairing_text(" ".join(_line_text(line) for line in para.get("lines", [])))
            == _pairing_text(block["text"])
        ]
        if len(candidates) != 1 or len(candidates[0].get("lines", [])) < 2:
            continue
        fragments = []
        for line in candidates[0]["lines"]:
            text = _line_text(line)
            cross_page = line.get("cross_page") or any(s.get("cross_page") for s in line.get("spans", []))
            members = [
                original
                for original in originals
                if (original["page_index"] > page_index if cross_page else original["page_index"] == page_index)
                and close(converted(line.get("bbox"), original["page_index"]), original["bbox"])
                and _pairing_text(original["text"]) == _pairing_text(text)
            ]
            if len(members) != 1:
                fragments = []
                break
            member = members[0]
            existing = [
                b for b in blocks if b["page_index"] == member["page_index"] and close(b["bbox"], member["bbox"])
            ]
            if len(existing) > 1:
                fragments = []
                break
            if existing:
                region_block = existing[0]
            else:
                region_block = {
                    **member,
                    "page": member["page_index"] + 1,
                    "source_bbox": member["bbox"],
                    "coordinate_space": "pdf_points",
                    "text_hash": _digest(text),
                    "block_type": block["block_type"],
                    "order": len(blocks),
                    "block_id": f"mineru:physical:{member['page_index']}:{len(blocks)}",
                    "source_path": None,
                    "raw": {},
                    "source_region_only": True,
                }
                blocks.append(region_block)
            fragments.append({**member, "source_block_id": region_block["block_id"], "coordinate_space": "pdf_points"})
        if fragments:
            block["physical_fragments"] = fragments


def extract_mineru_blocks(
    artifacts: list[ParserArtifact],
    page_geometry: list[dict[str, Any]] | None = None,
    *,
    audit: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return ordered, locatable semantic blocks from immutable MinerU output."""
    payload = _artifact_payload(artifacts, "mineru_content_list")
    rows = payload if isinstance(payload, list) else []
    if rows and not isinstance(rows[0], dict):
        rows = _flatten_v2_pages(rows)
    if not rows:
        rows = _flatten_v2_pages(_artifact_payload(artifacts, "mineru_content_list_v2"))

    if audit is not None:
        audit.update({"raw_blocks": len(rows), "accepted_blocks": 0, "rejected_blocks": 0, "rejection_reasons": {}})

    def reject(reason: str) -> None:
        if audit is not None:
            audit["rejected_blocks"] += 1
            reasons = audit["rejection_reasons"]
            reasons[reason] = reasons.get(reason, 0) + 1

    normalized: list[dict[str, Any]] = []
    geometry = geometry_by_page(page_geometry or [])
    for order, raw in enumerate(rows):
        if not isinstance(raw, dict):
            reject("INVALID_BLOCK")
            continue
        try:
            page_index = int(raw.get("page_idx", raw.get("page_index")))
        except (TypeError, ValueError):
            reject("INVALID_PAGE")
            continue
        source_bbox = _bbox(raw.get("bbox"))
        text = _block_text(raw)
        block_type = str(raw.get("type") or raw.get("block_type") or "text").lower()
        if (
            page_index < 0
            or source_bbox is None
            or (not text and block_type not in _VISUAL_BLOCK_TYPES | {"table", "text", "ref_text"})
        ):
            reject("INVALID_OR_EMPTY_REGION")
            continue
        bbox = source_bbox
        coordinate_space = "mineru_1000"
        if geometry:
            page = geometry.get(page_index)
            if page is None:
                reject("PAGE_OUT_OF_RANGE")
                continue
            converted = mineru_bbox_to_pdf_points(source_bbox, page)
            if converted is None:
                reject("INVALID_GEOMETRY")
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
                "source_region_only": not text and block_type in {"text", "ref_text"},
            }
        )
    if audit is not None:
        audit["accepted_blocks"] = len(normalized)
    _recover_text_fragments(normalized, artifacts, geometry)
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
        if block.get("source_region_only"):
            continue
        quote = str(block["text"])
        fragments = tuple(
            EvidenceFragment(
                page_index=int(member["page_index"]),
                bbox=member["bbox"],
                coordinate_space=str(member.get("coordinate_space") or "mineru_1000"),
                text=member.get("text", quote),
                source_block_id=member.get("source_block_id", block["block_id"]),
                source_path=str(block.get("source_path") or ""),
            )
            for member in block.get("physical_fragments", [block])
        )
        if not quote and block["block_type"] not in _VISUAL_BLOCK_TYPES | {"table"}:
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
                fragments=fragments,
                anchor_type=str(block["block_type"]),
                locator_quality="HIGH",
                confidence=1.0,
                locatable=True,
                source="mineru",
            )
        )
    return anchors
