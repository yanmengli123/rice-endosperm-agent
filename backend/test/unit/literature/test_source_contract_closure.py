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
from yuxi.literature.notes import service as notes, wiki_publish
from yuxi.literature.reading import service as reading
from yuxi.literature.translate import service as translate
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
    ArticleReference,
    CitationMention,
    KnowledgeChunk,
    LiteratureArtifactRevision,
    LiteratureCard,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
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
    for module in (notes, reading, translate, reader_bundle, cards):
        monkeypatch.setattr(module, "get_minio_client", lambda: assets)
    queued = []

    class Queue:
        async def enqueue_job(self, name, *args, _job_id=None):
            queued.append((name, args, _job_id))

    async def pool():
        return Queue()

    for module in (notes, reading, translate):
        monkeypatch.setattr(module, "get_arq_pool", pool)

    async def publish(*args, **kwargs):
        return {"published": False}

    monkeypatch.setattr(wiki_publish, "publish_literature_notes", publish)

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


async def test_source_acls_apply_before_pagination_and_assets_graph_reports(env):
    async with access.access_scope(USER, TENANT) as scope:
        assert scope.rids == {"doi-public", "doi-department", "doi-user", "bib"}
        result = await readops.list_papers(TENANT, page_size=1)
        assert result["total"] == 4 and len(result["items"]) == 1
        assert await readops.get_paper_detail(TENANT, "doi-private") is None
        assert await access.authorize_asset(f"tenants/7/documents/{'1' * 64}/reader_render/rev-public/images/F1.png")
        assert not await access.authorize_asset(f"tenants/7/documents/{'2' * 64}/reader_render/rev-private/render.md")
        assert not await access.authorize_asset("tenants/7/literature/notes/doi-private/L1_note.md")
        assert await access.authorize_asset("tenants/7/literature/reports/2.md")
        assert not await access.authorize_asset("tenants/7/literature/reports/1.md")
        assert [r["id"] for r in await cards.list_reports(TENANT)] == [2]
        neighbors = await graph_service.graph_neighbors(TENANT, "doi-public")
        assert neighbors["references"] == []
        network = await graph_service.graph_network(TENANT, exclude_isolated=False)
        assert {node["rid"] for node in network["nodes"]} == scope.rids
        assert all(node["library_citations"] == 0 for node in network["nodes"])
        preview = await graph_service.graph_network(TENANT, exclude_isolated=False, preview=True)
        assert preview["meta"]["matched_nodes"] == len(scope.rids)
    owner = SimpleNamespace(uid="owner", role="user", department_id=None)
    async with access.access_scope(owner, TENANT) as scope:
        assert "doi-private" in scope.rids and "other-tenant" not in scope.rids


async def test_http_and_agent_cannot_bypass_source_permission(env, monkeypatch):
    import server.routers.literature_router as router
    import yuxi.agents.toolkits.literature.tools as tools

    app = FastAPI()
    app.include_router(router.literature, prefix="/api")
    app.dependency_overrides[router.get_required_user] = lambda: USER

    async def tenant(_user):
        return TENANT

    monkeypatch.setattr(router, "_current_tenant", tenant)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        listing = await client.get("/api/literature/papers?tenant_id=99")
        assert listing.status_code == 200 and listing.json()["total"] == 4
        for operation in ("", "/reader", "/source-pdf"):
            assert (await client.get("/api/literature/papers/doi-private" + operation)).status_code == 404
        assert (await client.post("/api/literature/papers/doi-private/compile")).status_code == 404
        assert (
            await client.get("/api/literature/assets", params={"uri": "kbasset://tenants/7/literature/reports/1.md"})
        ).status_code == 404

    async def user(_self, uid):
        return USER

    monkeypatch.setattr(tools, "_principal_tenant", tenant)
    monkeypatch.setattr(tools.UserRepository, "get_by_uid", user)
    runtime = SimpleNamespace(context=SimpleNamespace(uid="reader"))
    result = json.loads(await tools.search_literature_papers.coroutine("", runtime=runtime))
    assert {p["rid"] for p in result["papers"]} == {"doi-public", "doi-department", "doi-user", "bib"}


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
    assert queued[0][1] == ("rev-public", "reader_render_v1")
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


async def test_reading_version_invalidates_cache_without_deleting_history(env):
    sessions, assets, _ = env
    base = await _ready(env)
    first = await notes.enqueue_literature_compile(paper_id=1, level="L1")
    translation = await translate.enqueue_literature_translation(paper_id=1)
    async with sessions() as session:
        session.add(
            LiteratureVariant(
                id=1,
                tenant_id=TENANT,
                paper_id=1,
                parse_revision_id="rev-public",
                variant_kind="zh",
                lang="zh",
                uri=f"kbasset://{base}variants/zh.md",
                content_digest="d" * 64,
            )
        )
        paper = await session.get(LiteraturePaper, 1)
        paper.reading_version += 1
    assert not (await reader_bundle.get_variant_content(TENANT, "doi-public", "zh"))["ready"]
    second = await notes.enqueue_literature_compile(paper_id=1, level="L1")
    next_translation = await translate.enqueue_literature_translation(paper_id=1)
    assert second["job_id"] != first["job_id"]
    assert next_translation["job_id"] != translation["job_id"]
    assert (await notes.process_literature_compile({}, first["job_id"]))["status"] == "STALE"
    assert (await translate.process_literature_translation({}, translation["job_id"]))["status"] == "STALE"
    async with sessions() as session:
        assert len((await session.execute(select(LiteratureCompileJob))).scalars().all()) == 2
        assert len((await session.execute(select(LiteratureVariant))).scalars().all()) == 1


async def test_source_changed_during_llm_call_cannot_publish_current_notes(env, monkeypatch):
    await _ready(env)
    sessions, _, _ = env
    queued = await notes.enqueue_literature_compile(paper_id=1, level="L1")

    async def compile_changed(*args, **kwargs):
        async with sessions() as session:
            paper = await session.get(LiteraturePaper, 1)
            paper.reading_version += 1
        return {"l1": {"one_liner": "old source", "concepts": []}, "l2": None, "ai_value": 1, "topic_score": None}

    monkeypatch.setattr(notes.compiler, "compile_l1_l2", compile_changed)
    assert (await notes.process_literature_compile({}, queued["job_id"]))["status"] == "STALE"
    assert await readops.get_note_uri(TENANT, "doi-public", "L1") is None
    async with sessions() as session:
        assert (await session.get(LiteraturePaper, 1)).ai_value_score is None


async def test_revoked_requester_cannot_publish_translation(env, monkeypatch):
    await _ready(env)
    sessions, _, _ = env
    job = await translate.enqueue_literature_translation(paper_id=1)
    async with sessions() as session:
        row = await session.get(LiteratureTranslationJob, job["job_id"])
        row.requested_by = "reader"
        source = (await session.execute(select(KnowledgeBase).where(KnowledgeBase.kb_id == "kb-public"))).scalar_one()
        source.share_config = {"access_level": "private"}
    assert (await translate.process_literature_translation({}, job["job_id"]))["status"] == "STALE"


async def test_same_doi_different_source_acl_does_not_rebind_existing_paper(env):
    public = await notes.register_literature_paper(tenant_id=TENANT, doi="10.1/public", source_revision_id="rev-public")
    private = await notes.register_literature_paper(
        tenant_id=TENANT, doi="10.1/public", source_revision_id="rev-private"
    )
    # Existing source file identity wins even when later metadata identifies the same DOI.
    assert public.id != private.id and public.source_revision_id == "rev-public"
    async with access.access_scope(USER, TENANT) as scope:
        assert public.rid in scope.rids and private.rid not in scope.rids


@pytest.mark.parametrize("fail_outbox", [False, True])
async def test_real_pdf_orchestration_activates_and_records_delivery_atomically(env, monkeypatch, fail_outbox):
    """Only parser and vector I/O are stubbed; activation/lineage/bridge are real."""
    import hashlib
    from yuxi.services import scientific_pdf_ingest_service as ingest
    from yuxi.knowledge.pdf_evidence.contracts import ParserArtifact, PipelineResult, UnifiedArticle
    from yuxi.knowledge.implementations.milvus import MilvusKB

    sessions, assets, queued = env
    source_bytes = b"%PDF-1.7 deterministic source fixture"
    sha = hashlib.sha256(source_bytes).hexdigest()
    async with sessions() as session:
        file = (await session.execute(select(KnowledgeFile).where(KnowledgeFile.file_id == "file-public"))).scalar_one()
        file.file_type = "pdf"
        file.content_hash = sha
        file.active_parse_revision_id = "rev-public"
        file.processing_params = {"strategy": "scientific_pdf", "pdf_evidence_pipeline": True}
        session.add(
            KnowledgeWiki(
                wiki_id="literature-wiki-old",
                kb_id="kb-notes",
                tenant_id=TENANT,
                created_by="owner",
                current_publication_id="publication-old",
                status="ACTIVE",
            )
        )
        session.add(
            WikiSourceBinding(
                binding_id="binding-old",
                wiki_id="literature-wiki-old",
                source_kb_id="kb-public",
                security_domain="test",
                created_by="owner",
            )
        )
        session.add(
            WikiPublication(
                publication_id="publication-old",
                wiki_id="literature-wiki-old",
                tenant_id=TENANT,
                snapshot_id="snapshot-old",
                build_id="build-old",
                status="ACTIVE",
                manifest_hash="a" * 64,
                manifest_json={},
            )
        )

    async def load_source(file):
        return source_bytes

    async def parse(_self, path, params, **kwargs):
        assert params["pdf_evidence_pipeline"]
        article = UnifiedArticle("test", sha, "Contract PDF", "# Contract PDF\n\nBody.", {}, [], [], [], [], {})
        return PipelineResult(
            "INDEXED_CONTENT_ONLY",
            article,
            {"capabilities": {"fulltext_search": True}},
            [ParserArtifact("mineru_markdown", "full.md", article.markdown.encode(), "text/markdown")],
            article.markdown,
            "frozen-parser",
        )

    async def index(kb_id, file_id, *, operator_id, params):
        revision = params["_index_revision_id"]
        token = MilvusKB._index_revision_token(revision)
        async with sessions() as session:
            session.add(
                KnowledgeChunk(
                    chunk_id=f"{file_id}_rev_{token}_chunk_0",
                    kb_id=kb_id,
                    file_id=file_id,
                    chunk_index=0,
                    content="Body",
                    source_provenance={"index_revision_id": revision},
                )
            )
        return {"chunk_count": 1, "token_count": 1}

    async def cleanup(*args):
        return None

    async def pool():
        class Queue:
            async def enqueue_job(self, name, *args, _job_id=None):
                queued.append((name, args, _job_id))

        return Queue()

    monkeypatch.setattr(ingest, "_load_source", load_source)
    monkeypatch.setattr(ingest, "get_minio_client", lambda: assets)
    monkeypatch.setattr(ingest.ScientificPdfPipeline, "run", parse)
    monkeypatch.setattr(ingest.knowledge_base, "index_file", index)
    monkeypatch.setattr(ingest.knowledge_base, "cleanup_inactive_file_index_revisions", cleanup)
    monkeypatch.setattr(ingest, "get_arq_pool", pool)
    request = await ingest.create_or_reuse_scientific_pdf_ingest(
        kb_id="kb-public", file_id="file-public", operator_id="reader", enqueue=False
    )
    revision_id = request["revision_id"]
    if fail_outbox:

        async def broken(*args):
            raise RuntimeError("outbox unavailable")

        monkeypatch.setattr(reconcile, "record_source_event", broken)
        with pytest.raises(RuntimeError, match="outbox unavailable"):
            await ingest.process_scientific_pdf_ingest({}, revision_id)
    else:
        result = await ingest.process_scientific_pdf_ingest({}, revision_id)
        assert result["activated"] and result["status"] == "INDEXED_CONTENT_ONLY"
    async with sessions() as session:
        file = (await session.execute(select(KnowledgeFile).where(KnowledgeFile.file_id == "file-public"))).scalar_one()
        events = (await session.execute(select(LiteratureSourceEvent))).scalars().all()
        wiki = (await session.execute(select(KnowledgeWiki))).scalar_one()
        publication = (await session.execute(select(WikiPublication))).scalar_one()
        if fail_outbox:
            assert file.active_parse_revision_id == "rev-public" and not events
            assert wiki.current_publication_id == "publication-old" and publication.status == "ACTIVE"
        else:
            assert file.active_parse_revision_id == revision_id and file.active_index_revision_id
            assert len(events) == 1 and events[0].status == "COMPLETED"
            assert (await session.get(LiteraturePaper, 1)).source_revision_id == revision_id
            assert any(name == reading.RENDER_JOB_NAME and args[0] == revision_id for name, args, _ in queued)
            assert wiki.current_publication_id is None and publication.status == "SUPERSEDED"
