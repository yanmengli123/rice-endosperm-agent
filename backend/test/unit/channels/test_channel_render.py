"""渠道渲染单测：Markdown 降级、按平台限制分片（字符/字节两种度量）。"""

from __future__ import annotations

import pytest

from yuxi.channels.render import chunk_text, render_outbound, strip_markdown_to_plain

pytestmark = [pytest.mark.unit]


class TestStrip:
    def test_links_unwrapped(self) -> None:
        assert strip_markdown_to_plain("见 [文档](https://example.com)") == "见 文档（https://example.com）"

    def test_images_removed(self) -> None:
        assert "logo" not in strip_markdown_to_plain("前 ![logo](https://x/y.png) 后")

    def test_emphasis_and_code(self) -> None:
        assert strip_markdown_to_plain("**粗体** 与 *斜体* 与 `code`") == "粗体 与 斜体 与 code"


class TestChunk:
    def test_short_text_single_chunk(self) -> None:
        assert chunk_text("hello", "telegram") == ["hello"]

    def test_paragraph_boundary_respected(self) -> None:
        paragraphs = "\n\n".join(f"段{i}_" + "x" * 3000 for i in range(3))
        chunks = chunk_text(paragraphs, "telegram")
        assert len(chunks) >= 2
        # 重组不丢内容（分片以空行连接）
        assert "\n\n".join(chunks).replace("\n\n", "") == paragraphs.replace("\n\n", "")

    def test_oversized_paragraph_hard_split(self) -> None:
        single = "字" * 10000
        chunks = chunk_text(single, "telegram")
        assert all(len(chunk) <= 3800 for chunk in chunks)
        assert "".join(chunks) == single

    def test_byte_limit_not_torn_utf8(self) -> None:
        text = "🧬" * 3000  # 每字符 4 字节，公众号限 1900 字节
        chunks = chunk_text(text, "wechat_oa")
        assert all(len(chunk.encode("utf-8")) <= 1900 for chunk in chunks)
        assert "".join(chunks) == text

    def test_empty(self) -> None:
        assert chunk_text("", "feishu") == []


class TestRenderOutbound:
    def test_wechat_oa_downgraded_to_plain(self) -> None:
        _, chunks = render_outbound("**答案** [ref](https://x)", "wechat_oa")
        joined = "".join(chunks)
        assert "**" not in joined and "ref（https://x）" in joined

    def test_web_url_appended(self) -> None:
        _, chunks = render_outbound("答案", "feishu", web_url="http://w/agent/t1")
        assert any("http://w/agent/t1" in chunk for chunk in chunks)

    def test_empty_output_placeholder(self) -> None:
        text, chunks = render_outbound("", "feishu")
        assert text and chunks
