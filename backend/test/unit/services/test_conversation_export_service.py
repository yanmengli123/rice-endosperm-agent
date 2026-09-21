"""会话 HTML 导出渲染层单测：XSS 防线、渲染结构、QA 配对、文件名清洗。"""

from __future__ import annotations

from datetime import datetime, UTC

import pytest
from bs4 import BeautifulSoup

from yuxi.services.conversation_export_service import (
    _CN_TZ,
    build_filename,
    render_conversation_html,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def _msg(msg_type: str, content: str = "", **extra) -> dict:
    message = {
        "id": 1,
        "type": msg_type,
        "content": content,
        "created_at": "2026-09-20T08:30:00",
        "extra_metadata": {},
    }
    message.update(extra)
    return message


def _render(history: list[dict], *, title: str = "胚乳发育问答", **overrides) -> str:
    params = {
        "title": title,
        "agent_slug": "sci-agent",
        "thread_id": "thread-abc.123",
        "created_at": datetime(2026, 9, 20, 8, 0, 0),
        "updated_at": datetime(2026, 9, 20, 9, 0, 0),
        "history": history,
        "exported_at": datetime(2026, 9, 21, 10, 0, 0, tzinfo=_CN_TZ),
    }
    params.update(overrides)
    return render_conversation_html(**params)


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


async def test_render_basic_conversation_structure():
    html = _render(
        [
            _msg("human", "水稻胚乳发育分几类？"),
            _msg("ai", "按发育模式分为三类：**Ⅰ型**、**Ⅱ型**、**Ⅲ型**。"),
            _msg("human", "第二问"),
            _msg("ai", "第二个回答。"),
        ]
    )
    soup = _soup(html)

    assert soup.find("meta", attrs={"http-equiv": "Content-Security-Policy"}) is not None
    assert len(soup.select(".qa")) == 2
    assert len(soup.select(".qa-q-text")) == 2
    # 两条问答以上出目录，锚点指向卡片 id
    toc_links = soup.select(".toc a")
    assert len(toc_links) == 2
    assert toc_links[0]["href"] == "#qa-1"
    assert "水稻胚乳发育分几类" in toc_links[0].get_text()
    # 元数据表齐整
    meta = {
        th.get_text(): td.get_text() for th, td in zip(soup.select(".meta-table th"), soup.select(".meta-table td"))
    }
    assert meta["智能体"] == "sci-agent"
    assert meta["会话标识"] == "thread-abc.123"
    assert meta["问答轮数"] == "2"
    # naive UTC 时间换算为 UTC+8
    assert meta["创建时间"] == "2026-09-20 16:00"


async def test_pairing_skips_tool_system_and_handles_leading_ai():
    html = _render(
        [
            _msg("ai", "先导回答（无提问记录）"),
            _msg("system", "系统指令不应出现"),
            _msg("human", "正式提问"),
            _msg("tool", "工具消息不应出现"),
            _msg("ai", "正式回答"),
        ]
    )
    soup = _soup(html)

    assert len(soup.select(".qa")) == 2
    assert "系统指令不应出现" not in html
    assert "工具消息不应出现" not in html
    # 先导回答轮没有问题块
    assert len(soup.select(".qa-q")) == 1


async def test_failed_round_renders_error_note():
    html = _render(
        [
            _msg("human", "会失败的问题"),
            _msg("ai", "", error_message="模型供应商超时"),
            _msg("ai", "重试后的正常回答"),
        ]
    )
    soup = _soup(html)

    assert "模型供应商超时" in soup.select_one(".answer-error").get_text()
    assert "重试后的正常回答" in html


async def test_raw_html_in_markdown_is_escaped_not_executed():
    html = _render(
        [
            _msg("human", "输入 <b>加粗</b> 与 <img src=x onerror=alert(1)>"),
            _msg("ai", "回答包含 <script>alert('xss')</script> 与 <iframe src=\"https://evil\"></iframe>"),
        ]
    )
    soup = _soup(html)

    assert soup.find("script") is None
    assert soup.find("iframe") is None
    assert soup.find("img") is None
    assert soup.find("b") is None  # 提问里的 <b> 也是转义文本
    assert "&lt;script&gt;" in html
    assert "onerror=alert(1)" in soup.select_one(".qa-q-text").get_text()


async def test_javascript_link_is_neutralized_to_plain_text():
    html = _render(
        [
            _msg("human", "q"),
            _msg("ai", "[点我](javascript:alert(1)) 和 [大写](JAVASCRIPT:alert(2)) 与 [正常](https://example.com)"),
        ]
    )
    soup = _soup(html)

    # markdown-it validateLink 在词法层拒绝 javascript:， unsafe 链接保持原文（转义文本），不产生 <a>
    links = soup.select(".qa-a a")
    assert len(links) == 1
    assert links[0]["href"] == "https://example.com"
    assert links[0]["rel"] == ["noopener", "noreferrer"]
    assert all("javascript" not in a.get("href", "").lower() for a in links)
    answer_text = soup.select_one(".qa-a").get_text()
    assert "点我" in answer_text and "大写" in answer_text


async def test_data_uri_link_and_image_are_neutralized():
    html = _render(
        [
            _msg("human", "q"),
            # data:text/html 被词法层拒绝为字面文本；data:image/* 会成 token，由第二层白名单降级
            _msg(
                "ai",
                "[文件](data:text/html;base64,PHNjcmlwdD4pCjwvc2NyaXB0Pg==)"
                " ![截图](data:image/png;base64,AAAA)"
                " [图链](data:image/png;base64,BBBB)",
            ),
        ]
    )
    soup = _soup(html)

    assert soup.select(".qa-a a") == []
    assert soup.find("img") is None
    # 任何残留标签属性都不允许携带 data: 载荷
    for tag in soup.find_all(True):
        for attr in tag.attrs.values():
            assert not (isinstance(attr, str) and attr.startswith("data:"))
    answer_text = soup.select_one(".qa-a").get_text()
    assert "文件" in answer_text and "截图" in answer_text and "图链" in answer_text


async def test_math_and_code_block_rendering():
    answer = "行内公式 $E=mc^2$ 如下。\n\n$$\\int_0^1 f(x)\\,dx$$\n\n```python\ndef hello():\n    return 'world'\n```\n"
    html = _render([_msg("human", "公式与代码"), _msg("ai", answer)])
    soup = _soup(html)

    inline_math = soup.select_one(".math-inline")
    assert inline_math is not None and "E=mc^2" in inline_math.get_text()
    block_math = soup.select_one(".math-block")
    assert block_math is not None and "\\int_0^1" in block_math.get_text()

    assert soup.select_one(".code-lang").get_text() == "python"
    assert '<span class="k">def</span>' in html  # pygments Keyword 高亮
    assert soup.select_one(".qa-a pre code").get_text().startswith("def hello():")


async def test_markdown_table_renders():
    answer = "| 类型 | 特征 |\n| --- | --- |\n| Ⅰ型 | 细胞化游离核 |\n"
    html = _render([_msg("human", "列表"), _msg("ai", answer)])
    soup = _soup(html)

    table = soup.select_one(".qa-a table")
    assert table is not None
    assert "细胞化游离核" in table.get_text()


async def test_question_meta_lists_attachments_and_image_note():
    question = _msg(
        "human",
        "看下这份材料",
        image_content="base64...",
        extra_metadata={
            "attachments": [
                {"file_name": "胚乳切片.pdf", "file_size": 1048576},
                {"file_name": "数据.xlsx", "file_size": 20480},
            ]
        },
    )
    html = _render([question, _msg("ai", "回答")])
    meta_text = _soup(html).select_one(".qa-meta").get_text()

    assert "胚乳切片.pdf" in meta_text and "1.0 MB" in meta_text
    assert "数据.xlsx" in meta_text and "20.0 KB" in meta_text
    assert "含图片消息" in meta_text


async def test_title_is_escaped_in_document():
    html = _render([_msg("human", "q"), _msg("ai", "a")], title='会话"<b>注入</b>&amp;')
    soup = _soup(html)

    assert soup.find("b") is None
    assert "<b>注入</b>" not in html


async def test_build_filename_sanitizes_illegal_characters():
    moment = datetime(2026, 9, 21, 10, 30, tzinfo=UTC)

    dirty = build_filename('a/b\\c:*?"<>|d\r\n\t', moment)
    for ch in '\\/:*?"<>|\r\n\t':
        assert ch not in dirty
    assert dirty == "语析对话_a b c d_20260921-183000.html"

    assert build_filename(None, moment) == "语析对话_未命名会话_20260921-183000.html"
    assert build_filename("   ", moment).startswith("语析对话_未命名会话_")

    long_title = "长" * 200
    cleaned = build_filename(long_title, moment).removeprefix("语析对话_").removesuffix("_20260921-183000.html")
    assert len(cleaned) == 60


async def test_zero_rounds_still_renders_valid_document():
    html = _render([], title="空会话")
    soup = _soup(html)

    assert soup.select(".qa") == []
    assert soup.select_one(".thread-title").get_text() == "空会话"


# ── 上游保真修复：真实会话中采集的压扁语料（answer 流水线 join 掉了结构换行） ──


async def test_flattened_single_line_table_is_repaired():
    flattened = (
        "**主要发育阶段** | 阶段 | 时间 | 关键特征 | | --- | --- | --- | "
        "| 合胞体期 | 0–3 DAF | 游离核快速分裂 | | 细胞化起始 | 约 3 DAF | 形成细胞壁 | "
        "| 细胞分裂 | 3–10 DAF | 胚乳细胞增殖 |"
    )
    html = _render([_msg("human", "q"), _msg("ai", flattened)])
    soup = _soup(html)

    table = soup.select_one(".qa-a table")
    assert table is not None, "压扁的单行表格应被重排为真正的 GFM 表格"
    assert len(table.select("thead th")) == 4
    assert len(table.select("tbody tr")) == 3
    assert "合胞体期" in table.select("tbody tr")[0].get_text()
    assert "| --- |" not in soup.select_one(".qa-a").get_text()


async def test_flattened_fence_is_repaired():
    flattened = (
        "可借助 Python 简单模拟该曲线： ```python import numpy as np "
        "t = np.linspace(0, 30, 100) W = 25.0 * (1 - np.exp(-0.15 * t)) ```"
    )
    html = _render([_msg("human", "q"), _msg("ai", flattened)])
    soup = _soup(html)

    assert soup.select_one(".code-lang").get_text() == "python"
    code = soup.select_one(".qa-a pre code")
    assert code is not None and "np.linspace" in code.get_text()


async def test_proper_multiline_table_and_fence_are_untouched():
    proper = "| 阶段 | 时间 |\n| --- | --- |\n| 合胞体期 | 0–3 DAF |\n\n```python\nprint('x')\n```\n"
    html = _render([_msg("human", "q"), _msg("ai", proper)])
    soup = _soup(html)

    table = soup.select_one(".qa-a table")
    assert table is not None and len(table.select("tbody tr")) == 1
    assert soup.select_one(".qa-a pre code").get_text() == "print('x')\n"


async def test_prose_with_pipes_is_not_converted_to_table():
    prose = "输入 A | B 与 C | D 的组合进行检索"
    html = _render([_msg("human", "q"), _msg("ai", prose)])
    soup = _soup(html)

    assert soup.select_one(".qa-a table") is None
    assert "A | B" in soup.select_one(".qa-a").get_text()


async def test_evidence_markers_are_styled():
    content = (
        "胚乳灌浆过程中干物质积累近似服从一阶饱和曲线〔证据E3｜正文·第3页｜A co-fractionation mass…〕。\n\n"
        "（注：以下结论未在原文中定位到对应依据，请谨慎采信以下结论、胚乳灌浆曲线）\n"
        "【证据引用】（后端渲染，页码来自证据锚点）\n\n"
        "- E3｜正文·第3页｜A co-fractionation mass…"
    )
    html = _render([_msg("human", "q"), _msg("ai", content)])
    soup = _soup(html)

    chip = soup.select_one(".evidence-chip")
    assert chip is not None
    assert "E3 · 正文·第3页" in chip.get_text()
    assert chip.get("title") == "证据 E3 · A co-fractionation mass…"
    assert "〔证据" not in html  # 原始标记不再以裸文本出现

    assert soup.select_one(".evidence-list-label") is not None
    assert "后端渲染" not in html

    note = soup.select_one(".answer-note")
    assert note is not None and "请谨慎采信" in note.get_text()


async def test_evidence_marker_inside_flattened_table_cell_stays_intact():
    # 真实事故形态：证据标记位于压扁表格的最后一个单元格，其内部的｜不是列分隔符
    flattened = (
        "**主要发育阶段** | 阶段 | 时间 | 关键特征 | | --- | --- | --- | "
        "| 分化与灌浆 | >10 DAF | 外层分化为糊粉层 | 〔证据E3｜正文·第3页｜A co-fractionation mass…〕"
    )
    html = _render([_msg("human", "q"), _msg("ai", flattened)])
    soup = _soup(html)

    rows = soup.select(".qa-a tbody tr")
    assert len(rows) == 1
    chip = soup.select_one(".evidence-chip")
    assert chip is not None, "表格单元格内的证据标记应完整保留并样式化"
    assert "E3 · 正文·第3页" in chip.get_text()
    # 标记未被拆碎到多个单元格
    assert "〔证据E3</td>" not in html
