"""「本机浏览器」开关未开启时的引导：意图识别 + 提示词装配。

事故背景：用户在对话里明确要求用本机浏览器打开网页，但输入框开关未开，
工具组未装配，模型答「我没有这个工具」——用户因此判断功能不可用。
本模块测试保证：只要用户本轮确在要求操作浏览器，提示词必须带上可执行的引导。
"""

from __future__ import annotations

from types import SimpleNamespace

from yuxi.agents.buildin.chatbot.prompt import build_prompt_with_context
from yuxi.agents.toolkits.browser.prompt import (
    BROWSER_DISABLED_NOTICE,
    detect_local_browser_intent,
)

_TOOL_PROMPT_MARKER = "本机浏览器执行约束"
_NOTICE_MARKER = "本机浏览器开关未开启"


# ---------------------------------------------------------------------------
# 意图识别 —— 召回优先
# ---------------------------------------------------------------------------


def test_detects_explicit_feature_name():
    """真实事故原句：点名「本机浏览器工具」+ 显式 URL + 动作。"""
    assert detect_local_browser_intent(
        "请必须使用本机浏览器工具访问 https://example.com ，读取页面标题和主标题后告诉我；不要使用网络搜索。"
    )


def test_detects_feature_name_without_url():
    assert detect_local_browser_intent("用本机浏览器打开百度首页")
    assert detect_local_browser_intent("Use my local browser to open the dashboard")


def test_detects_browser_tool_names():
    assert detect_local_browser_intent("请调用 browser_navigate 打开 https://x.io")


def test_detects_domain_word_plus_action():
    assert detect_local_browser_intent("帮我用浏览器访问一下这个网站")
    assert detect_local_browser_intent("用 chrome 打开这个链接并截图")


def test_detects_explicit_url_plus_action_without_browser_word():
    assert detect_local_browser_intent("打开 https://example.com 看看标题是什么")
    assert detect_local_browser_intent("访问 www.example.org 读取内容")


def test_detects_english_browsing_intent():
    assert detect_local_browser_intent("Please open https://example.com in the browser")
    assert detect_local_browser_intent("browse to https://example.com and screenshot it")


def test_detects_site_target_without_browser_word():
    assert detect_local_browser_intent("打开百度首页看看")
    assert detect_local_browser_intent("访问 XX 官网并截图")


def test_domain_token_containing_action_token_is_not_a_false_positive():
    """回归：「浏览」是「浏览器」的子串，不得因为域词而误判动作。"""
    assert not detect_local_browser_intent("浏览器是一种常见的软件")
    assert not detect_local_browser_intent("网页设计的好看吗")


def test_detects_colloquial_shortened_actions():
    """回归：「查一下/搜一下」等口语缩短变体不在动词表时,真实用户口语被漏检
    （现场事故:「帮我在浏览器里查一下今天的新闻」得到干瘪否定且无引导）。"""
    assert detect_local_browser_intent("帮我在浏览器里查一下今天的新闻")
    assert detect_local_browser_intent("用浏览器帮我搜一下这个关键词")
    assert detect_local_browser_intent("网页打开后帮我看一下第一屏写了什么")
    assert detect_local_browser_intent("在这个网站上翻一下有没有联系我们")


def test_colloquial_action_without_domain_word_is_not_intent():
    """缩短动作词单独出现（无域词/URL 共现）不得触发。"""
    assert not detect_local_browser_intent("帮我查一下这个词的意思")
    assert not detect_local_browser_intent("搜一下本地缓存里的这个配置项")


def test_ignores_unrelated_questions():
    """普通科研提问不得触发：否则每条回答都会沾上浏览器开关提示。"""
    assert not detect_local_browser_intent("水稻胚乳灌浆期的淀粉合成酶有哪些？")
    assert not detect_local_browser_intent("帮我把这份 CSV 的列名规范化一下")
    assert not detect_local_browser_intent("")
    assert not detect_local_browser_intent(None)


# ---------------------------------------------------------------------------
# 提示词装配 —— 三种状态互斥
# ---------------------------------------------------------------------------


def _prompt(browser_enabled: bool, browser_intent: bool) -> str:
    return build_prompt_with_context(
        SimpleNamespace(system_prompt="", browser_enabled=browser_enabled, browser_intent=browser_intent)
    )


def test_intent_notices_are_injected_only_when_intent_and_disabled():
    notice = _prompt(browser_enabled=False, browser_intent=True)
    assert _NOTICE_MARKER in notice
    assert BROWSER_DISABLED_NOTICE.strip() in notice
    # 未开启轮不得带工具执行约束（否则模型会以为工具已装配）
    assert _TOOL_PROMPT_MARKER not in notice


def test_enabled_turn_gets_execution_constraints_not_disabled_notice():
    enabled = _prompt(browser_enabled=True, browser_intent=True)
    assert _TOOL_PROMPT_MARKER in enabled
    assert _NOTICE_MARKER not in enabled


def test_plain_turn_gets_neither_notice():
    plain = _prompt(browser_enabled=False, browser_intent=False)
    assert _TOOL_PROMPT_MARKER not in plain
    assert _NOTICE_MARKER not in plain


def test_notice_tells_model_to_enable_switch_not_to_deny_capability():
    notice = BROWSER_DISABLED_NOTICE
    # 必须给出可执行下一步，而不是干瘪否定「本平台没有浏览器能力」
    assert "开关" in notice
    assert "重新发送" in notice
    assert "没有浏览器能力" in notice  # 以「不得声称…」的形式出现
    # 必须禁止用常识冒充已访问
    assert "冒充" in notice
    # 引导需覆盖离线/未配对的现实前置条件（Chrome 未开时开关会被前端拦下）
    assert "Chrome" in notice or "扩展已连接" in notice
