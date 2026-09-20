import json
from itertools import count
from unittest.mock import AsyncMock

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from yuxi.storage.postgres.models_business import AgentRun
from yuxi.storage.postgres import models_knowledge as m

import pytest

from yuxi.services import wiki_candidate_service as svc
from yuxi.services.wiki_service import WikiServiceError


@pytest.fixture(autouse=True)
def isolated_run_boundary(monkeypatch):
    """Never dispatch a queue job or read production run results in this suite."""
    from yuxi.services import agent_run_service

    for name in (
        "create_wiki_candidate_run_view",
        "create_agent_run_view",
        "get_agent_run_result",
        "cancel_agent_run_view",
    ):
        monkeypatch.setattr(
            agent_run_service, name, AsyncMock(side_effect=AssertionError("unmocked run boundary")), raising=False
        )


@pytest.mark.parametrize("mutation", ["fake_id", "fake_quote", "operation", "too_many"])
def test_untrusted_output_must_match_frozen_evidence(mutation):
    evidence = [
        {"evidence_id": "wce_1", "text": "Wx regulates amylose synthesis.", "source_kb_id": "s", "file_id": "f"}
    ]
    candidate = {
        "kind": "alias",
        "title": "Waxy",
        "text": "Wx alias",
        "terms": ["Waxy"],
        "citations": [{"evidence_id": "wce_1", "quote": "Wx regulates amylose"}],
    }
    if mutation == "fake_id":
        candidate["citations"][0]["evidence_id"] = "model-invented"
    elif mutation == "fake_quote":
        candidate["citations"][0]["quote"] = "Wx regulates protein"
    elif mutation == "operation":
        candidate["execute"] = "publish"
    output = {"candidates": [candidate] * (13 if mutation == "too_many" else 1)}
    with pytest.raises(WikiServiceError):
        svc.validate_candidate_output(json.dumps(output), evidence)


def test_valid_output_assigns_ids_server_side_and_starts_unreviewed():
    evidence = [
        {"evidence_id": "wce_1", "text": "Wx regulates amylose synthesis.", "source_kb_id": "s", "file_id": "f"}
    ]
    output = {
        "candidates": [
            {
                "kind": "topic",
                "title": "Amylose",
                "text": "Navigation summary",
                "terms": ["amylose"],
                "citations": [{"evidence_id": "wce_1", "quote": "amylose synthesis"}],
            }
        ]
    }
    result = svc.validate_candidate_output(json.dumps(output), evidence)
    assert result[0]["candidate_id"].startswith("wcc_")
    assert result[0]["review_status"] == "PENDING"
    assert result[0]["include_in_publication"] is False


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        m.KnowledgeBase,
        m.KnowledgeWiki,
        m.WikiSourceBinding,
        m.KnowledgeFile,
        m.KnowledgeChunk,
        m.KnowledgeGraphEntity,
        m.KnowledgeGraphEntityAlias,
        m.KnowledgeGraphTriple,
        m.KnowledgeGraphRelationEvidence,
        m.WikiBuildSnapshot,
        m.WikiBuildSnapshotItem,
        m.WikiBuildRun,
        m.WikiBuildArtifact,
        m.WikiAuditEvent,
        m.WikiPage,
        m.WikiPageRevision,
        AgentRun,
    ]
    async with engine.begin() as conn:
        for model in tables:
            await conn.run_sync(model.__table__.create)
    ids = count(100)

    def assign_test_bigint_ids(session, *_):
        for obj in session.new:
            if isinstance(obj, tuple(tables[:-1])) and getattr(obj, "id", None) is None:
                obj.id = next(ids)

    event.listen(Session, "before_flush", assign_test_bigint_ids)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            session.add(
                m.KnowledgeBase(
                    id=1,
                    kb_id="s",
                    name="Source",
                    kb_type="milvus",
                    tenant_id=7,
                    share_config={"access_level": "global"},
                )
            )
            owner = m.KnowledgeWiki(
                id=1,
                wiki_id="w",
                kb_id="wiki-kb",
                tenant_id=7,
                created_by="u",
                security_domain=svc.wiki._security_domain(7, {"access_level": "global"}),
                policy_json={"auto_publish": True},
            )
            session.add_all(
                [
                    owner,
                    m.WikiSourceBinding(
                        id=1,
                        binding_id="bind",
                        wiki_id="w",
                        source_kb_id="s",
                        enabled=True,
                        security_domain=owner.security_domain,
                        created_by="u",
                    ),
                    m.KnowledgeFile(
                        id=1,
                        file_id="f",
                        kb_id="s",
                        filename="paper.pdf",
                        content_hash="hash",
                        active_parse_revision_id="p",
                        active_index_revision_id="i",
                        status="done",
                        is_folder=False,
                    ),
                    m.KnowledgeChunk(
                        id=1,
                        chunk_id="c",
                        kb_id="s",
                        file_id="f",
                        chunk_index=0,
                        content="Wx regulates amylose synthesis.",
                        source_provenance={"parse_revision_id": "p", "index_revision_id": "i"},
                    ),
                ]
            )
            await session.flush()
            _, _, manifest, _ = await svc.wiki._current_source_manifests(session, wiki=owner)
            session.add_all(
                [
                    m.WikiBuildSnapshot(
                        id=1,
                        snapshot_id="ss",
                        wiki_id="w",
                        tenant_id=7,
                        content_snapshot_hash=svc.wiki._digest(manifest),
                        retrieval_snapshot_hash="r",
                        manifest_json=manifest,
                    ),
                    m.WikiBuildSnapshotItem(id=1, snapshot_id="ss", source_kb_id="s", file_id="f"),
                    m.WikiBuildRun(
                        id=1,
                        build_id="b",
                        wiki_id="w",
                        tenant_id=7,
                        snapshot_id="ss",
                        build_key="key",
                        compiler_fingerprint="compiler",
                        verification_policy_version="v",
                        status="SUCCEEDED",
                        created_by="u",
                    ),
                    m.WikiPage(id=1, page_id="page", wiki_id="w", page_key="document:f", title="paper.pdf"),
                    m.WikiPageRevision(
                        id=1,
                        page_revision_id="pr",
                        page_id="page",
                        build_id="b",
                        content_markdown="# paper",
                        content_sha256="pagehash",
                    ),
                ]
            )
            await session.commit()
            yield session
    finally:
        event.remove(Session, "before_flush", assign_test_bigint_ids)
        await engine.dispose()


ARGS = {"wiki_id": "w", "tenant_id": 7, "actor_uid": "u", "permitted_source_kb_ids": {"s"}}


@pytest.mark.parametrize("policy_value", [None, False, 1, "true"])
async def test_default_disabled_zero_run_calls(db, monkeypatch, policy_value):
    from yuxi.services import agent_run_service

    if policy_value is not None:
        (await db.get(m.KnowledgeWiki, 1)).policy_json = {"llm_candidates_enabled": policy_value}
        await db.commit()
    run = AsyncMock()
    monkeypatch.setattr(agent_run_service, "create_wiki_candidate_run_view", run, raising=False)
    with pytest.raises(WikiServiceError, match="默认关闭"):
        await svc.generate_candidates(db, **ARGS, build_id="b", agent_slug="agent", chunk_ids=["c"])
    run.assert_not_called()


async def test_full_generate_collect_review_export_and_source_invalidation(db, monkeypatch):
    from yuxi.services import agent_run_service

    async def create_run(**kwargs):
        assert kwargs["current_uid"] == "u"
        assert "tenant_id" not in kwargs and "actor_uid" not in kwargs
        assert not db.in_transaction()  # Wiki lock released before queue entry.
        db.add(
            AgentRun(
                id="run",
                uid="u",
                tenant_id=7,
                request_id=kwargs["request_id"],
                agent_slug="agent",
                conversation_thread_id=kwargs["thread_id"],
                status="completed",
                input_payload={"wiki_candidate_profile": kwargs["candidate_profile"]},
            )
        )
        await db.commit()
        return {"run_id": "run"}

    monkeypatch.setattr(agent_run_service, "create_wiki_candidate_run_view", create_run, raising=False)
    await svc.set_candidate_policy(db, **ARGS, enabled=True)
    owner = await db.get(m.KnowledgeWiki, 1)
    assert owner.policy_json == {"auto_publish": True, "llm_candidates_enabled": True}
    await db.commit()
    batch = await svc.generate_candidates(db, **ARGS, build_id="b", agent_slug="agent", chunk_ids=["c"])
    assert batch["run_id"] == "run"
    evidence_id = batch["evidence"][0]["evidence_id"]
    output = json.dumps(
        {
            "candidates": [
                {
                    "kind": "alias",
                    "title": "Waxy",
                    "text": "Candidate summary only",
                    "terms": ["Waxy"],
                    "citations": [{"evidence_id": evidence_id, "quote": "amylose synthesis"}],
                }
            ]
        }
    )
    monkeypatch.setattr(
        agent_run_service, "get_agent_run_result", AsyncMock(return_value={"status": "completed", "output": output})
    )
    batch = await svc.collect_candidates(db, **ARGS, batch_id=batch["batch_id"])
    assert await svc.publication_candidate_navigation(db, "w", "b", 7) == []
    candidate = batch["candidates"][0]
    batch = await svc.review_candidate(
        db,
        **ARGS,
        batch_id=batch["batch_id"],
        candidate_id=candidate["candidate_id"],
        decision="APPROVED",
        basis="人工对照来源 p/i 引文并确认仅导航用途",
        expected_revision=1,
        include_in_publication=True,
    )
    entries = await svc.publication_candidate_navigation(db, "w", "b", 7)
    assert entries[0]["aliases"] == ["Waxy"]
    assert entries[0]["page_revision_id"] == "pr"
    assert "text" not in entries[0] and "quote" not in entries[0]
    canonical_entries = await svc.wiki._publication_navigation_entries(
        db, wiki_id="w", build_id="b", source_ids=["s"], document_terms={}
    )
    assert entries[0].keys() == canonical_entries[0].keys()
    log = (
        await db.execute(select(m.WikiAuditEvent).where(m.WikiAuditEvent.event_type == "CANDIDATE_REVIEWED"))
    ).scalar_one()
    assert log.actor_uid == "u" and log.payload_json["basis"].startswith("人工")
    chunk = await db.get(m.KnowledgeChunk, 1)
    chunk.content = "Changed without updating source version"
    await db.commit()
    assert await svc.publication_candidate_navigation(db, "w", "b", 7) == []
    with pytest.raises(WikiServiceError, match="证据内容已变化"):
        await svc.review_candidate(
            db,
            **ARGS,
            batch_id=batch["batch_id"],
            candidate_id=candidate["candidate_id"],
            decision="APPROVED",
            basis="again",
            expected_revision=2,
            include_in_publication=True,
        )


@pytest.mark.parametrize("override", [{"tenant_id": 8}, {"permitted_source_kb_ids": set()}])
async def test_cross_tenant_or_source_access_denied(db, override):
    with pytest.raises(WikiServiceError):
        await svc.set_candidate_policy(db, **{**ARGS, **override}, enabled=True)


async def test_cross_source_chunk_denied_before_run(db):
    await svc.set_candidate_policy(db, **ARGS, enabled=True)
    with pytest.raises(WikiServiceError, match="跨来源"):
        await svc.generate_candidates(db, **ARGS, build_id="b", agent_slug="agent", chunk_ids=["foreign"])


def model_output(evidence_id="wce_1", **overrides):
    candidate = {
        "kind": "alias",
        "title": "Waxy",
        "text": "Navigation only",
        "terms": ["Waxy"],
        "citations": [{"evidence_id": evidence_id, "quote": "amylose synthesis"}],
    }
    return json.dumps({"candidates": [{**candidate, **overrides}]})


@pytest.mark.parametrize(
    "overrides",
    [
        {"title": "x" * 121},
        {"title": " "},
        {"text": "x" * 1001},
        {"text": " "},
        {"terms": ["x" * 81]},
        {"terms": [" "]},
        {"terms": ["x"] * 9},
        {"terms": [123]},
        {"citations": [{"evidence_id": "wce_1", "quote": "amylose synthesis"}] * 5},
        {"candidate_id": "from-model"},
        {"review_status": "APPROVED"},
        {"tenant_id": 8},
        {"actor_uid": "other"},
    ],
)
def test_schema_and_field_limits(overrides):
    with pytest.raises(WikiServiceError):
        svc.validate_candidate_output(
            model_output(**overrides), [{"evidence_id": "wce_1", "text": "amylose synthesis"}]
        )


@pytest.mark.parametrize("output", [None, {}, "not json", "{}", '{"candidates":[]}', " " * (svc.MAX_OUTPUT_CHARS + 1)])
def test_invalid_or_oversized_output(output):
    with pytest.raises(WikiServiceError):
        svc.validate_candidate_output(output, [])


@pytest.fixture
async def generated(db, monkeypatch):
    from yuxi.services import agent_run_service

    async def create_run(
        *, db, current_uid, agent_slug, request_id, thread_id, input_message, candidate_profile, model_spec
    ):
        assert not db.in_transaction()
        assert current_uid == ARGS["actor_uid"]
        db.add(
            AgentRun(
                id="run",
                uid=current_uid,
                tenant_id=7,
                request_id=request_id,
                agent_slug=agent_slug,
                conversation_thread_id=thread_id,
                status="completed",
                input_payload={"wiki_candidate_profile": candidate_profile},
            )
        )
        await db.commit()
        return {"run_id": "run"}

    monkeypatch.setattr(agent_run_service, "create_wiki_candidate_run_view", create_run)
    await svc.set_candidate_policy(db, **ARGS, enabled=True)
    batch = await svc.generate_candidates(db, **ARGS, build_id="b", agent_slug="agent", chunk_ids=["c"])
    monkeypatch.setattr(
        agent_run_service,
        "get_agent_run_result",
        AsyncMock(return_value={"status": "completed", "output": model_output(batch["evidence"][0]["evidence_id"])}),
    )
    return batch


async def test_missing_restricted_run_adapter_fails_closed(db, monkeypatch):
    from yuxi.services import agent_run_service

    monkeypatch.delattr(agent_run_service, "create_wiki_candidate_run_view")
    await svc.set_candidate_policy(db, **ARGS, enabled=True)
    with pytest.raises(WikiServiceError, match="受限.*未接线"):
        await svc.generate_candidates(db, **ARGS, build_id="b", agent_slug="agent", chunk_ids=["c"])
    agent_run_service.create_agent_run_view.assert_not_called()
    assert (await db.execute(select(m.WikiBuildArtifact))).scalars().all() == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "other"),
        ("tenant_id", 8),
        ("uid", "other"),
        ("agent_slug", "other"),
        ("conversation_thread_id", "other"),
        ("input_payload", {"wiki_candidate_profile": {"version": 1}}),
    ],
)
async def test_collect_rejects_run_identity_mismatch(db, generated, field, value):
    from yuxi.services import agent_run_service

    run = await db.get(AgentRun, "run")
    setattr(run, field, value)
    await db.commit()
    with pytest.raises(WikiServiceError, match="运行不存在|不匹配"):
        await svc.collect_candidates(db, **ARGS, batch_id=generated["batch_id"])
    agent_run_service.get_agent_run_result.assert_not_called()


@pytest.mark.parametrize("mutation", ["fake_id", "fake_quote", "extra_key", "oversized"])
async def test_collect_rejects_untrusted_model_result(db, generated, monkeypatch, mutation):
    from yuxi.services import agent_run_service

    output = json.loads(model_output(generated["evidence"][0]["evidence_id"]))
    candidate = output["candidates"][0]
    if mutation == "fake_id":
        candidate["citations"][0]["evidence_id"] = "invented"
    elif mutation == "fake_quote":
        candidate["citations"][0]["quote"] = "fabricated quotation"
    elif mutation == "extra_key":
        output["execute"] = "publish"
    else:
        candidate["text"] = "x" * (svc.MAX_OUTPUT_CHARS + 1)
    monkeypatch.setattr(
        agent_run_service,
        "get_agent_run_result",
        AsyncMock(return_value={"status": "completed", "output": json.dumps(output)}),
    )
    with pytest.raises(WikiServiceError):
        await svc.collect_candidates(db, **ARGS, batch_id=generated["batch_id"])
    artifact = (
        await db.execute(select(m.WikiBuildArtifact).where(m.WikiBuildArtifact.artifact_id == generated["batch_id"]))
    ).scalar_one()
    assert artifact.metadata_json["state"] == "GENERATING"
    assert artifact.metadata_json["candidates"] == []
    assert await svc.publication_candidate_navigation(db, "w", "b", 7) == []


@pytest.mark.parametrize(
    "change", ["parse", "index", "hash", "deleted_chunk", "disabled_source", "source_acl", "policy"]
)
async def test_approved_navigation_invalidates_on_source_or_policy_change(db, generated, change):
    batch = await svc.collect_candidates(db, **ARGS, batch_id=generated["batch_id"])
    await svc.review_candidate(
        db,
        **ARGS,
        batch_id=batch["batch_id"],
        candidate_id=batch["candidates"][0]["candidate_id"],
        decision="APPROVED",
        basis="Checked verbatim evidence",
        expected_revision=1,
        include_in_publication=True,
    )
    assert await svc.publication_candidate_navigation(db, "w", "b", 7)
    if change in {"parse", "index", "hash"}:
        file = await db.get(m.KnowledgeFile, 1)
        setattr(
            file,
            {"parse": "active_parse_revision_id", "index": "active_index_revision_id", "hash": "content_hash"}[change],
            "changed",
        )
    elif change == "deleted_chunk":
        await db.delete(await db.get(m.KnowledgeChunk, 1))
    elif change == "disabled_source":
        (await db.get(m.WikiSourceBinding, 1)).enabled = False
    elif change == "source_acl":
        (await db.get(m.KnowledgeBase, 1)).share_config = {"access_level": "private"}
    else:
        await svc.set_candidate_policy(db, **ARGS, enabled=False)
    await db.commit()
    assert await svc.publication_candidate_navigation(db, "w", "b", 7) == []


@pytest.mark.parametrize("decision,opt_in", [("APPROVED", False), ("REJECTED", True), ("EDIT", True)])
async def test_only_explicit_approval_and_opt_in_can_export(db, generated, decision, opt_in):
    batch = await svc.collect_candidates(db, **ARGS, batch_id=generated["batch_id"])
    candidate_id = batch["candidates"][0]["candidate_id"]
    replacement = (
        json.loads(model_output(batch["evidence"][0]["evidence_id"]))["candidates"][0] if decision == "EDIT" else None
    )
    reviewed = await svc.review_candidate(
        db,
        **ARGS,
        batch_id=batch["batch_id"],
        candidate_id=candidate_id,
        decision=decision,
        basis="Manual review",
        expected_revision=1,
        include_in_publication=opt_in,
        replacement=replacement,
    )
    assert reviewed["candidates"][0]["revision"] == 2
    assert await svc.publication_candidate_navigation(db, "w", "b", 7) == []


@pytest.mark.parametrize("override", [{"tenant_id": 8}, {"permitted_source_kb_ids": set()}])
async def test_collect_cross_scope_denied_before_result(db, generated, override):
    from yuxi.services import agent_run_service

    with pytest.raises(WikiServiceError):
        await svc.collect_candidates(db, **{**ARGS, **override}, batch_id=generated["batch_id"])
    agent_run_service.get_agent_run_result.assert_not_called()
