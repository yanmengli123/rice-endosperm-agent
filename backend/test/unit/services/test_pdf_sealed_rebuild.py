"""A sealed revision cannot borrow another cache or rewrite physical evidence."""

from contextlib import asynccontextmanager
from datetime import timedelta
import hashlib
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from yuxi.services import scientific_pdf_ingest_service as service
from yuxi.services import pdf_evidence_service as locator
from yuxi.storage.postgres import models_knowledge as models


async def test_missing_own_sealed_cache_requires_new_revision(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for model in (models.KnowledgeFile, models.KnowledgeParseRevision, models.KnowledgeParseStage):
            await conn.run_sync(model.__table__.create)
    async with AsyncSession(engine, expire_on_commit=False) as db:
        source = b"%PDF-immutable"
        sealed_at = service._workflow_now()
        revision = models.KnowledgeParseRevision(
            id=1,
            revision_id="sealed",
            kb_id="kb",
            file_id="file",
            tenant_id=1,
            source_sha256=hashlib.sha256(source).hexdigest(),
            parser_fingerprint="f" * 64,
            pipeline_version="test",
            status="RUNNING",
            sealed_at=sealed_at,
            lease_owner="worker",
            lease_expires_at=sealed_at + timedelta(minutes=5),
            reused_from_revision_id="another-revision",
        )
        file = models.KnowledgeFile(
            id=1,
            file_id="file",
            kb_id="kb",
            filename="paper.pdf",
            active_parse_revision_id="sealed",
            active_index_revision_id="live-index",
            markdown_file="minio://kb/serving.md",
            evidence_status="INDEXED_CONTENT_ONLY",
        )
        db.add_all([revision, file])
        await db.flush()

        @asynccontextmanager
        async def context():
            yield db
            await db.flush()

        async def forbidden(*args, **kwargs):
            pytest.fail("sealed rebuild must not reparse, copy another revision or rewrite evidence")

        monkeypatch.setattr(service.pg_manager, "get_async_session_context", context)
        monkeypatch.setattr(service, "_claim_revision", AsyncMock(return_value=revision))
        monkeypatch.setattr(service, "_renew_revision_lease", AsyncMock())
        monkeypatch.setattr(service, "_release_identity_cache", AsyncMock())
        cache = AsyncMock(return_value=None)
        monkeypatch.setattr(service, "_load_cached_pipeline_result", cache)
        monkeypatch.setattr(locator, "download_pdf_original", AsyncMock(return_value={"content": source}))
        for name in (
            "_load_source",
            "materialize_reused_revision_assets",
            "_persist_article_records",
            "_store_artifacts",
        ):
            monkeypatch.setattr(service, name, forbidden)
        with pytest.raises(ValueError, match="PUBLISHED_PDF_CACHE_UNAVAILABLE_CREATE_NEW_REVISION"):
            await service._process_scientific_pdf_ingest({"job_id": "worker"}, "sealed")
        cache.assert_awaited_once_with("sealed", "f" * 64)
        assert revision.sealed_at == sealed_at
        assert file.active_parse_revision_id == "sealed" and file.active_index_revision_id == "live-index"
        assert file.markdown_file == "minio://kb/serving.md"
        with pytest.raises(ValueError, match="IMMUTABLE_CREATE_NEW_REVISION"):
            await service.rebuild_figure_index_for_file(kb_id="kb", file_id="file")
        from yuxi.knowledge.vision.figure_ingestor import prepare_figure_assets

        with pytest.raises(ValueError, match="IMMUTABLE_CREATE_NEW_REVISION"):
            await prepare_figure_assets(revision, {}, [], source)
        # An expired owner cannot even publish progress or release another
        # attempt's identity cache during error recovery.
        revision.status = "RUNNING"
        revision.lease_owner = "worker"
        revision.lease_expires_at = service._workflow_now() - timedelta(seconds=1)
        stage = models.KnowledgeParseStage(
            id=1,
            stage_id="native",
            revision_id="sealed",
            stage_name="NATIVE",
            input_fingerprint="f" * 64,
            status="RUNNING",
            lease_owner="worker",
            lease_expires_at=revision.lease_expires_at,
        )
        db.add(stage)
        await db.flush()
        with pytest.raises(RuntimeError, match="租约已丢失"):
            await service._set_stage_status("sealed", "NATIVE", "SUCCEEDED", worker_id="worker")
        with pytest.raises(RuntimeError, match="租约已丢失"):
            await service._link_stage_artifacts("sealed", worker_id="worker")
        service._release_identity_cache.reset_mock()
        with pytest.raises(ValueError, match="CACHE_UNAVAILABLE_CREATE_NEW_REVISION"):
            await service._process_scientific_pdf_ingest({"job_id": "worker"}, "sealed")
        assert revision.status == "RUNNING" and stage.status == "RUNNING"
        service._release_identity_cache.assert_not_awaited()
    await engine.dispose()
