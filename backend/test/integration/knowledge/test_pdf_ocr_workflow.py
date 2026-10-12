"""Real ONNX inference plus cached layout through the full physical workflow."""

import json
import os

import pytest

from test.unit.knowledge.pdf_variant_corpus import build_variant
from yuxi.knowledge.pdf_evidence.contracts import ParserArtifact
from yuxi.knowledge.pdf_evidence.pipeline import ScientificPdfPipeline
from yuxi.knowledge.pdf_evidence.region_ocr import ocr_manifest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("PDF_EVIDENCE_RUN_OCR") != "1", reason="explicit real model inference"),
]


@pytest.mark.parametrize("variant", ["scanned", "scanned_chinese", "hybrid"])
async def test_real_ocr_pipeline_produces_verified_word_locations(tmp_path, variant):
    assert ocr_manifest()["ready"], "models must be provisioned before deployment"
    content, markdown, rows = build_variant(variant)
    path = tmp_path / f"{variant}.pdf"
    path.write_bytes(content)
    artifacts = [ParserArtifact("mineru_content_list", "content.json", json.dumps(rows).encode(), "application/json")]
    result = await ScientificPdfPipeline().run(
        path, {"grobid_enabled": False, "region_ocr_enabled": True}, mineru_cache=(markdown, artifacts)
    )
    article = result.unified_article.to_dict()
    report = result.qa_report["quality_vector"]["ocr"]
    assert report["attempted"] > 0 and report["ready"] == report["attempted"]
    assert report["unavailable"] == report["budget_exceeded"] == 0
    sentences = [u for u in article["evidence_units"] if u["evidence_type"] == "sentence"]
    scan_units = (
        sentences if variant != "hybrid" else [u for u in sentences if u["locator"]["fragments"][0]["page_index"] == 1]
    )
    assert scan_units
    precise = [u for u in scan_units if u["locator"]["precision"] == "WORD"]
    assert precise, "OCR text must reach verified sentence geometry, not just an OCR sidecar"
    for unit in precise:
        assert unit["locator"]["status"] == "VERIFIED"
        assert all(f["quads"] for f in unit["locator"]["fragments"])
    if variant == "scanned_chinese":
        assert any("水稻" in u["quote"] for u in precise)
    if variant == "hybrid":
        assert any(u["locator"]["precision"] == "CHAR" for u in sentences)
