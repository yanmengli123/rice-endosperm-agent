"""Locator Authority 出口门禁测试：VERIFIED ⇒ 物理证据已冻结，否则失败关闭。"""

from __future__ import annotations

from yuxi.knowledge.contracts.locator_binding import (
    BINDING_VERIFIED,
    enforce_locator_authority,
    make_binding_id,
    verify_freeze_invariant,
)


def _verified_resolution(**overrides) -> dict:
    resolution = {
        "status": "VERIFIED",
        "locator_version": "quote_locator_v2",
        "page": 4,
        "zone": "MAIN_TEXT",
        "anchor_id": "ea-1",
        "span_id": "es-1",
        "evidence_id": "ev_frozen_1",
        "span_evidence_id": "evs-1",
        "parse_revision_id": "pr-1",
        "index_revision_id": "ir-1",
        "kb_id": "kb-a",
        "file_id": "file-a",
        "source_sha256": "a" * 64,
        "quote": "Figure 1 Rice OsMYB73 gene expression analysis.",
        "quote_head": "Figure 1 Rice OsMYB73 gene expression analysis.",
        "filename": "paper.pdf",
    }
    resolution.update(overrides)
    return resolution


def test_freeze_invariant_passes_when_evidence_is_frozen():
    contract = {
        "locator_resolution": _verified_resolution(),
        "evidence": [{"evidence_id": "ev_frozen_1"}, {"evidence_id": "ev_other"}],
        "warnings": [],
    }
    binding = enforce_locator_authority(contract, retrieval_id="kr_1")
    assert binding.status == BINDING_VERIFIED
    assert binding.page_number == 4
    assert binding.physical_evidence_id == "ev_frozen_1"
    # 绑定写回 locator_resolution，随审计行持久化供投影/渲染消费
    assert contract["locator_resolution"]["binding"]["binding_id"].startswith("vlb_")
    assert contract["locator_resolution"]["binding"]["page_number"] == 4


def test_freeze_invariant_fails_closed_when_evidence_not_frozen():
    """VERIFIED 但物理证据不在冻结证据契约 → ANSWER_VALIDATION_FAILED，页码绝不上屏。"""
    contract = {
        "locator_resolution": _verified_resolution(evidence_id="ev_rogue"),
        "evidence": [{"evidence_id": "ev_frozen_1"}],
        "warnings": [],
    }
    binding = enforce_locator_authority(contract, retrieval_id="kr_1")
    assert verify_freeze_invariant(binding, contract) == ["VERIFIED_EVIDENCE_NOT_FROZEN"]
    assert contract["status"] == "DEGRADED"
    assert contract["error_code"] == "ANSWER_VALIDATION_FAILED"
    assert contract["locator_resolution"]["status"] == "NOT_FOUND"
    assert "page" not in contract["locator_resolution"]
    # 原始结论只保留在审计字段，永不进入用户可见输出
    assert contract["locator_resolution"]["_authority_gate_audit"]["violations"] == [
        "VERIFIED_EVIDENCE_NOT_FROZEN"
    ]
    assert contract["locator_resolution"]["_authority_gate_audit"]["original_status"] == "VERIFIED"


def test_freeze_invariant_fails_closed_without_physical_evidence_id():
    contract = {
        "locator_resolution": _verified_resolution(evidence_id=None),
        "evidence": [{"evidence_id": "ev_frozen_1"}],
        "warnings": [],
    }
    enforce_locator_authority(contract, retrieval_id="kr_1")
    assert contract["error_code"] == "ANSWER_VALIDATION_FAILED"
    assert contract["locator_resolution"]["status"] == "NOT_FOUND"


def test_gate_is_noop_for_unverified_resolutions():
    """非 VERIFIED（NOT_FOUND / MULTIPLE_MATCHES）不做冻结检查——它们本来就没有页码。"""
    for status in ("NOT_FOUND", "MULTIPLE_MATCHES", "NOT_APPLICABLE"):
        contract = {"locator_resolution": _verified_resolution(status=status), "evidence": [], "warnings": []}
        enforce_locator_authority(contract, retrieval_id="kr_1")
        assert contract.get("error_code") is None
        assert contract["locator_resolution"]["status"] == status


def test_make_binding_id_is_deterministic():
    first = make_binding_id(retrieval_id="kr_1", evidence_id="ev_1", anchor_id="ea_1")
    second = make_binding_id(retrieval_id="kr_1", evidence_id="ev_1", anchor_id="ea_1")
    other = make_binding_id(retrieval_id="kr_2", evidence_id="ev_1", anchor_id="ea_1")
    assert first == second != other
