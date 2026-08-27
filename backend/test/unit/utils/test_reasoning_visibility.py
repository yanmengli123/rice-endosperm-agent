from yuxi.utils.reasoning_visibility import (
    ReasoningVisibilityBuffer,
    redact_reasoning_metadata,
    sanitize_visible_text,
    split_reasoning_text,
)


L = chr(0x3c)  # '<'  -- written via chr() because the harness display eats '<'.
G = chr(0x3e)  # '>'

def test_sanitize_visible_text_keeps_normal_answer() -> None:
    assert sanitize_visible_text("你好！我是稻芯智析。") == "你好！我是稻芯智析。"


def test_sanitize_visible_text_removes_tagged_escaped_and_entity_reasoning() -> None:
    samples = (
        "<think>private chain</think>公开答案",
        r"\\\<THINK>private chain\\</think>公开答案",
        "&lt;think&gt;private chain&lt;/think&gt;公开答案",
        "&#x3c;think&#x3e;private chain&#x3c;/think&#x3e;公开答案",
    )
    for sample in samples:
        assert sanitize_visible_text(sample) == "公开答案"


def test_unclosed_reasoning_fails_closed() -> None:
    split = split_reasoning_text("公开前缀<think>private chain")
    assert split.visible == "公开前缀"
    assert split.reasoning_open is True


def test_stream_buffer_never_leaks_split_tag_or_reasoning() -> None:
    buffer = ReasoningVisibilityBuffer()
    assert buffer.feed("<th") == ("", True)
    assert buffer.feed("ink>private chain")[0] == ""
    assert buffer.feed("</think>你好！")[0] == "你好！"


def test_stream_buffer_holds_whitespace_interior_split_tags() -> None:
    # Providers may deliver "<", " ", "\n", "think>" as separate deltas.
    buffer = ReasoningVisibilityBuffer()
    assert buffer.feed("IP") == ("IP", False)
    for delta in ("< ", " ", "\n", "\t", "think>"):
        visible_delta, _ = buffer.feed(delta)
        assert visible_delta == "", f"fragment leaked: {delta!r}"
    assert buffer.feed(" private chain")[0] == ""
    assert buffer.feed("" + L + "/think" + G + "最终回答")[0] == "最终回答"


def test_stream_buffer_holds_entity_and_escaped_split_tags() -> None:
    scenarios = (
        # (deltas, final_visible) -- reasoning body must never leak; only the
        # body after the closing tag may be emitted.
        (("&l", "t;think" + G + "secret", "&lt;/think" + G + "final"), "final"),
        (("&#6", "0;think" + G + "secret", "&#x3", "c;/think" + G + "可见"), "可见"),
        (("\\" + L, "th", "ink" + G + "secret"), ""),
    )
    for deltas, expected in scenarios:
        buffer = ReasoningVisibilityBuffer()
        emitted = []
        for delta in deltas:
            visible_delta, _ = buffer.feed(delta)
            if visible_delta:
                emitted.append(visible_delta)
        assert "secret" not in "".join(emitted), f"reasoning leaked: {emitted}"
        assert "".join(emitted) == expected, f"unexpected emission {emitted!r}, want {expected!r}"




def test_stateless_sanitize_keeps_incomplete_tag_as_visible_text() -> None:
    # A complete message may legitimately end with a literal "<t" (code/HTML).
    # The stateless path must not truncate it; only the streaming buffer holds
    # it until the stream ends.
    assert sanitize_visible_text("页面推荐：" + L + "t") == "页面推荐：" + L + "t"
    assert sanitize_visible_text("使用 a " + L + " b 比较") == "使用 a " + L + " b 比较"
    assert sanitize_visible_text("结尾一个\\") == "结尾一个\\"


def test_stream_buffer_does_not_overhold_legit_prose() -> None:
    buffer = ReasoningVisibilityBuffer()
    for delta in ("如果 x " + L + " y 则", " 递增", "，如：a " + L + " b", " 结束"):
        buffer.feed(delta)
    # 无标签的普通正文应完整放出，不应被 pending-tag 逻辑误吞 " < y"。
    assert buffer.visible.endswith("结束")
    assert "x " + L + " y" in buffer.visible


def test_redact_reasoning_metadata_retains_only_non_sensitive_state() -> None:
    metadata = redact_reasoning_metadata(
        {
            "content": "<think>private</think>公开答案",
            "reasoning_content": "private structured chain",
            "additional_kwargs": {"reasoning_content": "private duplicate", "finish_reason": "stop"},
        }
    )
    assert metadata["content"] == "公开答案"
    assert metadata["additional_kwargs"] == {"finish_reason": "stop"}
    assert metadata["reasoning_redacted"] is True
    assert "reasoning_content" not in metadata
