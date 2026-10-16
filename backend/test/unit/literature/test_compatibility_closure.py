"""Real relational compatibility contracts, fake model/object transport only."""

import hashlib
import json
import re
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from yuxi.literature import (
    access,
    fulltext,
    retrieval,
    navigation_index,
    search_runtime,
    translation_workspace as workspace,
)
from yuxi.literature.translate import settings
from yuxi.literature import llm_gateway
from yuxi.storage.postgres.models_business import User, UserModelPreference, UserModelCredential, AgentRun, UsageLedger
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeBase,
    KnowledgeFile,
    KnowledgeParseRevision,
    KnowledgeParseArtifact,
    LiteraturePaper,
    LiteratureCompileJob,
    LiteratureNoteSearch,
    LiteratureAttachment,
    LiteratureCard,
    LiteratureCitation,
    LiteratureNavigationIndex,
    LiteratureOriginalSearch,
    LiteratureReaderRender,
    LiteratureTranslationTask,
    LiteratureTranslationSettings,
    LiteratureTranslationJob,
    LiteratureVariant,
)
from yuxi.utils.datetime_utils import utc_now

pytestmark = pytest.mark.unit


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for model in (
            User,
            UserModelPreference,
            UserModelCredential,
            AgentRun,
            UsageLedger,
            KnowledgeBase,
            KnowledgeFile,
            KnowledgeParseRevision,
            KnowledgeParseArtifact,
            LiteraturePaper,
            LiteratureCompileJob,
            LiteratureNoteSearch,
            LiteratureAttachment,
            LiteratureCard,
            LiteratureCitation,
            LiteratureNavigationIndex,
            LiteratureOriginalSearch,
            LiteratureReaderRender,
            LiteratureTranslationTask,
            LiteratureTranslationSettings,
            LiteratureTranslationJob,
            LiteratureVariant,
        ):
            await conn.run_sync(model.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:

        @asynccontextmanager
        async def context():
            try:
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

        monkeypatch.setattr(workspace.pg_manager, "get_async_session_context", context)
        user = User(id=1, uid="u1", username="owner", password_hash="fixture-unused", role="superadmin")
        db.add(user)
        db.add(KnowledgeBase(id=1, kb_id="kb", tenant_id=7, name="PDF source", kb_type="milvus", created_by="u1"))
        db.add(KnowledgeFile(id=1, file_id="file", kb_id="kb", filename="draft.pdf"))
        db.add(
            KnowledgeParseRevision(
                id=1,
                revision_id="draft",
                tenant_id=7,
                kb_id="kb",
                file_id="file",
                source_sha256="a" * 64,
                parser_fingerprint="b" * 64,
                pipeline_version="fixture",
                processing_mode="parse_only",
                status="PARSED_ONLY",
                created_by="u1",
                article_summary={"title": "Rice draft"},
            )
        )
        body = "# Rice draft\n\nStarch $x^2$ changes grain filling.\n\nER stress balances the response."
        digest = hashlib.sha256(body.encode()).hexdigest()
        db.add(
            KnowledgeParseArtifact(
                id=1,
                artifact_id="artifact",
                revision_id="draft",
                kind="indexed_markdown",
                object_uri="minio://test/draft.md",
                sha256=digest,
                size_bytes=len(body.encode()),
            )
        )
        await db.commit()
        scope = access.LiteratureAccess(7, frozenset({"p"}), frozenset({1}), {}, {}, user)
        token = access._access.set(scope)

        async def build(user, tenant):
            return replace(scope, user=user)

        monkeypatch.setattr(access, "build_access", build)
        monkeypatch.setattr(search_runtime, "build_access", build)

        async def request_user(row):
            return user

        monkeypatch.setattr("yuxi.literature.workflow._request_user", request_user)

        async def quotas(**kwargs):
            return {"tenant_id": 7, "credential_policy": "platform_only", "policy_version": 1}

        monkeypatch.setattr("yuxi.services.agent_run_service._enforce_user_quota", quotas)
        monkeypatch.setattr("yuxi.services.agent_run_service._enforce_platform_token_quota", quotas)
        model = SimpleNamespace(spec="fixture:chat", model_type="chat", provider_type="openai", display_name="Fixture")
        monkeypatch.setattr(settings.model_cache, "get_all_specs", lambda kind: [model])
        monkeypatch.setattr(settings.model_cache, "get_model_info", lambda spec: model if spec == model.spec else None)

        async def no_queue(*args, **kwargs):
            return False

        monkeypatch.setattr(workspace, "_dispatch", no_queue)

        async def vectors(*args, **kwargs):
            return [], "vector unavailable in offline fixture"

        monkeypatch.setattr(navigation_index, "search_navigation_vectors", vectors)
        files, calls = {"draft.md": body.encode()}, []

        class Storage:
            async def adownload_file(self, bucket, name):
                return files[name]

            async def aupload_file(self, **kwargs):
                files[kwargs["object_name"]] = kwargs["data"]

        storage = Storage()
        monkeypatch.setattr(workspace, "get_minio_client", lambda: storage)
        monkeypatch.setattr("yuxi.literature.drafts.get_minio_client", lambda: storage)

        class Model:
            async def call(self, messages, stream=False):
                calls.append(messages)
                text = "\n".join(item["content"] for item in messages)
                ids = text.split("待译段落（para_id）：", 1)[-1].split("\n", 1)[0].split(", ")
                translations = []
                for pid in ids:
                    paragraph = re.search(r"\[" + re.escape(pid) + r"\] (.*)", text).group(1)
                    tags = re.findall(r"\[\[MATH\d+\]\]", paragraph)
                    translations.append({"para_id": pid, "zh": "真实夹具译文 " + " ".join(tags)})
                return SimpleNamespace(
                    content=json.dumps({"translations": translations}),
                    usage_metadata={"input_tokens": 22, "output_tokens": 11},
                    response_metadata={"finish_reason": "stop"},
                )

        monkeypatch.setattr("yuxi.models.chat.select_model", lambda **kwargs: Model())
        await settings.save_settings(
            7, "u1", [{"id": "one", "model_spec": model.spec, "batch_chars": 3000, "reasoning_effort": "low"}], "one"
        )
        from yuxi.literature.translate import service as translate_service

        async def _skip_precheck(paper_id, requested_by, **kwargs):
            return None

        monkeypatch.setattr(translate_service, "_precheck_translation_model", _skip_precheck)
        try:
            yield db, storage, files, calls, scope
        finally:
            access._access.reset(token)
    await engine.dispose()


async def test_draft_translation_is_durable_governed_and_never_activates(env):
    db, _, _, calls, _ = env
    submitted = await workspace.enqueue_task(7, "u1", source_kind="draft", source_ref="draft")
    assert submitted["enqueue_deferred"] and submitted["status"] == "PENDING"
    reused = await workspace.enqueue_task(7, "u1", source_kind="draft", source_ref="draft")
    assert reused["task_id"] == submitted["task_id"] and reused["reused"]
    result = await workspace.process_literature_translation_task({}, submitted["task_id"])
    assert result["status"] == "SUCCEEDED" and len(calls) == 1
    variant = await workspace.get_task(7, "u1", submitted["task_id"], content=True)
    assert "$x^2$" in variant["markdown"] and len(variant["alignment"]) == 2
    assert variant["evidence"] is False and variant["alignment"][0]["para_id"] == "P001"
    assert (await db.get(KnowledgeParseRevision, 1)).status == "PARSED_ONLY"
    assert (await db.get(KnowledgeFile, 1)).active_parse_revision_id is None
    assert (await db.execute(select(func.count()).select_from(LiteraturePaper))).scalar_one() == 0
    assert (await db.execute(select(func.count()).select_from(LiteratureCompileJob))).scalar_one() == 0
    ledger = (await db.execute(select(UsageLedger))).scalar_one()
    assert (ledger.uid, ledger.tenant_id, ledger.total_tokens) == ("u1", 7, 33)
    assert await workspace.process_literature_translation_task({}, submitted["task_id"]) is None
    assert (await db.execute(select(func.count()).select_from(UsageLedger))).scalar_one() == 1


async def test_draft_owner_activation_digest_and_revocation_fail_closed(env):
    db, _, files, calls, scope = env
    submitted = await workspace.enqueue_task(7, "u1", source_kind="draft", source_ref="draft")
    token = access._access.set(replace(scope, user=SimpleNamespace(uid="u2")))
    try:
        with pytest.raises(PermissionError):
            await workspace.get_task(7, "u1", submitted["task_id"])
        with pytest.raises(ValueError):
            await workspace.get_task(7, "u2", submitted["task_id"])
    finally:
        access._access.reset(token)
    files["draft.md"] += b"tampered"
    result = await workspace.process_literature_translation_task({}, submitted["task_id"])
    assert result["status"] != "SUCCEEDED" and not calls
    revision = await db.get(KnowledgeParseRevision, 1)
    revision.status = "ACTIVE"
    await db.commit()
    with pytest.raises(ValueError):
        await workspace.get_task(7, "u1", submitted["task_id"])


async def test_settings_are_owner_only_and_task_snapshot_never_changes(env):
    db, _, _, _, _ = env
    before = await settings.get_settings(7, "u1")
    one_off = await settings.translation_options(7, "u1", model_spec="fixture:chat")
    assert one_off["model_spec"] == "fixture:chat"
    assert await settings.get_settings(7, "u1") == before
    with pytest.raises(ValueError, match="已登记模型"):
        await settings.translation_options(7, "u1", model_spec="unknown:private")
    with pytest.raises(ValueError):
        await settings.translation_options(7, "u1", profile_id="one", model_spec="fixture:chat")
    job = await workspace.enqueue_task(7, "u1", source_kind="draft", source_ref="draft")
    await settings.save_settings(
        7, "u1", [{"id": "one", "model_spec": "fixture:chat", "batch_chars": 12000, "reasoning_effort": "high"}], "one"
    )
    row = await db.get(LiteratureTranslationTask, job["task_id"])
    assert (row.options_json["batch_chars"], row.options_json["reasoning_effort"]) == (3000, "low")
    next_job = await workspace.enqueue_task(7, "u1", source_kind="draft", source_ref="draft")
    assert next_job["task_id"] != job["task_id"]
    with pytest.raises(ValueError):
        await settings.save_settings(7, "u1", [{"id": "x", "model_spec": "arbitrary:unknown"}], "x")
    with pytest.raises(ValueError):
        await settings.save_settings(7, "u1", [{"id": "x", "model_spec": "fixture:chat", "batch_chars": 48001}], "x")
    with pytest.raises(PermissionError):
        await settings.get_settings(99, "u1")


async def test_probe_requires_explicit_cost_ack_and_stops_on_corpus_limit(env):
    db, _, files, calls, _ = env
    body = "\n\n".join("Starch signaling in grain. " * 50 for _ in range(5))
    files["draft.md"] = body.encode()
    artifact = await db.get(KnowledgeParseArtifact, 1)
    artifact.sha256, artifact.size_bytes = hashlib.sha256(body.encode()).hexdigest(), len(body.encode())
    await db.commit()
    with pytest.raises(ValueError, match="费用"):
        await workspace.enqueue_task(7, "u1", task_kind="probe", source_kind="draft", source_ref="draft")
    assert calls == []
    before = await settings.get_settings(7, "u1")
    submitted = await workspace.enqueue_task(
        7, "u1", task_kind="probe", source_kind="auto", source_ref="current", cost_ack=True
    )
    result = await workspace.process_literature_translation_task({}, submitted["task_id"])
    assert result["status"] == "SUCCEEDED"
    task = await workspace.get_task(7, "u1", submitted["task_id"])
    assert [trial["status"] for trial in task["result"]["trials"]] == ["PASSED", "PASSED", "INSUFFICIENT_CORPUS"]
    assert task["result"]["suggested_batch_chars"] == 3600 and len(calls) == 2
    assert (await settings.get_settings(7, "u1"))["profiles"] == before["profiles"]
    assert task["result"]["auto_saved"] is False


async def test_probe_truncation_stops_after_one_call_no_automatic_paid_retry(env, monkeypatch):
    db, _, _, calls, _ = env
    row = LiteratureTranslationTask(
        id=22,
        tenant_id=7,
        requested_by="u1",
        task_kind="probe",
        source_kind="draft",
        source_ref="draft",
        source_digest="s",
        options_json={},
        status="RUNNING",
    )
    db.add(row)
    await db.commit()

    async def truncated(messages):
        calls.append(messages)
        return SimpleNamespace(content="{}", response_metadata={"finish_reason": "length"})

    monkeypatch.setattr(llm_gateway, "call", truncated)
    result = await workspace._probe(["word " * 500, "word " * 500], 22, 0)
    assert len(calls) == 1 and result["trials"][0]["status"] == "FAILED"
    assert result["suggested_batch_chars"] is None
    row.updated_at = utc_now() - timedelta(hours=1)
    await db.commit()
    await workspace.reconcile_literature_translation_tasks()
    await db.refresh(row)
    assert row.status == "DEAD", "A stale probe cannot silently spend the budget again"


async def test_l2_l3_and_attachment_recall_are_current_indexed_and_cache_bound(env):
    db, _, _, _, _ = env
    from yuxi.literature.fulltext import lexical_text

    db.add(LiteraturePaper(id=1, tenant_id=7, rid="p", title="Rice paper"))
    db.add(LiteraturePaper(id=2, tenant_id=7, rid="hidden", title="Hidden"))
    await db.flush()
    for index, level in enumerate(("L1", "L2", "L3"), 1):
        job = LiteratureCompileJob(
            id=index,
            tenant_id=7,
            paper_id=1,
            level=level,
            status="SUCCEEDED",
            source_revision_id="",
            reading_version=1,
            card_revision=0,
            note_uri=f"kbasset://{level}.md",
        )
        db.add(job)
        await db.flush()
        await fulltext.project_note(db, job, f"{level} rare-{level.lower()} starch context")
    for index, paper_id in ((1, 1), (2, 2)):
        text = "supplement-only ER signaling 淀粉独特结果"
        db.add(
            LiteratureAttachment(
                id=index,
                tenant_id=7,
                paper_id=paper_id,
                filename="si.txt",
                kind="si",
                uri=f"kbasset://{index}/original",
                content_digest="c",
                text_uri=f"kbasset://{index}/text",
                text_digest=hashlib.sha256(text.encode()).hexdigest(),
                text_body=text,
                text_lexical=lexical_text(text),
            )
        )
    await db.commit()
    for level in ("L2", "L3"):
        hits = await fulltext.search_notes(7, [f"rare-{level.lower()}"])
        assert len(hits) == 1 and hits[0]["level"] == level
        result = await retrieval.search_compiled(7, f"rare-{level.lower()}")
        assert result["hits"][0]["note_level"] == level
    result = await retrieval.search_compiled(7, "supplement-only")
    assert [hit["rid"] for hit in result["hits"]] == ["p"]
    assert result["hits"][0]["attachment_id"] == 1
    assert "attachment_fulltext" in result["channels_used"]
    assert not {"evidence_id", "quote", "claim", "content"}.intersection(result["hits"][0])
    _, manifest = await navigation_index._visible_inputs(7)
    assert set(manifest["1"]["notes"]) == {"L1", "L2", "L3"}
    assert len(manifest["1"]["attachments"]) == 1 and "2" not in manifest
    before = await search_runtime.search_identity(7, {})
    attachment = await db.get(LiteratureAttachment, 1)
    attachment.text_digest = "changed"
    await db.commit()
    assert before != await search_runtime.search_identity(7, {})
    job = await db.get(LiteratureCompileJob, 2)
    job.note_uri = "kbasset://new-L2.md"
    await db.commit()
    assert await fulltext.search_notes(7, ["rare-l2"]) == []


async def test_attachment_backfill_checksum_acl_cas_and_immutable_text(env, monkeypatch):
    from yuxi.literature import attachment_search

    db, storage, files, calls, _ = env
    monkeypatch.setattr(attachment_search, "get_minio_client", lambda: storage)
    db.add(LiteraturePaper(id=1, tenant_id=7, rid="p", title="Rice"))
    body = b"Supplementary local extraction retains starch calibration."
    digest = hashlib.sha256(body).hexdigest()
    files["legacy/si.txt"] = body
    row = LiteratureAttachment(
        id=20,
        tenant_id=7,
        paper_id=1,
        kind="si",
        filename="si.txt",
        uri="kbasset://legacy/si.txt",
        content_digest=digest,
    )
    db.add(row)
    await db.commit()
    assert await attachment_search.sync_attachment(row.id)
    assert row.text_digest == digest and row.text_body == body.decode()
    assert await access.authorize_asset(row.text_uri.removeprefix("kbasset://"))
    assert (await attachment_search.search_attachments(7, ["calibration"]))[0]["attachment_id"] == 20
    assert await attachment_search.sync_attachment(row.id) is False
    assert not calls, "Legacy repair is strictly local, never OCR/LLM"

    row.text_digest = None
    await db.commit()
    files["legacy/si.txt"] += b"tampered"
    with pytest.raises(ValueError, match="校验"):
        await attachment_search.sync_attachment(row.id)
    assert row.text_digest is None
    files["legacy/si.txt"] = body
    download = storage.adownload_file

    async def replaced_during_read(bucket, name):
        data = await download(bucket, name)
        row.content_digest = "newer original"
        row.uri = "kbasset://newer/si.txt"
        await db.commit()
        return data

    monkeypatch.setattr(storage, "adownload_file", replaced_during_read)
    assert await attachment_search.sync_attachment(row.id) is False
    assert row.text_digest is None, "CAS must not attach old text to a newer original"


@pytest.mark.parametrize("budget", [20_000, 1])
async def test_navigation_build_includes_all_levels_and_attachment_tail(env, monkeypatch, budget):
    from yuxi.literature import embedding_gateway

    db, storage, files, _, _ = env
    db.add(LiteraturePaper(id=1, tenant_id=7, rid="p", title="Rice"))
    for index, level in enumerate(("L1", "L2", "L3"), 1):
        uri = f"{level}.md"
        body = (level + " starch context\n") * 4000 + f"TAIL-{level}"
        files[uri] = body.encode()
        job = LiteratureCompileJob(
            id=index,
            tenant_id=7,
            paper_id=1,
            level=level,
            status="SUCCEEDED",
            source_revision_id="",
            reading_version=1,
            card_revision=0,
            note_uri=f"kbasset://{uri}",
        )
        db.add(job)
        await db.flush()
        await fulltext.project_note(db, job, body)
    files["si.txt"] = b"TAIL-ATTACHMENT starch supplement"
    db.add(
        LiteratureAttachment(
            id=20,
            tenant_id=7,
            paper_id=1,
            kind="si",
            filename="si.txt",
            uri="kbasset://original",
            content_digest="original",
            text_uri="kbasset://si.txt",
            text_digest=hashlib.sha256(files["si.txt"]).hexdigest(),
        )
    )
    await db.commit()
    _, manifest = await navigation_index._visible_inputs(7)
    row = LiteratureNavigationIndex(
        id=1,
        index_id="nav_offline",
        tenant_id=7,
        requested_by="u1",
        model_spec="fixture:embedding",
        collection_name="fixture",
        input_manifest=manifest,
    )
    db.add(row)
    await db.commit()

    async def policy(*args, **kwargs):
        return SimpleNamespace(model_spec="fixture:embedding")

    captured = {}

    async def encode(policy, texts):
        captured["texts"] = texts
        return [[0.0, 1.0] for _ in texts]

    def write(name, documents, vectors):
        captured["documents"] = documents

    monkeypatch.setattr(llm_gateway, "policy_for_job_attempt", policy)
    monkeypatch.setattr(embedding_gateway, "encode", encode)
    monkeypatch.setattr(navigation_index, "_write_collection", write)
    monkeypatch.setattr("yuxi.storage.minio.get_minio_client", lambda: storage)
    monkeypatch.setattr(navigation_index, "MAX_NAVIGATION_CHUNKS", budget)
    result = await navigation_index.process_literature_navigation_index({}, row.index_id)
    if budget == 1:
        assert result["status"] != "SUCCEEDED"
        assert not captured, "Over-budget builds must not call Embedding or activate partial snapshots"
        return
    assert result["status"] == "SUCCEEDED"
    assert {doc["product_kind"] for doc in captured["documents"] if doc["channel"] == "compiled"} == {
        "L1",
        "L2",
        "L3",
        "attachment",
    }
    assert all(any(f"TAIL-{kind}" in text for text in captured["texts"]) for kind in ("L1", "L2", "L3", "ATTACHMENT"))
    assert all(not {"evidence_id", "quote", "claim", "content"}.intersection(doc) for doc in captured["documents"])


async def test_newer_draft_config_prevents_old_publication_and_reasoning_is_frozen(env, monkeypatch):
    db, _, _, _, _ = env
    first = await workspace.enqueue_task(7, "u1", source_kind="draft", source_ref="draft")
    seen = []

    class SlowModel:
        async def call(self, messages, stream=False):
            await settings.save_settings(
                7,
                "u1",
                [{"id": "one", "model_spec": "fixture:chat", "batch_chars": 6000, "reasoning_effort": "high"}],
                "one",
            )
            await workspace.enqueue_task(7, "u1", source_kind="draft", source_ref="draft")
            return SimpleNamespace(
                content=json.dumps(
                    {"translations": [{"para_id": "P001", "zh": "淀粉 [[MATH0]]"}, {"para_id": "P002", "zh": "内质网"}]}
                ),
                usage_metadata={"input_tokens": 10, "output_tokens": 10},
                response_metadata={},
            )

    def select_model(**kwargs):
        seen.append(kwargs)
        return SlowModel()

    monkeypatch.setattr("yuxi.models.chat.select_model", select_model)
    assert (await workspace.process_literature_translation_task({}, first["task_id"]))["status"] == "STALE"
    row = await db.get(LiteratureTranslationTask, first["task_id"])
    assert row.variant_uri is None and seen[0]["reasoning_effort"] == "low"
    assert (await db.execute(select(func.count()).select_from(UsageLedger))).scalar_one() == 1


async def test_preflight_failure_releases_pending_native_run_without_charging(env):
    db, _, files, calls, _ = env
    job = await workspace.enqueue_task(7, "u1", source_kind="draft", source_ref="draft")
    row = await db.get(LiteratureTranslationTask, job["task_id"])
    files["draft.md"] += b"tampered"
    await workspace.process_literature_translation_task({}, row.id)
    run = await db.get(AgentRun, row.llm_run_id)
    assert run.status == "failed" and run.finished_at is not None
    assert calls == [] and (await db.execute(select(func.count()).select_from(UsageLedger))).scalar_one() == 0


async def test_detail_exposes_only_latest_translation_config_per_variant(env):
    from yuxi.literature import readops

    db, _, _, _, _ = env
    db.add(LiteraturePaper(id=1, tenant_id=7, rid="p", title="Rice"))
    for index, kind, version in ((1, "zh", 1), (2, "zh", 1), (3, "en_zh", 1), (4, "zh", 0)):
        db.add(
            LiteratureTranslationJob(
                id=index,
                tenant_id=7,
                paper_id=1,
                variant_kind=kind,
                source_revision_id="",
                reading_version=version,
                config_digest=str(index),
                status="PENDING" if index == 2 else "SUCCEEDED",
                model_spec="fixture:chat",
                options_json={"uid": "u1", "id": "private-profile", "batch_chars": 3000, "reasoning_effort": "low"},
            )
        )
    await db.commit()
    detail = await readops.get_paper_detail(7, "p")
    assert {job["job_id"] for job in detail["translation_jobs"]} == {2, 3}
    assert next(job for job in detail["translation_jobs"] if job["variant_kind"] == "zh")["status"] == "PENDING"
    assert all(set(job["options"]) == {"batch_chars", "reasoning_effort"} for job in detail["translation_jobs"])


async def test_explicit_native_custom_model_is_owner_bound_and_no_credentials_are_copied(env, monkeypatch):
    db, _, _, _, _ = env

    async def entitlement(**kwargs):
        return {"tenant_id": 7, "credential_policy": "byok_required", "policy_version": 2}

    async def custom(db, uid):
        assert uid == "u1"
        return {"owner-custom:model"}

    async def credential(db, uid, provider):
        assert (uid, provider) == ("u1", "owner-custom")
        return SimpleNamespace(id=42)

    monkeypatch.setattr("yuxi.services.agent_run_service._enforce_user_quota", entitlement)
    monkeypatch.setattr("yuxi.services.user_credential_service.list_active_custom_model_specs", custom)
    monkeypatch.setattr("yuxi.services.user_credential_service.get_active_user_credential", credential)
    policy = await llm_gateway.freeze_literature_policy(
        db,
        tenant_id=7,
        uid="u1",
        operation="translate",
        resource_ref="custom-model-fixture",
        model_spec="owner-custom:model",
    )
    assert policy.credential_source == "user_byok" and policy.credential_id == 42
    payload = (await db.get(AgentRun, policy.run_id)).input_payload
    assert payload["user_credential"] == {"credential_id": 42, "provider_id": "owner-custom"}
    assert not {"api_key", "base_url", "encrypted_key"}.intersection(payload)
    with pytest.raises(ValueError, match="模型不存在"):
        await llm_gateway.freeze_literature_policy(
            db,
            tenant_id=7,
            uid="u1",
            operation="translate",
            resource_ref="foreign-model-fixture",
            model_spec="other-custom:model",
        )


async def test_translation_checkpoint_renews_lease_and_fences_spend(env, monkeypatch):
    from yuxi.literature.translate.service import _translate_document, renew_translation_lease
    from yuxi.literature.provenance import StaleLiteratureInput

    db, _, _, calls, _ = env
    row = LiteratureTranslationTask(
        id=55,
        tenant_id=7,
        requested_by="u1",
        task_kind="draft",
        source_kind="draft",
        source_ref="draft",
        source_digest="fixture",
        options_json={},
        status="RUNNING",
        updated_at=utc_now() - timedelta(hours=1),
    )
    db.add(row)
    await db.commit()
    await renew_translation_lease(row, 0)
    await workspace.reconcile_literature_translation_tasks()
    await db.refresh(row)
    assert row.status == "RUNNING" and row.attempt == 0
    row.attempt = 1
    await db.commit()
    with pytest.raises(StaleLiteratureInput):
        await _translate_document(["Starch"], checkpoint=lambda: renew_translation_lease(row, 0))
    assert calls == [], "No repair path may spend after checkpoint detects a lost claim"


async def test_translation_config_roundtrip_and_force_use_new_generations(env, monkeypatch):
    from yuxi.literature.translate import service

    db, _, _, _, _ = env
    db.add(LiteraturePaper(id=1, tenant_id=7, rid="p", title="Rice"))
    await db.commit()
    queue_calls = []

    class Queue:
        async def enqueue_job(self, *args, **kwargs):
            queue_calls.append((args, kwargs))

    async def queue():
        return Queue()

    monkeypatch.setattr(service, "get_arq_pool", queue)
    a = await service.enqueue_literature_translation(paper_id=1, requested_by="u1")
    first = await db.get(LiteratureTranslationJob, a["job_id"])
    first.status, first.variant_uri = "SUCCEEDED", "kbasset://old-a.md"
    await db.commit()
    await settings.save_settings(
        7, "u1", [{"id": "one", "model_spec": "fixture:chat", "batch_chars": 6000, "reasoning_effort": "high"}], "one"
    )
    b = await service.enqueue_literature_translation(paper_id=1, requested_by="u1")
    second = await db.get(LiteratureTranslationJob, b["job_id"])
    second.status = "SUCCEEDED"
    await db.commit()
    await settings.save_settings(
        7, "u1", [{"id": "one", "model_spec": "fixture:chat", "batch_chars": 3000, "reasoning_effort": "low"}], "one"
    )
    restored = await service.enqueue_literature_translation(paper_id=1, requested_by="u1")
    assert restored["job_id"] > b["job_id"] and not restored["reused"]
    current = await db.get(LiteratureTranslationJob, restored["job_id"])
    assert current.generation == 1 and current.config_digest == first.config_digest
    assert first.status == "SUCCEEDED" and first.variant_uri == "kbasset://old-a.md"
    active = await service.enqueue_literature_translation(paper_id=1, requested_by="u1", force=True)
    assert active["reused"] and active["job_id"] == current.id, "Force must not steal a fresh lease"
    current.status = "SUCCEEDED"
    await db.commit()
    forced = await service.enqueue_literature_translation(paper_id=1, requested_by="u1", force=True)
    latest = await db.get(LiteratureTranslationJob, forced["job_id"])
    assert latest.id > current.id and latest.generation == 2 and current.status == "SUCCEEDED"
    assert latest.llm_run_id != current.llm_run_id and len(queue_calls) == 4
    latest.status = "RUNNING"
    latest.updated_at = utc_now() - timedelta(hours=1)
    await db.commit()
    replacement = await service.enqueue_literature_translation(paper_id=1, requested_by="u1", force=True)
    assert replacement["job_id"] > latest.id and latest.status == "STALE"
    assert (await db.get(AgentRun, latest.llm_run_id)).status == "failed", "Superseded unstarted quota slots must close"
    from yuxi.literature.provenance import StaleLiteratureInput

    with pytest.raises(StaleLiteratureInput):
        await service.renew_translation_lease(latest, 0)


async def test_draft_completed_configuration_roundtrip_creates_new_task(env):
    db, _, _, _, _ = env
    a = await workspace.enqueue_task(7, "u1", source_kind="draft", source_ref="draft")
    assert (await workspace.process_literature_translation_task({}, a["task_id"]))["status"] == "SUCCEEDED"
    await settings.save_settings(
        7, "u1", [{"id": "one", "model_spec": "fixture:chat", "batch_chars": 6000, "reasoning_effort": "high"}], "one"
    )
    b = await workspace.enqueue_task(7, "u1", source_kind="draft", source_ref="draft")
    assert (await workspace.process_literature_translation_task({}, b["task_id"]))["status"] == "SUCCEEDED"
    await settings.save_settings(
        7, "u1", [{"id": "one", "model_spec": "fixture:chat", "batch_chars": 3000, "reasoning_effort": "low"}], "one"
    )
    restored = await workspace.enqueue_task(7, "u1", source_kind="draft", source_ref="draft")
    assert restored["task_id"] > b["task_id"] and not restored["reused"]
    latest = await workspace.latest_draft_task(7, "u1", "draft")
    assert latest["task_id"] == restored["task_id"] and latest["status"] == "PENDING"
    assert (await db.get(LiteratureTranslationTask, a["task_id"])).status == "SUCCEEDED"


@pytest.mark.parametrize("draft", [False, True], ids=["paper", "draft"])
@pytest.mark.parametrize("later_status", ["PENDING", "DEAD"])
async def test_running_configuration_roundtrip_fences_old_spend_and_creates_latest(
    env, monkeypatch, draft, later_status
):
    from yuxi.literature.translate import service
    from yuxi.literature.provenance import StaleLiteratureInput

    db, _, _, calls, _ = env

    class Queue:
        async def enqueue_job(self, *args, **kwargs):
            return None

    async def queue():
        return Queue()

    monkeypatch.setattr(service, "get_arq_pool", queue)
    if not draft:
        db.add(LiteraturePaper(id=1, tenant_id=7, rid="p", title="Rice"))
        await db.commit()
    model, key = (LiteratureTranslationTask, "task_id") if draft else (LiteratureTranslationJob, "job_id")

    async def enqueue():
        if draft:
            return await workspace.enqueue_task(7, "u1", source_kind="draft", source_ref="draft")
        return await service.enqueue_literature_translation(paper_id=1, requested_by="u1")

    a = await enqueue()
    first = await db.get(model, a[key])
    first.status = "RUNNING"
    await db.commit()
    await settings.save_settings(
        7, "u1", [{"id": "one", "model_spec": "fixture:chat", "batch_chars": 6000, "reasoning_effort": "high"}], "one"
    )
    b = await enqueue()
    (await db.get(model, b[key])).status = later_status
    await db.commit()
    with pytest.raises(StaleLiteratureInput):
        await service._translate_document(["Starch"], checkpoint=lambda: service.renew_translation_lease(first, 0))
    assert calls == [], "Even a failed newer submission must fence old paid calls"
    await settings.save_settings(
        7, "u1", [{"id": "one", "model_spec": "fixture:chat", "batch_chars": 3000, "reasoning_effort": "low"}], "one"
    )
    current = await enqueue()
    assert current[key] > b[key] and not current["reused"], "A→B→A must not return the old running A"
    repeat = await enqueue()
    assert repeat[key] == current[key] and repeat["reused"], "Only the latest identical submission is idempotent"
