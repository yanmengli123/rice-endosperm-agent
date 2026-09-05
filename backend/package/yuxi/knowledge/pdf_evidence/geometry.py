from __future__ import annotations

from collections.abc import Iterable
from typing import Any

MINERU_COORDINATE_EXTENT = 1000.0
GEOMETRY_EPSILON = 1.0


def geometry_by_page(rows: Iterable[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for row in rows:
        try:
            page_index = int(row["page_index"])
            width = float(row["width"])
            height = float(row["height"])
        except (KeyError, TypeError, ValueError):
            continue
        if page_index >= 0 and width > 0 and height > 0:
            result[page_index] = {**row, "page_index": page_index, "width": width, "height": height}
    return result


def mineru_bbox_to_pdf_points(
    bbox: tuple[float, float, float, float],
    geometry: dict[str, Any],
) -> tuple[float, float, float, float] | None:
    """Convert MinerU's documented 0..1000 page space to PDF points.

    Values outside the declared coordinate extent are rejected rather than
    clamped: a fabricated rectangle is worse than an unlocatable citation.
    """
    left, top, right, bottom = bbox
    if (
        left < -GEOMETRY_EPSILON
        or top < -GEOMETRY_EPSILON
        or right > MINERU_COORDINATE_EXTENT + GEOMETRY_EPSILON
        or bottom > MINERU_COORDINATE_EXTENT + GEOMETRY_EPSILON
        or right <= left
        or bottom <= top
    ):
        return None
    width = float(geometry["width"])
    height = float(geometry["height"])
    return tuple(
        round(value, 2)
        for value in (
            left * width / MINERU_COORDINATE_EXTENT,
            top * height / MINERU_COORDINATE_EXTENT,
            right * width / MINERU_COORDINATE_EXTENT,
            bottom * height / MINERU_COORDINATE_EXTENT,
        )
    )


def bbox_is_on_page(bbox: tuple[float, float, float, float], geometry: dict[str, Any]) -> bool:
    left, top, right, bottom = bbox
    return (
        left >= -GEOMETRY_EPSILON
        and top >= -GEOMETRY_EPSILON
        and right <= float(geometry["width"]) + GEOMETRY_EPSILON
        and bottom <= float(geometry["height"]) + GEOMETRY_EPSILON
        and right > left
        and bottom > top
    )
