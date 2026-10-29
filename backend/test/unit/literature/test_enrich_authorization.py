"""Actor-bound enrichment and bounded independent metadata sources; no real providers."""

import asyncio
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.literature import access, enrich
from yuxi.services.principal import PrincipalResolutionError
from yuxi.services.run_worker import process_literature_enrich_pending
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import Tenant, TenantMembership, User
from yuxi.storage.postgres.models_knowledge import KnowledgeBase, KnowledgeFile, KnowledgeParseRevision, LiteraturePaper

pytestmark = pytest.mark.unit


@pytest_asyncio.fixture
async def environment(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for model in (
            Tenant, User, TenantMembership, KnowledgeBase, KnowledgeFile, KnowledgeParseRevision, LiteraturePaper,
        ):
            await conn.run_sync(model.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def sessions():
        async with factory() as db:
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    monkeypatch.setattr(pg_manager, "get_async_session_context", sessions)
    monkeypatch.setattr(pg_manager, "async_engine", engine)
    calls = []

    async def metadata(doi):
        calls.append(doi)
        return {"title": "Authorized metadata", "journal": "Journal"}

    async def empty(_doi):
        return None

    monkeypatch.setattr(enrich, "fetch_openalex", metadata)
    monkeypatch.setattr(enrich, "fetch_crossref", empty)
    monkeypatch.setattr(enrich, "fetch_semantic_scholar", empty)
    async with sessions() as db:
        db.add_all([Tenant(id=7, name="Active"), Tenant(id=8, name="Other")])
        user = User(id=1, uid="submitter", username="submitter", role="user", password_hash="fixture-only")
        db.add(user)
        db.add(TenantMembership(id=1, tenant_id=7, uid="submitter", role="member", status="active"))
        for index, kind in enumerate(("public", "private"), 1):
            db.add(KnowledgeBase(
                id=index, kb_id=kind, name=kind, tenant_id=7, kb_type="milvus", created_by="another-user",
                share_config={"access_level": "global" if kind == "public" else "private"},
            ))
            db.add(KnowledgeFile(
                id=index, kb_id=kind, file_id=kind, filename=kind + ".pdf", active_parse_revision_id=kind,
            ))
            db.add(KnowledgeParseRevision(
                id=index, revision_id=kind, kb_id=kind, file_id=kind, tenant_id=7,
                source_sha256=str(index) * 64, parser_fingerprint="fixture", pipeline_version="scientific_pdf_v3.2",
                status="INDEXED_FULL",
            ))
            db.add(LiteraturePaper(
                id=index, tenant_id=7, rid=kind, doi="10.1/" + kind, source_revision_id=kind, quartile="",
            ))
    yield sessions, user, calls
    await engine.dispose()


async def test_worker_restores_actor_and_never_enriches_another_users_private_source(environment):
    sessions, _user, calls = environment
    result = await process_literature_enrich_pending({}, 7, requested_by="submitter")
    assert result["processed"] == 1 and result["ok"] == 1
    assert calls == ["10.1/public"]
    assert access.current_access() is None
    async with sessions() as db:
        assert (await db.get(LiteraturePaper, 1)).title == "Authorized metadata"
        assert (await db.get(LiteraturePaper, 2)).title is None


async def test_legacy_anonymous_jobs_and_unscoped_batch_calls_fail_closed(environment):
    _sessions, _user, calls = environment
    with pytest.raises(PermissionError, match="可信提交者"):
        await process_literature_enrich_pending({}, 7)
    with pytest.raises(PermissionError, match="有效用户"):
        await enrich.enrich_pending(7)
    assert not calls


@pytest.mark.parametrize("invalid", ["disabled", "deleted", "membership", "tenant"])
async def test_worker_rechecks_current_user_and_active_membership(environment, invalid):
    sessions, _user, calls = environment
    async with sessions() as db:
        user = await db.get(User, 1)
        if invalid == "disabled":
            user.is_disabled = True
        elif invalid == "deleted":
            user.is_deleted = True
        elif invalid == "membership":
            (await db.get(TenantMembership, 1)).status = "disabled"
    with pytest.raises((PermissionError, PrincipalResolutionError)):
        await process_literature_enrich_pending({}, 8 if invalid == "tenant" else 7, requested_by="submitter")
    assert not calls


async def test_revocation_during_fetch_blocks_publication_and_later_provider_requests(environment, monkeypatch):
    sessions, user, calls = environment

    async def revoke(doi):
        calls.append(doi)
        async with sessions() as db:
            (await db.get(KnowledgeBase, 1)).share_config = {"access_level": "private"}
        return {"title": "Must not be published"}

    monkeypatch.setattr(enrich, "fetch_openalex", revoke)
    async with access.access_scope(user, 7):
        first = await enrich.enrich_paper(7, "public")
        assert first["reason"] == "source_access_revoked"
        again = await enrich.enrich_paper(7, "public")
        assert again["reason"] == "source_access_revoked"
    assert calls == ["10.1/public"]
    async with sessions() as db:
        assert (await db.get(LiteraturePaper, 1)).title is None


async def test_sources_run_concurrently_but_merge_in_stable_priority_order(environment, monkeypatch):
    _sessions, _user, _calls = environment
    started, barrier = [], asyncio.Event()

    def source(label):
        async def fetch(_doi):
            started.append(label)
            if len(started) == 3:
                barrier.set()
            await asyncio.wait_for(barrier.wait(), 0.5)
            return {"title": label, "journal": label}
        return fetch

    monkeypatch.setattr(enrich, "fetch_openalex", source("openalex"))
    monkeypatch.setattr(enrich, "fetch_crossref", source("crossref"))
    monkeypatch.setattr(enrich, "fetch_semantic_scholar", source("semantic_scholar"))
    result = await enrich.enrich_paper(7, "public")
    assert result["ok"] and result["sources"] == ["openalex", "crossref", "semantic_scholar"]
    async with environment[0]() as db:
        assert (await db.get(LiteraturePaper, 1)).title == "openalex"


async def test_slow_or_failed_source_is_bounded_and_keeps_other_sources_results(environment, monkeypatch):
    cancelled = asyncio.Event()

    async def slow(_doi):
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()

    async def fast(_doi):
        return {"title": "Available source"}

    async def broken(_doi):
        raise RuntimeError("fixture source failed")

    monkeypatch.setattr(enrich, "_SOURCE_DEADLINE_SECONDS", 0.02)
    monkeypatch.setattr(enrich, "fetch_openalex", slow)
    monkeypatch.setattr(enrich, "fetch_crossref", fast)
    monkeypatch.setattr(enrich, "fetch_semantic_scholar", broken)
    started = time.monotonic()
    result = await enrich.enrich_paper(7, "public")
    assert time.monotonic() - started < 1
    assert result["ok"] and result["sources"] == ["crossref"] and cancelled.is_set()


async def test_http_enrichment_freezes_submitter_and_returns_completed_status(monkeypatch):
    import server.routers.literature_router as router

    app = FastAPI()
    app.include_router(router.literature, prefix="/api")
    user = SimpleNamespace(uid="submitter")
    app.dependency_overrides[router.get_required_user] = lambda: user
    app.dependency_overrides[router.authorize_literature_request] = lambda: None
    queued = []

    async def tenant(_user):
        return 7

    class Queue:
        async def enqueue_job(self, *args, **kwargs):
            queued.append((args, kwargs))
            return SimpleNamespace(job_id="fixture-job")

    async def queue():
        return Queue()

    async def completed(tenant_id, rid):
        assert (tenant_id, rid) == (7, "public")
        return {"rid": rid, "ok": True, "filled": ["title"]}

    monkeypatch.setattr(router, "_current_tenant", tenant)
    monkeypatch.setattr(router.enrich_module, "enrich_paper", completed)
    monkeypatch.setattr("yuxi.services.run_queue_service.get_arq_pool", queue)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://fixture") as client:
        response = await client.post("/api/literature/enrich/pending?tenant_id=999", json={"requested_by": "other"})
        assert response.status_code == 202 and response.json()["job_id"] == "fixture-job"
        args, kwargs = queued[0]
        assert args == ("process_literature_enrich_pending", 7, 50) and kwargs["requested_by"] == "submitter"
        response = await client.post("/api/literature/papers/public/enrich")
        assert response.status_code == 200 and response.json()["status"] == 200 and response.json()["ok"]

async def test_total_deadline_also_bounds_slow_doi_backfill(environment, monkeypatch):
    sessions, _user, calls = environment
    async with sessions() as db:
        (await db.get(LiteraturePaper, 1)).doi = None
    cancelled = asyncio.Event()

    async def slow_backfill(*_args):
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()

    monkeypatch.setattr(enrich, "_ENRICH_DEADLINE_SECONDS", 0.02)
    monkeypatch.setattr(enrich, "_backfill_doi_from_document", slow_backfill)
    started = time.monotonic()
    result = await enrich.enrich_paper(7, "public")
    assert time.monotonic() - started < 1
    assert result == {"rid": "public", "ok": False, "reason": "timeout"}
    assert cancelled.is_set() and not calls
