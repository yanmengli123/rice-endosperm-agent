"""PaperAgent semantic contracts on actual relational rows; no provider calls."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.literature import access, diary, retrieval, session as sessions, topics
from yuxi.literature import search_runtime, bib_import
from yuxi.storage.postgres.models_business import User, Conversation, Message
from yuxi.storage.postgres.models_knowledge import (
    LiteratureCard,
    LiteratureCitation,
    LiteratureCompileJob,
    LiteratureDiaryEntry,
    LiteratureNoteSearch,
    LiteraturePaper,
    LiteratureTopicSet,
    LiteratureVariant,
    LiteratureNavigationIndex,
    LiteratureOriginalSearch,
    LiteratureReaderRender,
    LiteratureAttachment,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def test_multiple_note_levels_do_not_multiply_a_papers_rrf_weight(env, monkeypatch):
    from yuxi.literature import fulltext

    async def notes(*args, **kwargs):
        return [
            {"rid": "seed", "level": "L2", "body": "xunique strongest L2 match"},
            {"rid": "seed", "level": "L3", "body": "xunique weaker L3 match"},
        ]

    monkeypatch.setattr(fulltext, "search_notes", notes)
    result = await retrieval.search_compiled(7, "xunique")
    hit = next(item for item in result["hits"] if item["rid"] == "seed")
    assert hit["rrf_score"] == round(1 / 61, 6)
    assert hit["note_level"] == "L2" and "strongest L2" in hit["note_excerpt"]


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for model in (
            User,
            Conversation,
            Message,
            LiteraturePaper,
            LiteratureTopicSet,
            LiteratureCard,
            LiteratureCitation,
            LiteratureCompileJob,
            LiteratureVariant,
            LiteratureDiaryEntry,
            LiteratureNoteSearch,
            LiteratureNavigationIndex,
            LiteratureOriginalSearch,
            LiteratureReaderRender,
            LiteratureAttachment,
        ):
            await conn.run_sync(model.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:

        @asynccontextmanager
        async def context():
            yield db

        monkeypatch.setattr(topics.pg_manager, "get_async_session_context", context)
        scope = access.LiteratureAccess(
            7, frozenset({"seed", "neighbor", "card-only"}), frozenset({1, 2, 3}), {}, {}, SimpleNamespace(uid="u1")
        )
        token = access._access.set(scope)

        async def build(*args, **kwargs):
            return scope

        monkeypatch.setattr(topics, "build_access", build)
        monkeypatch.setattr(diary, "build_access", build)
        monkeypatch.setattr(search_runtime, "build_access", build)
        instant = datetime(2026, 10, 1, 16, 1, tzinfo=UTC)
        db.add_all(
            [
                LiteraturePaper(
                    id=1,
                    tenant_id=7,
                    rid="seed",
                    doi="10.1001/seed",
                    title="Starch",
                    keywords_json=["starch", "starch"],
                    wos_categories_json=["Plant Science"],
                    external_cited=10,
                    paper_rank=0.2,
                    created_at=instant,
                ),
                LiteraturePaper(
                    id=2,
                    tenant_id=7,
                    rid="neighbor",
                    doi="10.1001/neighbor",
                    title="Related paper",
                    keywords_json=["starch"],
                    wos_categories_json=["Plant Science"],
                    external_cited=20,
                    paper_rank=0.4,
                    created_at=instant,
                ),
                LiteraturePaper(id=3, tenant_id=7, rid="card-only", title="Uncompiled paper", created_at=instant),
                LiteraturePaper(id=4, tenant_id=7, rid="denied", title="Secret paper", keywords_json=["starch"]),
                LiteraturePaper(id=5, tenant_id=99, rid="foreign", title="Foreign secret", keywords_json=["starch"]),
                LiteratureCitation(id=1, tenant_id=7, citing_rid="seed", cited_doi="10.1001/neighbor"),
                LiteratureCard(
                    id=1,
                    tenant_id=7,
                    paper_id=3,
                    card_type="note",
                    slug="new",
                    title="Observation",
                    content="starch card direct match",
                    created_by="u1",
                ),
                LiteratureCard(
                    id=2,
                    tenant_id=7,
                    paper_id=None,
                    card_type="qa",
                    slug="mine",
                    title="My QA",
                    content="starch private card",
                    created_by="u1",
                ),
                LiteratureCard(
                    id=3,
                    tenant_id=7,
                    paper_id=None,
                    card_type="qa",
                    slug="other",
                    title="Secret QA",
                    content="starch hidden",
                    created_by="u2",
                ),
                LiteratureCard(
                    id=4,
                    tenant_id=7,
                    paper_id=4,
                    card_type="qa",
                    slug="denied",
                    title="Hidden QA",
                    content="starch hidden",
                    created_by="u1",
                ),
            ]
        )
        for paper_id in (1, 2):
            db.add(
                LiteratureCompileJob(
                    id=paper_id,
                    tenant_id=7,
                    paper_id=paper_id,
                    level="L1",
                    status="SUCCEEDED",
                    source_revision_id="",
                    reading_version=1,
                    card_revision=0,
                    note_uri=f"kbasset://note-{paper_id}",
                    updated_at=instant,
                )
            )
        await db.commit()
        try:
            yield db, scope
        finally:
            access._access.reset(token)
    await engine.dispose()


async def test_topic_compile_threshold_owner_and_stale_metadata(env):
    db, _ = env
    report = await topics.compile_topics(7, field="keywords", min_papers=2)
    assert report["topics"] == 1 and report["papers_covered"] == 2 and report["model_called"] is False
    result = await topics.list_topics(7, field="keywords")
    item = result["topics"][0]
    assert item["paper_count"] == 2 and item["avg_cited"] == 15
    assert "papers" not in item
    detail = await topics.list_topics(7, field="keywords", slug=item["slug"])
    assert {p["rid"] for p in detail["topics"][0]["papers"]} == {"seed", "neighbor"}
    row = await db.get(LiteraturePaper, 2)
    row.keywords_json = ["another"]
    await db.commit()
    stale = await topics.list_topics(7, field="keywords")
    assert stale["stale"] and stale["topics"] == []
    await topics.compile_topics(7, field="keywords", min_papers=1)
    assert (await topics.list_topics(7, field="keywords"))["total"] == 2


async def test_topic_revocation_and_other_owner_do_not_get_members(env, monkeypatch):
    _, scope = env
    await topics.compile_topics(7, field="wos_categories")
    other = access.LiteratureAccess(7, scope.rids, scope.paper_ids, {}, {}, SimpleNamespace(uid="u2"))
    token = access._access.set(other)
    try:
        assert not (await topics.list_topics(7))["compiled"]
    finally:
        access._access.reset(token)

    async def revoked(*args, **kwargs):
        return access.LiteratureAccess(7, frozenset({"seed"}), frozenset({1}), {}, {}, scope.user)

    monkeypatch.setattr(topics, "build_access", revoked)
    with pytest.raises(ValueError, match="权限"):
        await topics.list_topics(7)


async def test_diary_authorized_activity_dates_and_personal_notes(env):
    db, _ = env
    db.add(
        LiteratureDiaryEntry(id=1, tenant_id=7, uid="u1", entry_date=datetime(2026, 10, 3).date(), content="my diary")
    )
    db.add(
        LiteratureDiaryEntry(
            id=2, tenant_id=7, uid="u2", entry_date=datetime(2026, 10, 4).date(), content="secret diary"
        )
    )
    db.add(
        LiteratureVariant(
            id=1,
            tenant_id=7,
            paper_id=1,
            parse_revision_id="",
            variant_kind="summary",
            lang="zh",
            uri="kbasset://summary",
            content_digest="a" * 64,
        )
    )
    await db.commit()
    result = await diary.activity_calendar(7, "u1", "2026-10")
    day = next(item for item in result["activities"] if item["date"] == "2026-10-02")
    assert day["imports"] == 3 and day["kb"] == 2
    assert not next(p for p in day["papers"] if p["rid"] == "seed")["translated"], "summary is not translation"
    assert {p["rid"] for p in day["papers"]} == {"seed", "neighbor", "card-only"}
    assert result["days"] == ["2026-10-02", "2026-10-03"]
    exported = await diary.export_month(7, "u1", "2026-10")
    assert "[[seed]]" in exported and "my diary" in exported and "secret" not in exported
    with pytest.raises(ValueError):
        await diary.activity_calendar(7, "u1", "2026-13")


async def test_diary_delivery_rechecks_acl_and_personal_owner(env, monkeypatch):
    _, scope = env

    with pytest.raises(PermissionError, match="当前用户"):
        await diary.activity_calendar(7, "u2", "2026-10")

    async def revoked(*args, **kwargs):
        return access.LiteratureAccess(7, frozenset({"seed"}), frozenset({1}), {}, {}, scope.user)

    monkeypatch.setattr(diary, "build_access", revoked)
    with pytest.raises(ValueError, match="权限已变更"):
        await diary.activity_calendar(7, "u1", "2026-10")


async def test_compiled_recall_cards_before_compile_and_citation_neighbors(env, monkeypatch):
    async def vectors(*args, **kwargs):
        return [], "not configured"

    from yuxi.literature import navigation_index

    monkeypatch.setattr(navigation_index, "search_navigation_vectors", vectors)
    result = await retrieval.search_compiled(7, "starch")
    hits = {hit["rid"]: hit for hit in result["hits"]}
    assert hits["card-only"]["channels_used"] == ["cards"]
    assert hits["neighbor"]["channels_used"] == ["neighbor"]
    assert {"metadata", "cards", "neighbor"} <= set(result["channels_used"])
    assert len(result["global_cards"]) == 1 and result["global_cards"][0]["card_id"] == 2
    assert "denied" not in hits and "foreign" not in hits
    assert all(not ({"evidence_id", "quote", "claim", "content"} & set(hit)) for hit in result["hits"])


async def test_chat_creation_native_binding_without_library_snapshot(env, monkeypatch):
    from yuxi.services import conversation_service

    async def create(**kwargs):
        assert kwargs["current_uid"] == "u1"
        assert kwargs["metadata"] == {"literature_session": {"schema_version": 1, "kind": "chat", "rid": ""}}
        return {"thread_id": "native-chat"}

    async def forbidden(*args):
        pytest.fail("ordinary chat must not enumerate the library")

    monkeypatch.setattr(conversation_service, "create_thread_view", create)
    monkeypatch.setattr(sessions, "build_library_session_context", forbidden)
    result = await sessions.create_literature_conversation(tenant_id=7, user=SimpleNamespace(uid="u1"), kind="chat")
    assert result["session"]["kind"] == "chat"
    assert "visible_rids" not in result["context"]


async def test_topic_duplicate_labels_and_slug_collision_are_not_double_counted():
    def paper(rid, values):
        return dict(rid=rid, doi=None, title=rid, keywords=values, external_cited=0, paper_rank=0)

    groups = topics.aggregate_topics(
        [paper("a", ["A B", "A B", "A-B"]), paper("b", ["A B"])], field="keywords", min_papers=1
    )
    assert len(groups) == 2 and len({t["slug"] for t in groups}) == 2
    assert max(t["paper_count"] for t in groups) == 2


async def test_cards_excerpt_finds_late_match_and_cache_tracks_owner_cards_and_edges(env):
    db, _ = env
    request = {"channel": "compiled", "query": "starch"}
    before = await search_runtime.search_identity(7, request)
    card = await db.get(LiteratureCard, 2)
    card.content = "background " * 2000 + "starch critical late observation"
    await db.commit()
    after = await search_runtime.search_identity(7, request)
    assert before != after
    _, global_cards = await retrieval._card_recall(7, ["starch"], kind="", limit=10)
    assert "critical late observation" in global_cards[0]["navigation_excerpt"]
    assert len(global_cards[0]["navigation_excerpt"]) <= 600
    other = await db.get(LiteratureCard, 3)
    other.content = "other owner's new card"
    await db.commit()
    assert after == await search_runtime.search_identity(7, request)
    db.add(LiteratureCitation(id=2, tenant_id=7, citing_rid="neighbor", cited_doi="10.1001/seed"))
    await db.commit()
    assert after != await search_runtime.search_identity(7, request)


async def test_ordinary_chat_restore_revalidates_identity_without_library_binding(env, monkeypatch):
    db, _ = env
    from yuxi.services import principal

    db.add(User(id=1, uid="u1", username="u1", password_hash="fixture-only", account_scope_id="fixture-scope"))
    db.add(
        Conversation(
            id=1,
            tenant_id=7,
            uid="u1",
            thread_id="native-chat",
            agent_id="default-chatbot",
            extra_metadata={"literature_session": {"kind": "chat", "rid": ""}},
        )
    )
    await db.commit()

    async def tenant(*args):
        return 7

    monkeypatch.setattr(principal, "resolve_tenant_id", tenant)

    def no_library(*args):
        pytest.fail("restoring ordinary chat must not inject library context")

    monkeypatch.setattr(access, "access_scope", no_library)
    binding = await sessions.load_conversation_session("native-chat", "u1")
    assert binding["session_kind"] == "chat" and "visible_rids" not in binding
    assert await sessions.load_conversation_session("native-chat", "u2") is None
    user = await db.get(User, 1)
    user.is_disabled = True
    await db.commit()
    with pytest.raises(PermissionError, match="用户"):
        await sessions.load_conversation_session("native-chat", "u1")


async def test_bib_import_retains_topic_source_fields(env):
    from sqlalchemy import select

    db, _ = env
    await bib_import.import_bib(
        7,
        "@article{p,doi={10.1001/new},title={New paper},"
        "WC={Plant Sciences;Biochemistry},SC={Life Sciences},keywords={starch;rice}}",
    )
    paper = (await db.execute(select(LiteraturePaper).where(LiteraturePaper.doi == "10.1001/new"))).scalar_one()
    assert paper.wos_categories_json == ["Plant Sciences", "Biochemistry"]
    assert paper.research_areas_json == ["Life Sciences"]
    assert paper.keywords_json == ["starch", "rice"]


async def test_original_recall_opt_in_version_and_cache_identity(env, monkeypatch):
    from datetime import timedelta
    from yuxi.literature import original_search, navigation_index
    from sqlalchemy import select

    db, _ = env

    async def reader(*args):
        return {"ready": True, "markdown": "# Source\n\nOriginal-only mechanism starchloop."}

    async def vectors(*args, **kwargs):
        return [], "not configured"

    monkeypatch.setattr(original_search, "get_reader_bundle", reader)
    monkeypatch.setattr(navigation_index, "search_navigation_vectors", vectors)
    paper = await db.get(LiteraturePaper, 3)
    paper.source_revision_id = "original-revision"
    render = LiteratureReaderRender(id=1, tenant_id=7, revision_id="original-revision", status="SUCCEEDED")
    db.add(render)
    await db.commit()
    assert await original_search.sync_original(3)
    assert not (await retrieval.search_compiled(7, "starchloop"))["hits"]
    result = await retrieval.search_compiled(7, "starchloop", include_original=True)
    assert result["hits"][0]["rid"] == "card-only"
    assert result["hits"][0]["channels_used"] == ["original_fulltext"]
    assert not {"quote", "claim", "evidence_id"} & set(result["hits"][0])
    before = await search_runtime.search_identity(7, {"include_original": True})
    projection = (await db.execute(select(LiteratureOriginalSearch))).scalar_one()
    render.updated_at = projection.updated_at + timedelta(seconds=1)
    await db.commit()
    assert not await original_search.search_originals(7, ["starchloop"])
    assert before != await search_runtime.search_identity(7, {"include_original": True})
    render.updated_at = projection.updated_at
    paper.updated_at = projection.updated_at + timedelta(seconds=1)
    await db.commit()
    assert not await original_search.search_originals(7, ["starchloop"])
    assert before != await search_runtime.search_identity(7, {"include_original": True})


async def test_original_projection_cas_rejects_concurrent_edit(env, monkeypatch):
    from yuxi.literature import original_search

    db, _ = env

    async def reader(*args):
        paper = await db.get(LiteraturePaper, 3)
        paper.reading_version += 1
        await db.commit()
        return {"ready": True, "markdown": "Text from old generation"}

    monkeypatch.setattr(original_search, "get_reader_bundle", reader)
    with pytest.raises(ValueError, match="版本变更"):
        await original_search.sync_original(3)
    assert await db.get(LiteratureOriginalSearch, 3) is None


async def test_history_search_owner_acl_visible_text_and_pagination(env, monkeypatch):
    from yuxi.literature import session_search

    db, scope = env

    async def authorized(*args):
        return scope

    monkeypatch.setattr(session_search, "build_access", authorized)
    for idx, uid, kind, rid, status in (
        (1, "u1", "chat", "", "active"),
        (2, "u2", "chat", "", "active"),
        (3, "u1", "paper", "denied", "active"),
        (4, "u1", "chat", "", "archived"),
        (5, "u1", "chat", "", "deleted"),
    ):
        db.add(
            Conversation(
                id=idx,
                tenant_id=7,
                uid=uid,
                thread_id=f"history-{idx}",
                agent_id="default-chatbot",
                title="Discussion",
                status=status,
                extra_metadata={"literature_session": {"kind": kind, "rid": rid}},
            )
        )
        db.add(Message(id=idx, conversation_id=idx, role="assistant", content="starch public body"))
    db.add(Message(id=6, conversation_id=1, role="tool", content="starch hidden tool"))
    db.add(Message(id=7, conversation_id=1, role="assistant", content="<think>hiddenword</think>Visible answer"))
    db.add(Message(id=8, conversation_id=1, role="assistant", content="starch failed", delivery_status="failed"))
    await db.commit()
    result = await session_search.search_history(7, "u1", "starch")
    assert [hit["message_id"] for hit in result["items"]] == [1]
    assert (await session_search.search_history(7, "u1", "starch", archived=True))["items"][0]["id"] == "history-4"
    assert not (await session_search.search_history(7, "u1", "hiddenword"))["items"]
    with pytest.raises(PermissionError):
        await session_search.search_history(7, "u2", "starch")
    from dataclasses import replace

    checks = 0

    async def revoked_during_delivery(*args):
        nonlocal checks
        checks += 1
        return scope if checks == 1 else replace(scope, rids=frozenset())

    monkeypatch.setattr(session_search, "build_access", revoked_during_delivery)
    with pytest.raises(ValueError, match="权限已变更"):
        await session_search.search_history(7, "u1", "starch")
