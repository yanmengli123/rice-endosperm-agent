"""五态语义的用户面固定文案（FOUND / NOT_FOUND / UNAVAILABLE / AMBIGUOUS / CONFLICT）。

定位（企业级状态语义）：
- FOUND：由数据面投影正常呈现（本模块返回 None，不适用）；
- NOT_FOUND：明确「未找到」——查询成功且权威源确认无该记录；
- UNAVAILABLE：明确「数据源暂不可用」——**绝不伪装成未找到**（连接失败/
  超时是基础设施状态，不是数据不存在的科学结论）；
- AMBIGUOUS：提示存在多个候选，引导用户带完整标识符点名重查；
- CONFLICT：多源值相互冲突，提示点名单一来源——不让模型裁决。

输入是 ``RunSourceManifest.source_uses``（``SourceUseRecord.status`` 已归一为
provider_status 或 audit status）。只要存在 adopted 成功调用就返回 None
（正常投影负责呈现值）；完全不适用（无 MCP 调用）也返回 None。
"""

from __future__ import annotations

from typing import Any

_UNAVAILABLE_STATUSES = frozenset({"UNAVAILABLE", "ERROR", "TIMEOUT", "UNAVAILABLE_ERROR"})
_NOT_FOUND_STATUSES = frozenset({"NOT_FOUND", "NO_EVIDENCE"})


def _use_value(use: Any, field: str) -> Any:
    if isinstance(use, dict):
        return use.get(field)
    return getattr(use, field, None)


def render_provider_status_answer(source_uses: list[Any] | None) -> str | None:
    """无 adopted 事实的 MCP 轮次 → 按 provider_status 给确定性终态文案。

    优先级：UNAVAILABLE > AMBIGUOUS > CONFLICT > NOT_FOUND——基础设施不可用
    必须最先声明，避免被同轮其他源的"未找到"稀释成数据不存在。
    """
    uses = [use for use in list(source_uses or []) if _use_value(use, "provider_id")]
    if not uses:
        return None
    if any(bool(_use_value(use, "adopted")) for use in uses):
        return None  # 存在成功调用：值呈现交给数据面投影

    statuses = {str(_use_value(use, "status") or "").upper() for use in uses}
    providers = sorted({str(_use_value(use, "provider_id")) for use in uses if _use_value(use, "provider_id")})

    def _named(copy: str) -> str:
        source_label = "、".join(f"`{provider}`" for provider in providers)
        return f"{copy}（数据源：{source_label}）"

    if statuses & _UNAVAILABLE_STATUSES:
        return _named("数据源暂不可用，请稍后重试。")
    if "AMBIGUOUS" in statuses:
        return _named("该查询存在多个候选记录，无法唯一确定；请携带完整标识符（如 RAP ID / MSU ID）重新点名查询。")
    if "CONFLICT" in statuses:
        return _named("不同数据源对同一记录返回了相互冲突的值；请点名单一数据源查询以获取该源的权威值。")
    if statuses & _NOT_FOUND_STATUSES:
        return _named("未找到匹配的记录。")
    return _named("未获取到可发布的数据值。")


__all__ = ["render_provider_status_answer"]
