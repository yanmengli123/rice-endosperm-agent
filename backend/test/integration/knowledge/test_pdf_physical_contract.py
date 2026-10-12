"""Opt-in PostgreSQL contract checks, rolled back without changing the corpus."""

import os
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from yuxi.storage.postgres.models_knowledge import (
    KnowledgeBase,
    PdfSourceRegionRecord,
    KnowledgeParseRevision,
    EvidenceSpanRecord,
    EvidenceSpanRegionRecord,
)
from test.unit.services.test_pdf_physical_locator import exercise_physical_projection

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("PDF_EVIDENCE_RUN_INTEGRATION") != "1", reason="explicit live PostgreSQL contract fixture"
    ),
]


async def test_postgres_sealed_index_rebuild_preserves_evidence(monkeypatch, tmp_path):
    engine = create_async_engine(os.environ["POSTGRES_URL"])
    async with engine.connect() as conn:
        transaction = await conn.begin()
        try:
            async with AsyncSession(conn, expire_on_commit=False) as db:
                tenant_id, user_id = (
                    await db.execute(
                        select(KnowledgeBase.tenant_id, KnowledgeBase.created_by)
                        .where(KnowledgeBase.tenant_id.is_not(None), KnowledgeBase.created_by.is_not(None))
                        .limit(1)
                    )
                ).one()
                await db.execute(text("SELECT set_config('yuxi.tenant_id', :tenant, true)"), {"tenant": str(tenant_id)})
                await exercise_physical_projection(db, monkeypatch, tmp_path, tenant_id, user_id, sealed_rebuild=True)
        finally:
            await transaction.rollback()
    await engine.dispose()


async def test_postgres_original_projection_and_sealed_regions(monkeypatch, tmp_path):
    engine = create_async_engine(os.environ["POSTGRES_URL"])
    async with engine.connect() as conn:
        transaction = await conn.begin()
        try:
            async with AsyncSession(conn, expire_on_commit=False) as db:
                tenant_id, user_id = (
                    await db.execute(
                        select(KnowledgeBase.tenant_id, KnowledgeBase.created_by)
                        .where(KnowledgeBase.tenant_id.is_not(None), KnowledgeBase.created_by.is_not(None))
                        .limit(1)
                    )
                ).one()
                await db.execute(text("SELECT set_config('yuxi.tenant_id', :tenant, true)"), {"tenant": str(tenant_id)})
                revision, span, table = await exercise_physical_projection(
                    db, monkeypatch, tmp_path, tenant_id, user_id
                )
                region = (
                    await db.execute(
                        select(PdfSourceRegionRecord)
                        .where(PdfSourceRegionRecord.parse_revision_id == "spr_pdf_physical_fixture")
                        .limit(1)
                    )
                ).scalar_one()
                with pytest.raises(Exception, match="PUBLISHED_PDF_EVIDENCE_IMMUTABLE"):
                    async with db.begin_nested():
                        await db.execute(
                            text("UPDATE pdf_source_regions SET text = 'tampered' WHERE id = :id"), {"id": region.id}
                        )
                for sql, message in (
                    (
                        "UPDATE knowledge_parse_revisions SET sealed_at = NULL WHERE revision_id = :rid",
                        "PDF_SOURCE_IMMUTABLE",
                    ),
                    (
                        "UPDATE knowledge_parse_artifacts SET sha256 = 'tampered' WHERE revision_id = :rid",
                        "PDF_ARTIFACT_IMMUTABLE",
                    ),
                    ("DELETE FROM evidence_spans WHERE parse_revision_id = :rid", "PDF_EVIDENCE_IMMUTABLE"),
                    ("DELETE FROM pdf_table_cells WHERE table_record_id = :tid", "PDF_EVIDENCE_IMMUTABLE"),
                ):
                    with pytest.raises(Exception, match=message):
                        async with db.begin_nested():
                            await db.execute(text(sql), {"rid": revision.revision_id, "tid": table.id})
                other = KnowledgeParseRevision(
                    revision_id="spr_pdf_other_fixture",
                    kb_id=revision.kb_id,
                    file_id=revision.file_id,
                    tenant_id=tenant_id,
                    source_sha256="f" * 64,
                    parser_fingerprint="e" * 64,
                    pipeline_version="test",
                    status="RUNNING",
                    created_by=user_id,
                )
                db.add(other)
                await db.flush()
                other_span = EvidenceSpanRecord(
                    span_id="es_pdf_other_fixture",
                    parse_revision_id=other.revision_id,
                    tenant_id=tenant_id,
                    kb_id=revision.kb_id,
                    file_id=revision.file_id,
                    evidence_type="sentence",
                    quote="Other text",
                    quote_hash="e" * 64,
                    evidence_id="ev_pdf_other_fixture",
                )
                db.add(other_span)
                await db.flush()
                with pytest.raises(Exception, match="PDF_SPAN_REGION_REVISION_MISMATCH"):
                    async with db.begin_nested():
                        db.add(
                            EvidenceSpanRegionRecord(
                                span_record_id=other_span.id, region_record_id=region.id, selector={}
                            )
                        )
                        await db.flush()
                # A temporary non-owner role proves RLS rather than merely
                # querying as the application database's bypassing owner.
                role = "pdf_contract_" + uuid.uuid4().hex
                await db.execute(text(f"CREATE ROLE {role} NOLOGIN NOBYPASSRLS"))
                await db.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
                await db.execute(
                    text(
                        "GRANT SELECT ON pdf_source_regions, pdf_tables, pdf_table_cells, evidence_span_regions "
                        f"TO {role}"
                    )
                )
                await db.execute(text(f"SET LOCAL ROLE {role}"))
                for table_name in ("pdf_source_regions", "pdf_tables", "pdf_table_cells", "evidence_span_regions"):
                    assert (await db.execute(text(f"SELECT count(*) FROM {table_name}"))).scalar_one() > 0
                await db.execute(text("SELECT set_config('yuxi.tenant_id', '99999999', true)"))
                for table_name in ("pdf_source_regions", "pdf_tables", "pdf_table_cells", "evidence_span_regions"):
                    assert (await db.execute(text(f"SELECT count(*) FROM {table_name}"))).scalar_one() == 0
                await db.execute(text("RESET ROLE"))
        finally:
            await transaction.rollback()
    await engine.dispose()


async def test_actual_region_ocr_preserves_word_boxes():
    import fitz
    from yuxi.knowledge.pdf_evidence.region_ocr import recognize_region
    from yuxi.knowledge.pdf_evidence.text_geometry import locate_text

    with fitz.open() as doc:
        page = doc.new_page(width=400, height=250)
        page.insert_text((80, 100), "A Rice starch", fontsize=25)
        region = [60, 60, 350, 130]
        result = recognize_region(page, region)
        assert result["status"] == "READY"
        assert all(region[0] <= w["bbox"][0] < w["bbox"][2] <= region[2] for w in result["words"])
        located = locate_text("Rice starch", {"characters": result["words"]}, region)
        assert located["status"] == "VERIFIED" and located["precision"] == "WORD"
        assert all(v != "UNAVAILABLE" for v in result["model_manifest"]["weights"].values())
