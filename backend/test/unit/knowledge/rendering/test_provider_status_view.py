"""五态语义用户面固定文案（provider_status_view）行为契约。

红线：UNAVAILABLE 绝不伪装成未找到；存在 adopted 成功调用时不适用（值呈现
交给数据面投影）；无 MCP 调用不适用。
"""

from __future__ import annotations

from yuxi.knowledge.rendering.provider_status_view import (
    PROJECTION_FAILURE_COPY,
    render_provider_status_answer,
    resolve_projection_publish,
)


def _use(provider: str, status: str, *, adopted: bool = False) -> dict:
    return {"provider_id": provider, "operation": "op", "status": status, "adopted": adopted}


def test_unavailable_never_disguises_as_not_found():
    """同轮 UNAVAILABLE + NOT_FOUND 并存：基础设施状态优先声明。"""
    answer = render_provider_status_answer([_use("gene-authority", "UNAVAILABLE"), _use("ricekb", "NOT_FOUND")])
    assert answer is not None
    assert answer.startswith("数据源暂不可用")
    assert "未找到" not in answer
    assert "`gene-authority`" in answer and "`ricekb`" in answer


def test_pure_not_found_names_the_source():
    answer = render_provider_status_answer([_use("ricekb", "NOT_FOUND")])
    assert answer is not None
    assert answer.startswith("未找到")
    assert "`ricekb`" in answer


def test_ambiguous_and_conflict_guide_explicit_disambiguation():
    ambiguous = render_provider_status_answer([_use("bio-mcp", "AMBIGUOUS")])
    assert ambiguous is not None and "多个候选" in ambiguous and "标识符" in ambiguous
    conflict = render_provider_status_answer([_use("ricekb", "CONFLICT")])
    assert conflict is not None and "冲突" in conflict and "点名单一数据源" in conflict


def test_adopted_success_delegates_to_projection():
    assert render_provider_status_answer([_use("ricekb", "SUCCESS", adopted=True)]) is None


def test_provider_status_is_independent_from_execution_status():
    use = _use("ricekb", "SUCCESS") | {"execution_status": "SUCCESS", "provider_status": "NOT_FOUND"}
    assert render_provider_status_answer([use]).startswith("未找到")


def test_argument_invalid_and_partial_are_not_reported_as_not_found():
    invalid = render_provider_status_answer([_use("gene-authority", "ARGUMENT_INVALID")])
    assert invalid is not None and invalid.startswith("查询参数未通过")
    assert "未找到" not in invalid
    partial = render_provider_status_answer([_use("gene-authority", "PARTIAL")])
    assert partial is not None and partial.startswith("数据源只返回了部分结果")
    assert "未找到" not in partial


def test_contract_drift_and_projection_invalid_have_distinct_copies():
    """内部契约类故障各有独立文案，且都不要求用户「补 MCP-F」。"""
    drift = render_provider_status_answer([_use("ricekb", "CONTRACT_DRIFT")])
    assert drift is not None and drift.startswith("数据源返回的记录与本次请求不一致")
    assert "MCP-F" not in drift
    projection = render_provider_status_answer([_use("ricekb", "PROJECTION_INVALID")])
    assert projection is not None and projection.startswith(PROJECTION_FAILURE_COPY)
    assert "MCP-F" not in projection
    assert drift.split("（数据源")[0] != projection.split("（数据源")[0]


def test_projection_publish_requires_an_actual_gate_pass():
    """发布裁决：只有门禁实际 PASSED 才允许发布；其余一律装配失败（调用方不得覆盖）。"""
    assert resolve_projection_publish({"status": "PASSED"}) == (True, "OK")
    for rejected in ({"status": "REJECTED"}, {"status": "DEGRADED"}, {"status": "FAILED"}, {}, None):
        publishable, projection_status = resolve_projection_publish(rejected)
        assert publishable is False, rejected
        assert projection_status == "PROJECTION_INVALID", rejected


def test_projection_failure_copy_never_demands_user_side_marker_repair():
    """装配失败必须是服务端问题，绝不出现「补 MCP-F」这类面向用户的指引。"""
    assert "MCP-F" not in PROJECTION_FAILURE_COPY
    answer = render_provider_status_answer([_use("ricekb", "PROJECTION_INVALID")])
    assert answer is not None and "MCP-F" not in answer


def test_no_mcp_uses_or_unknown_negative_falls_back():
    assert render_provider_status_answer([]) is None
    assert render_provider_status_answer(None) is None
    # 非 MCP（knowledge）通道不参与五态判定
    assert render_provider_status_answer([{"provider_id": None, "status": "NOT_FOUND", "adopted": False}]) is None
    fallback = render_provider_status_answer([_use("ricekb", "SOME_OTHER")])
    assert fallback is not None and fallback.startswith("未获取到可发布的数据值")
