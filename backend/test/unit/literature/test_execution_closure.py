"""Production-shaped progress, current-version, ACL and vector-channel regressions."""

from contextlib import asynccontextmanager
import io
import json
from types import SimpleNamespace
import zipfile

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.literature import (
    artifacts,
    cleaning,
    enrich,
    export_vault,
    navigation_index,
    reader_bundle,
    retrieval,
)
from yuxi.literature.access import LiteratureAccess, _access
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeParseRevision,
    KnowledgeParseArtifact,
    KnowledgeWiki,
    LiteratureArtifactRevision,
    LiteratureCompileJob,
    LiteratureNavigationIndex,
    LiteratureLibraryRun,
    LiteratureCleaningPlan,
    LiteratureCitation,
    LiteratureJournalMetric,
    LiteraturePaper,
    LiteratureHistoryItem,
    LiteratureNoteSearch,
    LiteratureAttachment,
    LiteratureReport,
    LiteratureReaderRender,
    LiteratureReviewDecision,
    LiteratureCard,
    LiteratureDiaryEntry,
    LiteratureTranslationJob,
    LiteratureVariant,
    LiteratureWorkflowRun,
    WikiPublication,
    WikiSourceBinding,
)

pytestmark = pytest.mark.unit
TENANT, RID, REVISION = 19, "doi-closure", "sir_closure"


async def test_reader_target_context_product_and_exact_precedence(env):
    from yuxi.literature.management import resolve_reader_target

    db, _, _, _ = env
    db.add(LiteraturePaper(id=7, tenant_id=TENANT, rid="same-title", title=RID))
    await db.commit()
    result = await resolve_reader_target(TENANT, "./_wiki.md#概念", source_rid=RID)
    assert result == {"rid": RID, "tab": "noteL2", "variant": None, "anchor": "概念", "ambiguous": False}
    assert (await resolve_reader_target(TENANT, RID))["rid"] == RID
    with pytest.raises(ValueError, match="上下文"):
        await resolve_reader_target(TENANT, "./zh.md")
    with pytest.raises(ValueError, match="无权"):
        await resolve_reader_target(TENANT, "./_note", source_rid="hidden")
    assert (await resolve_reader_target(TENANT, "../hidden/_note", source_rid=RID))["rid"] is None


async def test_business_backup_fail_closed_on_mutation_and_missing_products(env, monkeypatch):
    from yuxi.literature import backup

    db, storage, _, _ = env
    monkeypatch.setattr(backup, "get_minio_client", lambda: storage)

    async def vault(*args, **kwargs):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("_manifest.json", '{"warnings": [{"reason": "missing"}]}')
        return output.getvalue()

    monkeypatch.setattr(backup, "export_obsidian_vault", vault)
    with pytest.raises(ValueError, match="不完整"):
        await backup.export_business_backup(TENANT, "u1")
    with pytest.raises(PermissionError):
        await backup.export_business_backup(TENANT, "someone")

    async def changed(*args, **kwargs):
        paper = await db.get(LiteraturePaper, 1)
        paper.title = "Changed mid-export"
        await db.commit()
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("_manifest.json", '{"warnings": []}')
        return output.getvalue()

    monkeypatch.setattr(backup, "export_obsidian_vault", changed)
    with pytest.raises(ValueError, match="数据变更"):
        await backup.export_business_backup(TENANT, "u1")


@pytest.mark.parametrize("changed", ["none", "card", "reading", "uri"])
async def test_fulltext_projection_queries_without_minio_and_rejects_stale(env, changed):
    from yuxi.literature import fulltext

    db, storage, monkeypatch, _ = env
    job = LiteratureCompileJob(
        id=1,
        tenant_id=TENANT,
        paper_id=1,
        level="L1",
        status="SUCCEEDED",
        source_revision_id=REVISION,
        reading_version=1,
        card_revision=0,
        note_uri="kbasset://search-note",
    )
    hidden = LiteratureCompileJob(
        id=2,
        tenant_id=TENANT,
        paper_id=2,
        level="L1",
        status="SUCCEEDED",
        source_revision_id="",
        reading_version=1,
        card_revision=0,
        note_uri="kbasset://hidden-note",
    )
    db.add_all([job, hidden])
    await db.flush()
    body = "background " * 20000 + "Starch 淀粉 biosynthesis footer"
    await fulltext.project_note(db, job, body)
    await fulltext.project_note(db, hidden, "Hidden starch 淀粉 result")
    paper = await db.get(LiteraturePaper, 1)
    if changed == "card":
        paper.card_revision += 1
    elif changed == "reading":
        paper.reading_version += 1
    elif changed == "uri":
        job.note_uri = "kbasset://new-note"
    await db.commit()

    def forbidden():
        raise AssertionError("A fulltext query must not fetch object-store corpus")

    monkeypatch.setattr(retrieval, "_get_minio", forbidden)
    english = await retrieval.search_compiled(TENANT, "starch")
    chinese = await retrieval.search_compiled(TENANT, "淀粉")
    expected = [RID] if changed == "none" else []
    assert [hit["rid"] for hit in english["hits"]] == expected
    assert [hit["rid"] for hit in chinese["hits"]] == expected
    if expected:
        assert "footer" in english["hits"][0]["note_excerpt"]
        assert len(english["hits"][0]["note_excerpt"]) <= 400
    assert not storage.files


@pytest.mark.parametrize("session_kind", ["paper", "chat"])
async def test_native_graph_candidate_cache_preserves_messages_usage_and_current_gates(env, session_kind):
    from dataclasses import dataclass
    from langchain.agents import create_agent
    from langchain.agents.middleware.types import AgentMiddleware
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage, HumanMessage
    from yuxi.agents.middlewares.literature_cache import LiteratureCandidateCacheMiddleware
    from yuxi.agents.middlewares.token_usage import TokenUsageMiddleware
    from yuxi.literature import answer_cache
    from yuxi.storage.postgres.models_business import AgentRun, User

    db, _, monkeypatch, _ = env
    db.add(User(id=1, uid="u1", username="u1", password_hash="fixture-only"))
    db.add(
        AgentRun(
            id="cache-run",
            uid="u1",
            tenant_id=TENANT,
            agent_slug="default-chatbot",
            conversation_thread_id="cache-thread",
            request_id="cache-request",
            status="running",
            run_type="chat",
            input_payload={"policy_version": 1},
        )
    )
    await db.commit()

    async def tenant(_db, _uid):
        return TENANT

    monkeypatch.setattr("yuxi.services.principal.resolve_tenant_id", tenant)
    values = {}

    async def read(prompt, **opts):
        return values.get(answer_cache._cache_key(prompt, **opts))

    async def write(prompt, answer, **opts):
        values[answer_cache._cache_key(prompt, **opts)] = answer

    monkeypatch.setattr(answer_cache, "cache_get", read)
    monkeypatch.setattr(answer_cache, "cache_set", write)
    trace_hits = []

    def emit(**kwargs):
        from yuxi.trace.protocol import build_trace_event

        trace_hits.append(build_trace_event(run_id="cache-run", thread_id="cache-thread", **kwargs))

    monkeypatch.setattr("yuxi.trace.emit_trace", emit)

    @dataclass
    class Context:
        uid: str = "u1"
        run_id: str = "cache-run"
        model: str = "test:chat"
        _literature_session_context: dict | None = None

    context = Context(
        _literature_session_context={
            "session_kind": session_kind,
            "tenant_id": TENANT,
            "bound_rid": RID if session_kind == "paper" else "",
            "source_revision_id": REVISION,
            "reading_version": 1,
        }
    )
    gate_calls = []

    class CurrentGate(AgentMiddleware):
        async def awrap_model_call(self, request, handler):
            response = await handler(request)
            gate_calls.append(response.result[0].content)
            return response

    model = FakeMessagesListChatModel(responses=[AIMessage(content="An untrusted generation candidate.")])
    graph = create_agent(
        model=model,
        tools=[],
        context_schema=Context,
        middleware=[CurrentGate(), TokenUsageMiddleware(), LiteratureCandidateCacheMiddleware()],
    )
    input_state = {"messages": [HumanMessage(content="Summarize this paper")]}
    first = await graph.ainvoke(input_state, context=context)
    events = [event async for event in graph.astream(input_state, context=context, stream_mode=["messages", "values"])]
    second = [data for mode, data in events if mode == "values"][-1]
    assert len(values) == 1 and len(gate_calls) == 2
    assert second["messages"][-1].additional_kwargs["literature_candidate_cache"]["hit"]
    assert first["messages"][-1].id != second["messages"][-1].id
    assert second["token_usage"]["model_usage"]["total_tokens"] == 0
    assert trace_hits[0]["event_type"] == "model.cache.hit"
    assert trace_hits[0]["attributes"] == {"cache_hit": True}
    assert any(mode == "messages" and "generation candidate" in str(data[0].content) for mode, data in events)
    paper = await db.get(LiteraturePaper, 1)
    paper.card_revision += 1
    await db.commit()
    third = await graph.ainvoke(input_state, context=context)
    assert not third["messages"][-1].additional_kwargs.get("literature_candidate_cache")
    assert len(values) == 2
    user = await db.get(User, 1)
    user.is_disabled = 1
    await db.commit()
    with pytest.raises(PermissionError, match="用户"):
        await graph.ainvoke(input_state, context=context)


async def test_fulltext_reconcile_backs_off_bad_artifacts_and_keeps_other_jobs(env):
    from yuxi.literature import fulltext

    db, _, monkeypatch, _ = env
    jobs = [
        LiteratureCompileJob(
            id=1,
            tenant_id=TENANT,
            paper_id=1,
            level="L1",
            status="SUCCEEDED",
            source_revision_id=REVISION,
            reading_version=1,
            card_revision=0,
            note_uri="kbasset://broken",
        ),
        LiteratureCompileJob(
            id=2,
            tenant_id=TENANT,
            paper_id=2,
            level="L1",
            status="SUCCEEDED",
            source_revision_id="",
            reading_version=1,
            card_revision=0,
            note_uri="kbasset://valid",
        ),
    ]
    db.add_all(jobs)
    await db.commit()
    calls = []

    async def sync(job_id):
        calls.append(job_id)
        if job_id == 1:
            raise OSError("private object endpoint must not enter diagnostics")
        await fulltext.project_note(db, jobs[1], "Valid scientific note")
        await db.commit()
        return True

    monkeypatch.setattr(fulltext, "sync_note", sync)
    assert await fulltext.reconcile_literature_fulltext() == 1
    failed = await db.get(LiteratureNoteSearch, 1)
    assert failed.error_code == "OSError" and failed.attempt == 1 and failed.next_retry_at is not None
    assert failed.note_uri == ""
    assert await fulltext.reconcile_literature_fulltext() == 0
    assert sorted(calls) == [1, 2]
    await fulltext.project_note(db, jobs[0], "A repaired scientific note")
    await db.commit()
    assert failed.attempt == 0 and failed.error_code is None and failed.next_retry_at is None


async def test_fulltext_does_not_recall_superseded_import_generator(env):
    from yuxi.literature import fulltext

    db, _, _, _ = env
    old = LiteratureCompileJob(
        id=1,
        tenant_id=TENANT,
        paper_id=1,
        level="L1",
        status="SUCCEEDED",
        source_revision_id=REVISION,
        reading_version=1,
        card_revision=0,
        generator_version="paperagent-import-v4",
        note_uri="kbasset://imported",
    )
    new = LiteratureCompileJob(
        id=2,
        tenant_id=TENANT,
        paper_id=1,
        level="L1",
        status="SUCCEEDED",
        source_revision_id=REVISION,
        reading_version=1,
        card_revision=0,
        note_uri="kbasset://native",
    )
    db.add_all([old, new])
    await db.flush()
    await fulltext.project_note(db, old, "A historical obsolete starch concept")
    await fulltext.project_note(db, new, "Current sucrose synthesis science")
    await db.commit()
    assert (await retrieval.search_compiled(TENANT, "starch"))["hits"] == []
    assert [hit["rid"] for hit in (await retrieval.search_compiled(TENANT, "sucrose"))["hits"]] == [RID]


async def test_search_cache_is_actual_versioned_recall_and_rejects_revocation(env):
    from yuxi.literature import search_runtime
    from yuxi.storage import redis
    from dataclasses import replace

    db, _, monkeypatch, scope = env
    values, calls = {}, []

    class Redis:
        async def get(self, key):
            return values.get(key)

        async def setex(self, key, ttl, value):
            assert ttl == 300
            values[key] = value

    async def client():
        return Redis()

    async def vector(*args, **kwargs):
        return [], "not configured"

    original = retrieval._search_catalog

    async def recall(*args, **kwargs):
        calls.append(1)
        return await original(*args, **kwargs)

    monkeypatch.setattr(redis, "get_async_redis_client", client)
    monkeypatch.setattr(navigation_index, "search_navigation_vectors", vector)
    monkeypatch.setattr(retrieval, "_search_catalog", recall)
    first = await retrieval.search_catalog(TENANT, "visible", use_cache=True)
    second = await retrieval.search_catalog(TENANT, "visible", use_cache=True)
    assert first[0]["rid"] == RID and second[0]["cache_hit"] and len(calls) == 1
    paper = await db.get(LiteraturePaper, 1)
    paper.card_revision += 1
    await db.commit()
    await retrieval.search_catalog(TENANT, "visible", use_cache=True)
    assert len(calls) == 2

    async def revoked(*args):
        return replace(scope, rids=frozenset(), paper_ids=frozenset())

    monkeypatch.setattr(search_runtime, "build_access", revoked)
    with pytest.raises(ValueError, match="权限已变更"):
        await retrieval.search_catalog(TENANT, "visible", use_cache=True)


async def test_wiki_link_resolution_never_returns_hidden_rid(env):
    from yuxi.literature.management import resolve_reader_target

    assert (await resolve_reader_target(TENANT, "Visible paper"))["rid"] == RID
    assert (await resolve_reader_target(TENANT, "hidden"))["rid"] is None


@pytest.mark.parametrize("case", ["retry", "long_backoff", "redirect", "oversize", "invalid", "network"])
async def test_metadata_http_boundary_is_bounded_and_does_not_forward_secrets(monkeypatch, case):
    import httpx

    calls = []
    client_class = httpx.AsyncClient

    def handle(request):
        calls.append(request)
        assert request.url.host == "api.semanticscholar.org"
        if case == "network" and len(calls) == 1:
            raise httpx.ConnectError("provider error may contain a credential", request=request)
        if case == "retry" and len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        if case == "long_backoff":
            return httpx.Response(429, headers={"Retry-After": "9999"})
        if case == "redirect":
            return httpx.Response(302, headers={"Location": "https://untrusted.example/collect"})
        if case == "oversize":
            return httpx.Response(200, content=b"x" * (enrich._MAX_RESPONSE_BYTES + 1))
        if case == "invalid":
            return httpx.Response(200, json=["not a metadata object"])
        return httpx.Response(200, json={"title": "A bounded scientific metadata result"})

    def factory(**kwargs):
        assert kwargs["follow_redirects"] is False
        return client_class(**kwargs, transport=httpx.MockTransport(handle))

    async def no_sleep(delay):
        assert delay <= 2

    monkeypatch.setattr(enrich.httpx, "AsyncClient", factory)
    monkeypatch.setattr(enrich.asyncio, "sleep", no_sleep)
    result = await enrich._get_json(
        "https://api.semanticscholar.org/graph/v1/paper/DOI:10.1/example", headers={"x-api-key": "fixture-only"}
    )
    assert bool(result) == (case in {"retry", "network"})
    assert len(calls) == (2 if case in {"retry", "network"} else 1)


@pytest.mark.parametrize("failure", [None, "text_only", "digest", "version", "revoked", "parse_only", "status_changed"])
async def test_canonical_parse_reading_fallback_is_verified_and_not_reviewed(env, failure):
    import hashlib
    from dataclasses import replace

    db, storage, monkeypatch, scope = env
    content = b"# Scientific source\n\nMeaningful scientific PDF content for reading."
    storage.files["canonical.md"] = content
    db.add(
        KnowledgeParseArtifact(
            id=1,
            artifact_id="canonical-artifact",
            revision_id=REVISION,
            kind="indexed_markdown",
            object_uri="minio://knowledgebases/canonical.md",
            sha256="f" * 64 if failure == "digest" else hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
        )
    )
    if failure == "parse_only":
        revision = await db.get(KnowledgeParseRevision, 1)
        revision.status = "PARSED_ONLY"
        revision.processing_mode = "parse_only"
    elif failure == "text_only":
        (await db.get(KnowledgeParseRevision, 1)).status = "INDEXED_TEXT_ONLY"
    await db.commit()
    original = storage.adownload_file

    async def downloaded(bucket, name):
        if failure == "version":
            paper = await db.get(LiteraturePaper, 1)
            paper.reading_version += 1
            await db.commit()
        elif failure == "status_changed":
            (await db.get(KnowledgeParseRevision, 1)).status = "REJECTED"
            await db.commit()
        return await original(bucket, name)

    monkeypatch.setattr(storage, "adownload_file", downloaded)
    if failure == "revoked":

        async def revoked(*args, **kwargs):
            return replace(scope, rids=frozenset())

        monkeypatch.setattr("yuxi.literature.access.build_access", revoked)
    if failure in {"digest", "version", "revoked", "status_changed"}:
        with pytest.raises((ValueError, PermissionError)):
            await reader_bundle.get_reader_bundle(TENANT, RID)
    else:
        result = await reader_bundle.get_reader_bundle(TENANT, RID)
        if failure == "parse_only":
            assert result["ready"] is False
        else:
            assert result["ready"] and result["source"] == "scientific_parse"
            assert result["knowledge_base_id"] == "kb-closure"
            assert result["resolved"] is False and result["frozen"] is False


@pytest.mark.parametrize("failure", [None, "provider", "duplicate", "nonfinite", "revoked"])
async def test_rerank_is_governed_strict_and_closes_on_failure(env, failure):
    from dataclasses import replace
    from yuxi.agents import models
    from yuxi.literature import llm_gateway, search_runtime
    from yuxi.models import rerank as adapters
    from yuxi.models.providers.cache import model_cache

    _, _, monkeypatch, scope = env
    calls = []
    usage = SimpleNamespace(input_tokens=0, estimated=False)

    async def freeze(db, **kwargs):
        assert kwargs["uid"] == "u1" and kwargs["tenant_id"] == TENANT
        assert kwargs["model_type"] == "rerank" and kwargs["operation"] == "navigation_rerank"
        calls.append("freeze")
        return SimpleNamespace(model_spec="fixture:rerank")

    @asynccontextmanager
    async def governed(policy):
        calls.append("governed")
        token = llm_gateway._usage_context.set(usage)
        try:
            yield
        finally:
            llm_gateway._usage_context.reset(token)
            calls.append("settled")

    class Model:
        def __init__(self, **kwargs):
            assert kwargs["api_key"] == "override-fixture"

        def _extract_results(self, payload):
            return payload

        async def _batch_rerank(self, query, documents, **kwargs):
            assert query == "query" and len(documents) == 1
            calls.append("provider")
            if failure == "provider":
                raise RuntimeError("provider failed")
            self._extract_results([{"index": 0, "relevance_score": 0.8}] * (2 if failure == "duplicate" else 1))
            return [float("nan") if failure == "nonfinite" else 0.8]

        async def aclose(self):
            calls.append("closed")

    monkeypatch.setattr(llm_gateway, "freeze_literature_policy", freeze)
    monkeypatch.setattr(llm_gateway, "governed_runtime", governed)
    info = SimpleNamespace(model_type="rerank", model_id="fixture", api_key="unused", base_url="fixture", extra={})
    monkeypatch.setattr(model_cache, "get_model_info", lambda _: info)
    monkeypatch.setattr(
        models, "apply_credential_override", lambda i: SimpleNamespace(**{**vars(i), "api_key": "override-fixture"})
    )
    monkeypatch.setattr(adapters, "OpenAIReranker", Model)
    fresh_count = 0

    async def fresh(*args):
        nonlocal fresh_count
        fresh_count += 1
        return replace(scope, rids=frozenset()) if failure == "revoked" and fresh_count > 1 else scope

    monkeypatch.setattr(search_runtime, "build_access", fresh)
    request = search_runtime.rerank(TENANT, "query", [{"rid": RID, "title": "Visible"}], "fixture:rerank")
    if failure:
        with pytest.raises((ValueError, RuntimeError)):
            await request
    else:
        assert (await request)[0]["rerank_score"] == 0.8
        assert usage.estimated and usage.input_tokens > 0
    assert calls == ["freeze", "governed", "provider", "closed", "settled"]


class Storage:
    def __init__(self):
        self.files = {}

    async def adownload_file(self, bucket, name):
        if name not in self.files:
            raise FileNotFoundError(name)
        return self.files[name]

    async def aupload_file(self, **kwargs):
        self.files[kwargs["object_name"]] = bytes(kwargs["data"])


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        from yuxi.storage.postgres.models_business import User, Conversation, Message, AgentRun

        await connection.run_sync(User.__table__.create)
        await connection.run_sync(Conversation.__table__.create)
        await connection.run_sync(Message.__table__.create)
        await connection.run_sync(AgentRun.__table__.create)
        for model in (
            KnowledgeParseRevision,
            KnowledgeParseArtifact,
            KnowledgeWiki,
            LiteraturePaper,
            LiteratureHistoryItem,
            LiteratureAttachment,
            LiteratureReport,
            LiteratureArtifactRevision,
            LiteratureCompileJob,
            LiteratureNoteSearch,
            LiteratureNavigationIndex,
            LiteratureLibraryRun,
            LiteratureCleaningPlan,
            LiteratureCitation,
            LiteratureJournalMetric,
            LiteratureReaderRender,
            LiteratureReviewDecision,
            LiteratureCard,
            LiteratureDiaryEntry,
            LiteratureTranslationJob,
            LiteratureVariant,
            LiteratureWorkflowRun,
            WikiPublication,
            WikiSourceBinding,
        ):
            await connection.run_sync(model.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:

        @asynccontextmanager
        async def session_context():
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise

        db.add(
            LiteraturePaper(
                id=1, tenant_id=TENANT, rid=RID, kind="paper", title="Visible paper", source_revision_id=REVISION
            )
        )
        db.add(LiteraturePaper(id=2, tenant_id=TENANT, rid="hidden", kind="paper", title="Private paper"))
        db.add(
            KnowledgeParseRevision(
                id=1,
                tenant_id=TENANT,
                revision_id=REVISION,
                kb_id="kb-closure",
                file_id="file-closure",
                source_sha256="a" * 64,
                parser_fingerprint="fp",
                pipeline_version="test",
                status="ACTIVE",
            )
        )
        await db.commit()
        # pg_manager 是进程级单例：patch 单例属性即覆盖全部文献模块的会话来源
        from yuxi.storage.postgres.manager import pg_manager

        monkeypatch.setattr(pg_manager, "get_async_session_context", session_context)
        storage = Storage()
        for module in (artifacts, reader_bundle, export_vault):
            monkeypatch.setattr(module, "get_minio_client", lambda: storage)
        monkeypatch.setattr(retrieval, "_get_minio", lambda: storage)

        async def enqueue(_run_id):
            return True

        scope = LiteratureAccess(TENANT, frozenset({RID}), frozenset({1}), {}, {}, SimpleNamespace(uid="u1"))
        token = _access.set(scope)

        async def fresh_access(_user, _tenant, *, include_trashed=False, rids=None):
            from dataclasses import replace

            papers = list(
                (await db.execute(select(LiteraturePaper).where(LiteraturePaper.rid.in_(scope.rids)))).scalars()
            )
            visible = frozenset(p.rid for p in papers if include_trashed or p.trashed_at is None)
            if rids is not None:
                visible &= frozenset(rids)
            return replace(scope, rids=visible, paper_ids=frozenset(p.id for p in papers if p.rid in visible))

        monkeypatch.setattr("yuxi.literature.access.build_access", fresh_access)
        monkeypatch.setattr("yuxi.literature.management.build_access", fresh_access)
        monkeypatch.setattr("yuxi.literature.qna.build_access", fresh_access)
        for module in ("products", "search_runtime"):
            monkeypatch.setattr(f"yuxi.literature.{module}.build_access", fresh_access)
        for module in ("products",):
            monkeypatch.setattr(f"yuxi.literature.{module}.get_minio_client", lambda: storage)
        monkeypatch.setattr(cleaning, "build_access", fresh_access)
        try:
            yield db, storage, monkeypatch, scope
        finally:
            _access.reset(token)
    await engine.dispose()


async def test_directory_filters_use_current_compile_identity_and_escape_like(env):
    from yuxi.literature import readops

    db, _, _, _ = env
    paper = await db.get(LiteraturePaper, 1)
    paper.pub_year, paper.journal = 2025, "Journal_Exact"
    db.add(
        LiteratureCompileJob(
            id=1,
            tenant_id=TENANT,
            paper_id=1,
            level="L1",
            source_revision_id=REVISION,
            reading_version=1,
            card_revision=0,
            status="SUCCEEDED",
            generator_version="literature-compile-v1",
        )
    )
    await db.commit()
    matching = await readops.list_papers(TENANT, journal="_Exact", year_min=2020, compiled_level="L1", source="pdf")
    assert matching["total"] == 1 and matching["items"][0]["pub_year"] == 2025
    assert (await readops.list_papers(TENANT, journal="%", source="pdf"))["total"] == 0
    paper.card_revision += 1
    await db.commit()
    assert (await readops.list_papers(TENANT, compiled_level="L1"))["total"] == 0
    assert (await readops.list_papers(TENANT, compiled_level="missing"))["total"] == 1
    with pytest.raises(ValueError):
        await readops.list_papers(TENANT, sort="sql injection")


async def test_session_list_owner_tenant_and_resource_acl(env):
    from yuxi.literature import session
    from yuxi.storage.postgres.models_business import Conversation

    db, _, _, _ = env
    for idx, uid, tenant, status, binding in (
        (1, "u1", TENANT, "active", {"kind": "paper", "rid": RID}),
        (2, "u2", TENANT, "active", {"kind": "global-qa"}),
        (3, "u1", TENANT + 1, "active", {"kind": "global-qa"}),
        (4, "u1", TENANT, "active", {"kind": "paper", "rid": "hidden"}),
        (5, "u1", TENANT, "archived", {"kind": "lit"}),
        (6, "u1", TENANT, "active", None),
    ):
        db.add(
            Conversation(
                id=idx,
                thread_id=f"thread-{idx}",
                uid=uid,
                tenant_id=tenant,
                agent_id="default-chatbot",
                status=status,
                title=f"标题 {idx}",
                extra_metadata={"literature_session": binding} if binding else {},
            )
        )
    await db.commit()
    assert [row["id"] for row in await session.list_literature_sessions(TENANT, "u1")] == ["thread-1"]
    assert [row["id"] for row in await session.list_literature_sessions(TENANT, "u1", archived=True)] == ["thread-5"]
    await session.update_literature_session(TENANT, "u1", "thread-1", status="archived")
    assert not await session.list_literature_sessions(TENANT, "u1")
    with pytest.raises(ValueError):
        await session.update_literature_session(TENANT, "u1", "thread-2", status="archived")
    with pytest.raises(PermissionError, match="撤销"):
        await session.update_literature_session(TENANT, "u1", "thread-4", status="archived")


async def test_diary_export_month_and_uid_isolation(env):
    from datetime import date
    from yuxi.literature import diary

    db, _, monkeypatch, scope = env

    async def unchanged_access(*args, **kwargs):
        return scope

    # This fixture isolates diary storage, not the knowledge-source ACL tables.
    # Revocation at delivery is exercised by test_semantic_parity independently.
    monkeypatch.setattr(diary, "build_access", unchanged_access)
    for idx, uid, day, text in (
        (1, "u1", "2026-10-03", "十月心得"),
        (2, "u2", "2026-10-03", "其他人"),
        (3, "u1", "2026-09-30", "九月心得"),
    ):
        db.add(
            LiteratureDiaryEntry(id=idx, tenant_id=TENANT, uid=uid, entry_date=date.fromisoformat(day), content=text)
        )
    await db.commit()
    result = await diary.export_month(TENANT, "u1", "2026-10")
    assert "十月心得" in result and "其他人" not in result and "九月心得" not in result
    with pytest.raises(ValueError):
        await diary.export_month(TENANT, "u1", "2026-13")


async def test_restore_http_boundary_allows_trash_only_for_restore(env):
    import httpx
    from fastapi import FastAPI
    import server.routers.literature_router as router

    db, _, monkeypatch, scope = env
    paper = await db.get(LiteraturePaper, 1)
    from yuxi.utils.datetime_utils import utc_now

    paper.trashed_at = utc_now()
    await db.commit()

    async def tenant(_user):
        return TENANT

    monkeypatch.setattr(router, "_current_tenant", tenant)
    app = FastAPI()
    app.include_router(router.literature, prefix="/api")
    app.dependency_overrides[router.get_required_user] = lambda: scope.user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get(f"/api/literature/papers/{RID}")).status_code == 404
        restored = await client.post(f"/api/literature/library/trash/{RID}/restore")
        assert restored.status_code == 200 and restored.json()["restored"]
        assert (await client.post("/api/literature/library/trash/hidden/restore")).status_code == 404


async def test_paper_evidence_binding_never_expands_agent_scope(env):
    from yuxi.literature import session

    _, _, _, scope = env
    source = {
        "kb_id": "kb-closure",
        "file_id": "file-closure",
        "parse_revision_id": REVISION,
        "reading_version": 1,
        "imported": False,
    }
    scope.sources[RID] = source
    snapshot = {"effective_kb_ids": ["kb-closure", "other"], "members": [{"kb_id": "kb-closure"}, {"kb_id": "other"}]}
    narrowed, mentions = await session.bind_paper_evidence_scope(
        user=scope.user, tenant_id=TENANT, binding={"kind": "paper", "rid": RID}, snapshot=snapshot, mentions={}
    )
    assert narrowed["effective_kb_ids"] == ["kb-closure"]
    assert narrowed["members"] == [{"kb_id": "kb-closure"}]
    assert mentions["document_ids"] == ["file-closure"]
    assert narrowed["document_scope_ceiling"] == {"file_ids": ["file-closure"]}
    with pytest.raises(ValueError, match="其他文献"):
        await session.bind_paper_evidence_scope(
            user=scope.user,
            tenant_id=TENANT,
            binding={"kind": "paper", "rid": RID},
            snapshot=snapshot,
            mentions={"document_ids": ["other-file"]},
        )
    empty, mentions = await session.bind_paper_evidence_scope(
        user=scope.user,
        tenant_id=TENANT,
        binding={"kind": "paper", "rid": RID},
        snapshot={"effective_kb_ids": ["other"]},
        mentions={},
    )
    assert not empty["effective_kb_ids"] and not mentions["document_ids"]
    await session.validate_frozen_paper_scope(user=scope.user, tenant_id=TENANT, snapshot=narrowed)
    scope.sources[RID] = {**source, "parse_revision_id": "new-revision"}
    with pytest.raises(PermissionError, match="版本已变更"):
        await session.validate_frozen_paper_scope(user=scope.user, tenant_id=TENANT, snapshot=narrowed)
    scope.sources[RID] = {**source, "file_id": "rebound-file"}
    with pytest.raises(PermissionError, match="版本已变更"):
        await session.validate_frozen_paper_scope(user=scope.user, tenant_id=TENANT, snapshot=narrowed)


async def test_qna_writeback_filters_internal_and_failed_messages_and_is_idempotent(env):
    from yuxi.literature import qna, cards
    from yuxi.storage.postgres.models_business import Conversation, Message, AgentRun

    db, _, _, _ = env
    db.add(
        Conversation(
            id=1,
            thread_id="qna-own",
            tenant_id=TENANT,
            uid="u1",
            agent_id="default-chatbot",
            title="本文问答",
            extra_metadata={"literature_session": {"kind": "paper", "rid": RID}},
        )
    )
    db.add(
        Conversation(
            id=2,
            thread_id="qna-other",
            tenant_id=TENANT,
            uid="u2",
            agent_id="default-chatbot",
            title="其他人",
            extra_metadata={"literature_session": {"kind": "paper", "rid": RID}},
        )
    )
    db.add(
        AgentRun(
            id="failed-qa",
            tenant_id=TENANT,
            conversation_thread_id="qna-own",
            agent_slug="default-chatbot",
            uid="u1",
            request_id="failed",
            status="failed",
        )
    )
    db.add_all(
        [
            Message(id=1, conversation_id=1, role="user", content="请解释"),
            Message(id=2, conversation_id=1, role="assistant", content="<think>内部推理</think>最终回答"),
            Message(id=3, conversation_id=1, role="assistant", message_type="tool_result", content="内部工具数据"),
            Message(id=4, conversation_id=1, role="assistant", content="失败答案", run_id="failed-qa"),
        ]
    )
    await db.commit()
    preview = await qna.answer_preview(TENANT, "u1", "qna-own")
    assert preview["message_id"] == 2
    assert "最终回答" in preview["content"] and "内部推理" not in preview["content"]
    assert "内部工具数据" not in preview["content"] and "失败答案" not in preview["content"]
    result = await qna.writeback(TENANT, "u1", "qna-own", message_id=2, expected_digest=preview["digest"])
    assert not result["reused"]
    assert (await qna.writeback(TENANT, "u1", "qna-own", message_id=2, expected_digest=preview["digest"]))["reused"]
    assert len(await cards.list_cards(TENANT, RID)) == 1
    with pytest.raises(ValueError):
        await qna.answer_preview(TENANT, "u1", "qna-other")
    with pytest.raises(cards.CardConflictError):
        await qna.writeback(TENANT, "u1", "qna-own", message_id=2, expected_digest="0" * 64)


async def test_clean_preview_protects_mains_locks_and_other_sources(env):
    db, _, _, _ = env
    paper = await db.get(LiteraturePaper, 1)
    paper.pub_year = 2000
    await db.commit()
    protected = await cleaning.preview_cleaning(TENANT, rules={"year": {"min": 2020}})
    assert protected["to_remove"] == 0
    paper.is_reference = True
    paper.meta_locked = True
    await db.commit()
    locked = await cleaning.preview_cleaning(TENANT, rules={"year": {"min": 2020}})
    assert locked["to_remove"] == 0
    paper.meta_locked = False
    await db.commit()
    preview = await cleaning.preview_cleaning(TENANT, rules={"year": {"min": 2020}})
    assert preview["to_remove"] == 1
    assert [sample["rid"] for sample in preview["samples"]] == [RID]
    assert (await cleaning.confirm_cleaning(TENANT, preview["plan_id"], preview["plan_hash"]))["removed"] == 1
    assert await reader_bundle.get_reader_bundle(TENANT, RID) is None
    assert [item["rid"] for item in await cleaning.list_trash(TENANT)] == [RID]
    await cleaning.restore_paper(TENANT, RID)
    assert (await db.get(LiteraturePaper, 1)).trashed_at is None


async def test_clean_confirmation_fails_atomic_on_changed_input(env):
    db, _, _, _ = env
    paper = await db.get(LiteraturePaper, 1)
    paper.pub_year = 2000
    paper.is_reference = True
    await db.commit()
    preview = await cleaning.preview_cleaning(TENANT, rules={"year": {"min": 2020}})
    paper.reading_version += 1
    await db.commit()
    with pytest.raises(ValueError, match="版本已变更"):
        await cleaning.confirm_cleaning(TENANT, preview["plan_id"], preview["plan_hash"])
    await db.refresh(paper)
    assert paper.trashed_at is None
    with pytest.raises(ValueError, match="哈希"):
        await cleaning.confirm_cleaning(TENANT, preview["plan_id"], "wrong")


async def test_enrich_human_lock_after_provider_call_protects_every_field(env):
    db, _, monkeypatch, _ = env
    paper = await db.get(LiteraturePaper, 1)
    paper.doi, paper.title = "10.1000/closure", None
    await db.commit()

    async def fetch(_doi):
        paper.meta_locked = True
        await db.commit()
        return {"title": "Do not overwrite", "journal": "J", "year": "2024"}

    async def empty(_doi):
        return None

    monkeypatch.setattr(enrich, "fetch_openalex", fetch)
    monkeypatch.setattr(enrich, "fetch_crossref", empty)
    monkeypatch.setattr(enrich, "fetch_semantic_scholar", empty)
    result = await enrich.enrich_paper(TENANT, RID)
    assert result["reason"] == "meta_locked"
    assert paper.title is None and paper.journal is None and paper.pub_year is None


@pytest.mark.parametrize("mutation", [None, "version", "lock", "doi"])
async def test_doi_backfill_reads_document_json_and_preserves_concurrent_edits(env, mutation):
    db, _, monkeypatch, _ = env
    paper = await db.get(LiteraturePaper, 1)
    original_version = paper.reading_version

    async def document(*args, **kwargs):
        if mutation == "version":
            paper.reading_version += 1
        elif mutation == "lock":
            paper.meta_locked = True
        elif mutation == "doi":
            paper.doi = "10.1000/manual"
        await db.commit()
        return {
            "content": json.dumps(
                {
                    "paragraphs": [
                        {"text_en": "DOI: 10.1000/article"},
                        {"text_en": "References\nhttps://doi.org/10.1000/reference"},
                    ]
                }
            ).encode()
        }

    monkeypatch.setattr(artifacts, "resolve_effective_document", document)
    result = await enrich._backfill_doi_from_document(TENANT, paper)
    if mutation in {"lock", "version"}:
        assert result is None and paper.doi is None
    else:
        assert result == ("10.1000/manual" if mutation == "doi" else "10.1000/article")
        assert paper.doi == result and paper.rid == RID and paper.source_revision_id == REVISION
        assert paper.reading_version == original_version


async def test_catalog_recalls_semantic_only_and_rechecks_visible_rids(env):
    _, _, monkeypatch, _ = env

    async def vector(*_args, **_kwargs):
        return [
            {"rid": RID, "excerpt": "navigation", "score": 0.9},
            {"rid": "hidden", "excerpt": "private", "score": 0.95},
        ], ""

    monkeypatch.setattr(navigation_index, "search_navigation_vectors", vector)
    hits = await retrieval.search_catalog(TENANT, "unrelated-query")
    assert [hit["rid"] for hit in hits] == [RID]
    assert hits[0]["channels_used"] == ["vector"]


def render_row(*, dual=False, status="RUNNING"):
    from yuxi.literature.reading.service import render_version_for

    return LiteratureReaderRender(
        id=1,
        tenant_id=TENANT,
        revision_id=REVISION,
        render_version=render_version_for(dual),
        status=status,
        markdown_uri=f"kbasset://tenants/{TENANT}/documents/{'a' * 64}/reader_render/{REVISION}/render.md",
    )


async def test_missing_dual_review_is_failure_not_empty_success(env):
    db, _, _, _ = env
    from yuxi.literature import review

    db.add(render_row(dual=True, status="SUCCEEDED"))
    await db.commit()
    with pytest.raises(ValueError, match="禁止绕过"):
        await review.get_review_items(TENANT, REVISION, require_dual=True)


async def test_human_pointer_drives_reader_and_first_save_cas(env):
    db, _, _, _ = env
    db.add(render_row(status="SUCCEEDED"))
    await db.commit()
    content = json.dumps({"paragraphs": [{"para_id": "P003", "text_en": "Manually corrected paragraph."}]}).encode()
    saved = await artifacts.store_revision(
        TENANT,
        RID,
        "document",
        content=content,
        content_type="application/json",
        operator="u1",
        freeze=True,
        expected_current_revision=None,
    )
    bundle = await reader_bundle.get_reader_bundle(TENANT, RID)
    assert bundle["markdown"] == "Manually corrected paragraph."
    assert bundle["frozen"] and bundle["source"] == "manual_frozen"
    assert bundle["revision_id"] == saved["revision_id"]
    with pytest.raises(artifacts.RevisionConflictError):
        await artifacts.store_revision(
            TENANT,
            RID,
            "document",
            content=content,
            content_type="application/json",
            operator="u1",
            freeze=True,
            expected_current_revision=None,
        )


@pytest.mark.parametrize("allowed", [True, False])
async def test_export_checks_image_owner_before_io_and_disambiguates_names(env, allowed):
    from urllib.parse import quote

    _, storage, monkeypatch, _ = env
    owner = 1 if allowed else 2
    objects = [f"tenants/{TENANT}/literature/attachments/{owner}/{kind}/F001.png" for kind in ("si", "note")]
    markdown = "\n".join(
        f"![Figure](/api/literature/assets?uri={quote('kbasset://' + name, safe='')})" for name in objects
    )
    for index, name in enumerate(objects):
        storage.files[name] = f"Figure {index}".encode()

    async def reader(*args):
        return {"ready": True, "markdown": markdown}

    monkeypatch.setattr(reader_bundle, "get_reader_bundle", reader)
    original = storage.adownload_file
    calls = []

    async def download(bucket, name):
        calls.append(name)
        return await original(bucket, name)

    monkeypatch.setattr(storage, "adownload_file", download)
    if not allowed:
        with pytest.raises(ValueError, match="无权访问"):
            await export_vault.export_obsidian_vault(TENANT)
        assert not calls
    else:
        archive = await export_vault.export_obsidian_vault(TENANT)
        with zipfile.ZipFile(io.BytesIO(archive)) as vault:
            images = [name for name in vault.namelist() if name.endswith("F001.png")]
            assert len(images) == 2
            assert {vault.read(name) for name in images} == {b"Figure 0", b"Figure 1"}
            rendered = vault.read(f"{RID}/render.md").decode()
            assert "/api/literature/assets" not in rendered


async def test_search_and_export_ignore_old_reading_version(env):
    db, storage, _, _ = env
    db.add(
        LiteratureCompileJob(
            id=1,
            tenant_id=TENANT,
            paper_id=1,
            level="L1",
            status="SUCCEEDED",
            source_revision_id=REVISION,
            reading_version=0,
            note_uri="kbasset://old",
        )
    )
    storage.files["old"] = b"private obsolete starch result"
    await db.commit()
    result = await retrieval.search_compiled(TENANT, "starch")
    assert result["hits"] == []
    # No rendered artifact exists: export metadata only, no stale note or hidden paper.
    data = await export_vault.export_obsidian_vault(TENANT)
    with zipfile.ZipFile(io.BytesIO(data)) as vault:
        assert not any("L1_note" in name or "hidden" in name for name in vault.namelist())
        assert len(json.loads(vault.read("_manifest.json"))["papers"]) == 1


async def test_semantic_only_vector_recall_and_acl_before_search(env):
    db, _, monkeypatch, _ = env
    from yuxi.literature import embedding_gateway

    _, manifest = await navigation_index._visible_inputs(TENANT)
    # The snapshot intentionally includes an inaccessible paper. Its ID must
    # never be passed to the vector store, not merely removed after retrieval.
    manifest["2"] = {"rid": "hidden", "fingerprint": "private", "note_uri": None}
    db.add(
        LiteratureNavigationIndex(
            id=1,
            index_id="lni-test",
            tenant_id=TENANT,
            requested_by="u1",
            model_spec="test:embedding",
            collection_name="literature_nav_test",
            status="SUCCEEDED",
            input_manifest=manifest,
        )
    )
    await db.commit()

    async def encode_query(tenant, model, query):
        assert (tenant, model, query) == (TENANT, "test:embedding", "semantic synonym")
        return [[1.0, 0.0]]

    def search(name, vector, paper_ids, channel, limit):
        assert paper_ids == [1] and channel == "catalog"
        return [{"entity": {"paper_id": 1, "rid": RID, "excerpt": "Semantic-only document"}, "distance": 0.9}]

    monkeypatch.setattr(embedding_gateway, "encode_query", encode_query)
    monkeypatch.setattr(navigation_index, "_vector_search", search)
    hits, reason = await navigation_index.search_navigation_vectors(TENANT, "semantic synonym", channel="catalog")
    assert [hit["rid"] for hit in hits] == [RID] and reason == ""
    paper = (await db.execute(select(LiteraturePaper).where(LiteraturePaper.id == 1))).scalar_one()
    paper.reading_version += 1
    await db.commit()
    hits, reason = await navigation_index.search_navigation_vectors(TENANT, "semantic synonym", channel="catalog")
    assert hits == [] and "当前权限/版本" in reason


async def test_render_is_candidate_only_after_human_freeze(env):
    db, storage, _, _ = env
    from yuxi.literature.reading import service

    paper = await db.get(LiteraturePaper, 1)
    content = json.dumps({"paragraphs": [{"para_id": "P007", "text_en": "Protected human text."}]}).encode()
    saved = await artifacts.store_revision(
        TENANT, RID, "document", content=content, content_type="application/json", operator="u1", freeze=True
    )
    version = paper.reading_version
    db.add(render_row(status="RUNNING"))
    await db.commit()
    auto = json.dumps({"paragraphs": [{"para_id": "P001", "text_en": "Automatic candidate."}]}).encode()
    uri = f"kbasset://tenants/{TENANT}/documents/{'a' * 64}/reader_render/{REVISION}/new/render.md"
    storage.files[uri.removeprefix("kbasset://").removesuffix("render.md") + "document.json"] = auto
    await service._finish_success(1, markdown_uri=uri, result={}, document_content=auto, expected_attempt=0)
    assert paper.reading_version == version and paper.artifact_pointers["document"] == saved["revision_id"]
    assert (await reader_bundle.get_reader_bundle(TENANT, RID))["markdown"] == "Protected human text."
    assert len(list((await db.execute(select(LiteratureArtifactRevision))).scalars())) == 2


async def test_citation_change_invalidates_cleaning_preview(env):
    db, _, _, _ = env
    paper = await db.get(LiteraturePaper, 1)
    paper.is_reference = True
    await db.commit()
    preview = await cleaning.preview_cleaning(TENANT, rules={"library_citations": {"min": 1}})
    db.add(LiteratureCitation(id=1, tenant_id=TENANT, citing_rid=RID, cited_doi=RID))
    await db.commit()
    with pytest.raises(ValueError, match="引用图"):
        await cleaning.confirm_cleaning(TENANT, preview["plan_id"], preview["plan_hash"])
    await db.refresh(paper)
    assert paper.trashed_at is None


async def test_export_fails_closed_when_archive_budget_is_exceeded(env):
    _, _, monkeypatch, _ = env
    monkeypatch.setattr(export_vault, "MAX_EXPORT_BYTES", 1)
    with pytest.raises(export_vault.ExportLimitError, match="安全预算"):
        await export_vault.export_obsidian_vault(TENANT)


async def test_export_rechecks_input_version_after_object_download(env):
    db, storage, monkeypatch, _ = env
    db.add(
        LiteratureCompileJob(
            id=1,
            tenant_id=TENANT,
            paper_id=1,
            level="L1",
            status="SUCCEEDED",
            source_revision_id=REVISION,
            reading_version=1,
            note_uri="kbasset://export-note",
        )
    )
    await db.commit()
    original = storage.adownload_file

    async def changed(bucket, name):
        if name == "export-note":
            paper = await db.get(LiteraturePaper, 1)
            paper.reading_version += 1
            await db.commit()
            return b"An obsolete navigation note."
        return await original(bucket, name)

    monkeypatch.setattr(storage, "adownload_file", changed)
    with pytest.raises(ValueError, match="版本已变更"):
        await export_vault.export_obsidian_vault(TENANT)


async def test_export_rechecks_recompile_generation_without_reading_change(env):
    db, storage, monkeypatch, _ = env
    db.add(
        LiteratureCompileJob(
            id=1,
            tenant_id=TENANT,
            paper_id=1,
            level="L1",
            status="SUCCEEDED",
            source_revision_id=REVISION,
            reading_version=1,
            note_uri="kbasset://export-note",
        )
    )
    await db.commit()
    original = storage.adownload_file

    async def recompiled(bucket, name):
        if name == "export-note":
            job = await db.get(LiteratureCompileJob, 1)
            job.note_uri = "kbasset://next-generation/L1_note.md"
            await db.commit()
            return b"Old generation that must not be delivered as current."
        return await original(bucket, name)

    monkeypatch.setattr(storage, "adownload_file", recompiled)
    with pytest.raises(ValueError, match="重编译或重译"):
        await export_vault.export_obsidian_vault(TENANT)


async def test_manual_trash_audits_and_roundtrips(env):
    """手动移入回收站：审计三件套落库、原因必填、重复回收 409、查询侧隐藏、恢复清空。"""
    import httpx
    from fastapi import FastAPI
    import server.routers.literature_router as router

    db, _, monkeypatch, scope = env

    async def tenant(_user):
        return TENANT

    monkeypatch.setattr(router, "_current_tenant", tenant)
    app = FastAPI()
    app.include_router(router.literature, prefix="/api")
    app.dependency_overrides[router.get_required_user] = lambda: scope.user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        blank = await client.post(f"/api/literature/papers/{RID}/trash", json={"reason": "   "})
        assert blank.status_code == 422, "空原因必须 422（审计要求）"

        ok = await client.post(f"/api/literature/papers/{RID}/trash", json={"reason": "重复导入，保留另一份"})
        assert ok.status_code == 200, ok.text
        body = ok.json()
        assert body["trashed"] is True and body["trashed_by"] == "u1"
        paper = await db.get(LiteraturePaper, 1)
        assert paper.trashed_at is not None
        assert paper.trashed_by == "u1"
        assert paper.trashed_reason.startswith("重复导入")

        assert (await client.get(f"/api/literature/papers/{RID}")).status_code == 404, (
            "回收后 paper_visibility 必须隐藏"
        )
        again = await client.post(f"/api/literature/papers/{RID}/trash", json={"reason": "再删一次"})
        assert again.status_code == 409, "已在回收站应幂等提示 409 而非报 500"

        trash_items = (await client.get("/api/literature/library/trash")).json()["items"]
        entry = next(item for item in trash_items if item["rid"] == RID)
        assert entry["trashed_by"] == "u1"
        assert entry["trashed_reason"].startswith("重复导入")

        assert (await client.post(f"/api/literature/library/trash/{RID}/restore")).status_code == 200
        paper = await db.get(LiteraturePaper, 1)
        assert paper.trashed_at is None and paper.trashed_by is None and paper.trashed_reason is None
        assert (await client.get(f"/api/literature/papers/{RID}")).status_code == 200, "恢复后立即回到可见集"


async def test_reconcile_dry_run_previews_and_tenant_scopes(env, monkeypatch):
    """新③：手动对账 dry_run 只计数不修复；tenant_id 传入作为 HTTP 边界。"""
    from yuxi.literature import reconcile as lit_reconcile
    from yuxi.storage.postgres.models_knowledge import KnowledgeFile, LiteratureSourceEvent

    db, _, _, _ = env
    # env 未建 knowledge_files / literature_source_events（reconcile 查询依赖）；就地补建
    await db.run_sync(
        lambda session: (
            KnowledgeFile.__table__.create(session.connection(), checkfirst=True),
            LiteratureSourceEvent.__table__.create(session.connection(), checkfirst=True),
        )
    )
    db.add(
        KnowledgeFile(
            id=1,
            kb_id="kb-closure",
            file_id="file-closure",
            filename="a.pdf",
            active_parse_revision_id=REVISION,
        )
    )
    # 对账只认 scientific_pdf_* 契约版本（env fixture 默认 pipeline_version=test）
    from yuxi.storage.postgres.models_knowledge import KnowledgeParseRevision as _KPR

    revision_row = await db.get(_KPR, 1)
    revision_row.pipeline_version = "scientific_pdf_test"
    await db.commit()
    repaired = []

    async def fake_register(revision_id, *, enqueue_render=True):
        repaired.append(revision_id)
        return {"status": "REGISTERED"}

    monkeypatch.setattr(lit_reconcile, "register_scientific_revision", fake_register)
    # env 已登记 paper 但无渲染行 → "缺渲染"即为待修项（group by 单 revision 计 1）
    pending = await lit_reconcile.reconcile_scientific_literature_papers(limit=10, dry_run=True, tenant_id=TENANT)
    assert pending == 1 and repaired == [], "dry_run 只计数，不得触达修复"
    # 再破坏登记（paper 与 revision 脱钩）：仍单 revision 计 1，且依旧不修复
    paper = await db.get(LiteraturePaper, 1)
    paper.source_revision_id = "sir_orphan"
    await db.commit()
    pending = await lit_reconcile.reconcile_scientific_literature_papers(limit=10, dry_run=True, tenant_id=TENANT)
    assert pending == 1 and repaired == []
    # 执行模式真正修复（register 被调用）
    done = await lit_reconcile.reconcile_scientific_literature_papers(limit=10, tenant_id=TENANT)
    assert done == 1 and repaired


def test_search_cache_pattern_is_tenant_scoped():
    """新④：检索缓存管理默认租户前缀，global 仅显式请求（router 层权限再校验）。"""
    from server.routers.literature_router import _search_cache_pattern

    assert _search_cache_pattern(7) == "yuxi:lit:search:t7:*"
    assert _search_cache_pattern(7, global_scope=True) == "yuxi:lit:search:*"
