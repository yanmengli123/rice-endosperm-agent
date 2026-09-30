"""渠道出站渲染：Markdown 归一与按平台限制分片。

原则：IM 是通知面，Web 是工作面——长内容分片推送 + 附网页全文链接；
不支持 Markdown 的平台降级为纯文本（链接展开、强调剥离、图片剔除）。
"""

from __future__ import annotations

import re

# 各平台单条消息限制（字符/字节），按保守值留余量；以官方文档为准。
CHUNK_LIMITS = {
    "feishu": ("chars", 20000),
    "wecom": ("bytes", 3800),
    "telegram": ("chars", 3800),
    "wechat_oa": ("bytes", 1900),
    "dingtalk": ("chars", 4500),
    "default": ("chars", 3000),
}

# 出站 Markdown 呈现策略（闭集，新增平台必须在此登记）：
# - native：消息体原生渲染 Markdown（企微 markdown / 钉钉 markdown 字段）；
# - card  ：卡片内的 Markdown 元素渲染（飞书 interactive card 的 markdown 元素）；
# - plain ：平台不渲染 Markdown，发送前剥离为纯文本（公众号、Telegram、default）。
MARKDOWN_NATIVE_PLATFORMS = frozenset({"wecom", "dingtalk"})
MARKDOWN_CARD_PLATFORMS = frozenset({"feishu"})

_IMAGE_PATTERN = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD_PATTERN = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC_PATTERN = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_CODE_FENCE_PATTERN = re.compile(r"```[a-zA-Z0-9_+-]*\n?")
_INLINE_CODE_PATTERN = re.compile(r"`([^`\n]+)`")
_HEADING_PATTERN = re.compile(r"^#{1,6}\s*", re.MULTILINE)


def strip_markdown_to_plain(markdown_text: str) -> str:
    """Markdown → 适合纯文本平台的近似 plain text。"""
    text = _IMAGE_PATTERN.sub("", markdown_text or "")
    text = _LINK_PATTERN.sub(lambda m: f"{m.group(1)}（{m.group(2)}）", text)
    text = _CODE_FENCE_PATTERN.sub("", text)
    text = _INLINE_CODE_PATTERN.sub(r"\1", text)
    text = _BOLD_PATTERN.sub(r"\1", text)
    text = _ITALIC_PATTERN.sub(r"\1", text)
    text = _HEADING_PATTERN.sub("", text)
    return text.strip()


def _measure(kind: str) -> tuple[str, int]:
    return CHUNK_LIMITS.get(kind, CHUNK_LIMITS["default"])


def _fits(chunk: str, unit: str, limit: int) -> bool:
    return len(chunk) <= limit if unit == "chars" else len(chunk.encode("utf-8")) <= limit


def _split_oversized(paragraph: str, unit: str, limit: int) -> list[str]:
    """单段超限（如长表格/长代码）时按行、再按字符硬切。"""
    lines: list[str] = []
    current = ""
    for line in paragraph.splitlines(keepends=True):
        candidate = current + line
        if _fits(candidate, unit, limit) or not current:
            current = candidate
            while not _fits(current, unit, limit):
                lines.append(current[:limit] if unit == "chars" else _hard_cut_bytes(current, limit))
                current = current[len(lines[-1]) :]
        else:
            lines.append(current)
            current = line
    if current:
        lines.append(current)
    return lines


def _hard_cut_bytes(text: str, limit: int) -> str:
    """按 UTF-8 字节上限切割且不撕开多字节字符。"""
    encoded = text.encode("utf-8")[:limit]
    return encoded.decode("utf-8", errors="ignore")


def chunk_text(text: str, channel_type: str) -> list[str]:
    """按段落边界分片；空文本返回空列表。"""
    unit, limit = _measure(channel_type)
    chunks: list[str] = []
    current = ""
    for paragraph in (text or "").split("\n\n"):
        block = paragraph if not current else f"\n\n{paragraph}"
        if _fits(current + block, unit, limit):
            current += block
            continue
        if current:
            chunks.append(current)
            current = ""
        if _fits(paragraph, unit, limit):
            current = paragraph
        else:
            chunks.extend(_split_oversized(paragraph, unit, limit))
    if current:
        chunks.append(current)
    return [chunk.strip() for chunk in chunks if chunk.strip()]


def markdown_mode(channel_type: str) -> str:
    """平台 Markdown 呈现模式：``native`` | ``card`` | ``plain``。

    出站渲染据此决定是否剥离 Markdown——历史上只有 ``wechat_oa`` 被剥离，
    导致飞书/Telegram 把 ``**加粗**``、表格竖线原样发给用户（可见缺陷）。
    """
    if channel_type in MARKDOWN_NATIVE_PLATFORMS:
        return "native"
    if channel_type in MARKDOWN_CARD_PLATFORMS:
        return "card"
    return "plain"


def render_outbound(markdown_text: str, channel_type: str, *, web_url: str | None = None) -> tuple[str, list[str]]:
    """按平台渲染：返回 (渲染后全文, 分片列表)。"""
    text = (markdown_text or "").strip()
    if not text:
        text = "（本次运行未产生文本输出）"
    if markdown_mode(channel_type) == "plain":
        text = strip_markdown_to_plain(text)
    if web_url:
        text = f"{text}\n\n📄 查看完整结果：{web_url}"
    return text, chunk_text(text, channel_type)
