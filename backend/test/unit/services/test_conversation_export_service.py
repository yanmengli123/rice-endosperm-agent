"""会话 HTML 导出渲染层单测：XSS 防线、渲染结构、QA 配对、文件名清洗、标记注册表与图片内嵌。"""

from __future__ import annotations

import base64
import io
from datetime import datetime, UTC

import pytest
from bs4 import BeautifulSoup
from PIL import Image

from yuxi.services.conversation_export_service import (
    _CN_TZ,
    _normalize_image_bytes,
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

    # 链接侧：data: 一律降级为纯文本（含 data:image 形态的链接也不放行）
    assert soup.select(".qa-a a") == []
    # 图片侧：data:image/* 是自包含内嵌的唯一通道，白名单放行（链接形态仍不放行）
    images = soup.select(".qa-a img")
    assert len(images) == 1
    assert images[0]["src"].startswith("data:image/")
    # 除图片外，任何标签属性都不允许携带 data: 载荷
    for tag in soup.find_all(True):
        if tag.name == "img":
            continue
        for attr in tag.attrs.values():
            assert not (isinstance(attr, str) and attr.startswith("data:"))
    answer_text = soup.select_one(".qa-a").get_text()
    assert "文件" in answer_text and "图链" in answer_text


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
    assert "证据E3 · 正文·第3页" in chip.get_text()
    assert chip.get("title") == "A co-fractionation mass…"
    assert "〔证据" not in html  # 原始标记不再以裸文本出现

    assert soup.select_one(".evidence-list-label") is not None
    assert "后端渲染" not in html

    note = soup.select_one(".answer-note")
    assert note is not None and "请谨慎采信" in note.get_text()


async def test_locator_authority_marker_is_chipped():
    # 权威标记第二形态：〔引文定位｜…〕，形态来自 authority_markers 单点定义
    html = _render([_msg("human", "q"), _msg("ai", "见〔引文定位｜QUOTE · 第8页〕。")])
    soup = _soup(html)

    chip = soup.select_one(".evidence-chip")
    assert chip is not None
    assert "引文定位 · QUOTE · 第8页" in chip.get_text()


def test_mcp_f_pattern_matches_canonical():
    # 锁定导出侧 MCP-F 形态与 source_output_guard 的单点定义不漂移
    from yuxi.knowledge.rendering.source_output_guard import _FACT_MARKER as canonical

    from yuxi.services.conversation_export_service import _MCP_FACT_MARKER

    assert _MCP_FACT_MARKER.pattern == canonical.pattern


async def test_mcp_f_markers_collapse_to_footnote_with_appendix():
    content = (
        "准符号为 `WX1`，对应基因名 `GLUTINOUS ENDOSPERM`。[MCP-F:171:f_4e941c3f061314b7] "
        "[MCP-F:171:f_cc2d33ce09bd5f0c] 均指同一条染色体。[MCP-F:167:f_aa0183a733b0fe44]（参见warnings）"
    )
    html = _render(
        [dict(_msg("human", "q"), id=1), dict(_msg("ai", content), id=2)],
        fact_records={
            171: {
                "server_slug": "gene-authority",
                "capability_name": "search_gene",
                "status": "success",
                "facts": [
                    {
                        "id": "f_4e941c3f061314b7",
                        "path": "/results/0/symbol",
                        "numeric_value": None,
                        "value_digest": "sha256:abc123",
                    },
                    {
                        "id": "f_cc2d33ce09bd5f0c",
                        "path": "/results/0/name",
                        "string_value": "GLUTINOUS ENDOSPERM",
                        "value_digest": "sha256:def456",
                    },
                ],
            }
        },
    )
    soup = _soup(html)

    refs = soup.select(".fact-ref")
    assert len(refs) == 2
    assert refs[0].get_text() == "M171"  # 同审计的连续标记收敛为一个上标
    assert refs[1].get_text() == "M167"
    assert "[MCP-F:" not in html  # 原始标记零残留

    appendix_rows = soup.select(".appendix-table tbody tr")
    assert len(appendix_rows) == 2
    first_cells = [td.get_text() for td in appendix_rows[0].select("td")]
    assert "M171" in first_cells[0] and "gene-authority · search_gene" in first_cells[1]
    assert "GLUTINOUS ENDOSPERM" in first_cells[3]
    assert "审计记录不可用" in appendix_rows[1].get_text()  # 167 未回查到 → 显式不可用


async def test_no_appendix_without_mcp_f_markers():
    html = _render([_msg("human", "q"), _msg("ai", "普通回答")])
    assert '<section class="appendix">' not in html  # CSS 类名常驻，只断言结构不出现


async def test_ledger_block_is_stripped():
    content = '答案正文。\n\n<YUXI_MCP_FACT_LEDGER>{"facts": []}</YUXI_MCP_FACT_LEDGER>'
    html = _render([_msg("human", "q"), _msg("ai", content)])
    assert "YUXI_MCP_FACT_LEDGER" not in html
    assert "答案正文" in html


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


# ── 图片内嵌（question_images / kbassets 注入，覆盖嵌入、降级与归一化） ────────


def _png_b64(width: int = 2, height: int = 2, color: tuple = (10, 20, 30)) -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


async def test_question_image_is_embedded():
    data_uri = f"data:image/png;base64,{_png_b64()}"
    html = _render(
        [dict(_msg("human", "看图", image_content=_png_b64()), id=1), dict(_msg("ai", "回答"), id=2)],
        question_images={1: data_uri},
    )
    soup = _soup(html)

    img = soup.select_one(".qa-q img.question-image")
    assert img is not None and img["src"] == data_uri
    assert "图片未内联" not in html
    assert "已内嵌图片 1 张" in html  # 页脚统计


async def test_question_image_placeholder_note_when_not_embedded():
    html = _render(
        [_msg("human", "看图", image_content="AAAA"), _msg("ai", "回答")],
        question_images={1: None},
    )
    assert "含图片消息（图片未内联）" in html


async def test_inline_kbasset_replaced_with_data_uri():
    data_uri = "data:image/png;base64,QUJD"
    content = "如图：![实验图](kbasset://file-1/rev-9/fig_3a.png) 所示。"
    html = _render(
        [dict(_msg("human", "q"), id=1), dict(_msg("ai", content), id=2)],
        kbassets={"kbasset://file-1/rev-9/fig_3a.png": data_uri},
    )
    soup = _soup(html)

    img = soup.select_one(".qa-a img")
    assert img is not None and img["src"] == data_uri
    assert "kbasset://" not in soup.select_one(".qa-a").get_text()


async def test_inline_kbasset_missing_degrades_to_alt_text():
    content = "如图：![实验图](kbasset://file-1/rev-9/fig_3a.png) 所示。"
    html = _render(
        [dict(_msg("human", "q"), id=1), dict(_msg("ai", content), id=2)],
        kbassets={"kbasset://file-1/rev-9/fig_3a.png": None},
    )
    soup = _soup(html)

    assert soup.select_one(".qa-a img") is None
    assert "实验图" in soup.select_one(".qa-a").get_text()  # alt 文本保留


async def test_figure_cards_from_citation_ready():
    figures = {
        "citation_ready": {
            "figures": [
                {
                    "kb_id": "kb-1",
                    "file_id": "file-1",
                    "revision_id": "rev-9",
                    "asset_name": "fig_3a.png",
                    "caption": "Figure 3A 扫描电镜",
                    "page": 8,
                }
            ]
        }
    }
    data_uri = "data:image/png;base64,QUJD"
    html = _render(
        [dict(_msg("human", "q"), id=1), dict(_msg("ai", "见图。", extra_metadata=figures), id=2)],
        kbassets={"kbasset://file-1/rev-9/fig_3a.png": data_uri},
    )
    soup = _soup(html)

    card = soup.select_one("figure.figure-card")
    assert card is not None
    assert card.select_one("img")["src"] == data_uri
    assert "Figure 3A 扫描电镜" in card.select_one("figcaption").get_text()
    assert "第8页" in card.select_one(".figure-page").get_text()


async def test_figure_card_missing_asset_renders_placeholder():
    figures = {
        "citation_ready": {
            "figures": [
                {
                    "kb_id": "kb-1",
                    "file_id": "file-1",
                    "revision_id": "rev-9",
                    "asset_name": "fig_3b.png",
                    "figure_label": "Figure 3B",
                    "page": 9,
                }
            ]
        }
    }
    html = _render(
        [dict(_msg("human", "q"), id=1), dict(_msg("ai", "见图。", extra_metadata=figures), id=2)],
        kbassets={"kbasset://file-1/rev-9/fig_3b.png": None},
    )
    soup = _soup(html)

    card = soup.select_one("figure.figure-card.figure-missing")
    assert card is not None
    assert "Figure 3B" in card.get_text() and "未内联" in card.get_text()


async def test_table_cards_from_citation_ready_are_escaped_and_structured():
    metadata = {
        "citation_ready": {
            "tables": [
                {
                    "table_id": "tbl-1",
                    "label": "Table 1",
                    "caption": "Table 1. <危险题注>",
                    "page": 11,
                    "rows": [
                        [
                            {"text": "Material", "header": True, "rowspan": 1, "colspan": 1},
                            {"text": "Value", "header": True, "rowspan": 1, "colspan": 2},
                        ],
                        [
                            {"text": "<script>alert(1)</script>", "header": False, "rowspan": 1, "colspan": 1},
                            {"text": "75.2", "header": False, "rowspan": 1, "colspan": 1},
                        ],
                    ],
                    "truncated": True,
                    "limited": False,
                }
            ]
        }
    }
    html = _render(
        [dict(_msg("human", "q"), id=1), dict(_msg("ai", "见表。", extra_metadata=metadata), id=2)]
    )
    soup = _soup(html)

    card = soup.select_one("figure.table-card")
    assert card is not None
    assert "Table 1. <危险题注>" in card.select_one("figcaption").get_text()
    assert card.select_one("script") is None
    assert card.select_one("th[colspan='2']").get_text() == "Value"
    assert "第11页" in card.select_one(".table-page").get_text()
    assert "跨页" in card.select_one(".table-note").get_text()


async def test_data_image_uri_allowed_but_data_links_still_neutralized():
    content = "![ok](data:image/png;base64,QUJD) 与 [x](data:text/html;base64,PGI+KSk=)"
    html = _render([_msg("human", "q"), _msg("ai", content)])
    soup = _soup(html)

    img = soup.select_one(".qa-a img")
    assert img is not None and img["src"].startswith("data:image/png")
    assert soup.select_one(".qa-a a") is None  # data: 链接仍降级为纯文本


async def test_normalize_image_bytes_resizes_and_reencodes():
    buffer = io.BytesIO()
    Image.new("RGB", (4000, 3000), (200, 30, 40)).save(buffer, format="PNG")

    normalized = _normalize_image_bytes(buffer.getvalue())
    assert normalized is not None
    mime, payload = normalized
    assert mime == "image/jpeg"
    assert Image.open(io.BytesIO(payload)).size[0] <= 1600


async def test_normalize_image_bytes_keeps_alpha_png():
    buffer = io.BytesIO()
    Image.new("RGBA", (8, 8), (255, 0, 0, 128)).save(buffer, format="PNG")

    normalized = _normalize_image_bytes(buffer.getvalue())
    assert normalized is not None
    mime, payload = normalized
    assert mime == "image/png"
    assert Image.open(io.BytesIO(payload)).mode == "RGBA"


async def test_normalize_image_bytes_rejects_non_image():
    assert _normalize_image_bytes(b"not an image at all") is None


# ── 图组分组渲染（题注每图组一次，子图缩略网格，对齐站内 FigureCardGroup） ────


def _figure(
    asset: str,
    *,
    role: str = "panel",
    panel_label: str = "",
    group_index: int = 0,
    binding_id: str = "b-1",
    caption: str = "Figure 5 籽粒表型分析",
    page: int = 8,
    asset_page: int = 0,
) -> dict:
    return {
        "kb_id": "kb-1",
        "file_id": "file-1",
        "revision_id": "rev-9",
        "asset_name": asset,
        "binding_id": binding_id,
        "role": role,
        "panel_label": panel_label,
        "group_index": group_index,
        "figure_label": "Figure 5",
        "caption": caption,
        "page": page,
        "asset_page": asset_page,
    }


def _render_with_figures(figures: list[dict], kbassets: dict) -> BeautifulSoup:
    payload = {"citation_ready": {"figures": figures}}
    html = _render(
        [dict(_msg("human", "q"), id=1), dict(_msg("ai", "见图。", extra_metadata=payload), id=2)],
        kbassets=kbassets,
    )
    return _soup(html)


async def test_figure_panels_grouped_under_single_caption():
    figures = [
        _figure("panel_b.png", panel_label="B", group_index=2),
        _figure("primary.png", role="primary", group_index=-1),
        _figure("panel_a.png", panel_label="A", group_index=1),
        _figure("panel_c.png", panel_label="C", group_index=3),
    ]
    kbassets = {
        f"kbasset://file-1/rev-9/{name}": f"data:image/png;base64,{name[:4].upper()}"
        for name in ("primary.png", "panel_a.png", "panel_b.png", "panel_c.png")
    }
    soup = _render_with_figures(figures, kbassets)

    # 一个图组一张卡：题注只出一次（figcaption 级），主图为大图
    cards = soup.select("figure.figure-card")
    assert len(cards) == 1
    assert len(cards[0].select("figcaption")) == 1
    assert "Figure 5 籽粒表型分析" in cards[0].select_one("figcaption").get_text()
    assert cards[0].select_one("img")["src"].endswith("PRIM")  # primary data URI

    # 子图按阅读序渲染为缩略网格，只带字母角标、不带题注与页码
    panels = cards[0].select(".figure-panel")
    assert [p.select_one("i").get_text() for p in panels] == ["A", "B", "C"]
    assert all(p.select_one("i") for p in panels)
    assert len(cards[0].select(".figure-page")) == 1


async def test_figure_group_without_primary_falls_back_to_first_member():
    figures = [_figure("p1.png", panel_label="A"), _figure("p2.png", panel_label="B")]
    kbassets = {
        "kbasset://file-1/rev-9/p1.png": "data:image/png;base64,QUE=",
        "kbasset://file-1/rev-9/p2.png": "data:image/png;base64,QkI=",
    }
    soup = _render_with_figures(figures, kbassets)

    card = soup.select_one("figure.figure-card")
    assert card is not None
    assert card.select_one("img")["src"].endswith("QUE=")  # 首条充当主图
    assert len(card.select(".figure-panel")) == 1


async def test_legacy_figures_without_grouping_fields_dedupe_by_caption():
    legacy = [
        {
            "kb_id": "kb-1",
            "file_id": "file-1",
            "revision_id": "rev-9",
            "asset_name": f"p{i}.png",
            "caption": "Figure 5 籽粒表型分析",
            "page": 8,
        }
        for i in (1, 2, 3)
    ]
    kbassets = {f"kbasset://file-1/rev-9/p{i}.png": f"data:image/png;base64,UD{i}=" for i in (1, 2, 3)}
    soup = _render_with_figures(legacy, kbassets)

    # 旧载荷无 binding_id/role：按题注归组，题注仍只出一次
    cards = soup.select("figure.figure-card")
    assert len(cards) == 1
    assert len(cards[0].select("figcaption")) == 1
    assert len(cards[0].select(".figure-panel")) == 2


async def test_cross_page_figure_shows_both_pages():
    figures = [_figure("primary.png", role="primary", page=8, asset_page=9)]
    kbassets = {"kbasset://file-1/rev-9/primary.png": "data:image/png;base64,QUE="}
    soup = _render_with_figures(figures, kbassets)

    assert "题注第8页 · 图第9页" in soup.select_one(".figure-page").get_text()

    same_page = _render_with_figures([_figure("primary.png", role="primary", page=8, asset_page=8)], kbassets)
    assert same_page.select_one(".figure-page").get_text() == "第8页"


async def test_primary_missing_panels_available_degrades_to_panel_grid():
    figures = [
        _figure("primary.png", role="primary"),
        _figure("panel_a.png", panel_label="A", group_index=1),
    ]
    kbassets = {
        "kbasset://file-1/rev-9/primary.png": None,
        "kbasset://file-1/rev-9/panel_a.png": "data:image/png;base64,QUE=",
    }
    soup = _render_with_figures(figures, kbassets)

    card = soup.select_one("figure.figure-card.figure-primary-missing")
    assert card is not None
    assert len(card.select("figcaption")) == 1  # 题注仍只出一次
    assert len(card.select(".figure-panel")) == 1
    assert "figure-missing-note" not in str(card)


async def test_ordered_asset_targets_prioritizes_primary_for_budget():
    from yuxi.services.conversation_export_service import _ordered_asset_targets

    figures = {
        "citation_ready": {
            "figures": [
                _figure("panel_a.png", panel_label="A"),
                _figure("primary.png", role="primary", binding_id="b-2", caption="Figure 6"),
            ]
        }
    }
    history = [
        dict(_msg("human", "q"), id=1),
        dict(_msg("ai", "见图 ![x](kbasset://file-1/rev-9/inline.png)", extra_metadata=figures), id=2),
    ]

    targets = list(_ordered_asset_targets(history))
    # primary 先于 panel，正文内联最后：预算不足时子图先降级
    assert targets[0] == "kbasset://file-1/rev-9/primary.png"
    assert targets[1] == "kbasset://file-1/rev-9/panel_a.png"
    assert targets[2] == "kbasset://file-1/rev-9/inline.png"
