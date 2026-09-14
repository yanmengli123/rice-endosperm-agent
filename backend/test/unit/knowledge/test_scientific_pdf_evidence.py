from __future__ import annotations

import json
import zipfile
from pathlib import Path

import fitz
import pytest

from yuxi.knowledge.chunking.ragflow_like import nlp
from yuxi.knowledge.chunking.ragflow_like import dispatcher as chunk_dispatcher
from yuxi.knowledge.chunking.ragflow_like.parsers.academic import chunk_markdown
from yuxi.knowledge.parser.factory import DocumentProcessorFactory
from yuxi.knowledge.parser.mineru_official import MinerUOfficialParser
from yuxi.knowledge.pdf_evidence import aligner as evidence_aligner
from yuxi.knowledge.pdf_evidence.aligner import align_texts_to_anchors
from yuxi.knowledge.pdf_evidence.contracts import ParserArtifact
from yuxi.knowledge.pdf_evidence.grobid import GrobidClient, parse_tei
from yuxi.knowledge.pdf_evidence.mineru_layout import build_mineru_anchors, extract_mineru_blocks
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


def test_golden_pdf_landmark_contract_is_versioned_and_complete():
    fixture_path = Path(__file__).parents[2] / "fixtures" / "pdf_evidence" / "golden_regulatory_factors_starch.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    landmarks = {item["label"]: item["pages"] for item in fixture["landmarks"]}

    assert fixture["fixture_version"] == "pdf_evidence_golden_v1"
    assert fixture["page_basis"] == "one_based_display"
    assert len(fixture["source_sha256"]) == 64
    assert landmarks == {
        "abstract": [1],
        "introduction": [1],
        "table_1": [3, 4],
        "transcription_factor_section": [7],
        "conclusions": [9],
        "references_start": [10],
        "figure_2": [10],
    }


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


def test_figure_ingestor_version_participates_in_parser_fingerprint(monkeypatch):
    from yuxi.knowledge.pdf_evidence import pipeline

    before = pipeline.build_parser_fingerprint("a" * 64, {"ocr_engine": "mineru_official"})
    monkeypatch.setattr(pipeline, "FIGURE_INGESTOR_VERSION", "figure_ingestor_future")
    after = pipeline.build_parser_fingerprint("a" * 64, {"ocr_engine": "mineru_official"})

    assert before != after


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


def test_deterministic_aligner_rejects_tiny_substring_inside_long_paragraph():
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

    assert matches[0] is None


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


def test_grobid_tei_coordinates_are_zero_based_and_keep_multiple_fragments():
    tei = """
    <TEI xmlns="http://www.tei-c.org/ns/1.0"><teiHeader><fileDesc><titleStmt>
    <title>Rice</title></titleStmt></fileDesc></teiHeader><text><body><div>
    <head coords="3,10,20,100,15">Results</head>
    <p coords="3,10,40,200,30;4,10,10,200,20">A paragraph spanning pages.</p>
    </div></body></text></TEI>
    """
    parsed = parse_tei(tei)

    assert parsed["sections"][0]["heading_coordinates"][0]["page_index"] == 2
    paragraph_coords = parsed["sections"][0]["paragraph_coordinates"][0]
    assert [item["page"] for item in paragraph_coords] == [3, 4]
    assert paragraph_coords[0]["bbox"] == [10.0, 40.0, 210.0, 70.0]


def test_mineru_content_list_is_the_primary_semantic_locator():
    artifacts = [
        ParserArtifact(
            kind="mineru_content_list",
            filename="content_list.json",
            content=json.dumps(
                [
                    {"type": "text", "page_idx": 0, "bbox": [10, 20, 300, 80], "text": "Abstract text"},
                    {
                        "type": "table",
                        "page_idx": 2,
                        "bbox": [20, 30, 500, 700],
                        "table_caption": ["Table 1"],
                        "table_body": "<table><tr><td>Wx amylose biosynthesis</td></tr></table>",
                    },
                ]
            ).encode(),
            content_type="application/json",
        )
    ]

    blocks = extract_mineru_blocks(artifacts)
    anchors = build_mineru_anchors("a" * 64, artifacts)

    assert [block["page"] for block in blocks] == [1, 3]
    assert anchors[1].page == 3
    assert anchors[1].fragments[0].page_index == 2
    assert anchors[1].source == "mineru"
    assert anchors[1].locator_quality == "HIGH"


def test_mineru_bbox_is_normalized_to_authoritative_pdf_geometry():
    artifacts = [
        ParserArtifact(
            kind="mineru_content_list",
            filename="content_list.json",
            content=json.dumps(
                [
                    {
                        "type": "text",
                        "page_idx": 0,
                        "bbox": [100, 200, 900, 800],
                        "text": "Rice endosperm evidence",
                    }
                ]
            ).encode(),
            content_type="application/json",
        )
    ]
    geometry = [
        {
            "page_index": 0,
            "width": 500.0,
            "height": 750.0,
            "cropbox": [0.0, 0.0, 500.0, 750.0],
            "mediabox": [0.0, 0.0, 500.0, 750.0],
            "rotation": 0,
        }
    ]

    blocks = extract_mineru_blocks(artifacts, geometry)
    anchors = build_mineru_anchors("a" * 64, artifacts, page_geometry=geometry, blocks=blocks)

    assert blocks[0]["source_bbox"] == (100.0, 200.0, 900.0, 800.0)
    assert blocks[0]["bbox"] == (50.0, 150.0, 450.0, 600.0)
    assert anchors[0].fragments[0].coordinate_space == "pdf_points"
    assert anchors[0].fragments[0].source_block_id == "mineru:0:0"


def test_decorative_image_without_scientific_caption_is_not_an_anchor():
    artifacts = [
        ParserArtifact(
            kind="mineru_content_list",
            filename="content_list.json",
            content=json.dumps(
                [
                    {
                        "type": "image",
                        "page_idx": 0,
                        "bbox": [800, 20, 900, 100],
                        "image_caption": [],
                        "img_path": "images/publisher-logo.png",
                    },
                    {
                        "type": "image",
                        "page_idx": 1,
                        "bbox": [100, 100, 900, 700],
                        "image_caption": ["Fig. 1 Starch synthesis pathway"],
                        "img_path": "images/figure-1.png",
                    },
                ]
            ).encode(),
            content_type="application/json",
        )
    ]

    anchors = build_mineru_anchors("a" * 64, artifacts)

    assert len(anchors) == 1
    assert anchors[0].page == 2
    assert anchors[0].quote == "Fig. 1 Starch synthesis pathway"


def test_mineru_merges_empty_cross_page_table_continuation_into_anchor_fragments():
    artifacts = [
        ParserArtifact(
            kind="mineru_content_list",
            filename="content_list.json",
            content=json.dumps(
                [
                    {
                        "type": "table",
                        "page_idx": 2,
                        "bbox": [20, 30, 500, 700],
                        "table_caption": ["Table 1"],
                        "table_body": "<table><tr><td>Wx amylose biosynthesis</td></tr></table>",
                    },
                    {
                        "type": "text",
                        "page_idx": 2,
                        "bbox": [20, 710, 500, 730],
                        "text": "Publisher footer between table fragments",
                    },
                    {
                        "type": "table",
                        "page_idx": 3,
                        "bbox": [20, 30, 500, 180],
                        "table_caption": [""],
                        "table_body": "",
                    },
                ]
            ).encode(),
            content_type="application/json",
        )
    ]

    anchors = build_mineru_anchors("a" * 64, artifacts)
    matches = align_texts_to_anchors(
        ["Table 1 <table><tr><td>Wx amylose biosynthesis</td></tr></table>"],
        [anchor.to_dict() for anchor in anchors],
    )

    assert len(anchors) == 2
    table_anchor = next(anchor for anchor in anchors if anchor.anchor_type == "table")
    assert [fragment.page_index for fragment in table_anchor.fragments] == [2, 3]
    assert matches[0]["pages"] == [3, 4]
    assert matches[0]["anchor_ids"] == [table_anchor.anchor_id, table_anchor.anchor_id]


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


def test_academic_chunking_splits_large_html_tables_only_between_rows():
    rows = "".join(f"<tr><td>Gene-{index}</td><td>{'function ' * 20}</td></tr>" for index in range(20))
    markdown = (
        "## Results\n\n<!-- yuxi-evidence-anchor:ea_table;page=3 -->\n"
        f"<table><tr><th>Gene</th><th>Function</th></tr>{rows}</table>"
    )

    chunks = chunk_markdown(markdown, {"chunk_token_num": 200, "hard_token_limit": 200})
    table_chunks = [chunk for chunk in chunks if chunk["block_type"] == "table"]

    assert len(table_chunks) > 1
    assert all(chunk["text"].startswith("<table>") and chunk["text"].endswith("</table>") for chunk in table_chunks)
    assert all("<th>Gene</th>" in chunk["text"] for chunk in table_chunks)
    assert all(chunk["anchor_ids"] == ["ea_table"] and chunk["pages"] == [3] for chunk in table_chunks)
    assert sum(chunk["text"].count("Gene-") for chunk in table_chunks) == 20


def test_academic_chunking_keeps_all_cross_page_markers_on_html_table():
    markdown = """
## Results

<!-- yuxi-evidence-anchor:ea_table;page=3 -->
<!-- yuxi-evidence-anchor:ea_table;page=4 -->
Table 1 Main regulators

<!-- yuxi-evidence-anchor:ea_table;page=3 -->
<!-- yuxi-evidence-anchor:ea_table;page=4 -->
<table><tr><th>Gene</th><th>Function</th></tr><tr><td>Wx</td><td>Amylose synthesis</td></tr></table>
    """

    chunks = chunk_markdown(markdown, {"chunk_token_num": 200, "hard_token_limit": 240})
    table_chunk = next(chunk for chunk in chunks if chunk["block_type"] == "table")

    assert table_chunk["anchor_ids"] == ["ea_table"]
    assert table_chunk["pages"] == [3, 4]
    assert table_chunk["text"].startswith("Table 1 Main regulators")
    assert "<table>" in table_chunk["text"]
    assert table_chunk["text"].endswith("</table>")
    assert table_chunk["embedding_text"].startswith("Table: Table 1 Main regulators")
    assert "Columns: Gene | Function" in table_chunk["embedding_text"]
    assert "Row 1: Gene: Wx; Function: Amylose synthesis" in table_chunk["embedding_text"]


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


def test_dispatcher_keeps_scientific_locator_separate_from_graph_extraction():
    records = chunk_dispatcher.chunk_markdown(
        "## Results\n\n<!-- yuxi-evidence-anchor:ea_1;page=3 -->\nWx controls amylose synthesis.",
        "file_1",
        "article.pdf",
        {"chunk_preset_id": "academic", "chunk_parser_config": {"chunk_token_num": 200}},
    )

    assert records[0]["source_provenance"]["evidence_anchor_ids"] == ["ea_1"]
    assert records[0]["source_provenance"]["page_numbers"] == [3]
    assert records[0]["extraction_result"] is None


def test_dispatcher_uses_table_natural_language_for_retrieval_but_keeps_html_for_display():
    records = chunk_dispatcher.chunk_markdown(
        """## Results

<!-- yuxi-evidence-anchor:ea_table;page=3 -->
Table 1 Starch genes
<table><tr><th>Gene</th><th>Function</th></tr><tr><td>Wx</td><td>Amylose synthesis</td></tr></table>
""",
        "file_1",
        "article.pdf",
        {"chunk_preset_id": "academic", "chunk_parser_config": {"chunk_token_num": 200}},
    )

    table_record = next(record for record in records if "<table>" in record["content"])
    assert "<table>" in table_record["content"]
    assert "Columns: Gene | Function" in table_record["retrieval_content"]
    assert "Row 1: Gene: Wx; Function: Amylose synthesis" in table_record["retrieval_content"]
    assert table_record["source_provenance"]["retrieval_representation"] == "table_nl_v1"


@pytest.mark.asyncio
async def test_pipeline_keeps_mineru_canonical_and_grobid_as_annotation(tmp_path: Path, monkeypatch):
    pdf_path = tmp_path / "article.pdf"
    _write_text_pdf(pdf_path)

    class FakeMinerU:
        def process_file_with_artifacts(self, file_path, params):
            del file_path, params
            markdown = (
                "# MinerU title\n\n## Results\n\nWx controls amylose synthesis.\n\n"
                "![](images/figure-1.png)\nFigure 1 Wx expression."
            )
            rows = [
                {"type": "title", "page_idx": 0, "bbox": [10, 10, 200, 30], "text": "MinerU title"},
                {"type": "title", "page_idx": 0, "bbox": [10, 40, 200, 60], "text": "Results"},
                {
                    "type": "text",
                    "page_idx": 0,
                    "bbox": [10, 70, 300, 100],
                    "text": "Wx controls amylose synthesis.",
                },
                {
                    "type": "image",
                    "page_idx": 0,
                    "bbox": [10, 110, 300, 300],
                    "text": "![](images/figure-1.png) Figure 1 Wx expression.",
                },
            ]
            return {
                "markdown": markdown,
                "artifacts": [
                    ParserArtifact(
                        kind="mineru_content_list",
                        filename="content_list.json",
                        content=json.dumps(rows).encode(),
                        content_type="application/json",
                    )
                ],
            }

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
    assert result.qa_report["capabilities"]["pdf_highlight"] is True
    assert result.qa_report["locator_profile"]["primary_source"] == "mineru"
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
    assert {artifact.kind for artifact in artifacts} == {"mineru_markdown", "mineru_content_list"}
