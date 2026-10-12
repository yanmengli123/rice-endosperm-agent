"""任意 PDF 变体语料生成器（矩阵测试专用，离线确定性）。

每个变体产出三元组 ``(pdf_bytes, markdown, content_list_rows)``：

- ``pdf_bytes``：fitz 合成的原始 PDF（原生文本/扫描光栅/旋转/裁剪/加密…）；
- ``markdown`` / ``content_list_rows``：模拟 MinerU 产物缓存——content_list
  的 bbox 按 MinerU 文档口径表达在**渲染页**（cropbox+rotation 后）的
  0..1000 坐标系，由 PyMuPDF 原生 rotation_matrix 独立换算。

生成器不复用被测代码的解析逻辑或坐标变换，
块级 bbox 由 rawdict 行矩形按文本顺序归并得到——这正是"画了什么就声明什么"。
"""

from __future__ import annotations

import io
from typing import Any

import fitz

PAGE_W, PAGE_H = 612.0, 792.0

_LATIN_PARAGRAPH = (
    "OsMYB73 encodes a MYB transcription factor expressed in rice endosperm. "
    "The loss-of-function mutant shows increased grain chalkiness and reduced size."
)
_LATIN_PARAGRAPH_2 = (
    "Starch biosynthesis requires ADP-glucose pyrophosphorylase and granule-bound synthase. "
    "Amylopectin branches are trimmed by debranching enzymes during grain filling."
)
_CJK_PARAGRAPH = "水稻胚乳发育过程中，淀粉与贮藏蛋白的合成受到多重转录因子调控。"
_CJK_PARAGRAPH_2 = "籽粒垩白性状与淀粉颗粒的排列结构密切相关，直接影响稻米的加工品质。"
_REFERENCES = [
    "Ying, J., Shan, J. and Gao, J. (2012) Identification of quantitative trait loci. "
    "J. Integr. Plant Biol. 54, 314-350.",
    "Crofts, N. and Tetlow, I. (2015) Starch biosynthesis in rice endosperm. Plant Sci. 12, 1021-1038.",
]


def _blocks_page0(cjk: bool = False) -> list[dict[str, Any]]:
    para = _CJK_PARAGRAPH if cjk else _LATIN_PARAGRAPH
    para2 = _CJK_PARAGRAPH_2 if cjk else _LATIN_PARAGRAPH_2
    return [
        {"kind": "text", "rect": (72, 72, 540, 100), "text": "OsMYB73 regulates grain size", "size": 14},
        {"kind": "text", "rect": (72, 120, 540, 180), "text": para},
        {"kind": "figure", "rect": (72, 210, 300, 330), "text": None},
        {
            "kind": "text",
            "rect": (72, 340, 540, 368),
            "text": "Figure 1. Expression pattern of OsMYB73 in developing endosperm.",
        },
        {"kind": "text", "rect": (72, 400, 540, 460), "text": para2},
        {"kind": "table", "rect": (72, 490, 540, 580), "text": None},
        {
            "kind": "text",
            "rect": (72, 590, 540, 615),
            "text": "Table 1. Grain weight of OsMYB73 mutants.",
        },
    ]


def _table_rows() -> list[list[str]]:
    return [
        ["Genotype", "Grain weight (mg)"],
        ["ZH11", "24.5"],
        ["cr-myb73", "19.8"],
        ["OE-3", "27.1"],
    ]


def _draw_document(
    *,
    pages: list[list[dict[str, Any]]],
    rotation: int = 0,
    cropbox: tuple[float, float, float, float] | None = None,
    table_texts: dict[int, list[list[str]]] | None = None,
) -> fitz.Document:
    document = fitz.open()
    table_texts = table_texts or {}
    for page_index, blocks in enumerate(pages):
        page = document.new_page(width=PAGE_W, height=PAGE_H)
        for block in blocks:
            rect = fitz.Rect(*block["rect"])
            if block["kind"] == "figure":
                page.draw_rect(rect, color=(0, 0, 0), width=1.2)
                page.draw_line(rect.tl, rect.br, color=(0.6, 0.6, 0.6), width=0.6)
                continue
            if block["kind"] == "table":
                rows = table_texts.get(page_index) or _table_rows()
                row_height = rect.height / len(rows)
                for row_index, row in enumerate(rows):
                    cell_width = rect.width / len(row)
                    for column_index, cell in enumerate(row):
                        cell_rect = fitz.Rect(
                            rect.x0 + column_index * cell_width,
                            rect.y0 + row_index * row_height,
                            rect.x0 + (column_index + 1) * cell_width,
                            rect.y0 + (row_index + 1) * row_height,
                        )
                        page.insert_textbox(
                            cell_rect + (4, 3, -4, -3),
                            cell,
                            fontsize=9,
                            fontname="helv",
                            align=0,
                        )
                for row_index in range(len(rows) + 1):
                    y = rect.y0 + row_index * row_height
                    page.draw_line(fitz.Point(rect.x0, y), fitz.Point(rect.x1, y), color=(0, 0, 0), width=0.8)
                for column_index in range(len(rows[0]) + 1):
                    x = rect.x0 + column_index * rect.width / len(rows[0])
                    page.draw_line((x, rect.y0), (x, rect.y1), color=(0, 0, 0), width=0.8)
                continue
            text = block.get("text")
            if text:
                page.insert_textbox(
                    rect,
                    text,
                    fontsize=block.get("size", 11),
                    fontname=block.get("font", "helv"),
                    align=0,
                )
        if cropbox:
            page.set_cropbox(fitz.Rect(*cropbox))
        if rotation:
            page.set_rotation(rotation)
    return document


def _canonical_block_rects(
    document: fitz.Document, pages: list[list[dict[str, Any]]]
) -> dict[tuple[int, int], fitz.Rect]:
    """块级 canonical bbox：按阅读顺序把 rawdict 行矩形归并到所属逻辑块。

    每个逻辑块的文本是唯一的，行文本按顺序消费块文本；图/表块用绘制矩形。
    """
    mapping: dict[tuple[int, int], fitz.Rect] = {}
    for page_index, blocks in enumerate(pages):
        page = document[page_index]
        lines: list[tuple[str, fitz.Rect]] = []
        for raw in page.get_text("dict", sort=True)["blocks"]:
            if raw.get("type") != 0:
                continue
            for line in raw.get("lines", []):
                text = "".join(span["text"] for span in line.get("spans", []))
                if text.strip():
                    lines.append((text, fitz.Rect(line["bbox"])))
        cursor = 0
        for block_index, block in enumerate(blocks):
            if block["kind"] in {"figure", "table"}:
                mapping[(page_index, block_index)] = fitz.Rect(*block["rect"]) * fitz.Matrix(
                    1, 0, 0, 1, -page.cropbox.x0, -page.cropbox.y0
                )
                continue
            want = str(block.get("text") or "").replace(" ", "")
            union = fitz.Rect()
            consumed = ""
            while cursor < len(lines):
                text, rect = lines[cursor]
                candidate = consumed + text.replace(" ", "")
                if not want.startswith(candidate):
                    break
                consumed = candidate
                union |= rect
                cursor += 1
                if consumed == want:
                    break
            if union.is_valid and not union.is_empty:
                mapping[(page_index, block_index)] = union
            else:  # 匹配失败退化为绘制矩形（保持块可见，由断言暴露问题）
                mapping[(page_index, block_index)] = fitz.Rect(*block["rect"])
    return mapping


def rendered_box(canonical: fitz.Rect, page: fitz.Page) -> list[float]:
    """Independent renderer oracle: unrotated page points → displayed 0..1000."""
    rect = fitz.Rect(canonical) * page.rotation_matrix
    scale_x = 1000.0 / page.rect.width
    scale_y = 1000.0 / page.rect.height
    return [
        round(rect.x0 * scale_x, 2),
        round(rect.y0 * scale_y, 2),
        round(rect.x1 * scale_x, 2),
        round(rect.y1 * scale_y, 2),
    ]


_TABLE_HTML = (
    "<table><tr><td>Genotype</td><td>Grain weight (mg)</td></tr>"
    "<tr><td>ZH11</td><td>24.5</td></tr><tr><td>cr-myb73</td><td>19.8</td></tr>"
    "<tr><td>OE-3</td><td>27.1</td></tr></table>"
)


def _content_list(
    document: fitz.Document,
    pages: list[list[dict[str, Any]]],
    *,
    table_bodies: dict[int, str] | None = None,
    table_captions: dict[int, str] | None = None,
    drop_table_text: set[int] | None = None,
) -> list[dict[str, Any]]:
    rects = _canonical_block_rects(document, pages)
    table_bodies = table_bodies or {}
    table_captions = table_captions or {}
    drop_table_text = drop_table_text or set()
    rows: list[dict[str, Any]] = []
    for page_index, blocks in enumerate(pages):
        page = document[page_index]
        for block_index, block in enumerate(blocks):
            rect = rects[(page_index, block_index)]
            row: dict[str, Any] = {
                "page_idx": page_index,
                "bbox": rendered_box(rect, page),
                "type": "text" if block["kind"] == "text" else block["kind"],
            }
            if block["kind"] == "figure":
                row["type"] = "image"
                row["image_caption"] = None
            if block["kind"] == "table":
                row["type"] = "table"
                if page_index in drop_table_text:
                    # 跨页续表：MinerU 只给几何不给文本，触发锚点 continuation 合并
                    rows.append(row)
                    continue
                row["table_body"] = table_bodies.get(page_index, _TABLE_HTML)
                row["table_caption"] = table_captions.get(page_index, "Table 1. Grain weight of OsMYB73 mutants.")
            else:
                row["text"] = str(block.get("text") or "")
            rows.append(row)
    return rows


def _markdown(pages: list[list[dict[str, Any]]], *, cjk: bool = False, table_pages: set[int] | None = None) -> str:
    table_pages = table_pages if table_pages is not None else {0}
    parts: list[str] = []
    for page_index, blocks in enumerate(pages):
        for block in blocks:
            if block["kind"] == "figure":
                parts.append("![](images/fig1.jpg)")
            elif block["kind"] == "table":
                if page_index in table_pages:
                    parts.append(_TABLE_HTML)
            elif block.get("text"):
                parts.append(str(block["text"]))
    return "\n\n".join(parts)


def _finalize(document: fitz.Document, *, encrypt: bool = False) -> bytes:
    buffer = io.BytesIO()
    if encrypt:
        document.save(buffer, encryption=fitz.PDF_ENCRYPT_AES_128, user_pw="secret", owner_pw="secret")
    else:
        document.save(buffer)
    return buffer.getvalue()


def rasterize(document: fitz.Document, *, page_indexes: set[int], dpi: int = 150) -> fitz.Document:
    """把指定页光栅化为整页图片（制造扫描页：无文本层）。"""
    target = fitz.open()
    for page_index, page in enumerate(document):
        if page_index not in page_indexes:
            target.insert_pdf(document, from_page=page_index, to_page=page_index)
        else:
            rotation = page.rotation
            page.set_rotation(0)
            pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), colorspace=fitz.csRGB, alpha=False)
            page.set_rotation(rotation)
            new = target.new_page(width=page.rect.width, height=page.rect.height)
            new.insert_image(new.rect, pixmap=pix)
            if rotation:
                new.set_rotation(rotation)
    return target


# ---- 变体构造器：返回 (pdf_bytes, markdown, content_list) ----


def build_variant(name: str) -> tuple[bytes, str, list[dict[str, Any]]]:
    pages1 = [
        _blocks_page0(),
        [
            {"kind": "text", "rect": (72, 72, 540, 130), "text": _LATIN_PARAGRAPH_2},
            {
                "kind": "text",
                "rect": (72, 160, 540, 180),
                "text": "References",
            },
            {"kind": "text", "rect": (72, 200, 540, 240), "text": _REFERENCES[0]},
            {"kind": "text", "rect": (72, 260, 540, 300), "text": _REFERENCES[1]},
        ],
    ]
    if name == "electronic":
        document = _draw_document(pages=pages1)
        return _finalize(document), _markdown(pages1), _content_list(document, pages1)
    if name in {"chinese", "scanned_chinese"}:
        pages = [_blocks_page0(cjk=True), pages1[1]]
        for block in pages[0]:
            if any("\u3400" <= c <= "\u9fff" for c in block.get("text") or ""):
                block["font"] = "china-s"
        document = _draw_document(pages=pages)
        if name == "scanned_chinese":
            document = rasterize(document, page_indexes={0, 1})
        return _finalize(document), _markdown(pages), _content_list(document, pages)
    if name == "two_column":
        left = [
            {"kind": "text", "rect": (72, 72, 292, 100), "text": "OsMYB73 regulates grain size", "size": 13},
            {"kind": "text", "rect": (72, 120, 292, 200), "text": _LATIN_PARAGRAPH},
            {"kind": "text", "rect": (72, 220, 292, 300), "text": _LATIN_PARAGRAPH_2},
        ]
        right = [
            {"kind": "text", "rect": (320, 120, 540, 200), "text": _LATIN_PARAGRAPH_2},
            {"kind": "figure", "rect": (320, 220, 540, 340), "text": None},
            {
                "kind": "text",
                "rect": (320, 350, 540, 378),
                "text": "Figure 1. Expression pattern of OsMYB73 in developing endosperm.",
            },
            {"kind": "table", "rect": (320, 400, 540, 490), "text": None},
            {
                "kind": "text",
                "rect": (320, 500, 540, 525),
                "text": "Table 1. Grain weight of OsMYB73 mutants.",
            },
        ]
        pages = [left + right, pages1[1]]
        document = _draw_document(pages=pages)
        return _finalize(document), _markdown(pages), _content_list(document, pages)
    if name == "rotated_90":
        document = _draw_document(pages=pages1, rotation=90)
        return _finalize(document), _markdown(pages1), _content_list(document, pages1)
    if name == "cropped":
        document = _draw_document(pages=pages1, cropbox=(24, 24, 588, 768))
        return _finalize(document), _markdown(pages1), _content_list(document, pages1)
    if name == "scanned":
        base = _draw_document(pages=pages1)
        document = rasterize(base, page_indexes={0, 1})
        return _finalize(document), _markdown(pages1), _content_list(document, pages1)
    if name == "hybrid":
        base = _draw_document(pages=pages1)
        document = rasterize(base, page_indexes={1})
        return _finalize(document), _markdown(pages1), _content_list(document, pages1)
    if name == "cross_page_table":
        table_rows = _table_rows()
        pages = [
            _blocks_page0(),
            [
                {"kind": "table", "rect": (72, 72, 540, 162), "text": None},
                {"kind": "text", "rect": (72, 180, 540, 205), "text": "References"},
                {"kind": "text", "rect": (72, 225, 540, 265), "text": _REFERENCES[0]},
            ],
        ]
        document = _draw_document(pages=pages, table_texts={0: table_rows[:2], 1: table_rows[2:]})
        content = _content_list(document, pages, table_bodies={0: _TABLE_HTML}, drop_table_text={1})
        return _finalize(document), _markdown(pages, table_pages={0}), content
    if name == "encrypted":
        document = _draw_document(pages=[_blocks_page0()])
        return _finalize(document, encrypt=True), "", []
    raise ValueError(f"unknown variant: {name}")


VARIANTS = [
    "electronic",
    "chinese",
    "two_column",
    "rotated_90",
    "cropped",
    "scanned",
    "scanned_chinese",
    "hybrid",
    "cross_page_table",
    "encrypted",
]
