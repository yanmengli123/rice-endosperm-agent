"""Real relational state for parse-only completion, fencing and serving isolation."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
import hashlib
import json
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.services import scientific_pdf_ingest_service as service
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeBase,
    KnowledgeFile,
    KnowledgeParseRevision,
    KnowledgeParseStage,
    KnowledgeParseArtifact,
)
from yuxi.knowledge.pdf_evidence.contracts import ParserArtifact, PipelineResult, UnifiedArticle

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("active", [None, "previous", "concurrent"])
@pytest.mark.parametrize("orchestrated,rejected", [(False, False), (True, False), (True, True)])
async def test_parse_only_finishes_without_serving_pointer_or_activation(active, orchestrated, rejected, monkeypatch):
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
            active_parse_revision_id=active,
            active_index_revision_id="index" if active else None,
            markdown_file="minio://kb/serving.md" if active else None,
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
            status="RUNNING",
            lease_owner="worker",
            lease_expires_at=service._workflow_now() + timedelta(minutes=5),
            created_by="u1",
        )
        kb = KnowledgeBase(
            id=1, kb_id="kb", name="Draft KB", kb_type="milvus", tenant_id=1, created_by="u1", governance_status="READY"
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
                    status="SUCCEEDED" if name not in {"INDEX", "ACTIVATE"} else "PENDING",
                )
            )
        await db.commit()
        snapshot = {
            "active_parse_revision_id": "previous",
            "active_index_revision_id": "index",
            "markdown_file": "minio://kb/serving.md",
            "status": "parsed",
            "processing_params": {},
            "evidence_status": "INDEXED_FULL",
            "evidence_capabilities": {"fulltext_search": True},
        }
        result = SimpleNamespace(capability="INDEXED_FULL", qa_report={"counts": {"paragraphs": 2}})
        if orchestrated:
            calls, objects = [], {}

            async def claim(*args):
                return rev

            async def noop(*args, **kwargs):
                return None

            async def forbidden(*args, **kwargs):
                pytest.fail("parse_only must not project evidence or index documents")

            async def load(*args):
                return source

            class Storage:
                async def aupload_file(self, **kwargs):
                    objects[kwargs["object_name"]] = kwargs["data"]
                    return SimpleNamespace(url=f"minio://knowledgebases/{kwargs['object_name']}")

                async def adownload_file(self, bucket, name):
                    return objects[name]

            class Pipeline:
                async def run(self, path, params, **kwargs):
                    calls.append("parse")
                    article = UnifiedArticle(
                        "test",
                        rev.source_sha256,
                        "Draft",
                        "Readable draft scientific document.",
                        {},
                        [],
                        [],
                        [],
                        [],
                        {},
                    )
                    return PipelineResult(
                        "REJECTED" if rejected else "INDEXED_FULL",
                        None if rejected else article,
                        {"counts": {"paragraphs": 2}},
                        [
                            ParserArtifact(
                                "unified_article",
                                "article.json",
                                json.dumps(article.to_dict()).encode(),
                                "application/json",
                            )
                        ],
                        "" if rejected else article.markdown,
                        rev.parser_fingerprint,
                    )

            monkeypatch.setattr(service, "_claim_revision", claim)
            monkeypatch.setattr(service, "_renew_revision_lease", noop)
            monkeypatch.setattr(service, "_load_source", load)
            monkeypatch.setattr(service, "_load_compatible_mineru_cache", noop)
            monkeypatch.setattr(service, "_publish_identity_cache", noop)
            monkeypatch.setattr(service, "_persist_article_records", forbidden)
            monkeypatch.setattr(service.knowledge_base, "index_file", forbidden)
            storage = Storage()
            monkeypatch.setattr(service, "get_minio_client", lambda: storage)
            monkeypatch.setattr(service, "ScientificPdfPipeline", Pipeline)
            outcome = await service._process_scientific_pdf_ingest({"job_id": "worker"}, "draft")
            assert outcome == (
                {"revision_id": "draft", "status": "REJECTED", "activated": False}
                if rejected
                else {"revision_id": "draft", "status": "PARSED_ONLY", "activated": False}
            )
            assert calls == ["parse"]
            assert len(list((await db.execute(select(KnowledgeParseArtifact))).scalars())) == (4 if rejected else 5)

        else:
            await service._complete_parse_only(
                rev, "worker", {"title": "Draft"}, result, {"unified_article": "minio://kb/draft.json"}, snapshot
            )
        assert rev.status == ("REJECTED" if rejected else "PARSED_ONLY") and rev.lease_owner is None
        assert not rev.capabilities.get("evidence", False)
        assert file.active_parse_revision_id == active
        assert file.active_index_revision_id == ("index" if active else None)
        assert file.markdown_file == ("minio://kb/serving.md" if active else None)
        if not active:
            assert file.evidence_status == ("REJECTED" if rejected else "PARSED_ONLY")
        if active == "previous" and not orchestrated:
            assert file.evidence_status == "INDEXED_FULL"
        if active == "concurrent" or (active == "previous" and orchestrated):
            assert file.evidence_status == "RUNNING"  # no stale snapshot restore
        stages = (await db.execute(select(KnowledgeParseStage))).scalars().all()
        assert all(
            s.status == ("PENDING" if rejected else "SKIPPED") for s in stages if s.stage_name in {"INDEX", "ACTIVATE"}
        )
        assert service._TASK_STATUS_BY_REVISION[rev.status] == ("failed" if rejected else "success")
    await engine.dispose()
