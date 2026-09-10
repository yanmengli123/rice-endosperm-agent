"""证据确定性验证器：证据由算法/模型提出，但必须在此通过验证才能出境。

八项检查（全部确定性，无模型参与）：

1. quote 非空且是可验证文本；
2. quote_hash 与重算一致（存储完整性，算法与 pdf_evidence._digest 对齐）；
3. anchor 与载体 chunk 属于同一 ParseRevision（版本链一致）；
4. word 偏移非负且 start ≤ end；
5. 一基页码合法（page ≥ 1）；
6. bbox/fragments 几何合法（PDF points、非负、坐标递增、有限值）；
7. quote 能在载体 chunk 正文中对齐（AMBIGUOUS_ALIGNMENT 只降级 prefix/suffix
   selector，不否定 anchor 本身）；
8. locatable 标志与物理定位一致（不可定位的 anchor 必须如实暴露）。

任何失败都返回错误码并给出整体状态：FAILED（不可作为证据出境）、
DEGRADED（可出境但 selector 有降级）、OK。
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any

from yuxi.knowledge.evidence.protocol import (
    VERIFICATION_DEGRADED,
    VERIFICATION_FAILED,
    VERIFICATION_OK,
)


def _digest(value: str) -> str:
    """与 pdf_evidence.native._digest 完全一致，保证跨模块可重算。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bbox_is_valid(bbox: Any) -> bool:
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return False
    try:
        values = [float(value) for value in bbox]
    except (TypeError, ValueError):
        return False
    if any(math.isnan(value) or math.isinf(value) for value in values):
        return False
    return values[0] >= 0 and values[1] >= 0 and values[2] > values[0] and values[3] > values[1]


def verify_evidence(anchor: Any, chunk: Any, *, source_sha256: str) -> dict[str, Any]:
    """对单个 (anchor, chunk) 组合执行确定性验证，返回验证结果 DTO 片段。

    只读输入、无副作用；调用方（assembler）把结果嵌入证据 DTO。
    """
    checks: dict[str, bool] = {}
    errors: list[str] = []

    quote_text = str(getattr(anchor, "quote", "") or "")
    checks["quote_present"] = bool(quote_text.strip())
    if not checks["quote_present"]:
        errors.append("UNLOCATABLE")

    stored_hash = str(getattr(anchor, "quote_hash", "") or "")
    recomputed = _digest(quote_text) if quote_text else ""
    checks["quote_hash_consistent"] = stored_hash == recomputed
    if quote_text and not checks["quote_hash_consistent"]:
        errors.append("QUOTE_MISMATCH")

    provenance = dict(getattr(chunk, "source_provenance", None) or {})
    chunk_revision = provenance.get("parse_revision_id")
    anchor_revision = getattr(anchor, "parse_revision_id", None)
    checks["revision_consistent"] = bool(chunk_revision) and chunk_revision == anchor_revision
    if not checks["revision_consistent"]:
        errors.append("SOURCE_REVISION_MISMATCH")
    source_sha = str(source_sha256 or "")
    checks["provenance_complete"] = bool(
        provenance
        and chunk_revision
        and provenance.get("index_revision_id")
        and getattr(chunk, "kb_id", None)
        and getattr(chunk, "file_id", None)
        and re.fullmatch(r"[0-9a-fA-F]{64}", source_sha)
    )
    if not checks["provenance_complete"]:
        errors.append("MISSING_PROVENANCE")

    word_start = int(getattr(anchor, "word_start", 0) or 0)
    word_end = int(getattr(anchor, "word_end", word_start) or word_start)
    checks["word_offsets_ordered"] = word_start >= 0 and word_start <= word_end
    if not checks["word_offsets_ordered"]:
        errors.append("INVALID_GEOMETRY")

    page = getattr(anchor, "page", None)
    checks["page_valid"] = isinstance(page, int) and page >= 1
    if not checks["page_valid"]:
        errors.append("INVALID_GEOMETRY")

    bbox = getattr(anchor, "bbox", None)
    fragments = getattr(anchor, "fragments", None)
    geometry_ok = _bbox_is_valid(bbox)
    fragment_rows = list(fragments) if isinstance(fragments, (list, tuple)) else []
    if fragment_rows:
        fragment_pages: set[int] = set()
        for fragment in fragment_rows:
            if not isinstance(fragment, dict):
                geometry_ok = False
                continue
            page_index = fragment.get("page_index")
            coordinate_space = str(fragment.get("coordinate_space") or "pdf_points")
            fragment_ok = (
                isinstance(page_index, int)
                and page_index >= 0
                and coordinate_space == "pdf_points"
                and _bbox_is_valid(fragment.get("bbox"))
            )
            geometry_ok = geometry_ok and fragment_ok
            if isinstance(page_index, int) and page_index >= 0:
                fragment_pages.add(page_index)
        # anchor.page 是一基主页面，至少一个 fragment 必须落在该页。
        geometry_ok = geometry_ok and isinstance(page, int) and page - 1 in fragment_pages
    checks["geometry_valid"] = bool(geometry_ok)
    if not checks["geometry_valid"]:
        errors.append("INVALID_GEOMETRY")

    chunk_content = str(getattr(chunk, "content", "") or "")
    checks["quote_aligned_in_chunk"] = bool(quote_text) and quote_text in chunk_content
    if quote_text and not checks["quote_aligned_in_chunk"]:
        # anchor 与 chunk 是多对多关系：未对齐只降级文本位置 selector，
        # 物理定位（page/bbox）仍然成立时证据可出境。
        errors.append("AMBIGUOUS_ALIGNMENT")

    anchor_locatable = bool(getattr(anchor, "locatable", False))
    checks["anchor_locatable"] = anchor_locatable
    if not anchor_locatable:
        errors.append("UNLOCATABLE")
    checks["locatable_flag_consistent"] = anchor_locatable == (checks["page_valid"] and checks["geometry_valid"])
    if not checks["locatable_flag_consistent"]:
        errors.append("INVALID_GEOMETRY")

    hard_failures = [e for e in errors if e != "AMBIGUOUS_ALIGNMENT"]
    if hard_failures:
        status = VERIFICATION_FAILED
    elif errors:
        status = VERIFICATION_DEGRADED
    else:
        status = VERIFICATION_OK
    return {
        "status": status,
        "citable": status == VERIFICATION_OK,
        "checks": checks,
        "errors": list(dict.fromkeys(errors)),
    }
