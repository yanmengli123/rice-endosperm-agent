"""yuxi.scientific-evidence.v1 单测：id 派生确定性、八项验证、DTO selector。"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from yuxi.knowledge.evidence.assembler import _summary
from yuxi.knowledge.evidence.protocol import (
    EVIDENCE_ID_ALGO_VERSION,
    SCIENTIFIC_EVIDENCE_SCHEMA_VERSION,
    _normalize_bbox,
    build_evidence_dto,
    derive_evidence_id,
)
from yuxi.knowledge.evidence.validator import verify_evidence

pytestmark = [pytest.mark.unit]


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _anchor(**overrides):
    base = dict(
        anchor_id="ea_001",
        parse_revision_id="spr_1",
        page=3,
        bbox=[40.48, 586.55, 281.57, 752.85],
        word_start=103,
        word_end=118,
        quote="it has two typical SANT domains between 115-164 and 167-215 amino acids",
        quote_hash=_digest("it has two typical SANT domains between 115-164 and 167-215 amino acids"),
        prefix_hash="x",
        suffix_hash="y",
        fragments=[{"page_index": 2, "bbox": [40.48, 586.55, 281.57, 752.85], "coordinate_space": "pdf_points"}],
        anchor_type="paragraph",
        locator_quality="HIGH",
        confidence=1.0,
        locatable=True,
        source="pymupdf",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _chunk(**overrides):
    base = dict(
        chunk_id="chunk_1",
        kb_id="kb_1",
        file_id="file_1",
        content=(
            "OsMYB73 is a R2R3 MYB transcription factor. "
            "it has two typical SANT domains between 115-164 and 167-215 amino acids. "
            "The protein is localized in the nucleus."
        ),
        source_provenance={
            "parse_revision_id": "spr_1",
            "index_revision_id": "sir_1",
            "anchor_ids": ["ea_001"],
        },
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_evidence_id_is_deterministic_and_versioned():
    args = {
        "source_sha256": "a" * 64,
        "page_number": 3,
        "bbox": [40.48, 586.55, 281.57, 752.85],
        "word_start": 10,
        "word_end": 20,
        "quote_hash": "h1",
    }
    first = derive_evidence_id(**args)
    second = derive_evidence_id(**args)
    other = derive_evidence_id(**{**args, "source_sha256": "b" * 64})
    # 同一 PDF 出现相同句子和相同页内词偏移时，物理页/矩形必须消除碰撞。
    repeated_on_other_page = derive_evidence_id(**{**args, "page_number": 4})
    repeated_at_other_bbox = derive_evidence_id(**{**args, "bbox": [40.48, 100.0, 281.57, 200.0]})
    assert first == second
    assert first != other
    assert first != repeated_on_other_page
    assert first != repeated_at_other_bbox
    assert first.startswith("ev_")
    # 算法版本进入派生输入：同位置跨版本产生新 id（升级时历史引用可迁移）
    bumped = derive_evidence_id(**{**args, "algo_version": EVIDENCE_ID_ALGO_VERSION + 1})
    assert bumped != first


def test_verify_ok_path_all_checks_pass():
    result = verify_evidence(_anchor(), _chunk(), source_sha256="a" * 64)
    assert result["status"] == "OK"
    assert result["errors"] == []
    assert all(result["checks"].values())


def test_verify_detects_quote_mismatch():
    anchor = _anchor(quote_hash="deadbeef")
    result = verify_evidence(anchor, _chunk(), source_sha256="a" * 64)
    assert result["status"] == "FAILED"
    assert "QUOTE_MISMATCH" in result["errors"]


def test_verify_detects_revision_mismatch():
    chunk = _chunk(source_provenance={"parse_revision_id": "spr_OTHER", "anchor_ids": ["ea_001"]})
    result = verify_evidence(_anchor(), chunk, source_sha256="a" * 64)
    assert result["status"] == "FAILED"
    assert "SOURCE_REVISION_MISMATCH" in result["errors"]


def test_verify_unaligned_quote_degrades_but_not_fails():
    chunk = _chunk(content="completely unrelated text without the anchor quote")
    result = verify_evidence(_anchor(), chunk, source_sha256="a" * 64)
    assert result["status"] == "DEGRADED"
    assert result["errors"] == ["AMBIGUOUS_ALIGNMENT"]
    # 物理定位仍然成立，证据可出境（prefix/suffix/char selector 为空）


def test_verify_invalid_geometry_fails():
    anchor = _anchor(bbox=[100.0, 200.0, 50.0, 300.0])  # x1 < x0
    result = verify_evidence(anchor, _chunk(), source_sha256="a" * 64)
    assert result["status"] == "FAILED"
    assert "INVALID_GEOMETRY" in result["errors"]


def test_verify_missing_provenance_fails():
    chunk = _chunk(source_provenance=None)
    result = verify_evidence(_anchor(), chunk, source_sha256="a" * 64)
    assert "MISSING_PROVENANCE" in result["errors"]


def test_verify_rejects_non_hex_source_hash():
    result = verify_evidence(_anchor(), _chunk(), source_sha256="z" * 64)

    assert result["status"] == "FAILED"
    assert result["checks"]["provenance_complete"] is False
    assert "MISSING_PROVENANCE" in result["errors"]
    assert result["status"] == "FAILED"


def test_build_dto_contains_three_selectors_and_version_chain():
    anchor = _anchor()
    chunk = _chunk()
    verification = verify_evidence(anchor, chunk, source_sha256="c" * 64)
    dto = build_evidence_dto(
        anchor=anchor,
        chunk=chunk,
        source_sha256="c" * 64,
        verification=verification,
        retrieval={"retrieval_id": "kr_1", "chunk_id": "chunk_1"},
    )
    assert dto["schema_version"] == SCIENTIFIC_EVIDENCE_SCHEMA_VERSION
    # selector 1：精确原文（可对齐时带 prefix/suffix）
    assert dto["quote"]["exact"] == anchor.quote
    assert dto["quote"]["prefix"] and dto["quote"]["suffix"]
    assert dto["quote"]["start_char"] is not None and dto["quote"]["end_char"] > dto["quote"]["start_char"]
    # selector 2：文本位置（词偏移来自 anchor）
    assert dto["quote"]["start_word"] == 103 and dto["quote"]["end_word"] == 118
    # selector 3：物理定位（fragments 归一化，页码 = 页索引 + 1）
    fragment = dto["locator"]["fragments"][0]
    assert fragment["page_index"] == 2 and fragment["page_number"] == 3
    assert fragment["bbox"] == [40.48, 586.55, 281.57, 752.85]
    # 版本链
    assert dto["source"]["parse_revision_id"] == "spr_1"
    assert dto["source"]["index_revision_id"] == "sir_1"
    assert dto["source"]["source_sha256"] == "c" * 64
    assert dto["source"]["kb_id"] == "kb_1"
    assert dto["source"]["chunk_id"] == "chunk_1"
    assert dto["evidence_role"] == "RETRIEVAL_CANDIDATE"
    assert dto["citable"] is True
    assert dto["evidence_id"] == derive_evidence_id(
        source_sha256="c" * 64,
        page_number=3,
        bbox=[40.48, 586.55, 281.57, 752.85],
        word_start=103,
        word_end=118,
        quote_hash=anchor.quote_hash,
        anchor_id=anchor.anchor_id,
    )
    assert dto["verification"]["status"] == "OK"


def test_build_dto_unaligned_quote_keeps_empty_char_selector():
    anchor = _anchor()
    chunk = _chunk(content="unrelated")
    verification = verify_evidence(anchor, chunk, source_sha256="d" * 64)
    dto = build_evidence_dto(anchor=anchor, chunk=chunk, source_sha256="d" * 64, verification=verification)
    assert dto["quote"]["start_char"] is None
    assert dto["quote"]["prefix"] is None
    assert dto["locator"]["fragments"]  # 物理定位仍在
    assert dto["verification"]["status"] == "DEGRADED"
    assert dto["citable"] is False


def test_build_dto_bbox_fallback_keeps_one_based_anchor_page():
    anchor = _anchor(fragments=[])
    verification = verify_evidence(anchor, _chunk(), source_sha256="e" * 64)
    dto = build_evidence_dto(
        anchor=anchor,
        chunk=_chunk(),
        source_sha256="e" * 64,
        verification=verification,
    )
    assert dto["locator"]["fragments"][0]["page_index"] == 2
    assert dto["locator"]["fragments"][0]["page_number"] == 3


def test_verify_rejects_unlocatable_anchor_and_zero_page():
    unlocatable = verify_evidence(
        _anchor(locatable=False),
        _chunk(),
        source_sha256="a" * 64,
    )
    zero_page = verify_evidence(
        _anchor(page=0, fragments=[]),
        _chunk(),
        source_sha256="a" * 64,
    )
    assert unlocatable["status"] == "FAILED"
    assert "UNLOCATABLE" in unlocatable["errors"]
    assert zero_page["status"] == "FAILED"
    assert "INVALID_GEOMETRY" in zero_page["errors"]


def test_verify_rejects_missing_source_hash_and_wrong_fragment_page():
    missing_hash = verify_evidence(_anchor(), _chunk(), source_sha256="")
    wrong_page = verify_evidence(
        _anchor(fragments=[{"page_index": 7, "bbox": [1, 1, 2, 2], "coordinate_space": "pdf_points"}]),
        _chunk(),
        source_sha256="a" * 64,
    )
    assert missing_hash["status"] == "FAILED"
    assert "MISSING_PROVENANCE" in missing_hash["errors"]
    assert wrong_page["status"] == "FAILED"
    assert "INVALID_GEOMETRY" in wrong_page["errors"]


def test_normalize_bbox_rejects_malformed():
    assert _normalize_bbox([1, 2, 3]) is None
    assert _normalize_bbox([1, 2, 3, "x"]) is None
    assert _normalize_bbox([10, 10, 5, 20]) is None  # x1 < x0
    assert _normalize_bbox([0.0, 0.0, 10.0, 20.0]) == [0.0, 0.0, 10.0, 20.0]


def test_summary_counts():
    items = [
        {"verification": {"status": "OK"}},
        {"verification": {"status": "DEGRADED"}},
        {"verification": {"status": "OK"}},
    ]
    summary = _summary(items, [{"verification": {"status": "FAILED"}}])
    assert summary == {"total": 3, "verified": 2, "degraded": 1, "rejected": 1}
