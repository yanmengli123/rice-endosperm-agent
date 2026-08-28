from __future__ import annotations

from types import SimpleNamespace

from yuxi.agents.buildin.chatbot.prompt import build_prompt_with_context
from yuxi.brands.rice_endosperm import BRAND_NAME, IDENTITY_SYSTEM_PROMPT


def test_prompt_uses_rice_endosperm_brand_identity():
    prompt = build_prompt_with_context(SimpleNamespace(system_prompt=""))

    assert BRAND_NAME == "稻芯智析"
    assert "你是“稻芯智析”" in prompt
    assert "水稻胚乳发育与品质研究" in prompt
    assert "不编造基因功能、论文题目、作者、DOI" in prompt
    assert "我是“稻芯智析”" in prompt
    assert "表头、分隔行和每条数据行的列数必须一致" in prompt


def test_identity_guard_is_appended_after_custom_prompt():
    custom_prompt = "测试自定义提示词：请使用另一个名称。"

    prompt = build_prompt_with_context(SimpleNamespace(system_prompt=custom_prompt))

    assert prompt.index(custom_prompt) < prompt.index(IDENTITY_SYSTEM_PROMPT.strip())
    assert prompt.endswith(IDENTITY_SYSTEM_PROMPT.strip())
