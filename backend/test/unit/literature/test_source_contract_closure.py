"""PDF source bridge, inherited ACLs and immutable derived-input identities."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
import json

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.literature import access, cards, graph_service, readops, reader_bundle, reconcile
from yuxi.literature.reading import service as reading
from yuxi.storage.postgres.models_business import User, Tenant, TenantMembership
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeBase,
    KnowledgeFile,
    KnowledgeParseRevision,
    KnowledgeParseArtifact,
    LiteraturePaper,
    LiteratureSourceEvent,
    LiteratureReaderRender,
    LiteratureCompileJob,
    LiteratureTranslationJob,
    LiteratureVariant,
    LiteratureAttachment,
    LiteratureCitation,
    LiteratureReport,
    KnowledgeWiki,
    WikiSourceBinding,
    WikiPublication,
    KnowledgeParseStage,
    KnowledgeIndexRevision,
    KnowledgeDocumentIdentityCache,
    EvidenceAnchorRecord,
    EvidenceSpanRecord,
    EvidenceSpanRegionRecord,
    PdfSourceRegionRecord,
    PdfTableRecord,
    PdfTableCellRecord,
    FigureEntityRecord,
    FigureAssetRecord,
    ArticleReference,
    CitationMention,
    KnowledgeChunk,
    LiteratureArtifactRevision,
    LiteratureCard,
)

pytestmark = [pytest.mark.unit]
TENANT = 7
USER = SimpleNamespace(uid="reader", role="user", department_id=12, is_disabled=False)


class MemoryAssets:
    def __init__(self):
        self.files = {}

    async def adownload_file(self, bucket, object_name):
        return self.files[object_name]

    async def aupload_file(self, *, bucket_name, object_name, data, content_type):
        self.files[object_name] = bytes(data)
        return SimpleNamespace(url=f"http://minio:9000/{bucket_name}/{object_name}")


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    models = (
        User,
        Tenant,
        TenantMembership,
        KnowledgeBase,
        KnowledgeFile,
        KnowledgeParseRevision,
        KnowledgeParseArtifact,
        LiteraturePaper,
        LiteratureSourceEvent,
        LiteratureReaderRender,
        LiteratureCompileJob,
        LiteratureTranslationJob,
        LiteratureVariant,
        LiteratureAttachment,
        LiteratureCitation,
        LiteratureReport,
        LiteratureCard,
        KnowledgeWiki,
        WikiSourceBinding,
        WikiPublication,
        KnowledgeParseStage,
        KnowledgeIndexRevision,
        KnowledgeDocumentIdentityCache,
        EvidenceAnchorRecord,
        EvidenceSpanRecord,
        EvidenceSpanRegionRecord,
        PdfSourceRegionRecord,
        PdfTableRecord,
        PdfTableCellRecord,
        FigureEntityRecord,
        FigureAssetRecord,
        ArticleReference,
        CitationMention,
        KnowledgeChunk,
    )
    async with engine.begin() as conn:
        for model in models:
            await conn.run_sync(model.__table__.create)
        await conn.run_sync(LiteratureArtifactRevision.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def sessions():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    monkeypatch.setattr(reading.pg_manager, "get_async_session_context", sessions)
    assets = MemoryAssets()
    for module in (reading, reader_bundle, cards):
        monkeypatch.setattr(module, "get_minio_client", lambda: assets)
    queued = []

    class Queue:
        async def enqueue_job(self, name, *args, _job_id=None):
            queued.append((name, args, _job_id))
            return object()

    async def pool():
        return Queue()
    for module in (reading,):
        monkeypatch.setattr(module, "get_arq_pool", pool)


    async def publish(*args, **kwargs):
        return {"published": False}


    policies = {
        "public": {"access_level": "global"},
        "private": {"access_level": "private"},
        "department": {"access_level": "department", "department_ids": [12]},
        "other_department": {"access_level": "department", "department_ids": [13]},
        "user": {"access_level": "user", "user_uids": ["reader"]},
        "other_user": {"access_level": "user", "user_uids": ["someone"]},
    }
    async with sessions() as session:
        session.add(User(id=1, uid="reader", username="reader", role="user", password_hash="test-only"))
        session.add(Tenant(id=TENANT, name="test"))
        session.add(TenantMembership(id=1, tenant_id=TENANT, uid="reader", role="member"))
        for idx, (kind, policy) in enumerate(policies.items(), 1):
            sha = str(idx) * 64
            revision = f"rev-{kind}"
            session.add(
                KnowledgeBase(
                    id=idx,
                    kb_id=f"kb-{kind}",
                    name=kind,
                    tenant_id=TENANT,
                    kb_type="milvus",
                    created_by="owner",
                    share_config=policy,
                )
            )
            session.add(
                KnowledgeFile(
                    id=idx,
                    file_id=f"file-{kind}",
                    kb_id=f"kb-{kind}",
                    filename=f"{kind}.pdf",
                    active_parse_revision_id=revision,
                )
            )
            session.add(
                KnowledgeParseRevision(
                    id=idx,
                    revision_id=revision,
                    tenant_id=TENANT,
                    file_id=f"file-{kind}",
                    kb_id=f"kb-{kind}",
                    source_sha256=sha,
                    parser_fingerprint=f"fp-{kind}",
                    pipeline_version="scientific_pdf_v3.2",
                    status="INDEXED_FULL",
                    capabilities={"pdf_highlight": True},
                )
            )
            session.add(
                LiteraturePaper(
                    id=idx, rid=f"doi-{kind}", tenant_id=TENANT, doi=f"10.1/{kind}", source_revision_id=revision
                )
            )
        session.add(LiteraturePaper(id=20, tenant_id=TENANT, rid="orphan", source_revision_id="missing"))
        session.add(LiteraturePaper(id=21, tenant_id=TENANT, rid="bib"))
        session.add(LiteraturePaper(id=22, tenant_id=99, rid="other-tenant"))
        session.add_all(
            [
                LiteratureCitation(id=1, tenant_id=TENANT, citing_rid="doi-public", cited_doi="10.1/private"),
                LiteratureReport(
                    id=1,
                    tenant_id=TENANT,
                    report_kind="topic_moc",
                    title="mixed private",
                    content="private body",
                    source_rids_json=["doi-public", "doi-private"],
                ),
                LiteratureReport(
                    id=2,
                    tenant_id=TENANT,
                    report_kind="topic_moc",
                    title="visible",
                    content="public body",
                    source_rids_json=["doi-public"],
                ),
                LiteratureReport(
                    id=3, tenant_id=TENANT, report_kind="topic_moc", title="legacy", content="legacy body"
                ),
            ]
        )
    yield sessions, assets, queued
    await engine.dispose()


async def test_ingest_progress_enforces_source_acl_before_limit_and_revocation(env):
    sessions, _assets, _queue = env
    async with sessions() as db:
        for kb in (await db.execute(select(KnowledgeBase))).scalars():
            kb.contract_key = "pdf_evidence"
        private = await db.scalar(select(KnowledgeParseRevision).where(
            KnowledgeParseRevision.revision_id == "rev-private"
        ))
        private.error_message = "private-source-diagnostics"
    async with access.access_scope(USER, TENANT, catalog_only=True):
        items = await readops.list_paper_tasks(TENANT, limit=100)
        assert {item["ref"] for item in items} == {"rev-public", "rev-department", "rev-user"}
        assert "private-source-diagnostics" not in json.dumps(items)
        assert len(await readops.list_paper_tasks(TENANT, limit=1)) == 1
        assert await readops.list_paper_tasks(99) == []
    async with sessions() as db:
        kb = await db.scalar(select(KnowledgeBase).where(KnowledgeBase.kb_id == "kb-public"))
        kb.share_config = {"access_level": "private"}
    async with access.access_scope(USER, TENANT, catalog_only=True):
        assert {item["ref"] for item in await readops.list_paper_tasks(TENANT)} == {"rev-department", "rev-user"}


async def test_http_single_paper_scope_does_not_load_the_library(env, monkeypatch):
    import server.routers.literature_router as router

    app = FastAPI()
    app.include_router(router.literature, prefix="/api")
    app.dependency_overrides[router.get_required_user] = lambda: USER

    async def tenant(_user):
        return TENANT

    async def detail(tenant_id, rid):
        assert access.current_access().rids == {rid}
        return {"rid": rid}

    async def tasks(tenant_id, *, limit):
        scope = access.current_access()
        assert scope.catalog_only and not scope.rids and scope.allowed_kbs
        return []

    monkeypatch.setattr(router, "_current_tenant", tenant)
    monkeypatch.setattr(router.readops, "get_paper_detail", detail)
    monkeypatch.setattr(router.readops, "list_paper_tasks", tasks)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        assert (await client.get("/api/literature/papers/doi-public")).status_code == 200
        assert (await client.get("/api/literature/papers/doi-private")).status_code == 404
        assert (await client.get("/api/literature/tasks")).json() == {"tasks": []}


async def test_activation_event_is_transactional_and_registered_missing_render_is_repaired(env, monkeypatch):
    sessions, assets, queued = env
    with pytest.raises(RuntimeError):
        async with sessions() as session:
            await reconcile.record_source_event(session, "rev-public", TENANT)
            raise RuntimeError("activation rollback")
    async with sessions() as session:
        assert (await session.execute(select(LiteratureSourceEvent))).scalars().all() == []
        await reconcile.record_source_event(session, "rev-public", TENANT)
        await reconcile.record_source_event(session, "rev-public", TENANT)
    # Real bridge invokes the keyword-only enqueue service, not a permissive stub.
    result = await reconcile.register_scientific_revision("rev-public")
    assert result["status"] == "REGISTERED" and result["render"]["status"] == "PENDING"
    assert queued[0][1] == ("rev-public", "reader_render_v1", 0, 0)
    async with sessions() as session:
        event = (await session.execute(select(LiteratureSourceEvent))).scalar_one()
        assert event.status == "COMPLETED"
        assert (await session.execute(select(LiteratureReaderRender))).scalar_one().status == "PENDING"
    # All other active sources were already registered but lacked reader jobs.
    assert await reconcile.reconcile_scientific_literature_papers() == 5
    assert await reconcile.reconcile_scientific_literature_papers() == 0
    assert len(queued) == 6


async def _ready(env):
    sessions, assets, _ = env
    base = f"tenants/7/documents/{'1' * 64}/reader_render/rev-public/"
    assets.files[base + "document.json"] = json.dumps({"paragraphs": [{"text_en": "Body"}]}).encode()
    async with sessions() as session:
        session.add(
            LiteratureReaderRender(
                id=1,
                revision_id="rev-public",
                tenant_id=TENANT,
                status="SUCCEEDED",
                markdown_uri=f"kbasset://{base}render.md",
            )
        )
    return base


