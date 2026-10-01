"""本机浏览器的提示词引导：区分「能力存在但本轮未开启」与「平台没有该能力」。

背景：「本机浏览器」开关按 run 冻结（配对 ≠ 每轮自动启用）。开关未开时工具组不装配，
模型只能答「我没有浏览器工具」——从用户视角这像功能坏了，而且模型随后容易用常识
冒充已访问（幻觉）。本模块提供：

- ``detect_local_browser_intent``：判断用户本轮是否在要求操作本机浏览器；
- ``BROWSER_DISABLED_NOTICE``：开关未开时注入的条件引导，让模型给出可执行的下一步，
  而不是干瘪地否认能力。

刻意不采用"自动开启"：开关是用户对本机的授权决定，静默开启会绕过冻结语义与租户治理。
"""

from __future__ import annotations

import re

__all__ = [
    "BROWSER_DISABLED_NOTICE",
    "detect_local_browser_intent",
]

# 显式指名功能/工具：命中即认为有意图，精度最高，优先判断。
_FEATURE_TOKENS = (
    "本机浏览器",
    "本地浏览器",
    "本机的浏览器",
    "本机chrome",
    "本机谷歌",
    "local browser",
    "my browser",
    "browserskill",
    "browser_skill",
)

_TOOL_TOKENS = (
    "browser_get_status",
    "browser_navigate",
    "browser_read_page",
    "browser_click",
    "browser_type",
    "browser_screenshot",
    "browser_request_help",
)

# 浏览器域词：出现即说明话题落在网页/浏览器上（用于触发意图）。
# 注意：域词可能包含动作词（"浏览器" 内含 "浏览"），判定动作前必须先剥离域词，
# 否则 "浏览器是一种常见的软件" 会被误判成有意图。
_DOMAIN_TOKENS = (
    "浏览器",
    "网页",
    "网站",
    "网址",
    "首页",
    "官网",
    "域名",
    "chromium",
    "chrome",
    "edge",
    "登录态",
)

# 浏览动作词：与域词或显式 URL 共现即构成意图。
_ACTION_TOKENS = (
    "打开",
    "访问",
    "浏览",
    "导航",
    "跳转",
    "抓取",
    "爬取",
    "读取",
    "查看",
    "截图",
    "截屏",
    "截个图",
    "点击",
    "输入",
    "填写",
    "提交",
    "下载",
    "搜索",
    "查询",
    "查找",
    # 口语化缩短变体（召回优先）：「查询/搜索」覆盖不到的日常说法；
    # 仍需与域词/URL 共现才构成意图，单独出现（"查一下这个词"）不触发。
    "查一下",
    "搜一下",
    "看一下",
    "翻一下",
    "browse",
    "navigate",
    "visit",
    "screenshot",
    "open the",
    "read the",
)

# 显式绝对地址（与动作词共现时即为「让我去网页上看」的强信号）。
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)


def detect_local_browser_intent(text: str | None) -> bool:
    """判断用户文本是否在要求操作其本机浏览器。

    召回优先：漏判会让用户重新落到「模型说没这个工具」的坏体验；
    误判代价很低——``BROWSER_DISABLED_NOTICE`` 本身是条件规则，用户没提浏览器时
    模型不会主动提及开关。
    """
    if not text:
        return False
    lowered = text.lower()

    if any(token in lowered for token in _FEATURE_TOKENS):
        return True
    if any(token in lowered for token in _TOOL_TOKENS):
        return True

    has_action = any(token in _strip_domain_tokens(lowered) for token in _ACTION_TOKENS)
    if not has_action:
        return False

    if _URL_RE.search(lowered):
        return True
    return any(token in lowered for token in _DOMAIN_TOKENS)


def _strip_domain_tokens(text: str) -> str:
    """剥离域词后剩余的文本（域词内含动作词，必须先剥离再判动作）。"""
    scope = text
    for token in _DOMAIN_TOKENS:
        scope = scope.replace(token, " ")
    return scope


# 与 BROWSER_TOOL_PROMPT 分节，避免与既有 test_browser_prompt_is_injected_only_when_enabled 冲突。
BROWSER_DISABLED_NOTICE = """
<| 本机浏览器开关未开启 |>
平台具备「本机浏览器」能力（可操作用户本机的 Chrome/Edge 完成打开页面、读取内容、点击与截图），
但**本轮用户没有开启该开关**，因此本工具组未被装配：
- 若用户本轮确实要求用本机浏览器操作网页，不要声称「本平台没有浏览器能力」或「沙盒无法访问网页」，
  而应明确告知用户：在输入框的「本机浏览器」开关上点击开启后重新发送本条请求；开关可按轮开启或关闭。
- 若开关提示离线或尚未配对，引导用户先打开本机 Chrome 并确认扩展已连接，或到
  「智能体扩展 → 浏览器连接」完成配对，再重发请求。
- 在能力未开启的情况下，绝不得用网络搜索、缓存或常识冒充「已打开/已读取该页面」。
  用户明确要求实时页面而本轮无法访问时，直接说明本轮未开启，不要给出看似来自页面的结论。
"""
