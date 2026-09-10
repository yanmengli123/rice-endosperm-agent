"""yuxi.scientific-evidence.v1：科研证据对象协议。

定位（ADR 对齐 ADR-0001 四平面）：

- 证据是 Authority Plane 的读取投影：由不可变 ParseRevision + EvidenceAnchor
  组装，**只读、不落新表**（P0）；检索平面（Milvus/索引）升级不影响证据身份；
- ``evidence_id`` 由原始 PDF 哈希、页码、物理矩形、词偏移和 quote 哈希确定性
  派生。同页重复文本不会碰撞；解析器升级但原文与物理位置不变时 id 保持稳定；
- 三种定位 selector（对齐 W3C Web Annotation 多 selector 思想）：
  精确原文（exact/prefix/suffix）、文本位置（word/char 偏移）、
  物理位置（page + bbox fragments, pdf_points）；
- 组装后的证据必须通过 :mod:`validator` 的确定性验证；验证失败如实返回
  错误码，绝不生成近似位置。
"""

from __future__ import annotations

import hashlib
from typing import Any

SCIENTIFIC_EVIDENCE_SCHEMA_VERSION = "yuxi.scientific-evidence.v1"

# evidence_id 派生算法版本：v2 增加页码与物理矩形，消除同文重复句碰撞。
EVIDENCE_ID_ALGO_VERSION = 2

# 确定性验证错误码（validator 产出，DTO 原样携带）
VERIFICATION_OK = "OK"
VERIFICATION_DEGRADED = "DEGRADED"
VERIFICATION_FAILED = "FAILED"

EVIDENCE_ERROR_CODES = (
    "UNLOCATABLE",  # anchor 不存在或不可定位
    "QUOTE_MISMATCH",  # quote 哈希与存储不一致（存储完整性破坏）
    "SOURCE_REVISION_MISMATCH",  # chunk 与 anchor 分属不同 ParseRevision
    "AMBIGUOUS_ALIGNMENT",  # quote 无法在载体 chunk 中对齐（prefix/suffix 缺失）
    "INVALID_GEOMETRY",  # bbox/页面/词偏移几何非法
    "MISSING_PROVENANCE",  # chunk 缺少 source_provenance，无法回源
)

EV_ID_PREFIX = "ev_"


def derive_evidence_id(
    *,
    source_sha256: str,
    page_number: int,
    bbox: Any,
    word_start: int,
    word_end: int,
    quote_hash: str = "",
    anchor_id: str = "",
    algo_version: int = EVIDENCE_ID_ALGO_VERSION,
) -> str:
    """确定性 evidence id：同一 PDF 物理原文位置得到同一 id。

    词偏移是页内偏移，MinerU 锚点还可能为 ``0, 0``，所以不能单独作为
    身份。v2 将一基页码和规范化 PDF points 矩形纳入身份；只有缺少合法
    矩形时才用 ``anchor_id`` 作 fail-closed 的退化区分符（该条随后会被
    validator 拒绝为不可引用）。
    """
    normalized_bbox = _normalize_bbox(bbox)
    location = (
        ",".join(f"{value:.2f}" for value in normalized_bbox)
        if normalized_bbox is not None
        else f"unlocated:{anchor_id}"
    )
    digest = hashlib.sha256(
        (
            f"yuxi-evidence|v{algo_version}|{source_sha256}|p{page_number}|{location}|"
            f"w{word_start}:{word_end}|{quote_hash}"
        ).encode()
    ).hexdigest()[:20]
    return f"{EV_ID_PREFIX}{digest}"


def _normalize_bbox(bbox: Any) -> list[float] | None:
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return None
    try:
        values = [float(value) for value in bbox]
    except (TypeError, ValueError):
        return None
    if any(value != value or value in (float("inf"), float("-inf")) for value in values):
        return None
    if values[2] <= values[0] or values[3] <= values[1]:
        return None
    return values


def _normalize_fragments(fragments: Any) -> list[dict[str, Any]]:
    """归一化 anchor fragments；非法项丢弃而非伪造。"""
    result: list[dict[str, Any]] = []
    if not isinstance(fragments, (list, tuple)):
        return result
    for fragment in fragments:
        if not isinstance(fragment, dict):
            continue
        bbox = _normalize_bbox(fragment.get("bbox"))
        page_index = fragment.get("page_index")
        if bbox is None or not isinstance(page_index, int) or page_index < 0:
            continue
        result.append(
            {
                "page_index": page_index,
                "page_number": page_index + 1,
                "bbox": bbox,
                "coordinate_space": str(fragment.get("coordinate_space") or "pdf_points"),
                "rotation": int(fragment.get("rotation") or 0),
            }
        )
    return result


def build_evidence_dto(
    *,
    anchor: Any,
    chunk: Any,
    source_sha256: str,
    verification: dict[str, Any],
    retrieval: dict[str, Any] | None = None,
    semantic_location: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """组装单条证据 DTO（验证结果由 validator 给出，此处只做归一化）。

    anchor/chunk 为 ORM 记录（EvidenceAnchorRecord / KnowledgeChunk）；
    prefix/suffix 尽力从载体 chunk 正文派生，无法对齐时置空并在验证结果中
    体现 AMBIGUOUS_ALIGNMENT。
    """
    quote_text = str(anchor.quote or "")
    word_start = int(anchor.word_start or 0)
    word_end = int(anchor.word_end or word_start)

    prefix = suffix = None
    start_char = end_char = None
    chunk_content = str(getattr(chunk, "content", "") or "")
    position = chunk_content.find(quote_text) if quote_text else -1
    if position >= 0:
        start_char, end_char = position, position + len(quote_text)
        prefix = chunk_content[max(0, position - 80) : position] or None
        suffix = chunk_content[end_char : end_char + 80] or None

    provenance = dict(getattr(chunk, "source_provenance", None) or {})
    anchor_page = getattr(anchor, "page", None)
    fallback_page_number = anchor_page if isinstance(anchor_page, int) and anchor_page >= 1 else None
    fragments = _normalize_fragments(anchor.fragments) or (
        [
            {
                "page_index": fallback_page_number - 1,
                "page_number": fallback_page_number,
                "bbox": normalized_bbox,
                "coordinate_space": "pdf_points",
                "rotation": 0,
            }
        ]
        if fallback_page_number is not None and (normalized_bbox := _normalize_bbox(anchor.bbox)) is not None
        else []
    )
    identity_fragment = fragments[0] if fragments else None
    identity_page_number = int(identity_fragment["page_number"]) if identity_fragment else int(anchor_page or 0)
    identity_bbox = identity_fragment["bbox"] if identity_fragment else getattr(anchor, "bbox", None)

    dto: dict[str, Any] = {
        "schema_version": SCIENTIFIC_EVIDENCE_SCHEMA_VERSION,
        # P0 是检索候选的证据读取投影；只有后续 Claim 绑定才能升级为答案引用。
        "evidence_role": "RETRIEVAL_CANDIDATE",
        "citable": verification.get("status") == VERIFICATION_OK,
        "evidence_id": derive_evidence_id(
            source_sha256=source_sha256,
            page_number=identity_page_number,
            bbox=identity_bbox,
            word_start=word_start,
            word_end=word_end,
            quote_hash=str(getattr(anchor, "quote_hash", "") or ""),
            anchor_id=str(getattr(anchor, "anchor_id", "") or ""),
        ),
        "source": {
            "kb_id": getattr(chunk, "kb_id", None),
            "file_id": getattr(chunk, "file_id", None),
            "chunk_id": getattr(chunk, "chunk_id", None),
            "source_sha256": source_sha256,
            "parse_revision_id": provenance.get("parse_revision_id") or getattr(anchor, "parse_revision_id", None),
            "index_revision_id": provenance.get("index_revision_id"),
            "anchor_id": getattr(anchor, "anchor_id", None),
        },
        "semantic_location": semantic_location
        or {
            "evidence_type": str(getattr(anchor, "anchor_type", "") or "paragraph"),
            "section_path": provenance.get("section_path") or [],
        },
        "quote": {
            "exact": quote_text,
            "prefix": prefix,
            "suffix": suffix,
            "quote_hash": getattr(anchor, "quote_hash", None),
            "start_char": start_char,
            "end_char": end_char,
            "start_word": word_start,
            "end_word": word_end,
        },
        "locator": {
            "anchor_id": getattr(anchor, "anchor_id", None),
            "quality": str(getattr(anchor, "locator_quality", "") or "UNKNOWN"),
            "confidence": float(getattr(anchor, "confidence", 0.0) or 0.0),
            "locatable": bool(getattr(anchor, "locatable", False)),
            "fragments": fragments,
        },
        "verification": verification,
    }
    if retrieval:
        dto["retrieval"] = retrieval
    return dto
