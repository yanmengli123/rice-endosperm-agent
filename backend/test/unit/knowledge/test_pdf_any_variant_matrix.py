"""「适应任何 PDF」诚实降级契约矩阵（离线、确定性、无外部服务）。

对合成变体（电子/中文/双栏/旋转/裁剪/扫描/混合/跨页表/加密/超预算）重放
MinerU 产物缓存跑全流水线，断言的不是"每个 PDF 都拿到同等精度"，而是：

1. 工作流对每个变体都有明确落点：完成并给出能力/质量向量，或 fail-closed；
2. 精度声明永远诚实：有原生 glyph 才可能 CHAR/WORD，扫描页必须 REGION；
3. 几何永远有效：无效块计数为 0，块接受率 1.0；
4. 跨页表合并为多 fragment；OCR 预算超限显式降级而非硬失败。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pdf_variant_corpus import VARIANTS, build_variant

from yuxi.knowledge.pdf_evidence.contracts import ParserArtifact
from yuxi.knowledge.pdf_evidence.pipeline import ScientificPdfPipeline
from yuxi.knowledge.pdf_evidence.native import PdfInputError, inspect_native_pdf
from yuxi.knowledge.pdf_evidence.region_ocr import recognize_region

_PRECISE = {"CHAR", "WORD"}


async def _run(tmp_path: Path, variant: str, **params: Any) -> Any:
    pdf_bytes, markdown, content_list = build_variant(variant)
    path = tmp_path / f"{variant}.pdf"
    path.write_bytes(pdf_bytes)
    artifacts = [
        ParserArtifact("mineru_markdown", f"{variant}.md", markdown.encode("utf-8"), "text/markdown"),
        ParserArtifact(
            "mineru_content_list",
            f"{variant}.json",
            json.dumps(content_list, ensure_ascii=False).encode("utf-8"),
            "application/json",
        ),
    ]
    return await ScientificPdfPipeline().run(
        path,
        {"grobid_enabled": False, "region_ocr_enabled": False, **params},
        mineru_cache=(markdown, artifacts),
    )


def _units(article: dict, evidence_type: str = "sentence") -> list[dict]:
    return [unit for unit in article.get("evidence_units", []) if unit.get("evidence_type") == evidence_type]


def _precise(unit: dict) -> bool:
    return unit.get("locator", {}).get("precision") in _PRECISE


def _contract_common(result: Any) -> dict:
    vector = result.qa_report["quality_vector"]
    assert result.unified_article is not None
    assert vector["source_binding_coverage"] == 1.0
    assert vector["raw_block_acceptance"] == 1.0
    assert result.qa_report["locator_profile"]["invalid_geometry_count"] == 0
    return vector


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", [name for name in VARIANTS if name != "encrypted"])
async def test_any_pdf_variant_completes_with_honest_contract(tmp_path, variant):
    result = await _run(tmp_path, variant)
    article = result.unified_article.to_dict()
    vector = _contract_common(result)
    sentences = _units(article)
    assert sentences, f"{variant}: 未产出句子证据单元"
    anchors = article["anchors"]
    assert anchors, f"{variant}: 未产出锚点"

    if variant == "electronic":
        # 原生 glyph 齐备 → 正文句必须拿到字符/词级精确定位
        assert sum(_precise(unit) for unit in sentences) >= len(sentences) * 0.95, (
            f"electronic: 精确定位覆盖不足 {_precise_stat(sentences)}"
        )
        assert vector["text_precise_locator_coverage"] >= 0.95
        # 题注单元：container_label 识别 + 定位
        captions = _units(article, "caption")
        assert any(unit.get("container_label", "").startswith("Figure 1") for unit in captions)
        # 图片区域存在且带页码
        image_regions = [
            region for region in article.get("regions", []) if region["kind"] in {"image", "figure", "chart"}
        ]
        assert image_regions and all(region["page_index"] == 0 for region in image_regions)
        # 表格单元格：原值 + 定位
        tables = article.get("tables", [])
        assert tables, "electronic: 未产出结构化表格"
        cells = [cell for table in tables for cell in table.get("cells", [])]
        values = {cell.get("raw_value") for cell in cells}
        assert {"24.5", "19.8", "27.1"} <= values
        assert vector["table_cell_locator_coverage"] is not None and vector["table_cell_locator_coverage"] >= 0.95

    elif variant == "chinese":
        chinese_units = [unit for unit in sentences if "水稻" in unit.get("quote", "")]
        assert chinese_units, "chinese: 中文句未进入证据单元"
        assert sum(_precise(unit) for unit in chinese_units) >= len(chinese_units) * 0.95

    elif variant == "two_column":
        left = next(unit for unit in sentences if "OsMYB73 encodes" in unit.get("quote", ""))
        right = next(unit for unit in sentences if "Starch biosynthesis requires" in unit.get("quote", ""))
        assert _precise(left) and _precise(right)
        left_box = left["locator"]["fragments"][0]["bbox"]
        assert left_box[2] <= 320, "two_column: 左栏句子几何越栏"

    elif variant == "rotated_90":
        # 旋转页：MinerU 渲染空间 → canonical 换算正确才能精确定位
        precise = sum(_precise(unit) for unit in sentences)
        assert precise >= len(sentences) * 0.9, f"rotated_90: 旋转页精确定位不足 {precise}/{len(sentences)}"
        for unit in sentences:
            for fragment in unit["locator"].get("fragments", []):
                assert 0 <= fragment["page_index"] <= 1

    elif variant == "cropped":
        precise = sum(_precise(unit) for unit in sentences)
        assert precise >= len(sentences) * 0.9, f"cropped: 裁剪页精确定位不足 {precise}/{len(sentences)}"

    elif variant in {"scanned", "scanned_chinese"}:
        # 无原生 glyph：全部诚实降级 REGION，绝不伪造精确定位
        assert all(unit.get("locator", {}).get("precision") == "REGION" for unit in sentences)
        assert result.qa_report["grobid"]["attempted"] is False
        assert result.capability != "INDEXED_FULL"

    elif variant == "hybrid":
        page0 = [unit for unit in sentences if unit["locator"]["fragments"][0]["page_index"] == 0]
        page1 = [unit for unit in sentences if unit["locator"]["fragments"][0]["page_index"] == 1]
        assert page0 and page1
        assert all(_precise(unit) for unit in page0), "hybrid: 原生页应精确定位"
        assert all(unit["locator"]["precision"] == "REGION" for unit in page1), "hybrid: 扫描页必须降级 REGION"

    elif variant == "cross_page_table":
        table_anchors = [anchor for anchor in anchors if anchor.get("anchor_type") == "table"]
        assert table_anchors, "cross_page_table: 未产出表格锚点"
        pages = {fragment["page_index"] for anchor in table_anchors for fragment in anchor.get("fragments", [])}
        assert pages == {0, 1}, f"cross_page_table: 跨页 fragment 未合并 {pages}"
        tables = article.get("tables", [])
        assert tables and tables[0].get("cells")
        table = tables[0]
        assert len(table["region_ids"]) == 2
        assert table["metadata"]["continuation_status"] == "VERIFIED_ROWS_AND_COLUMNS"
        assert len(table["cells"]) == 8
        for value in ("19.8", "27.1"):
            cell = next(c for c in table["cells"] if c["raw_value"] == value)
            assert cell["metadata"]["table_region_id"] == table["region_ids"][1]
            assert cell["bbox"] and cell["metadata"]["locator_status"] == "VERIFIED"
            assert cell["metadata"]["geometry_precision"] == "CELL"


def _precise_stat(sentences: list[dict]) -> str:
    precise = sum(_precise(unit) for unit in sentences)
    return f"{precise}/{len(sentences)}"


@pytest.mark.asyncio
async def test_encrypted_pdf_fails_closed(tmp_path):
    """加密 PDF：NATIVE 阶段 fail-closed（抛错），不产出任何假证据。"""
    with pytest.raises(PdfInputError, match="PDF_PASSWORD_REQUIRED"):
        await _run(tmp_path, "encrypted")


@pytest.mark.parametrize("content", [b"", b"%PDF-not-a-document"])
def test_corrupt_pdf_has_permanent_failure_code(tmp_path, content):
    path = tmp_path / "corrupt.pdf"
    path.write_bytes(content)
    with pytest.raises(PdfInputError, match="PDF_INVALID_DOCUMENT"):
        inspect_native_pdf(path)


@pytest.mark.parametrize(
    "env,value,code",
    [
        ("PDF_EVIDENCE_MAX_BYTES", "16", "PDF_INPUT_BYTE_BUDGET_EXCEEDED"),
        ("PDF_EVIDENCE_MAX_PAGES", "1", "PDF_INPUT_PAGE_BUDGET_EXCEEDED"),
    ],
)
def test_native_input_budget_is_enforced_before_extraction(tmp_path, monkeypatch, env, value, code):
    monkeypatch.setenv(env, value)
    path = tmp_path / "budget.pdf"
    path.write_bytes(build_variant("electronic")[0])
    with pytest.raises(PdfInputError, match=code):
        inspect_native_pdf(path)


def test_ocr_adaptive_dpi_and_padding_share_pixel_budget(tmp_path):
    import fitz

    with fitz.open(stream=build_variant("electronic")[0], filetype="pdf") as document:
        report = recognize_region(document[0], (70, 70, 80, 80), dpi=72, max_pixels=1000)
    assert report["status"] == "BUDGET_EXCEEDED"


def test_ocr_budget_exceeded_degrades_explicitly(tmp_path):
    """超像素预算的区域 OCR：显式 BUDGET_EXCEEDED，不硬失败不静默。"""
    import fitz

    from pdf_variant_corpus import PAGE_H, PAGE_W, _draw_document, _blocks_page0

    document = _draw_document(pages=[_blocks_page0()])
    path = tmp_path / "budget.pdf"
    document.save(str(path))
    with fitz.open(path) as opened:
        page = opened[0]
        report = recognize_region(
            page,
            (0, 0, PAGE_W, PAGE_H),
            dpi=600,  # 612*792*(600/72)^2 ≈ 25.5M px > 20M 预算
        )
    assert report["status"] == "BUDGET_EXCEEDED"
    assert report["words"] == []
