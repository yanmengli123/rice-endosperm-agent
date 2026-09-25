"""MCP 负状态语义的用户面固定文案（FOUND / NOT_FOUND / UNAVAILABLE / AMBIGUOUS / CONFLICT 及企业级分流）。

定位（企业级状态语义）：
- FOUND：由数据面投影正常呈现（本模块返回 None，不适用）；
- NOT_FOUND：明确「未找到」——查询成功且权威源确认无该记录；
- UNAVAILABLE：明确「数据源暂不可用」——**绝不伪装成未找到**（只有连接失败/
  超时/限流/上游 5xx 是基础设施状态，不是数据不存在的科学结论）；
- PARTIAL：只拿到部分记录 → 声明不完整并引导重试，绝不补齐；
- AMBIGUOUS：提示存在多个候选，引导用户带完整标识符点名重查；
- CONFLICT：多源值相互冲突，提示点名单一来源——不让模型裁决；
- ARGUMENT_INVALID：参数未通过工具契约校验（可重试/需修契约），非数据源故障；
- CONTRACT_DRIFT：上游记录与本次请求不一致（标识符/类型/完整性），已拒绝发布；
- PROJECTION_INVALID：内部结果装配失败——绝不要求用户「补 MCP-F」。

输入是 ``RunSourceManifest.source_uses``（执行状态与来源五态分别保存在
``execution_status`` / ``provider_status``，并兼容旧清单的 ``status``）。只要存在 adopted 成功调用就返回 None
（正常投影负责呈现值）；完全不适用（无 MCP 调用）也返回 None。
"""

from __future__ import annotations

from typing import Any

from yuxi.knowledge.planning.turn_execution_plan import source_use_provider_status

_UNAVAILABLE_STATUSES = frozenset({"UNAVAILABLE", "ERROR", "TIMEOUT", "UNAVAILABLE_ERROR"})
_NOT_FOUND_STATUSES = frozenset({"NOT_FOUND", "NO_EVIDENCE"})
_ARGUMENT_STATUSES = frozenset({"ARGUMENT_INVALID"})
_CONTRACT_STATUSES = frozenset({"CONTRACT_DRIFT"})
_PROJECTION_STATUSES = frozenset({"PROJECTION_INVALID"})

#: 装配失败文案：唯一出口，数据面状态视图与确定性终态共用（绝不要求用户补 MCP-F）。
PROJECTION_FAILURE_COPY = "内部结果装配失败，系统已拒绝发布未经核验的结果；请重试或联系管理员查看审计。"


def resolve_projection_publish(validation: dict[str, Any] | None) -> tuple[bool, str]:
    """确定性投影的发布裁决：只有事实门禁**实际 PASSED** 才允许发布值视图。

    调用方禁止覆盖门禁结果（历史缺陷：只要存在 deterministic_text 就强制标
    PASSED，导致被拒绝的值表照样发布）。返回 ``(publishable, projection_status)``：
    ``(True, "OK")`` 原样发布投影文本；``(False, "PROJECTION_INVALID")`` 只发固定
    失败文案并保留门禁判定文本仅作审计留痕。
    """

    if str((validation or {}).get("status") or "").upper() == "PASSED":
        return True, "OK"
    return False, "PROJECTION_INVALID"


def _use_value(use: Any, field: str) -> Any:
    if isinstance(use, dict):
        return use.get(field)
    return getattr(use, field, None)


def render_provider_status_answer(source_uses: list[Any] | None) -> str | None:
    """无 adopted 事实的 MCP 轮次 → 按 provider_status 给确定性终态文案。

    优先级：参数契约错 > 记录契约漂移 > 装配失败 > UNAVAILABLE > PARTIAL >
    AMBIGUOUS > CONFLICT > NOT_FOUND——内部契约问题必须先于基础设施状态暴露，
    且任何一条都不得被下游「未找到」稀释。
    """
    uses = [use for use in list(source_uses or []) if _use_value(use, "provider_id")]
    if not uses:
        return None
    if any(bool(_use_value(use, "adopted")) for use in uses):
        return None  # 存在成功调用：值呈现交给数据面投影

    statuses = {source_use_provider_status(use) for use in uses}
    providers = sorted({str(_use_value(use, "provider_id")) for use in uses if _use_value(use, "provider_id")})

    def _named(copy: str) -> str:
        source_label = "、".join(f"`{provider}`" for provider in providers)
        return f"{copy}（数据源：{source_label}）"

    if statuses & _ARGUMENT_STATUSES:
        return _named("查询参数未通过数据源契约校验，系统未发布未经核验的结果；请重试或联系管理员检查工具契约。")
    if statuses & _CONTRACT_STATUSES:
        return _named(
            "数据源返回的记录与本次请求不一致（标识符、序列类型或完整性校验未通过），系统已拒绝发布；"
            "请重试或改用完整标识符点名查询。"
        )
    if statuses & _PROJECTION_STATUSES:
        return _named(PROJECTION_FAILURE_COPY)
    if statuses & _UNAVAILABLE_STATUSES:
        return _named("数据源暂不可用，请稍后重试。")
    if "PARTIAL" in statuses:
        return _named("数据源只返回了部分结果，当前结果不满足完整发布契约；请缩小查询范围后重试。")
    if "AMBIGUOUS" in statuses:
        return _named("该查询存在多个候选记录，无法唯一确定；请携带完整标识符（如 RAP ID / MSU ID）重新点名查询。")
    if "CONFLICT" in statuses:
        return _named("不同数据源对同一记录返回了相互冲突的值；请点名单一数据源查询以获取该源的权威值。")
    if statuses & _NOT_FOUND_STATUSES:
        return _named("未找到匹配的记录。")
    return _named("未获取到可发布的数据值。")


__all__ = ["PROJECTION_FAILURE_COPY", "render_provider_status_answer", "resolve_projection_publish"]
