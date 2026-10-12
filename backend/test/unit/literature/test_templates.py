"""输出模板渲染测试（P1）。

格式对标上游 PaperAgent ``packages/paperparse/templates/*.md.j2``：
- obsidian_bilingual: EN 空行 ZH，无标记；空参考文献 -> （参考文献未解析）
- obsidian_bilingual_alt: **EN** / **ZH** 标记；空参考文献无兜底
- plain: 仅 EN；空参考文献无兜底

核心不变量：切换模板**零 LLM 调用**（纯函数），且产物不含证据资格声明。
"""

from __future__ import annotations

import pytest

from yuxi.literature import templates

ITEMS = [
    {"type": "heading", "text": "Introduction"},
    {"type": "paragraph", "text_en": "Rice endosperm.", "text_zh": "水稻胚乳。"},
    {"type": "figure", "file": "fig1.png"},
    {"type": "paragraph", "text_en": "No translation yet.", "text_zh": ""},
]
REFS = [{"number": 1, "raw_text": "Smith 2020"}]


def test_default_template_is_bilingual():
    assert templates.DEFAULT_TEMPLATE == templates.TEMPLATE_BILINGUAL
    assert templates.resolve_template(None).key == templates.TEMPLATE_BILINGUAL


def test_unknown_template_falls_back_closed():
    """未知模板不静默套用近似模板（避免错排版冒充用户选择）。"""
    assert templates.resolve_template("does-not-exist").key == templates.DEFAULT_TEMPLATE
    assert templates.is_known_template("does-not-exist") is False
    assert templates.is_known_template(templates.TEMPLATE_PLAIN) is True


def test_list_templates_marks_exactly_one_default():
    listed = templates.list_templates()
    assert len(listed) == 3
    assert sum(1 for spec in listed if spec["default"]) == 1


def test_bilingual_layout_matches_upstream():
    """EN 空行 ZH，无语言标记；图与标题保留。"""
    out = templates.render_markdown(ITEMS, references=REFS, title="Rice")
    assert out.startswith("# Rice\n")
    assert "Rice endosperm.\n\n水稻胚乳。" in out
    assert "![](fig1.png)" in out
    assert "## Introduction" in out
    # bilingual 不使用 EN/ZH 标记
    assert "**EN**" not in out and "**ZH**" not in out


def test_bilingual_alt_uses_labels():
    out = templates.render_markdown(ITEMS, references=REFS, title="Rice", template=templates.TEMPLATE_BILINGUAL_ALT)
    assert "**EN** Rice endosperm." in out
    assert "**ZH** 水稻胚乳。" in out
    # 无译文的段落不得输出空的 **ZH** 标签
    assert "**ZH**" in out and out.count("**ZH**") == 1


def test_plain_drops_translation_entirely():
    out = templates.render_markdown(ITEMS, references=REFS, title="Rice", template=templates.TEMPLATE_PLAIN)
    assert "Rice endosperm." in out
    assert "水稻胚乳。" not in out
    assert "**EN**" not in out


def test_empty_references_placeholder_only_in_bilingual():
    """上游只有 bilingual 有空参考文献兜底，alt/plain 无——差异必须保留。"""
    assert "（参考文献未解析）" in templates.render_markdown(ITEMS, references=[], title="T")
    assert "（参考文献未解析）" not in templates.render_markdown(
        ITEMS, references=[], title="T", template=templates.TEMPLATE_BILINGUAL_ALT
    )
    assert "（参考文献未解析）" not in templates.render_markdown(
        ITEMS, references=[], title="T", template=templates.TEMPLATE_PLAIN
    )


def test_references_numbering():
    out = templates.render_markdown(ITEMS, references=REFS, title="T")
    assert "## References" in out
    assert "1. Smith 2020" in out


def test_render_is_pure_and_deterministic():
    """同一输入重复渲染字节一致——模板切换不引入随机性（可安全重渲染）。"""
    first = templates.render_markdown(ITEMS, references=REFS, title="T")
    second = templates.render_markdown(ITEMS, references=REFS, title="T")
    assert first == second


def test_accepts_yuxi_paragraph_row_shape():
    """兼容 Yuxi 实际的 source_text/translated_text 字段命名。"""
    rows = [{"source_text": "Alpha.", "translated_text": "阿尔法。"}]
    out = templates.render_markdown(rows, references=[], title="T")
    assert "Alpha." in out and "阿尔法。" in out


@pytest.mark.parametrize("bad", [None, "text", 123, []])
def test_malformed_items_are_skipped_not_crashed(bad):
    """脏数据不得让渲染炸掉（跳过即可，产物是派生物）。"""
    out = templates.render_markdown([bad, {"text_en": "ok"}], references=[], title="T")
    assert "ok" in out
