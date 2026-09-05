from __future__ import annotations

import json
import zipfile
from pathlib import Path

import fitz
import pytest

from yuxi.knowledge.chunking.ragflow_like import nlp
from yuxi.knowledge.chunking.ragflow_like.parsers.academic import chunk_markdown
from yuxi.knowledge.parser.factory import DocumentProcessorFactory
from yuxi.knowledge.parser.mineru_official import MinerUOfficialParser
from yuxi.knowledge.pdf_evidence import aligner as evidence_aligner
from yuxi.knowledge.pdf_evidence.aligner import align_texts_to_anchors
from yuxi.knowledge.pdf_evidence.grobid import GrobidClient, parse_tei
from yuxi.knowledge.pdf_evidence.native import inspect_native_pdf
from yuxi.knowledge.pdf_evidence.pipeline import ScientificPdfPipeline, build_parser_fingerprint
from yuxi.services.scientific_pdf_ingest_service import (
    _chunker_fingerprint,
    _ingest_job_id,
    _pdf_ingest_concurrency_limit,
    _persistent_processing_params,
    _scientific_chunking_contract,
    _workflow_now,
)


def _write_text_pdf(path: Path) -> None:
    document = fitz.open()
    page = document.new_page()
    page.insert_textbox(
        fitz.Rect(72, 72, 520, 700),
        (
            "Rice endosperm development and starch biosynthesis. "
            "Wx encodes granule-bound starch synthase I in developing endosperm. "
        )
        * 5,
    )
    document.save(path)
    document.close()


def test_native_pdf_anchors_are_stable_and_page_aware(tmp_path: Path):
    pdf_path = tmp_path / "article.pdf"
    _write_text_pdf(pdf_path)

    first = inspect_native_pdf(pdf_path)
    second = inspect_native_pdf(pdf_path)

    assert first.quality["native_text_usable"] is True
    assert first.page_count == 1
    assert first.anchors
    assert [anchor.anchor_id for anchor in first.anchors] == [anchor.anchor_id for anchor in second.anchors]
    assert first.anchors[0].page == 1
    assert len(first.anchors[0].bbox) == 4


def test_parser_fingerprint_is_deterministic_and_config_sensitive():
    first = build_parser_fingerprint("a" * 64, {"ocr_engine": "mineru_official"})
    second = build_parser_fingerprint("a" * 64, {"ocr_engine": "mineru_official"})
    changed = build_parser_fingerprint("a" * 64, {"ocr_engine": "mineru_ocr"})

    assert first == second
    assert first != changed


def test_retry_job_identity_changes_only_after_attempt_changes():
    assert _ingest_job_id("spr_1", 0) == _ingest_job_id("spr_1", 0)
    assert _ingest_job_id("spr_1", 0) != _ingest_job_id("spr_1", 1)


def test_pdf_ingest_concurrency_limit_is_bounded(monkeypatch):
    monkeypatch.setenv("SCIENTIFIC_PDF_MAX_CONCURRENCY", "99")
    assert _pdf_ingest_concurrency_limit() == 8
    monkeypatch.setenv("SCIENTIFIC_PDF_MAX_CONCURRENCY", "invalid")
    assert _pdf_ingest_concurrency_limit() == 2


def test_scientific_pdf_workflow_clock_is_aware_utc():
    now = _workflow_now()

    assert now.tzinfo is not None
    assert now.utcoffset().total_seconds() == 0


def test_chunker_version_participates_in_index_revision_identity():
    contract = _scientific_chunking_contract()
    changed = {**contract, "chunker_version": "future-version"}

    assert _chunker_fingerprint("spr_1", contract) != _chunker_fingerprint("spr_1", changed)


def test_runtime_asset_callback_never_enters_persisted_processing_params():
    persisted = _persistent_processing_params(
        {
            "ocr_engine": "mineru_official",
            "image_prefix": "tenants/1/documents/hash/mineru/revision/images",
            "asset_uri_builder": lambda object_name: f"kbasset://{object_name}",
        },
        _scientific_chunking_contract(),
    )

    assert persisted["ocr_engine"] == "mineru_official"
    assert persisted["pdf_evidence_pipeline"] is True
    assert "image_prefix" not in persisted
    assert "asset_uri_builder" not in persisted
    json.dumps(persisted)


def test_deterministic_aligner_is_monotonic_and_leaves_low_confidence_unmatched():
    anchors = [
        {"anchor_id": "a1", "page": 1, "quote": "Wx controls amylose synthesis in rice endosperm."},
        {"anchor_id": "a2", "page": 2, "quote": "OsbZIP58 regulates starch biosynthesis genes."},
    ]
    matches = align_texts_to_anchors(
        [
            "Wx controls amylose synthesis in rice endosperm",
            "unrelated hallucinated sentence without a source",
            "OsbZIP58 regulates starch biosynthesis genes",
        ],
        anchors,
    )
    assert matches[0]["anchor_id"] == "a1"
    assert matches[1] is None
    assert matches[2]["anchor_id"] == "a2"


def test_deterministic_aligner_matches_long_merged_paragraph_to_native_block():
    anchors = [
        {
            "anchor_id": "a1",
            "page": 7,
            "quote": "GIF1 is required for carbon parti- tioning during early grain filling.",
        }
    ]
    merged_paragraph = (
        "Rice grain size is controlled by several regulators. "
        "GIF1 is required for carbon partitioning during early grain filling and affects yield. "
        "Additional experiments confirmed the phenotype in developing endosperm."
    )

    matches = align_texts_to_anchors([merged_paragraph], anchors)

    assert matches[0] is not None
    assert matches[0]["anchor_id"] == "a1"
    assert matches[0]["page"] == 7


def test_deterministic_aligner_uses_bounded_candidate_set(monkeypatch):
    anchors = [
        {
            "anchor_id": f"a{index}",
            "page": index // 20 + 1,
            "quote": f"Unique marker {index:04d} regulates rice endosperm development pathway {index:04d}.",
        }
        for index in range(400)
    ]
    texts = [anchors[index]["quote"] for index in range(0, 400, 4)]
    score_calls = 0
    original_score = evidence_aligner._score

    def counted_score(source: str, candidate: str):
        nonlocal score_calls
        score_calls += 1
        return original_score(source, candidate)

    monkeypatch.setattr(evidence_aligner, "_score", counted_score)
    matches = align_texts_to_anchors(texts, anchors)

    assert all(match is not None for match in matches)
    assert [match["anchor_id"] for match in matches if match] == [
        anchors[index]["anchor_id"] for index in range(0, 400, 4)
    ]
    assert score_calls <= len(texts) * (
        evidence_aligner.NEARBY_CANDIDATE_LIMIT + evidence_aligner.FUZZY_CANDIDATE_LIMIT
    )


def test_grobid_tei_normalization_preserves_references_and_mentions():
    tei = """
    <TEI xmlns="http://www.tei-c.org/ns/1.0">
      <teiHeader>
        <fileDesc>
          <titleStmt><title>Rice Wx</title></titleStmt>
          <sourceDesc><biblStruct><analytic>
            <author><persName><forename>Li</forename><surname>Ming</surname></persName>
            <email>liming@example.org</email></author>
          </analytic></biblStruct></sourceDesc>
        </fileDesc>
        <profileDesc><abstract><p>Abstract text.</p></abstract></profileDesc>
      </teiHeader>
      <text><body><div><head>Results</head><p>Wx controls amylose
      <ref type="bibr" target="#b0">[1]</ref>.</p></div></body>
      <back><listBibl><biblStruct xml:id="b0"><analytic><title level="a">A Wx study</title></analytic>
      <idno type="DOI">10.1000/wx.1</idno></biblStruct></listBibl></back></text>
    </TEI>
    """
    parsed = parse_tei(tei)

    assert parsed["metadata"]["title"] == "Rice Wx"
    assert parsed["metadata"]["authors"] == ["Li Ming"]
    assert parsed["sections"][0]["heading"] == "Results"
    assert parsed["references"][0]["doi"] == "10.1000/wx.1"
    assert parsed["citation_mentions"][0]["reference_id"] == "b0"


def test_academic_chunking_does_not_cross_sections_or_index_references():
    markdown = """
# Article

## Abstract

<!-- yuxi-evidence-anchor:ea_abstract;page=1 -->
Wx is important for amylose synthesis.

## Results

<!-- yuxi-evidence-anchor:ea_results;page=3 -->
Loss of Wx reduced amylose content in the tested material.

## References

Reference that must not enter ordinary retrieval.
    """
    chunks = chunk_markdown(markdown, {"chunk_token_num": 200, "hard_token_limit": 240})

    assert chunks
    assert all("Reference that must not" not in chunk["text"] for chunk in chunks)
    assert any(chunk["anchor_ids"] == ["ea_results"] and chunk["pages"] == [3] for chunk in chunks)
    assert all(nlp.count_tokens(chunk["text"]) <= 240 for chunk in chunks)
    assert all(len(chunk["section_path"]) <= 2 for chunk in chunks)


def test_academic_chunking_excludes_untitled_reference_tail_after_acknowledgements():
    markdown = """
## Acknowledgements

We thank the rice research community for helpful discussion.

1. Alpha A, Beta B: A rice endosperm study. Plant Journal 2021, 1:1-9.

2. Gamma C, Delta D: A starch biosynthesis study. Plant Physiology 2022, 2:10-20.
    """

    chunks = chunk_markdown(markdown, {"chunk_token_num": 200, "hard_token_limit": 240})

    combined = "\n".join(chunk["text"] for chunk in chunks)
    assert "We thank the rice research community" in combined
    assert "Alpha A" not in combined
    assert "Gamma C" not in combined


def test_academic_chunking_preserves_html_tables_as_table_blocks():
    markdown = """
## Results

Table 1
<table><tr><td>Gene</td><td>Function</td></tr><tr><td>Wx</td><td>Amylose synthesis</td></tr></table>
    """

    chunks = chunk_markdown(markdown, {"chunk_token_num": 200, "hard_token_limit": 240})

    assert any(chunk["block_type"] == "table" and "<table>" in chunk["text"] for chunk in chunks)


def test_academic_chunking_preserves_figure_asset_and_caption_together():
    markdown = """
## Results

![](images/figure-1.png)
Figure 1 OsMYB73 expression during rice endosperm development.
    """

    chunks = chunk_markdown(markdown, {"chunk_token_num": 200, "hard_token_limit": 240})

    figure_chunks = [chunk for chunk in chunks if chunk["block_type"] == "figure"]
    assert len(figure_chunks) == 1
    assert "figure-1.png" in figure_chunks[0]["text"]
    assert "OsMYB73 expression" in figure_chunks[0]["text"]


@pytest.mark.asyncio
async def test_pipeline_keeps_mineru_canonical_and_grobid_as_annotation(tmp_path: Path, monkeypatch):
    pdf_path = tmp_path / "article.pdf"
    _write_text_pdf(pdf_path)

    class FakeMinerU:
        def process_file(self, file_path, params):
            del file_path, params
            return (
                "# MinerU title\n\n## Results\n\nWx controls amylose synthesis.\n\n"
                "![](images/figure-1.png)\nFigure 1 Wx expression."
            )

    async def fake_grobid(self, file_path):
        del self, file_path
        return "<TEI/>", {
            "metadata": {"title": "GROBID title", "authors": ["Author"]},
            "references": [{"reference_id": "b0", "title": "Reference", "doi": "10.1000/ref"}],
            "citation_mentions": [],
        }

    monkeypatch.setattr(DocumentProcessorFactory, "get_processor", classmethod(lambda cls, engine: FakeMinerU()))
    monkeypatch.setattr(GrobidClient, "process", fake_grobid)

    stages: list[tuple[str, str]] = []

    async def record_stage(stage, status, detail):
        del detail
        stages.append((stage, status))

    result = await ScientificPdfPipeline().run(
        pdf_path,
        {"ocr_engine": "mineru_official"},
        stage_callback=record_stage,
    )

    assert result.capability == "INDEXED_FULL"
    assert result.unified_article is not None
    assert result.unified_article.title == "GROBID title"
    assert result.unified_article.markdown.startswith("# MinerU title")
    assert result.unified_article.parser_provenance["canonical_content_source"] == "mineru"
    assert result.unified_article.references[0]["doi"] == "10.1000/ref"
    assert result.qa_report["capabilities"]["fulltext_search"] is True
    assert result.qa_report["capabilities"]["figure_retrieval"] is True
    assert ("NATIVE", "SUCCEEDED") in stages
    assert ("QUALITY", "SUCCEEDED") in stages


@pytest.mark.asyncio
async def test_pipeline_degrades_to_content_only_when_grobid_is_unavailable(tmp_path: Path, monkeypatch):
    pdf_path = tmp_path / "article.pdf"
    _write_text_pdf(pdf_path)

    class FakeMinerU:
        def process_file(self, file_path, params):
            del file_path, params
            return "# Rice article\n\n## Results\n\nWx controls amylose synthesis in endosperm."

    async def unavailable_grobid(self, file_path):
        del self, file_path
        raise ConnectionError("simulated GROBID outage")

    monkeypatch.setattr(DocumentProcessorFactory, "get_processor", classmethod(lambda cls, engine: FakeMinerU()))
    monkeypatch.setattr(GrobidClient, "process", unavailable_grobid)

    stages: list[tuple[str, str, dict | None]] = []

    async def record_stage(stage, status, detail):
        stages.append((stage, status, detail))

    result = await ScientificPdfPipeline().run(
        pdf_path,
        {"ocr_engine": "mineru_official"},
        stage_callback=record_stage,
    )

    grobid_stage = next(item for item in stages if item[:2] == ("GROBID", "DEGRADED"))
    assert result.capability == "INDEXED_CONTENT_ONLY"
    assert result.qa_report["accepted"] is True
    assert result.qa_report["capabilities"]["fulltext_search"] is True
    assert result.qa_report["capabilities"]["citation_navigation"] is False
    assert result.unified_article is not None
    assert result.unified_article.markdown.startswith("# Rice article")
    assert grobid_stage[2]["error_code"] == "GROBID_UNAVAILABLE"


def test_mineru_raw_artifacts_exclude_images_and_source_pdf(tmp_path: Path):
    zip_path = tmp_path / "result.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("full.md", "# Article")
        archive.writestr("content_list.json", "[]")
        archive.writestr("images/figure.png", b"png")
        archive.writestr("source.pdf", b"pdf")

    artifacts = MinerUOfficialParser._collect_raw_artifacts(str(zip_path))

    assert {artifact.filename for artifact in artifacts} == {"full.md", "content_list.json"}
