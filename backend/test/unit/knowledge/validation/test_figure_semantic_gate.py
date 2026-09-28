"""图表语义发布门禁测试（P3 企业级升级，2026-09-27）。

覆盖：Claim 分类、条件覆盖校验、数值溯源校验、三态视觉状态、
UNSUPPORTED 断言删除、未知图号拒绝。
"""

from __future__ import annotations

import pytest

from yuxi.knowledge.validation.figure_semantic_gate import (
    ClaimType,
    Verdict,
    VisualStatus,
    classify_claim,
    determine_visual_status,
    run_semantic_gate,
)

pytestmark = [pytest.mark.unit]


# ---- Claim 分类 ----


def test_classify_mechanism():
    assert classify_claim("OsMYB73 通过调控生长素信号通路影响粒长") == ClaimType.MECHANISM
    assert classify_claim("The transcription factor regulates downstream genes") == ClaimType.MECHANISM


def test_classify_numeric():
    assert classify_claim("osmyb73 垩白度为 26.8%") == ClaimType.NUMERIC
    assert classify_claim("粒长增加 0.84 mm") == ClaimType.NUMERIC  # 无显式比较双方
    assert classify_claim("osmyb73 比 WT 升高 14.7 个百分点") == ClaimType.COMPARISON  # 有"比"


def test_classify_comparison():
    assert classify_claim("osmyb73 比 WT 显著升高 14.7 个百分点") == ClaimType.COMPARISON


def test_classify_hypothesis():
    assert classify_claim("作者提出 OsMYB73 可能参与调控") == ClaimType.HYPOTHESIS


def test_classify_direct_observation():
    assert classify_claim("突变体籽粒表现为粒长变长") == ClaimType.DIRECT_OBSERVATION


# ---- 条件覆盖校验 ----


def test_heat_claim_with_only_control_data_rejected():
    """Q4 场景：断言含热胁迫条件，但可用数据只有常温 → CONDITION_MISMATCH。"""
    verdicts = run_semantic_gate(
        "热胁迫下 osmyb73 垩白度为 95.6%",
        available_conditions={"control"},
    )
    assert any(v.reason_code == "CONDITION_MISMATCH" for v in verdicts)


def test_heat_claim_with_both_conditions_supported():
    verdicts = run_semantic_gate(
        "热胁迫下 osmyb73 垩白度为 26.8%",
        available_conditions={"control", "heat"},
    )
    assert all(v.reason_code != "CONDITION_MISMATCH" for v in verdicts)


# ---- 数值溯源校验 ----


def test_numeric_not_in_table_rejected():
    """数值不在表格中 → NUMERIC_NOT_IN_SOURCE（防编造数字）。"""
    table = [
        [{"text": "Genotype"}, {"text": "Chalkiness"}],
        [{"text": "WT"}, {"text": "12.1"}],
        [{"text": "osmyb73"}, {"text": "26.8"}],
    ]
    verdicts = run_semantic_gate(
        "osmyb73 垩白度为 95.6%",
        available_conditions={"control"},
        table_rows=table,
    )
    assert any(v.reason_code == "NUMERIC_NOT_IN_SOURCE" for v in verdicts)


def test_numeric_in_table_supported():
    table = [
        [{"text": "Genotype"}, {"text": "Chalkiness"}],
        [{"text": "WT"}, {"text": "12.1"}],
        [{"text": "osmyb73"}, {"text": "26.8"}],
    ]
    verdicts = run_semantic_gate(
        "osmyb73 垩白度为 26.8%",
        available_conditions={"control"},
        table_rows=table,
    )
    assert not any(v.reason_code == "NUMERIC_NOT_IN_SOURCE" for v in verdicts)


# ---- 未知图号 ----


def test_unknown_figure_label_rejected():
    verdicts = run_semantic_gate(
        "Figure 99 展示了突变体表型",
        figure_labels_in_registry={"Figure 2", "Figure 5"},
    )
    assert any(v.reason_code == "UNKNOWN_FIGURE_LABEL" for v in verdicts)


def test_known_figure_label_supported():
    verdicts = run_semantic_gate(
        "Figure 5 展示了突变体表型",
        figure_labels_in_registry={"Figure 5", "Figure 6"},
    )
    assert not any(v.reason_code == "UNKNOWN_FIGURE_LABEL" for v in verdicts)


# ---- 三态视觉状态 ----


def test_visual_status_three_states():
    assert (
        determine_visual_status(caption_verified=True, has_asset=True, semantic_passed=True)
        == VisualStatus.VERIFIED_WITH_ASSET
    )
    assert (
        determine_visual_status(caption_verified=True, has_asset=False, semantic_passed=True)
        == VisualStatus.VERIFIED_CAPTION_ONLY
    )
    assert (
        determine_visual_status(caption_verified=False, has_asset=False, semantic_passed=False)
        == VisualStatus.REFERENCE_ONLY
    )


def test_visual_status_enum_covers_all_emitted_values():
    """跨端契约锁（2026-09-27）：载荷会发出的每个值都必须是枚举成员。

    事故：载荷发出 ``VERIFIED_WITH_TABLE`` 而枚举未定义该成员——任何按枚举校验
    的客户端（桌面 Rust / TS）会把它判为非法值，属跨端静默失效类缺陷。
    """
    from yuxi.services.chat_service import _visual_status_for_ref

    emitted = {
        _visual_status_for_ref(published_kind="table", has_caption_evidence=True),
        _visual_status_for_ref(published_kind="figure", has_caption_evidence=True),
        _visual_status_for_ref(published_kind=None, has_caption_evidence=True),
        _visual_status_for_ref(published_kind=None, has_caption_evidence=False),
    }
    assert emitted == {member.value for member in VisualStatus}
    for value in emitted:
        assert VisualStatus(value)  # 非法值会在此抛 ValueError


def test_visual_status_reference_only_is_reachable():
    """REFERENCE_ONLY 必须可达（此前永不产出，属枚举里的死成员）。"""
    from yuxi.services.chat_service import _visual_status_for_ref

    assert _visual_status_for_ref(published_kind=None, has_caption_evidence=False) == VisualStatus.REFERENCE_ONLY.value


# ---- 端到端：完整正文多断言 ----


def test_full_answer_multiple_claims():
    """模拟 Q4 场景：常温数据有据 + 热胁迫断言无据 → 后者被删。"""
    text = (
        "在常温条件下，osmyb73 垩白度为 26.8%，WT 为 12.1%。"
        "热胁迫下 osmyb73 垩白度升高至 95.6%。"
        "Complemented 恢复至 13.0%。"
    )
    table = [
        [{"text": "Genotype"}, {"text": "Chalkiness"}],
        [{"text": "WT"}, {"text": "12.1"}],
        [{"text": "osmyb73"}, {"text": "26.8"}],
        [{"text": "Complemented"}, {"text": "13.0"}],
    ]
    verdicts = run_semantic_gate(
        text,
        figure_labels_in_registry={"Table 1"},
        available_conditions={"control"},
        table_rows=table,
    )
    # "95.6" 不在表格中 → 至少一条 NUMERIC_NOT_IN_SOURCE 或 CONDITION_MISMATCH
    unsupported = [v for v in verdicts if v.verdict == Verdict.UNSUPPORTED]
    assert len(unsupported) >= 1
    # "26.8" 和 "13.0" 在表格中 → SUPPORTED
    supported = [v for v in verdicts if v.verdict == Verdict.SUPPORTED]
    assert len(supported) >= 1
