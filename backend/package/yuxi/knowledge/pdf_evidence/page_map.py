from __future__ import annotations

from typing import Any

from yuxi.knowledge.pdf_evidence.contracts import NativePdfSnapshot
from yuxi.knowledge.pdf_evidence.geometry import bbox_is_on_page, geometry_by_page

PHYSICAL_PAGE_MAP_VERSION = "physical_page_map_v1"


def _append_coordinates(
    pages: list[dict[str, Any]],
    coordinates: list[dict[str, Any]],
    *,
    node_type: str,
    node_id: str,
) -> None:
    for coordinate in coordinates:
        try:
            page_index = int(coordinate["page_index"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= page_index < len(pages):
            pages[page_index]["grobid_nodes"].append(
                {
                    "node_type": node_type,
                    "node_id": node_id,
                    "bbox": coordinate.get("bbox"),
                }
            )


def build_physical_page_map(
    native: NativePdfSnapshot,
    mineru_blocks: list[dict[str, Any]],
    grobid_data: dict[str, Any],
) -> dict[str, Any]:
    """Build the physical evidence view shared by all downstream locators."""
    geometry = geometry_by_page(native.page_geometry)
    pages = [
        {
            "page_index": index,
            "page": index + 1,
            "geometry": geometry.get(index),
            "native_blocks": list(native.pages[index].get("blocks") or []),
            "mineru_blocks": [],
            "grobid_nodes": [],
        }
        for index in range(native.page_count)
    ]
    invalid_blocks: list[str] = []
    for block in mineru_blocks:
        page_index = int(block["page_index"])
        block_id = str(block.get("block_id") or f"mineru:{page_index}:{block.get('order', 0)}")
        page_geometry = geometry.get(page_index)
        if not 0 <= page_index < len(pages) or page_geometry is None:
            invalid_blocks.append(block_id)
            continue
        if not bbox_is_on_page(tuple(block["bbox"]), page_geometry):
            invalid_blocks.append(block_id)
            continue
        pages[page_index]["mineru_blocks"].append(
            {
                "block_id": block_id,
                "block_type": block.get("block_type"),
                "bbox": list(block["bbox"]),
                "text_hash": block.get("text_hash"),
            }
        )

    for section in grobid_data.get("sections") or []:
        section_id = f"section:{section.get('section_index', 0)}"
        _append_coordinates(
            pages,
            list(section.get("heading_coordinates") or []),
            node_type="heading",
            node_id=section_id,
        )
        for paragraph_index, coordinates in enumerate(section.get("paragraph_coordinates") or []):
            _append_coordinates(
                pages,
                list(coordinates or []),
                node_type="paragraph",
                node_id=f"{section_id}:p{paragraph_index}",
            )
    for reference in grobid_data.get("references") or []:
        _append_coordinates(
            pages,
            list(reference.get("coordinates") or []),
            node_type="reference",
            node_id=str(reference.get("reference_id") or ""),
        )
    for mention in grobid_data.get("citation_mentions") or []:
        _append_coordinates(
            pages,
            list(mention.get("coordinates") or []),
            node_type="citation_mention",
            node_id=str(mention.get("mention_id") or ""),
        )

    return {
        "schema_version": PHYSICAL_PAGE_MAP_VERSION,
        "source_sha256": native.pdf_sha256,
        "page_count": native.page_count,
        "pages": pages,
        "validation": {
            "coordinate_space": "pdf_points",
            "invalid_mineru_block_count": len(invalid_blocks),
            "invalid_mineru_block_ids": invalid_blocks,
        },
    }
