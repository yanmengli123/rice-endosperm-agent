"""失败重试预算：预算内异常回 PENDING（由对账 cron 以新 attempt 重排），超预算置 FAILED 死信。"""

from contextlib import asynccontextmanager
from types import SimpleNamespace
import hashlib

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.services import scientific_pdf_ingest_service as service
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeBase,
    KnowledgeFile,
    KnowledgeParseArtifact,
    KnowledgeParseRevision,
    KnowledgeParseStage,
)

pytestmark = pytest.mark.unit


async def _drive_failure(monkeypatch, *, attempt: int, failure=None):
    failure = failure or RuntimeError("供应商瞬断")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for model in (
            KnowledgeBase,
            KnowledgeFile,
            KnowledgeParseRevision,
            KnowledgeParseStage,
            KnowledgeParseArtifact,
        ):
            await conn.run_sync(model.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:

        @asynccontextmanager
        async def context():
            yield db
            await db.commit()

        monkeypatch.setattr(service.pg_manager, "get_async_session_context", context)
        source = b"%PDF-fixture"
        file = KnowledgeFile(
            id=1,
            file_id="f",
            kb_id="kb",
            filename="paper.pdf",
            file_type="pdf",
            evidence_status="RUNNING",
        )
        rev = KnowledgeParseRevision(
            id=1,
            revision_id="draft",
            tenant_id=1,
            file_id="f",
            kb_id="kb",
            source_sha256=hashlib.sha256(source).hexdigest(),
            parser_fingerprint="b" * 64,
            pipeline_version="test",
            processing_mode="parse_only",
            status="PENDING",
            attempt=attempt,
            created_by="u1",
        )
        kb = KnowledgeBase(
            id=1, kb_id="kb", name="KB", kb_type="milvus", tenant_id=1, created_by="u1", governance_status="READY"
        )
        db.add_all([kb, file, rev])
        for idx, name in enumerate(service.PARSE_STAGE_NAMES, 1):
            db.add(
                KnowledgeParseStage(
                    id=idx,
                    stage_id=f"s{idx}",
                    revision_id="draft",
                    stage_name=name,
                    input_fingerprint="b" * 64,
                    status="PENDING",
                )
            )
        await db.commit()

        async def claim(*args):
            return rev

        async def noop(*args, **kwargs):
            return None

        async def load(*args):
            return source

        class Pipeline:
            async def run(self, path, params, **kwargs):
                raise failure

        monkeypatch.setattr(service, "_renew_revision_lease", noop)
        monkeypatch.setattr(service, "_load_source", load)
        monkeypatch.setattr(service, "_load_compatible_mineru_cache", noop)
        monkeypatch.setattr(service, "_publish_identity_cache", noop)
        monkeypatch.setattr(service, "_release_identity_cache", noop)
        monkeypatch.setattr(service, "get_minio_client", lambda: SimpleNamespace())
        monkeypatch.setattr(service, "ScientificPdfPipeline", Pipeline)

        with pytest.raises(type(failure), match=str(failure)):
            await service._process_scientific_pdf_ingest({"job_id": "worker"}, "draft")

        await db.refresh(rev)
        await db.refresh(file)
        if hasattr(failure, "code"):
            assert rev.qa_report["failure"] == {"code": failure.code, "retryable": False}
        outcome = (
            rev.status,
            int(rev.attempt or 0),
            rev.lease_owner,
            file.status,
            file.evidence_status,
            rev.completed_at is not None,
        )
    await engine.dispose()
    return outcome


async def test_failure_within_budget_returns_pending(monkeypatch):
    status, attempt, lease_owner, file_status, evidence, completed = await _drive_failure(monkeypatch, attempt=0)
    assert status == "PENDING"
    assert attempt == 1
    assert lease_owner is None
    assert file_status == "parsing"
    assert evidence == "PENDING"
    assert not completed


async def test_failure_beyond_budget_goes_dead_letter(monkeypatch):
    status, attempt, lease_owner, file_status, evidence, completed = await _drive_failure(
        monkeypatch, attempt=service.MAX_INGEST_ATTEMPTS - 1
    )
    assert status == "FAILED"
    assert attempt == service.MAX_INGEST_ATTEMPTS
    assert lease_owner is None
    assert file_status == "error_parsing"
    assert evidence == "FAILED"
    assert completed


async def test_input_rejection_is_terminal_without_supplier_retries(monkeypatch):
    from yuxi.knowledge.pdf_evidence.native import PdfInputError

    status, attempt, lease_owner, file_status, evidence, completed = await _drive_failure(
        monkeypatch, attempt=0, failure=PdfInputError("PDF_PASSWORD_REQUIRED")
    )
    assert (status, attempt, lease_owner) == ("FAILED", 1, None)
    assert (file_status, evidence, completed) == ("error_parsing", "FAILED", True)
