"""Production-shaped progress, current-version, ACL and vector-channel regressions."""

from contextlib import asynccontextmanager
import io
import json
from types import SimpleNamespace
import sqlite3
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
    library_pipeline,
    navigation_index,
    reader_bundle,
    retrieval,
    workflow,
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


async def test_business_backup_snapshot_roundtrip_and_owner_filter(env, monkeypatch, tmp_path):
    from yuxi.literature import backup, history_import, importer
    from yuxi.storage.postgres.models_business import Conversation, Message
    from yuxi.utils.datetime_utils import utc_now

    db, storage, _, _ = env
    now = utc_now()
    db.add_all(
        [
            LiteratureCard(
                id=1,
                tenant_id=TENANT,
                paper_id=1,
                created_by="u1",
                card_type="card-note",
                slug="note1",
                title="My note",
                content="Curated knowledge",
                para_ids_json=["p17"],
                tags_json=["rice"],
            ),
            LiteratureCard(
                id=2,
                tenant_id=TENANT,
                created_by="someone",
                card_type="card-note",
                slug="private",
                title="Private global",
                content="Must not export",
            ),
            LiteratureDiaryEntry(id=1, tenant_id=TENANT, uid="u1", entry_date=now.date(), content="Today's work"),
            LiteratureDiaryEntry(id=2, tenant_id=TENANT, uid="someone", entry_date=now.date(), content="Private diary"),
            LiteratureReport(
                id=1,
                tenant_id=TENANT,
                report_kind="topic_moc",
                title="Rice topic",
                content="Guide",
                created_by="u1",
                source_rids_json=[RID],
            ),
            LiteratureReport(
                id=2,
                tenant_id=TENANT,
                report_kind="topic_moc",
                title="Private report",
                content="Secret",
                created_by="someone",
                source_rids_json=["hidden"],
            ),
            Conversation(
                id=1,
                tenant_id=TENANT,
                uid="u1",
                thread_id="lit1",
                agent_id="default-chatbot",
                title="Paper chat",
                status="active",
                extra_metadata={"literature_session": {"kind": "paper", "rid": RID}, "token": "DO_NOT_EXPORT"},
            ),
            Message(id=1, conversation_id=1, role="user", content="Question", delivery_status="complete"),
            Message(id=2, conversation_id=1, role="assistant", content="Answer", delivery_status="complete"),
            Message(id=3, conversation_id=1, role="tool", content="DO_NOT_EXPORT", delivery_status="complete"),
        ]
    )
    await db.commit()

    async def vault(*args, **kwargs):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("_manifest.json", '{"warnings": []}')
            archive.writestr(f"{RID}/render.md", "# Scientific original\n\nReliable original content.")
            archive.writestr(f"{RID}/L1_note.md", "# Current manual note")
            archive.writestr(f"{RID}/source.pdf", b"%PDF-original")
        return output.getvalue()

    async def effective(*args, **kwargs):
        return {
            "content": json.dumps(
                {"paragraphs": [{"para_id": "p17", "text_en": "Reliable original content."}]}
            ).encode()
        }

    monkeypatch.setattr(backup, "get_minio_client", lambda: storage)
    monkeypatch.setattr(backup, "export_obsidian_vault", vault)
    monkeypatch.setattr(artifacts, "resolve_effective_document", effective)
    result = await backup.export_business_backup(TENANT, "u1")
    with zipfile.ZipFile(io.BytesIO(result)) as archive:
        from yuxi.literature.import_archive import _validated_members

        _validated_members(archive)
        assert "knowledge_base/closure/_note.md" in archive.namelist()
        assert archive.read("knowledge_base/closure/source.pdf") == b"%PDF-original"
        doc = json.loads(archive.read("knowledge_base/closure/document.json"))
        assert doc["paragraphs"][0]["para_id"] == "p17"
        raw = archive.read("knowledge_base/_yuxi/current-business.json")
        assert b"DO_NOT_EXPORT" not in raw and b"Private diary" not in raw and b"Private report" not in raw
        manifest = json.loads(archive.read("_yuxi_history_manifest.json"))
        assert manifest["counts"]["cards"] == 1 and manifest["counts"]["messages"] == 2
        assert manifest["scope"] == "current_authorized_business"
        for name, digest in manifest["members"].items():
            import hashlib

            assert hashlib.sha256(archive.read(name)).hexdigest() == digest
        archive.extractall(tmp_path)
    from yuxi.literature.import_archive import _validate_business_manifest

    _validate_business_manifest(tmp_path)
    (tmp_path / "knowledge_base/closure/_note.md").write_text("tampered")
    with pytest.raises(ValueError, match="摘要校验"):
        _validate_business_manifest(tmp_path)
    papers, citations = importer._read_biblio(tmp_path / "data/biblio/biblio.db")
    assert len(papers) == 1 and papers[0]["rid"] == RID and not citations
    discovered = history_import.discover(tmp_path)
    categories = {item.category for item in discovered}
    assert {"papers", "papers_meta", "sessions", "messages", "card", "diary", "report"} <= categories


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


async def test_manual_note_cas_freeze_and_current_input(env):
    from yuxi.literature import products, readops
    from yuxi.literature.notes import service

    db, storage, _, _ = env
    job = LiteratureCompileJob(
        id=1,
        tenant_id=TENANT,
        paper_id=1,
        level="L1",
        status="SUCCEEDED",
        source_revision_id=REVISION,
        reading_version=1,
        card_revision=0,
        note_uri="kbasset://base-note",
    )
    db.add(job)
    storage.files["base-note"] = b"An original scientific note with sufficient meaningful text."
    await db.commit()
    opened = await products.read_product(TENANT, RID, "note_L1")
    snapshot = {key: value for key, value in opened.items() if key.startswith("expected_")}
    saved = await products.save_product(
        TENANT,
        RID,
        "note_L1",
        uid="u1",
        content="A manually curated scientific note with important annotations.",
        snapshot=snapshot,
    )
    assert saved["frozen"] and await readops.get_note_uri(TENANT, RID, "L1") == saved["uri"]
    with pytest.raises(artifacts.RevisionConflictError):
        await products.save_product(
            TENANT, RID, "note_L1", uid="u1", content="Stale parallel editor content.", snapshot=snapshot
        )
    await db.refresh(job)
    await service._finish_l1_l2_bundle(job, l1_uri="kbasset://automatic-candidate", l2_uri=None, value_score=4)
    assert job.note_uri == saved["uri"] and job.value_score is None
    paper = await db.get(LiteraturePaper, 1)
    paper.reading_version += 1
    await db.commit()
    with pytest.raises(ValueError, match="来源已变更"):
        await readops.get_note_uri(TENANT, RID, "L1")
    reopened = await products.read_product(TENANT, RID, "note_L1")
    assert reopened["stale"] and "manually curated" in reopened["content"]
    rebased = await products.save_product(
        TENANT,
        RID,
        "note_L1",
        uid="u1",
        content=reopened["content"],
        snapshot={key: value for key, value in reopened.items() if key.startswith("expected_")},
    )
    assert rebased["revision_id"] != saved["revision_id"]
    assert await readops.get_note_uri(TENANT, RID, "L1") == rebased["uri"]


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


async def test_business_history_is_owner_private_resumable_and_conflict_preserving(env, tmp_path):
    from yuxi.literature import history_import, history_archive
    from yuxi.storage.postgres.models_business import User, Conversation, Message
    from dataclasses import replace

    db, storage, _, scope = env
    db.add(User(id=1, uid="u1", username="u1", password_hash="fixture-only", role="admin"))
    await db.commit()
    root = tmp_path / "history"
    for name in ("biblio", "chat", "system", "reference"):
        (root / "data" / name).mkdir(parents=True)
    with sqlite3.connect(root / "data/biblio/biblio.db") as con:
        con.execute("CREATE TABLE papers (id INTEGER, doc_json TEXT)")
        con.execute("INSERT INTO papers VALUES (?, ?)", (1, f"library/{RID}/document.json"))
    with sqlite3.connect(root / "data/chat/chat.db") as con:
        con.execute("CREATE TABLE sessions (id INTEGER, paper_id INTEGER, kind TEXT, title TEXT)")
        con.execute("INSERT INTO sessions VALUES (1, 1, 'paper', 'Historical discussion')")
        con.execute(
            "CREATE TABLE messages (id INTEGER, session_id INTEGER, role TEXT, content TEXT, reasoning_content TEXT)"
        )
        con.execute(
            "INSERT INTO messages VALUES (1, 1, 'assistant', 'Historical unverified answer', 'Private reasoning')"
        )
    with sqlite3.connect(root / "data/system/app.db") as con:
        con.execute("CREATE TABLE settings (key TEXT, value TEXT)")
        con.execute("INSERT INTO settings VALUES ('api_key', 'DO-NOT-IMPORT')")
        con.execute("CREATE TABLE tasks (id INTEGER, status TEXT)")
        con.execute("INSERT INTO tasks VALUES (1, 'pending')")
        con.execute("CREATE TABLE llm_usage (id INTEGER, tokens INTEGER)")
        con.execute("INSERT INTO llm_usage VALUES (1, 123)")
    with sqlite3.connect(root / "data/reference/journals.db") as con:
        con.execute("CREATE TABLE jcr (journal_name TEXT, year INTEGER, jif REAL, quartile TEXT)")
        con.execute("INSERT INTO jcr VALUES ('Scientific Journal', 2024, 4.5, 'Q1')")
    card = root / f"knowledge_base/{RID}/cards/note-user.md"
    card.parent.mkdir(parents=True)
    card.write_text("---\ntype: card-note\npara_ids: [P001]\ntags: [research]\n---\nUser annotation", encoding="utf-8")
    diary = root / "knowledge_base/_diary/2026-10-02.md"
    diary.parent.mkdir(parents=True)
    diary.write_text("Original daily reading notes", encoding="utf-8")
    report = root / "knowledge_base/_reports/review.md"
    report.parent.mkdir(parents=True)
    report.write_text("Historical literature report", encoding="utf-8")
    preview = await history_import.import_history(
        root, tenant_id=TENANT, uid="u1", archive_sha256="a" * 64, dry_run=True
    )
    assert preview["source_counts"]["messages"] == 1
    assert not list((await db.execute(select(LiteratureHistoryItem))).scalars())
    first = await history_import.import_history(
        root, tenant_id=TENANT, uid="u1", archive_sha256="a" * 64, dry_run=False
    )
    assert first["applied"] == 6 and first["preserved"] == 3
    inert = list(
        (
            await db.execute(
                select(LiteratureHistoryItem).where(LiteratureHistoryItem.category.in_(["tasks", "llm_usage"]))
            )
        ).scalars()
    )
    assert len(inert) == 2 and all(row.status == "PRESERVED" and row.target_ref is None for row in inert)
    receipt_count = len(list((await db.execute(select(LiteratureHistoryItem))).scalars()))
    again = await history_import.import_history(
        root, tenant_id=TENANT, uid="u1", archive_sha256="b" * 64, dry_run=False
    )
    assert again["skipped"] == receipt_count and again["applied"] == 0
    # Deduplicated originals still belong to both complete archive manifests.
    await history_archive.seal_archive(root, TENANT, "u1", "a" * 64)
    await history_archive.seal_archive(root, TENANT, "u1", "b" * 64)
    snapshot = await history_archive.export_archive(TENANT, "u1", "b" * 64)
    from yuxi.literature.import_archive import _extract_archive

    archive_path = tmp_path / "roundtrip.zip"
    archive_path.write_bytes(snapshot)
    restored = _extract_archive(archive_path, tmp_path / "restored")
    repeated = await history_import.import_history(
        restored, tenant_id=TENANT, uid="u1", archive_sha256="d" * 64, dry_run=False
    )
    assert repeated["skipped"] == receipt_count and repeated["applied"] == 0
    assert "settings" not in history_archive.table_columns(restored).get("data/system/app.db", {})
    assert len(list((await db.execute(select(Message))).scalars())) == 1
    session = (await db.execute(select(Conversation))).scalar_one()
    assert session.uid == "u1" and session.status == "archived"
    # Restoring the conversation does not certify its imported answers.
    from yuxi.literature import qna

    session.status = "active"
    await db.commit()
    with pytest.raises(ValueError, match="尚无完整"):
        await qna.answer_preview(TENANT, "u1", session.thread_id)
    assert all(b"DO-NOT-IMPORT" not in value for value in storage.files.values())
    card.write_text("---\ntype: card-note\n---\nChanged archive version", encoding="utf-8")
    changed = await history_import.import_history(
        root, tenant_id=TENANT, uid="u1", archive_sha256="c" * 64, dry_run=False
    )
    assert changed["conflicts"] == [f"knowledge_base/{RID}/cards/note-user.md"]
    assert (await db.execute(select(LiteratureCard))).scalar_one().content == "User annotation"
    items = await history_import.list_history(TENANT, "u1")
    assert items["items"]
    token = _access.set(replace(scope, user=SimpleNamespace(uid="u2")))
    try:
        assert not await history_archive.list_archives(TENANT, "u2")
        with pytest.raises(ValueError, match="唯一已封存"):
            await history_archive.export_archive(TENANT, "u2", "a" * 64)
        with pytest.raises(PermissionError):
            await history_archive.export_archive(TENANT, "u1", "a" * 64)
        assert not (await history_import.list_history(TENANT, "u2"))["items"]
        with pytest.raises(ValueError):
            await history_import.read_history(TENANT, "u2", items["items"][0]["id"])
    finally:
        _access.reset(token)


async def test_history_roundtrip_all_allowlisted_tables_empty_table_and_blob(env, tmp_path):
    import hashlib
    from yuxi.literature import history_import, history_archive
    from yuxi.literature.import_archive import _extract_archive

    db, storage, _, _ = env
    root = tmp_path / "source"
    for logical, tables in history_import.TABLES.items():
        path = root / logical
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as con:
            for table in tables:
                con.execute(f'CREATE TABLE "{table}" (id INTEGER, payload BLOB, comment TEXT)')
                if table != "answer_cache":
                    con.execute(f'INSERT INTO "{table}" VALUES (?, ?, ?)', (1, b"\x00\xffbinary", "中文历史记录"))
    asset = root / "library/paper/images/F001.png"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"original-image\x00\xff")
    originals = history_import.discover(root)
    for index, item in enumerate(originals):
        content = item.content if item.content is not None else item.path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        owner = hashlib.sha256(b"u1").hexdigest()[:24]
        key = f"tenants/{TENANT}/literature/history/{owner}/{digest}.bin"
        storage.files[key] = content
        db.add(
            LiteratureHistoryItem(
                id=index + 1,
                tenant_id=TENANT,
                uid="u1",
                source_key=item.key,
                category=item.category,
                content_digest=digest,
                object_uri=f"kbasset://{key}",
                archive_sha256="a" * 64,
                status="PRESERVED",
            )
        )
    await db.commit()
    await history_archive.seal_archive(root, TENANT, "u1", "a" * 64)
    exported = await history_archive.export_archive(TENANT, "u1", "a" * 64)
    archive = tmp_path / "restore.zip"
    archive.write_bytes(exported)
    restored = _extract_archive(archive, tmp_path / "restored")

    def identities(items):
        return sorted(
            (
                item.key,
                item.category,
                hashlib.sha256(item.content if item.content is not None else item.path.read_bytes()).hexdigest(),
            )
            for item in items
        )

    assert identities(history_import.discover(restored)) == identities(originals)
    assert history_archive.table_columns(restored) == history_archive.table_columns(root)
    with sqlite3.connect(restored / "data/biblio/biblio.db") as con:
        assert con.execute("SELECT payload FROM papers").fetchone()[0] == b"\x00\xffbinary"
    with sqlite3.connect(restored / "data/chat/chat.db") as con:
        assert con.execute("SELECT count(*) FROM answer_cache").fetchone()[0] == 0
    # Missing or corrupted immutable originals must never produce a partial ZIP.
    row = (
        await db.execute(select(LiteratureHistoryItem).where(LiteratureHistoryItem.category == "asset"))
    ).scalar_one()
    key = row.object_uri.removeprefix("kbasset://")
    storage.files[key] = b"tampered"
    with pytest.raises(ValueError, match="摘要校验失败"):
        await history_archive.export_archive(TENANT, "u1", "a" * 64)
    storage.files[key] = b"original-image\x00\xff"
    await db.delete(row)
    await db.commit()
    with pytest.raises(ValueError, match="原件缺失"):
        await history_archive.export_archive(TENANT, "u1", "a" * 64)
    with pytest.raises(ValueError, match="回执尚不完整"):
        await history_archive.seal_archive(root, TENANT, "u1", "b" * 64)


async def test_history_archive_budget_namespace_and_preflight_fail_closed(env, tmp_path):
    from yuxi.literature import history_archive
    from yuxi.literature.import_archive import _extract_archive

    _, _, monkeypatch, _ = env
    with pytest.raises(ValueError, match="非法归档"):
        await history_archive.export_archive(TENANT, "u1", "../secret")
    row = SimpleNamespace(uid="u1", tenant_id=TENANT, object_uri="kbasset://tenants/999/private", content_digest="a")
    with pytest.raises(ValueError, match="命名空间"):
        await history_archive._read_checked(row)
    # Our own output must pass reimport's ZIP bomb gate, including repetitive assets.
    output = history_archive._build_archive(
        {"data/biblio/biblio.db": {"papers": [{"id": 1}]}},
        {"library/large-zero.bin": b"\x00" * (2 * 1024 * 1024)},
        b"manifest",
        {"data/biblio/biblio.db": {"papers": ["id"]}},
    )
    archive = tmp_path / "repetitive.zip"
    archive.write_bytes(output)
    restored = _extract_archive(archive, tmp_path / "restored")
    assert (restored / "library/large-zero.bin").stat().st_size == 2 * 1024 * 1024
    monkeypatch.setattr(history_archive, "MAX_ARCHIVE_BYTES", 10)
    with pytest.raises(ValueError, match="安全预算"):
        history_archive._build_archive({}, {}, b"manifest", {"data/biblio/biblio.db": {"papers": ["id"]}})


async def test_wiki_link_resolution_never_returns_hidden_rid(env):
    from yuxi.literature.management import resolve_reader_target

    assert (await resolve_reader_target(TENANT, "Visible paper"))["rid"] == RID
    assert (await resolve_reader_target(TENANT, "hidden"))["rid"] is None


@pytest.mark.parametrize("changed", ["none", "digest", "owner", "native", "disabled", "runtime", "tenant"])
async def test_history_retry_appends_receipt_without_overwriting_original(env, changed):
    import hashlib
    from dataclasses import replace
    from yuxi.literature import history_import
    from yuxi.storage.postgres.models_business import User

    db, storage, monkeypatch, scope = env
    actor = User(id=1, uid="u1", username="u1", password_hash="fixture-only", is_disabled=changed == "disabled")
    db.add(actor)
    paper = await db.get(LiteraturePaper, 1)
    paper.source_revision_id = "pair-retry" if changed != "native" else REVISION
    content = b"---\ntype: card-note\n---\nA historical annotation"
    digest = hashlib.sha256(content).hexdigest()
    receipt = LiteratureHistoryItem(
        id=1,
        tenant_id=TENANT,
        uid="u1",
        source_key=f"knowledge_base/{RID}/cards/note-retry.md",
        category="card" if changed != "runtime" else "tasks",
        content_digest=digest,
        object_uri="kbasset://retry-original",
        archive_sha256="a" * 64,
        status="PRESERVED",
        detail="Old contract did not support mapping",
    )
    db.add(receipt)
    storage.files["retry-original"] = content
    await db.commit()

    async def fresh(*args, **kwargs):
        return scope

    monkeypatch.setattr(history_import, "build_access", fresh)

    async def tenant_for_actor(*args):
        return TENANT + int(changed == "tenant")

    monkeypatch.setattr("yuxi.services.principal.resolve_tenant_id", tenant_for_actor)
    kwargs = {"expected_digest": digest, "request_id": "eaa7a8ba-2775-4cca-b3f5-2f80dd1aac31"}
    if changed == "digest":
        kwargs["expected_digest"] = "b" * 64
    token = _access.set(replace(scope, user=SimpleNamespace(uid="u2"))) if changed == "owner" else None
    try:
        if changed in {"digest", "owner", "disabled", "runtime", "tenant"}:
            with pytest.raises((ValueError, PermissionError)):
                await history_import.retry_history_mapping(TENANT, "u1", 1, **kwargs)
            assert len(list((await db.execute(select(LiteratureHistoryItem))).scalars())) == 1
        else:
            result = await history_import.retry_history_mapping(TENANT, "u1", 1, **kwargs)
            assert result["status"] == ("APPLIED" if changed == "none" else "CONFLICT")
            repeated = await history_import.retry_history_mapping(TENANT, "u1", 1, **kwargs)
            assert result == repeated
            assert len(list((await db.execute(select(LiteratureHistoryItem))).scalars())) == 2
            cards = list((await db.execute(select(LiteratureCard))).scalars())
            assert len(cards) == (1 if changed == "none" else 0)
        await db.refresh(receipt)
        assert receipt.status == "PRESERVED" and receipt.target_ref is None
        assert storage.files["retry-original"] == content
    finally:
        if token is not None:
            _access.reset(token)


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


@pytest.mark.parametrize("reason", ["all_sources_failed", "source_access_revoked", "no_doi", "meta_locked"])
async def test_library_enrichment_reports_partial_failure_instead_of_success(env, reason):
    _, _, monkeypatch, scope = env

    async def result(tenant_id, rid):
        return {"rid": rid, "ok": reason == "meta_locked", "reason": reason}

    monkeypatch.setattr(enrich, "enrich_paper", result)
    status, detail, _ = await library_pipeline._execute(
        SimpleNamespace(tenant_id=TENANT, payload_json={"rids": [RID]}),
        "enrich_metadata",
        {},
        scope.user,
    )
    assert detail["processed"] == 1
    assert status == ("SUCCEEDED" if reason in {"no_doi", "meta_locked"} else "FAILED")
    assert detail["failed"] == int(status == "FAILED")
    assert detail["skipped"] == int(reason == "no_doi")


async def test_history_journal_mapping_requires_current_admin_role(env):
    from yuxi.literature import history_import
    from yuxi.storage.postgres.models_business import User

    db, _, _, _ = env
    actor = User(id=1, uid="u1", username="u1", password_hash="fixture-only", role="user")
    db.add(actor)
    await db.commit()
    item = history_import.Item(
        "data/reference/journals.db/jcr/retry",
        "jcr",
        b"{}",
        {"journal_name": "Scientific Journal", "year": 2025, "jif": 3.5},
    )
    options = {
        "tenant_id": TENANT,
        "uid": "u1",
        "papers": {},
        "uri": "kbasset://raw-journal",
        "digest": "a" * 64,
        "old_papers": {},
    }
    result = await history_import._promote(db, item, **options)
    assert result[0] == "PRESERVED"
    assert not list((await db.execute(select(LiteratureJournalMetric))).scalars())
    actor.role = "admin"
    await db.commit()
    assert (await history_import._promote(db, item, **options))[0] == "APPLIED"


async def test_history_attachment_root_maps_native_parent_and_type(env, tmp_path):
    from yuxi.literature import history_import
    from yuxi.storage.postgres.models_business import User

    db, storage, _, _ = env
    db.add(User(id=1, uid="u1", username="u1", password_hash="fixture-only"))
    await db.commit()
    attachment = tmp_path / "attachments" / RID / "si" / "support.txt"
    attachment.parent.mkdir(parents=True)
    attachment.write_text("Supplementary methods", encoding="utf-8")
    result = await history_import.import_history(
        tmp_path, tenant_id=TENANT, uid="u1", archive_sha256="a" * 64, dry_run=False
    )
    assert result["applied"] == 1 and result["preserved"] == 0
    row = (await db.execute(select(LiteratureAttachment))).scalar_one()
    assert row.paper_id == 1 and row.kind == "si"
    assert storage.files[row.uri.removeprefix("kbasset://")] == b"Supplementary methods"


@pytest.mark.parametrize("failure", [None, "ambiguous", "new_source", "manual", "ai_only", "identity", "malformed"])
async def test_historical_review_maps_exact_human_choice_without_replaying(env, tmp_path, failure):
    import hashlib
    from yuxi.literature import history_import, review, history_review
    from yuxi.storage.postgres.models_business import User

    db, storage, monkeypatch, _ = env
    monkeypatch.setattr(history_review, "get_minio_client", lambda: storage)
    monkeypatch.setattr(review, "get_minio_client", lambda: storage)
    db.add(User(id=1, uid="u1", username="u1", password_hash="fixture-only"))
    paper = await db.get(LiteraturePaper, 1)
    revision = await db.get(KnowledgeParseRevision, 1)
    imported = "pair-history"
    paper.source_revision_id = revision.revision_id = imported
    paper.artifact_pointers = {"document": "lar-history"}
    paragraphs = [{"para_id": "P001", "source_block_ids": ["B001"], "text_en": "Meaningful starch science paragraph."}]
    if failure == "ambiguous":
        paragraphs.append({**paragraphs[0], "para_id": "P002"})
    document = json.dumps({"paragraphs": paragraphs}).encode()
    storage.files["history-document"] = document
    db.add(
        LiteratureArtifactRevision(
            id=1,
            revision_id="lar-history",
            tenant_id=TENANT,
            paper_id=1,
            kind="document",
            source="human" if failure == "manual" else "imported",
            frozen=True,
            object_uri="kbasset://history-document",
            content_digest=hashlib.sha256(document).hexdigest(),
            source_revision_id=imported,
            input_reading_version=1,
            input_card_revision=0,
            created_by="u1",
        )
    )
    db.add(
        LiteratureReaderRender(
            id=1,
            tenant_id=TENANT,
            revision_id=imported,
            render_version="paperagent_v4",
            status="SUCCEEDED",
            markdown_uri="kbasset://history-render/render.md",
        )
    )
    if failure == "new_source":
        paper.source_revision_id = "sir-new-native"
    await db.commit()
    payload = {
        "items": [
            {
                "mineru": {"block_id": "B001", "text": paragraphs[0]["text_en"]},
                "paddleocr": {"text": "A different corrected paragraph."},
                "user_choice": "paddleocr",
                "ai_verdict": "paddleocr",
                "resolved": True,
            }
        ]
    }
    if failure == "ai_only":
        payload["items"][0].pop("user_choice")
    if failure == "identity":
        payload["items"][0]["item_id"] = "foreign-render#blk:B001"
    if failure == "malformed":
        payload["items"][0]["mineru"] = "not-an-object"
    path = tmp_path / "library" / RID / "dual" / "review.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = await history_import.import_history(
        tmp_path, tenant_id=TENANT, uid="u1", archive_sha256="a" * 64, dry_run=False
    )
    mapped = failure in {None, "ai_only"}
    assert result["applied"] == int(mapped)
    assert storage.files["history-document"] == document  # No automatic replay.
    decisions = list((await db.execute(select(LiteratureReviewDecision))).scalars())
    assert len(decisions) == int(failure is None)
    if mapped:
        current = await review.get_review_items(TENANT, imported)
        assert current["total"] == 1
        assert bool(current["items"][0]["decision"]) == (failure is None)
        if failure is None:
            assert current["items"][0]["decision"]["choice"] == "B"
    repeated = await history_import.import_history(
        tmp_path, tenant_id=TENANT, uid="u1", archive_sha256="a" * 64, dry_run=False
    )
    assert repeated["skipped"] == 1


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

        async def revoked(*args):
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

        monkeypatch.setattr(workflow.pg_manager, "get_async_session_context", session_context)
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
        storage = Storage()
        for module in (artifacts, reader_bundle, export_vault):
            monkeypatch.setattr(module, "get_minio_client", lambda: storage)
        monkeypatch.setattr(retrieval, "_get_minio", lambda: storage)

        async def enqueue(_run_id):
            return True

        monkeypatch.setattr(workflow, "_enqueue_tick", enqueue)
        monkeypatch.setattr(library_pipeline, "_dispatch", enqueue)
        scope = LiteratureAccess(TENANT, frozenset({RID}), frozenset({1}), {}, {}, SimpleNamespace(uid="u1"))
        token = _access.set(scope)

        async def fresh_access(_user, _tenant, *, include_trashed=False):
            from dataclasses import replace

            papers = list(
                (await db.execute(select(LiteraturePaper).where(LiteraturePaper.rid.in_(scope.rids)))).scalars()
            )
            visible = frozenset(p.rid for p in papers if include_trashed or p.trashed_at is None)
            return replace(scope, rids=visible, paper_ids=frozenset(p.id for p in papers if p.rid in visible))

        monkeypatch.setattr("yuxi.literature.access.build_access", fresh_access)
        monkeypatch.setattr("yuxi.literature.management.build_access", fresh_access)
        monkeypatch.setattr("yuxi.literature.qna.build_access", fresh_access)
        for module in ("products", "search_runtime", "history_import"):
            monkeypatch.setattr(f"yuxi.literature.{module}.build_access", fresh_access)
        for module in ("products", "history_import", "history_archive"):
            monkeypatch.setattr(f"yuxi.literature.{module}.get_minio_client", lambda: storage)
        monkeypatch.setattr(cleaning, "build_access", fresh_access)
        # 模型供应商预检（网络探测）不属于本文件的编排语义测试范围。
        from yuxi.literature.translate import service as translate_service

        async def _skip_precheck(paper_id, requested_by, **kwargs):
            return None

        monkeypatch.setattr(translate_service, "_precheck_translation_model", _skip_precheck)
        try:
            yield db, storage, monkeypatch, scope
        finally:
            _access.reset(token)
    await engine.dispose()


async def test_translation_retry_survives_arq_deduplication(env, monkeypatch):
    from yuxi.literature.translate import service

    db, _, _, _ = env
    job = LiteratureTranslationJob(id=101, tenant_id=TENANT, paper_id=1, source_revision_id=REVISION, variant_kind="zh")
    db.add(job)
    await db.commit()
    blocked = {f"lit-translate:{job.id}"}
    scheduled = []

    class Queue:
        async def enqueue_job(self, _name, *args, _job_id):
            if _job_id in blocked:
                return None
            blocked.add(_job_id)
            scheduled.append((args, _job_id))
            return object()

    async def pool():
        return Queue()

    monkeypatch.setattr(service, "get_arq_pool", pool)
    # Missing render is a durable failure. Both retries must be dispatched
    # despite the original job still occupying ARQ's deduplication key.
    for _ in range(service.MAX_TRANSLATE_ATTEMPTS):
        result = await service.process_literature_translation({}, job.id)
    assert result["status"] == "DEAD" and job.attempt == service.MAX_TRANSLATE_ATTEMPTS
    assert len(scheduled) == service.MAX_TRANSLATE_ATTEMPTS - 1
    assert len({key for _, key in scheduled}) == len(scheduled)


async def test_translation_pending_recovery_keeps_attempt_identity(env, monkeypatch):
    from datetime import timedelta
    from yuxi.literature.translate import service

    db, _, _, _ = env
    job = LiteratureTranslationJob(
        id=101,
        tenant_id=TENANT,
        paper_id=1,
        source_revision_id=REVISION,
        variant_kind="zh",
        attempt=1,
        updated_at=service.utc_now() - timedelta(seconds=service.PENDING_REQUEUE_SECONDS + 1),
    )
    db.add(job)
    await db.commit()
    scheduled = []

    class Queue:
        async def enqueue_job(self, _name, *args, _job_id):
            scheduled.append(_job_id)

    async def pool():
        return Queue()

    monkeypatch.setattr(service, "get_arq_pool", pool)
    assert await service.reconcile_stale_literature_translations() == 1
    assert scheduled == [f"lit-translate:{job.id}:attempt:1"]
    job.status = "RUNNING"
    job.attempt = service.MAX_TRANSLATE_ATTEMPTS - 1
    job.updated_at = service.utc_now() - timedelta(seconds=service.RUNNING_LEASE_SECONDS + 1)
    await db.commit()
    assert await service.reconcile_stale_literature_translations() == 0
    assert job.status == "DEAD"


async def test_governed_translation_resubmission_survives_cached_arq_results(env, monkeypatch):
    from yuxi.literature import llm_gateway
    from yuxi.literature.translate import service, settings

    db, _, _, _ = env
    scheduled = []
    runs = iter(("first-submission", "second-submission"))

    async def freeze(*args, **kwargs):
        return SimpleNamespace(
            run_id=next(runs),
            model_spec="fixture:model",
            credential_source="platform",
            credential_id=None,
            provider_id="fixture",
            policy_version=1,
        )

    async def options(*args):
        return {}

    class Queue:
        async def enqueue_job(self, _name, *args, _job_id):
            scheduled.append(_job_id)

    async def pool():
        return Queue()

    monkeypatch.setattr(llm_gateway, "freeze_literature_policy", freeze)
    monkeypatch.setattr(settings, "translation_options", options)
    monkeypatch.setattr(service, "get_arq_pool", pool)
    # SQLite's BIGINT fixture needs an explicit ID.
    db.add(
        LiteratureTranslationJob(
            id=101, tenant_id=TENANT, paper_id=1, source_revision_id=REVISION, variant_kind="zh", status="DEAD"
        )
    )
    await db.commit()
    first = await service.enqueue_literature_translation(paper_id=1, requested_by="u1")
    job = await db.get(LiteratureTranslationJob, first["job_id"])
    job.status = "DEAD"
    await db.commit()
    again = await service.enqueue_literature_translation(paper_id=1, requested_by="u1")
    assert again["job_id"] != first["job_id"] and not again["reused"]
    assert job.status == "DEAD", "新译文代次不得清除旧任务历史"
    assert len(scheduled) == 2 and scheduled[0] != scheduled[1]


async def test_translation_cancellation_is_durable(env, monkeypatch):
    import asyncio
    from yuxi.literature.translate import service

    db, _, _, _ = env
    paper = await db.get(LiteraturePaper, 1)
    paper.artifact_pointers = {"document": "test-document"}
    job = LiteratureTranslationJob(id=101, tenant_id=TENANT, paper_id=1, source_revision_id=REVISION, variant_kind="zh")
    db.add(job)
    await db.commit()
    scheduled = []

    async def interrupted(*args, **kwargs):
        raise asyncio.CancelledError

    class Queue:
        async def enqueue_job(self, _name, *args, _job_id):
            scheduled.append(_job_id)

    async def pool():
        return Queue()

    monkeypatch.setattr(service, "get_arq_pool", pool)
    monkeypatch.setattr(artifacts, "resolve_effective_document", interrupted)
    with pytest.raises(asyncio.CancelledError):
        await service.process_literature_translation({}, job.id)
    assert job.status == "PENDING" and job.attempt == 1
    assert "WORKER_INTERRUPTED" in job.error
    assert scheduled == [f"lit-translate:{job.id}:attempt:1"]
    job.status = "SUCCEEDED"
    await db.commit()
    assert await service._finish_translation_failure(job.id, 0, "late cancellation") == "STALE"
    assert job.status == "SUCCEEDED"


async def test_metadata_cas_invalidates_notes_but_not_source_evidence(env):
    from yuxi.literature import management

    db, _, _, _ = env
    paper = await db.get(LiteraturePaper, 1)
    old_version = paper.reading_version
    original = await management.get_metadata(TENANT, RID)
    result = await management.update_metadata(
        TENANT, RID, expected_token=original["metadata_token"], changes={"title": "人工标题", "meta_locked": True}
    )
    assert result["title"] == "人工标题" and result["meta_locked"]
    assert paper.card_revision == 1 and paper.reading_version == old_version
    assert paper.source_revision_id == REVISION
    with pytest.raises(management.MetadataConflict):
        await management.update_metadata(
            TENANT, RID, expected_token=original["metadata_token"], changes={"title": "旧编辑"}
        )
    await db.refresh(paper)
    assert paper.title == "人工标题"
    # DOI 已放开人工编辑（P1 死锁修复）：非法形态仍拒绝，且 RID 身份不变。
    with pytest.raises(ValueError, match="DOI"):
        await management.update_metadata(TENANT, RID, expected_token=result["metadata_token"], changes={"doi": "evil"})
    doi_result = await management.update_metadata(
        TENANT, RID, expected_token=result["metadata_token"], changes={"doi": "10.1186/s12284-022-00562-8"}
    )
    assert doi_result["doi"] == "10.1186/s12284-022-00562-8"
    await db.refresh(paper)
    assert paper.doi == "10.1186/s12284-022-00562-8" and paper.rid == RID


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


async def test_run_history_hides_other_actors_and_revoked_manifest(env):
    from yuxi.literature import management

    db, _, _, _ = env
    for idx, actor, rids in ((1, "u1", [RID]), (2, "u2", [RID]), (3, "u1", ["hidden"])):
        db.add(
            LiteratureLibraryRun(
                id=idx,
                run_id=f"library-{idx}",
                tenant_id=TENANT,
                requested_by=actor,
                input_hash=str(idx) * 64,
                payload_json={"rids": rids},
                status="SUCCEEDED",
            )
        )
    db.add(
        LiteratureNavigationIndex(
            id=1,
            index_id="nav-visible",
            tenant_id=TENANT,
            requested_by="u1",
            model_spec="test:embedding",
            collection_name="test_visible",
            input_manifest={"1": {"rid": RID}},
            status="SUCCEEDED",
        )
    )
    db.add(
        LiteratureNavigationIndex(
            id=2,
            index_id="nav-hidden",
            tenant_id=TENANT,
            requested_by="u1",
            model_spec="test:embedding",
            collection_name="test_hidden",
            input_manifest={"2": {"rid": "hidden"}},
            status="SUCCEEDED",
        )
    )
    await db.commit()
    assert {row["id"] for row in await management.list_runs(TENANT)} == {"library-1", "nav-visible"}


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


async def test_enrich_queue_is_durable_actor_bound_and_metadata_only(env):
    from yuxi.storage.postgres.models_business import User

    db, _, monkeypatch, scope = env
    db.add(User(id=1, uid="u1", username="owner", password_hash="fixture-unused"))
    await db.commit()
    fetched = []

    async def unavailable(_run_id):
        return False

    async def actor(_run):
        return scope.user

    async def metadata(tenant, rid):
        assert (tenant, rid) == (TENANT, RID)
        fetched.append(rid)
        return {"rid": rid, "ok": True, "filled": ["title"]}

    monkeypatch.setattr(library_pipeline, "_dispatch", unavailable)
    started = await enrich.enqueue_enrich_pending(TENANT)
    assert started["enqueue_deferred"] and not fetched and started["batch_size"] == 1
    again = await enrich.enqueue_enrich_pending(TENANT)
    assert again["reused"] and again["run_id"] == started["run_id"]
    run = (await db.execute(select(LiteratureLibraryRun))).scalar_one()
    assert run.requested_by == "u1" and run.payload_json["rids"] == [RID]
    monkeypatch.setattr(workflow, "_request_user", actor)
    monkeypatch.setattr(enrich, "enrich_paper", metadata)
    result = await library_pipeline.process_literature_library({}, run.run_id)
    assert result["status"] == "SUCCEEDED" and fetched == [RID]
    state = await library_pipeline.get_library_run(TENANT, run.run_id)
    assert state["steps"]["enrich_metadata"]["detail"]["processed"] == 1
    assert all(
        state["steps"][name]["detail"]["skipped"] for name in library_pipeline.STEP_NAMES if name != "enrich_metadata"
    )


async def test_enrich_queue_rechecks_revoked_sources_before_external_calls(env):
    from yuxi.storage.postgres.models_business import User
    from yuxi.utils.datetime_utils import utc_now

    db, _, monkeypatch, scope = env
    db.add(User(id=1, uid="u1", username="owner", password_hash="fixture-unused"))
    await db.commit()
    started = await enrich.enqueue_enrich_pending(TENANT)
    paper = await db.get(LiteraturePaper, 1)
    paper.trashed_at = utc_now()
    await db.commit()

    async def actor(_run):
        return scope.user

    async def forbidden(*args):
        pytest.fail("Revoked sources must never reach a metadata supplier")

    monkeypatch.setattr(workflow, "_request_user", actor)
    monkeypatch.setattr(enrich, "enrich_paper", forbidden)
    result = await library_pipeline.process_literature_library({}, started["run_id"])
    assert result["status"] == "FAILED"


@pytest.mark.parametrize("status", ["PENDING", "RUNNING", "SUCCEEDED"])
async def test_translation_reuse_does_not_probe_unavailable_provider(env, status):
    from yuxi.literature.translate import service, settings

    db, _, monkeypatch, _ = env
    db.add(
        LiteratureTranslationJob(
            id=701,
            tenant_id=TENANT,
            paper_id=1,
            variant_kind="zh",
            source_revision_id=REVISION,
            status=status,
            config_digest="",
        )
    )
    await db.commit()

    async def options(*args):
        return {}

    async def forbidden(*args, **kwargs):
        pytest.fail("Existing translation must be reusable during a provider outage")

    monkeypatch.setattr(settings, "translation_options", options)
    monkeypatch.setattr(service, "_precheck_translation_model", forbidden)
    result = await service.enqueue_literature_translation(paper_id=1, requested_by="u1")
    assert result == {"job_id": 701, "status": status, "reused": True}


async def test_new_translation_failed_precheck_creates_no_job_or_run(env):
    from yuxi.literature.translate import service, settings
    from yuxi.literature.model_precheck import ModelPrecheckError
    from yuxi.storage.postgres.models_business import AgentRun
    from sqlalchemy import func

    db, _, monkeypatch, _ = env

    async def options(*args):
        return {}

    async def unavailable(*args, **kwargs):
        raise ModelPrecheckError("供应商不可达；请切换模型")

    monkeypatch.setattr(settings, "translation_options", options)
    monkeypatch.setattr(service, "_precheck_translation_model", unavailable)
    with pytest.raises(ModelPrecheckError):
        await service.enqueue_literature_translation(paper_id=1, requested_by="u1")
    assert (await db.execute(select(func.count()).select_from(LiteratureTranslationJob))).scalar_one() == 0
    assert (await db.execute(select(func.count()).select_from(AgentRun))).scalar_one() == 0


async def test_journal_step_persists_latest_metric_and_keeps_locked_papers(env):
    db, _, _, _ = env
    paper = await db.get(LiteraturePaper, 1)
    paper.journal = "Starch & Starke"
    db.add(
        LiteratureJournalMetric(
            id=1, tenant_id=TENANT, journal_key="starch and starke", year=2023, jif=2.0, quartile="Q3"
        )
    )
    db.add(
        LiteratureJournalMetric(
            id=2, tenant_id=TENANT, journal_key="starch and starke", year=2024, jif=3.0, quartile="Q2"
        )
    )
    await db.commit()
    assert (await library_pipeline.attach_journal_metrics(TENANT))["matched"] == 1
    assert paper.impact_factor == 3.0 and paper.quartile == "Q2"
    paper.meta_locked, paper.impact_factor = True, 7.0
    await db.commit()
    assert (await library_pipeline.attach_journal_metrics(TENANT))["protected"] == 1
    assert paper.impact_factor == 7.0


async def test_durable_library_waits_for_confirmation_and_vector_completion(env):
    db, _, monkeypatch, scope = env
    from contextlib import asynccontextmanager

    paper = await db.get(LiteraturePaper, 1)
    paper.is_reference, paper.pub_year = True, 2000
    await db.commit()

    async def user(_run):
        return scope.user

    @asynccontextmanager
    async def access(_user, _tenant):
        yield scope

    monkeypatch.setattr(workflow, "_request_user", user)
    monkeypatch.setattr(library_pipeline, "access_scope", access)
    started = await library_pipeline.run_full_pipeline(TENANT, cleaning_rules={"year": {"min": 2020}})
    reused = await library_pipeline.run_full_pipeline(TENANT, cleaning_rules={"year": {"min": 2020}})
    assert reused["run_id"] == started["run_id"] and reused["reused"]
    await library_pipeline.process_literature_library({}, started["run_id"])
    state = await library_pipeline.get_library_run(TENANT, started["run_id"])
    assert state["status"] == "WAITING_CONFIRM"
    assert state["steps"]["enrich_metadata"]["status"] == "PENDING"
    plan = state["steps"]["clean_preview"]["detail"]
    await cleaning.confirm_cleaning(TENANT, plan["plan_id"], plan["plan_hash"])
    await library_pipeline.process_literature_library({}, started["run_id"])
    final = await library_pipeline.get_library_run(TENANT, started["run_id"])
    assert final["status"] == "SUCCEEDED"
    assert final["steps"]["vector_build"]["detail"]["skipped"]


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


async def test_executor_waits_for_real_child_rows_before_publishing(env):
    db, _, monkeypatch, scope = env
    from yuxi.literature.reading import service as reading
    from yuxi.literature.notes import service as notes, wiki_publish

    calls = []

    async def user(_run):
        return scope.user

    @asynccontextmanager
    async def access(_user, _tenant):
        yield scope

    async def render(**kwargs):
        calls.append("render")
        db.add(render_row())
        await db.commit()
        return {"revision_id": REVISION, "status": "PENDING"}

    async def compile(**kwargs):
        assert kwargs["requested_by"] == "u1"
        calls.append("compile")
        db.add(
            LiteratureCompileJob(
                id=401,
                tenant_id=TENANT,
                paper_id=1,
                level="L1",
                status="PENDING",
                source_revision_id=REVISION,
                reading_version=1,
            )
        )
        await db.commit()
        return {"job_id": 401, "status": "PENDING"}

    async def publish(tenant, **kwargs):
        assert tenant == TENANT and kwargs["source_kb_id"] == "kb-closure"
        calls.append("publish")
        return {"published": True, "publication_id": "publication-closure"}

    monkeypatch.setattr(workflow, "_request_user", user)
    monkeypatch.setattr(workflow, "access_scope", access)
    monkeypatch.setattr(reading, "enqueue_reader_render", render)
    monkeypatch.setattr(notes, "enqueue_literature_compile", compile)
    monkeypatch.setattr(wiki_publish, "publish_literature_notes", publish)
    run = await workflow.start_workflow(tenant_id=TENANT, paper_rid=RID, mode="parse_compile", requested_by="u1")
    run_id = run["run_id"]
    assert (await workflow.process_literature_workflow({}, run_id))["waiting_for"] == "render"
    assert calls == ["render"]
    await workflow.process_literature_workflow({}, run_id)
    assert calls == ["render"]
    render = (await db.execute(select(LiteratureReaderRender))).scalar_one()
    render.status = "SUCCEEDED"
    await db.commit()
    assert (await workflow.process_literature_workflow({}, run_id))["waiting_for"] == "compile"
    assert calls == ["render", "compile"]
    job = (await db.execute(select(LiteratureCompileJob))).scalar_one()
    job.status = "SUCCEEDED"
    await db.commit()
    assert (await workflow.process_literature_workflow({}, run_id))["status"] == "SUCCEEDED"
    assert calls == ["render", "compile", "publish"]
    assert (await workflow.get_workflow(run_id, TENANT))["status"] == "SUCCEEDED"


async def test_missing_dual_review_is_failure_not_empty_success(env):
    db, _, _, _ = env
    from yuxi.literature import review

    db.add(render_row(dual=True, status="SUCCEEDED"))
    await db.commit()
    with pytest.raises(ValueError, match="禁止绕过"):
        await review.get_review_items(TENANT, REVISION, require_dual=True)


async def test_summary_step_enqueues_actual_summary_with_requester(env):
    db, _, monkeypatch, _ = env
    from yuxi.literature.translate import service

    async def enqueue(**kwargs):
        assert kwargs == {"paper_id": 1, "variant_kind": "summary", "requested_by": "u1"}
        return {"job_id": 511, "status": "PENDING"}

    monkeypatch.setattr(service, "enqueue_literature_translation", enqueue)
    paper = (await db.execute(select(LiteraturePaper).where(LiteraturePaper.id == 1))).scalar_one()
    result = await workflow._execute_step(
        SimpleNamespace(requested_by="u1"), paper, {"step": "summarize", "status": "PENDING"}
    )
    assert result[:2] == ("RUNNING", "511")


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


@pytest.mark.parametrize("changed", ["none", "stale", "corrupt", "missing", "concurrent"])
async def test_export_resolves_frozen_product_not_newer_auto_candidate(env, changed):
    import hashlib

    db, storage, monkeypatch, _ = env
    body = b"A protected human edited translation with scientific details."
    paper = await db.get(LiteraturePaper, 1)
    paper.artifact_pointers = {"variant_zh": "lar-export-frozen"}
    db.add(
        LiteratureArtifactRevision(
            id=1,
            revision_id="lar-export-frozen",
            tenant_id=TENANT,
            paper_id=1,
            kind="variant_zh",
            source="manual_frozen",
            frozen=True,
            created_by="u1",
            object_uri="kbasset://frozen-export",
            content_digest=hashlib.sha256(body).hexdigest(),
            source_revision_id=REVISION,
            input_reading_version=0 if changed == "stale" else 1,
            input_card_revision=0,
        )
    )
    db.add(
        LiteratureVariant(
            id=1,
            tenant_id=TENANT,
            paper_id=1,
            variant_kind="zh",
            lang="zh",
            parse_revision_id=REVISION,
            reading_version=1,
            generator_version="literature-translate-v1",
            uri="kbasset://automatic-candidate",
            content_digest="c" * 64,
        )
    )
    storage.files["automatic-candidate"] = b"Automatic candidate must not replace protected human product."
    if changed != "missing":
        storage.files["frozen-export"] = b"Corrupted" if changed == "corrupt" else body
    await db.commit()
    original_download = storage.adownload_file

    async def download(bucket, object_name):
        if object_name == "frozen-export" and changed == "concurrent":
            paper.artifact_pointers = {"variant_zh": "lar-new-pointer"}
            await db.commit()
        return await original_download(bucket, object_name)

    monkeypatch.setattr(storage, "adownload_file", download)
    if changed == "none":
        archive = await export_vault.export_obsidian_vault(TENANT)
        with zipfile.ZipFile(io.BytesIO(archive)) as vault:
            assert vault.read(f"{RID}/zh.md") == body
    else:
        with pytest.raises(ValueError):
            await export_vault.export_obsidian_vault(TENANT)


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


async def test_global_cards_are_owner_scoped_and_annotation_changes_stale_notes(env):
    db, _, _, scope = env
    from dataclasses import replace
    from yuxi.literature import cards
    from yuxi.literature.readops import get_note_uri
    from yuxi.storage.postgres.models_business import User

    db.add(User(id=1, uid="u1", username="u1", password_hash="fixture-only"))
    db.add(
        LiteratureCompileJob(
            id=1,
            tenant_id=TENANT,
            paper_id=1,
            level="L1",
            status="SUCCEEDED",
            source_revision_id=REVISION,
            reading_version=1,
            note_uri="kbasset://current",
            card_revision=0,
        )
    )
    await db.commit()
    await cards.write_card(TENANT, "", card_type="card-note", title="Private draft", content="Owner's draft")
    assert len(await cards.list_cards(TENANT, "")) == 1
    token = _access.set(replace(scope, user=SimpleNamespace(uid="u2")))
    try:
        assert await cards.list_cards(TENANT, "") == []
    finally:
        _access.reset(token)
    assert await get_note_uri(TENANT, RID, "L1") == "kbasset://current"
    paper = await db.get(LiteraturePaper, 1)
    paper.ai_value_score, paper.topic_score, paper.value_score = 4.0, 0.8, 4.0
    await db.commit()
    await cards.write_card(TENANT, RID, card_type="card-note", title="New question", content="A human hypothesis")
    assert await get_note_uri(TENANT, RID, "L1") is None
    assert (await db.get(LiteraturePaper, 1)).reading_version == 1
    assert paper.ai_value_score is None and paper.topic_score is None and paper.value_score == 0.0


async def test_native_conversation_binding_revalidates_tenant_owner_and_revoked_source(env):
    db, _, monkeypatch, _ = env
    from yuxi.literature import session as literature_session
    from yuxi.storage.postgres.models_business import Conversation, User

    db.add(User(id=1, uid="u1", username="u1", password_hash="fixture-only"))
    db.add(
        Conversation(
            id=1,
            tenant_id=TENANT,
            thread_id="thread-literature",
            uid="u1",
            agent_id="default-chatbot",
            extra_metadata={"literature_session": {"kind": "paper", "rid": RID}},
        )
    )
    await db.commit()

    async def tenant(_db, _uid):
        return TENANT

    monkeypatch.setattr("yuxi.services.principal.resolve_tenant_id", tenant)
    assert await literature_session.load_conversation_session("thread-literature", "someone") is None
    context = await literature_session.load_conversation_session("thread-literature", "u1")
    assert context["bound_rid"] == RID and context["reading_version"] == 1
    paper = await db.get(LiteraturePaper, 1)
    paper.reading_version = 2
    await db.commit()
    assert (await literature_session.load_conversation_session("thread-literature", "u1"))["reading_version"] == 2
    paper.trashed_at = workflow.utc_now()
    await db.commit()
    with pytest.raises(PermissionError, match="撤销"):
        await literature_session.load_conversation_session("thread-literature", "u1")


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
