"""五态语义用户面固定文案（provider_status_view）行为契约。

红线：UNAVAILABLE 绝不伪装成未找到；存在 adopted 成功调用时不适用（值呈现
交给数据面投影）；无 MCP 调用不适用。
"""

from __future__ import annotations

from yuxi.knowledge.rendering.provider_status_view import render_provider_status_answer


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


def test_no_mcp_uses_or_unknown_negative_falls_back():
    assert render_provider_status_answer([]) is None
    assert render_provider_status_answer(None) is None
    # 非 MCP（knowledge）通道不参与五态判定
    assert render_provider_status_answer([{"provider_id": None, "status": "NOT_FOUND", "adopted": False}]) is None
    fallback = render_provider_status_answer([_use("ricekb", "SOME_OTHER")])
    assert fallback is not None and fallback.startswith("未获取到可发布的数据值")
