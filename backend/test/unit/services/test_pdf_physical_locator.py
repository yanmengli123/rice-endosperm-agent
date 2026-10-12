"""Original PDF -> physical projection -> immutable locator, using real SQL."""

from contextlib import asynccontextmanager
from datetime import timedelta
import hashlib
import json

import fitz
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from yuxi.knowledge.pdf_evidence.contracts import ParserArtifact
from yuxi.knowledge.pdf_evidence.pipeline import ScientificPdfPipeline
from yuxi.knowledge.vision.figure_ingestor import prepare_figure_assets
from yuxi.services import pdf_evidence_service as locator
from yuxi.services import scientific_pdf_ingest_service as ingest
from yuxi.storage.postgres import models_knowledge as models


async def exercise_physical_projection(db, monkeypatch, tmp_path, tenant_id, user_id, *, sealed_rebuild=False):
    @asynccontextmanager
    async def context():
        yield db
        await db.flush()

    monkeypatch.setattr(ingest.pg_manager, "get_async_session_context", context)
    source_path = tmp_path / "fixture.pdf"
    with fitz.open() as doc:
        page = doc.new_page(width=400, height=400)
        page.insert_text((40, 60), "Rice starch synthesis increases.")
        page.draw_rect(fitz.Rect(40, 100, 240, 160))
        page.insert_text((50, 130), "Original figure A")
        table_page = doc.new_page(width=400, height=400)
        for y in (50, 80, 110):
            table_page.draw_line((40, y), (240, y))
        for x in (40, 140, 240):
            table_page.draw_line((x, 50), (x, 110))
        for x, y, text in ((45, 68, "Gene"), (145, 68, "Value"), (45, 98, "Wx"), (145, 98, "1.2")):
            table_page.insert_text((x, y), text)
        source_path.write_bytes(doc.tobytes())
    source = source_path.read_bytes()
    sha = hashlib.sha256(source).hexdigest()
    rows = [
        {"type": "text", "text": "Rice starch synthesis increases.", "page_idx": 0, "bbox": [95, 105, 520, 160]},
        {
            "type": "image",
            "image_caption": ["Figure 1 Rice starch."],
            "img_path": "images/fig.png",
            "page_idx": 0,
            "bbox": [100, 250, 600, 400],
        },
        {
            "type": "table",
            "table_caption": ["Table 1 Rice"],
            "table_body": "<table><tr><th>Gene</th><th>Value</th></tr><tr><td>Wx</td><td>1.2</td></tr></table>",
            "page_idx": 1,
            "bbox": [100, 125, 600, 275],
        },
    ]
    artifact = ParserArtifact("mineru_content_list", "content_list.json", json.dumps(rows).encode(), "application/json")
    result = await ScientificPdfPipeline().run(
        source_path,
        {"ocr_engine": "fixture", "grobid_enabled": False, "region_ocr_enabled": False},
        mineru_cache=("Rice starch synthesis increases.\n\n![](images/fig.png)\nFigure 1 Rice starch.", [artifact]),
    )
    kb_id, file_id, rev_id = "kb_pdf_physical_fixture", "file_pdf_physical_fixture", "spr_pdf_physical_fixture"
    kb = models.KnowledgeBase(
        kb_id=kb_id, name="Physical test", kb_type="milvus", tenant_id=tenant_id, created_by=user_id
    )
    file = models.KnowledgeFile(file_id=file_id, kb_id=kb_id, filename="fixture.pdf", file_type="pdf")
    revision = models.KnowledgeParseRevision(
        revision_id=rev_id,
        kb_id=kb_id,
        file_id=file_id,
        tenant_id=tenant_id,
        source_sha256=sha,
        parser_fingerprint=result.parser_fingerprint,
        pipeline_version="scientific_pdf_v4.0",
        status="RUNNING",
        lease_owner="physical-test",
        lease_expires_at=ingest._workflow_now() + timedelta(minutes=5),
        created_by=user_id,
    )
    db.add_all([kb, file, revision])
    await db.flush()
    objects = {}

    class Storage:
        async def aupload_file(self, bucket_name, object_name, data, **kwargs):
            from types import SimpleNamespace

            objects[object_name] = data
            return SimpleNamespace(url=f"minio://{bucket_name}/{object_name}")

        async def adownload_file(self, bucket, key):
            return objects[key]

    storage = Storage()
    from yuxi.knowledge.vision import figure_ingestor

    monkeypatch.setattr(figure_ingestor, "get_minio_client", lambda: storage, raising=False)
    from yuxi.storage.minio import client

    monkeypatch.setattr(client, "get_minio_client", lambda: storage)
    monkeypatch.setattr(ingest, "get_minio_client", lambda: storage)
    monkeypatch.setattr(locator, "get_minio_client", lambda: storage)
    article = result.unified_article.to_dict()
    await prepare_figure_assets(revision, article, result.artifacts, source)
    summary = await ingest._persist_article_records(rev_id, article, worker_id="physical-test", prepared_figures=True)
    assert summary["figure_index"]["anchored_assets"] == summary["figure_index"]["assets"] == 1
    span = (
        await db.execute(
            select(models.EvidenceSpanRecord).where(
                models.EvidenceSpanRecord.parse_revision_id == rev_id,
                models.EvidenceSpanRecord.quote == "Rice starch synthesis increases.",
            )
        )
    ).scalar_one()
    assert span.start_char == 0 and span.end_char == len(span.quote)
    bindings = (
        (
            await db.execute(
                select(models.EvidenceSpanRegionRecord).where(models.EvidenceSpanRegionRecord.span_record_id == span.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(bindings) == 1
    revision.qa_report = result.qa_report
    file.active_parse_revision_id = rev_id
    objects["source.pdf"] = source
    db.add(
        models.KnowledgeParseArtifact(
            artifact_id="spa_pdf_physical_fixture",
            revision_id=rev_id,
            kind="original_pdf",
            object_uri="minio://knowledgebases/source.pdf",
            sha256=sha,
            content_type="application/pdf",
            size_bytes=len(source),
        )
    )
    await db.flush()
    if sealed_rebuild:
        for kind, name, content in (
            ("unified_article", "article.json", json.dumps(article).encode()),
            ("indexed_markdown", "evidence.md", result.annotated_markdown.encode()),
        ):
            objects[name] = content
            db.add(
                models.KnowledgeParseArtifact(
                    artifact_id=f"spa_fixture_{kind}",
                    revision_id=rev_id,
                    kind=kind,
                    object_uri=f"minio://knowledgebases/{name}",
                    sha256=hashlib.sha256(content).hexdigest(),
                    size_bytes=len(content),
                )
            )
        revision.article_uri = "minio://knowledgebases/article.json"
        revision.capabilities = {"state": result.capability}
        await db.flush()
    revision.status = "INDEXED_CONTENT_ONLY"
    revision.sealed_at = ingest._workflow_now()
    await db.flush()
    response = await locator.resolve_pdf_evidence(kb_id=kb_id, file_id=file_id, span_id=span.span_id, source_sha256=sha)
    assert response["locator"]["precision"] == "CHAR"
    assert response["citable"] is True and response["source"]["parse_revision_id"] == rev_id
    assert response["locator"]["fragments"][0]["quads"]
    figure = await locator.resolve_pdf_evidence(kb_id=kb_id, file_id=file_id, figure_label="Figure 1")
    assert figure["source"]["region_id"] and figure["locator"]["precision"] == "REGION"
    downloaded = await locator.download_pdf_original(
        kb_id=kb_id, file_id=file_id, parse_revision_id=rev_id, source_sha256=sha
    )
    assert downloaded["content"] == source and downloaded["etag"] == sha
    table = (
        await db.execute(select(models.PdfTableRecord).where(models.PdfTableRecord.parse_revision_id == rev_id))
    ).scalar_one()
    cell = await locator.resolve_pdf_evidence(
        kb_id=kb_id,
        file_id=file_id,
        table_id=table.table_id,
        row_index=1,
        column_index=1,
    )
    assert cell["locator"]["precision"] == "CELL" and cell["citable"]
    assert cell["semantic_location"]["raw_value"] == "1.2"
    assert cell["semantic_location"]["column_headers"] == ["Value"]
    assert cell["semantic_location"]["row_header"] == "Wx"
    with pytest.raises(ValueError, match="REVISION_MISMATCH"):
        await locator.resolve_pdf_evidence(kb_id=kb_id, file_id=file_id, span_id=span.span_id, source_sha256="0" * 64)
    with pytest.raises(ValueError, match="NOT_FOUND"):
        await locator.resolve_pdf_evidence(kb_id="wrong-kb", file_id=file_id, span_id=span.span_id)
    with pytest.raises(ValueError, match="IMMUTABLE"):
        await ingest._persist_article_records(rev_id, article, prepared_figures=True)
    # Rebuilding an index changes operational status, never the physical facts
    # or the historical citation's availability.
    revision.status = "RUNNING"
    await db.flush()
    historical = await locator.resolve_pdf_evidence(kb_id=kb_id, file_id=file_id, span_id=span.span_id)
    assert historical["locator"] == response["locator"]
    with pytest.raises(ValueError, match="IMMUTABLE"):
        await ingest._persist_article_records(rev_id, article, prepared_figures=True)
    if sealed_rebuild:
        from unittest.mock import AsyncMock

        async def forbidden(*args, **kwargs):
            pytest.fail("sealed index rebuild must not modify original artifacts or physical evidence")

        async def index(kb_id, file_id, **kwargs):
            assert kwargs["params"]["_parse_revision_id"] == rev_id
            during = await locator.resolve_pdf_evidence(kb_id=kb_id, file_id=file_id, span_id=span.span_id)
            assert during["locator"] == response["locator"]
            return {"chunk_count": 1, "token_count": 12}

        seal = revision.sealed_at
        monkeypatch.setattr(ingest, "_claim_revision", AsyncMock(return_value=revision))
        monkeypatch.setattr(ingest, "_renew_revision_lease", AsyncMock())
        monkeypatch.setattr(ingest, "_publish_identity_cache", AsyncMock())
        for name in ("_load_source", "_persist_article_records", "_store_artifacts"):
            monkeypatch.setattr(ingest, name, forbidden)
        monkeypatch.setattr(figure_ingestor, "prepare_figure_assets", forbidden)
        monkeypatch.setattr(ingest.knowledge_base, "index_file", index)
        monkeypatch.setattr(ingest, "inspect_file_index_lineage", AsyncMock(return_value={"problems": []}))
        outcome = await ingest._process_scientific_pdf_ingest({"job_id": "physical-test"}, rev_id)
        assert outcome["activated"] and outcome["status"] == result.capability
        assert revision.sealed_at == seal and file.active_index_revision_id == outcome["index_revision_id"]
        after = await locator.resolve_pdf_evidence(kb_id=kb_id, file_id=file_id, span_id=span.span_id)
        assert after["locator"] == response["locator"]
    objects["source.pdf"] = b"tampered"
    with pytest.raises(ValueError, match="HASH_MISMATCH"):
        await locator.download_pdf_original(kb_id=kb_id, file_id=file_id, parse_revision_id=rev_id)
    return revision, span, table


async def test_pdf_original_projection_and_locator(monkeypatch, tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for model in (
            models.KnowledgeBase,
            models.KnowledgeFile,
            models.KnowledgeParseRevision,
            models.KnowledgeParseArtifact,
            models.EvidenceAnchorRecord,
            models.EvidenceSpanRecord,
            models.EvidenceSpanRegionRecord,
            models.PdfSourceRegionRecord,
            models.PdfTableRecord,
            models.PdfTableCellRecord,
            models.ArticleReference,
            models.CitationMention,
            models.ScientificLexicalIndexRecord,
            models.FigureEntityRecord,
            models.FigureAssetRecord,
        ):
            await conn.run_sync(model.__table__.create)
    async with AsyncSession(engine, expire_on_commit=False) as db:
        await exercise_physical_projection(db, monkeypatch, tmp_path, 1, "u1")
    await engine.dispose()
