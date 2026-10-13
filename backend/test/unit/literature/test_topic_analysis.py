"""Topic snapshots, immutable runs, ACL revocation and offline format safety."""

import asyncio
import io
from datetime import timedelta
from contextlib import asynccontextmanager
from types import SimpleNamespace
from zipfile import ZipFile

import numpy as np
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.literature import reference_import, topic_analysis, topic_legacy, topic_worker
from yuxi.literature.access import _access
from yuxi.literature.topic_contract import prepare_document, validate_result
from yuxi.literature.topic_worker import _validate_vectors
from yuxi.storage.postgres.models_business import Tenant
from yuxi.storage.postgres.models_knowledge import (
    LiteratureCitation,
    LiteraturePaper,
    LiteratureReferenceBatch,
    LiteratureReferenceMember,
    LiteratureReferenceRecord,
    LiteratureTopicRun,
    LiteratureTopicDocument,
)
from yuxi.utils.datetime_utils import utc_now

pytestmark = pytest.mark.unit


class MemoryObjects:
    def __init__(self):
        self.values = {}

    async def aupload_file(self, bucket, name, data, content_type=None):
        self.values[bucket, name] = data

    async def adownload_file(self, bucket, name):
        return self.values[bucket, name]


@pytest_asyncio.fixture
async def environment(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for model in (
            Tenant,
            LiteraturePaper,
            LiteratureCitation,
            LiteratureReferenceBatch,
            LiteratureReferenceMember,
            LiteratureReferenceRecord,
            LiteratureTopicRun,
            LiteratureTopicDocument,
        ):
            await conn.run_sync(model.__table__.create)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        db.add(Tenant(id=1, name="Topic fixture", created_at=utc_now()))
        await db.commit()

    @asynccontextmanager
    async def transaction():
        async with sessions() as db:
            try:
                yield db
            except BaseException:
                await db.rollback()
                raise

    @asynccontextmanager
    async def scope(uid="alice", allowed=None):
        async with sessions() as db:
            rids = set((await db.execute(select(LiteraturePaper.rid))).scalars())
        token = _access.set(
            SimpleNamespace(
                tenant_id=1, rids=frozenset(rids if allowed is None else allowed), user=SimpleNamespace(uid=uid)
            )
        )
        try:
            yield
        finally:
            _access.reset(token)

    async def dispatch(run_id):
        return True

    store = MemoryObjects()
    monkeypatch.setattr(topic_analysis.pg_manager, "get_async_session_context", transaction)
    monkeypatch.setattr(topic_analysis.pg_manager, "async_engine", engine)
    monkeypatch.setattr(topic_analysis, "get_minio_client", lambda: store)
    monkeypatch.setattr(topic_analysis, "dispatch", dispatch)
    monkeypatch.setattr(reference_import, "_store_source", lambda *args: _stored())
    from yuxi.models.providers.cache import model_cache

    monkeypatch.setattr(
        model_cache,
        "get_model_info",
        lambda spec: SimpleNamespace(model_id="BAAI/bge-m3", model_type="embedding", dimension=1024),
    )
    yield SimpleNamespace(scope=scope, store=store, sessions=sessions)
    await engine.dispose()


async def _stored():
    return "fixture/source"


async def populate(env):
    records = [
        f"PMID- {10000 + i}\nTI  - Rice topic {i}\nAB  - Abstract on grain and stress {i}\nDP  - 2020\n"
        for i in range(10)
    ]
    async with env.scope():
        return await reference_import.import_references(1, ("\n".join(records + records)).encode())


def result_for(documents):
    return {
        "schema_version": "literature-topics-v1",
        "models": {
            "joint_bge_m3": {
                "topics": [{"topic": 0, "count": len(documents) - 1, "label": "Grain", "keywords": []}],
                "assignments": [
                    {
                        "document_key": d["document_key"],
                        "topic": -1 if i == 0 else 0,
                        "membership": 0.5,
                        "centroid_cosine": 0.7,
                        "x": 1.0,
                        "y": 2.0,
                    }
                    for i, d in enumerate(documents)
                ],
            }
        },
    }


async def test_occurrence_mode_keeps_duplicates_without_duplicating_papers(environment):
    batch = await populate(environment)
    async with environment.scope():
        papers = await topic_analysis.preview(1)
        rows = await topic_analysis.preview(1, mode="records", batch_ids=[batch["batch_id"]])
    assert (papers["documents"], papers["unique_papers"]) == (10, 10)
    assert (rows["documents"], rows["unique_papers"]) == (20, 10)
    async with environment.scope(uid="bob"):
        with pytest.raises(PermissionError):
            await topic_analysis.preview(1, mode="records", batch_ids=[batch["batch_id"]])


async def test_history_survives_new_runs_and_revocation_hides_all_derived_data(environment):
    await populate(environment)
    async with environment.scope():
        first = await topic_analysis.start_run(
            1, {"allow_remote": True, "embedding_spec": "fixture:bge", "name": "First"}, {}
        )
        same = await topic_analysis.start_run(
            1, {"allow_remote": True, "embedding_spec": "fixture:bge", "name": "First"}, {}
        )
        assert first["run_id"] == same["run_id"]
        run, documents = await topic_analysis.authorized_run(1, first["run_id"])
        await topic_analysis.publish_result(run, documents, result_for(documents))
        second = await topic_analysis.start_run(
            1, {"allow_remote": True, "embedding_spec": "fixture:bge", "name": "Second"}, {}
        )
        assert len((await topic_analysis.list_runs(1))["items"]) == 2
        assert (await topic_analysis.get_run(1, first["run_id"]))["status"] == "SUCCEEDED"
        assert (await topic_analysis.result_view(1, first["run_id"], model="joint_bge_m3", topic=-1))["total"] == 1
        assert second["status"] == "PENDING"
    async with environment.scope(allowed=set()):
        assert (await topic_analysis.list_runs(1))["items"] == []
        with pytest.raises(PermissionError):
            await topic_analysis.export_run(1, first["run_id"], model="joint_bge_m3")
    async with environment.scope(uid="bob"):
        with pytest.raises(PermissionError):
            await topic_analysis.get_run(1, first["run_id"])


async def test_pending_cancel_and_preview_digest_prevent_silent_changed_input(environment):
    await populate(environment)
    async with environment.scope():
        with pytest.raises(ValueError, match="预览"):
            await topic_analysis.start_run(1, {"expected_digest": "0" * 64}, {})
        with pytest.raises(ValueError, match="显式"):
            await topic_analysis.start_run(1, {}, {})
        run = await topic_analysis.start_run(1, {"allow_remote": True, "embedding_spec": "fixture:bge"}, {})
        assert (await topic_analysis.cancel_run(1, run["run_id"]))["status"] == "CANCELLED"


def test_contract_rejects_extra_missing_duplicate_and_invalid_assignments():
    docs = [prepare_document("a", "a", {"title": "Rice A"}), prepare_document("b", "b", {"title": "Rice B"})]
    result = result_for(docs)
    assert validate_result(result, docs)["models"]["joint_bge_m3"]["outliers"] == 1
    for change in (
        lambda a: a.pop(),
        lambda a: a.append(a[0]),
        lambda a: a[0].update(membership=float("nan")),
        lambda a: a[0].update(topic=0.5),
    ):
        broken = result_for(docs)
        change(broken["models"]["joint_bge_m3"]["assignments"])
        with pytest.raises(ValueError):
            validate_result(broken, docs)


def test_embedding_indices_are_strict_bijection_and_order_is_normalized():
    payload = {
        "model": "model",
        "data": [{"index": 1, "embedding": [2.0] * 1024}, {"index": 0, "embedding": [1.0] * 1024}],
    }
    assert _validate_vectors(payload, 2, "model")[0, 0] == 1.0
    for index in (0.5, True, 0, None):
        broken = {"data": [{"index": index, "embedding": [1.0] * 1024}, {"index": 0, "embedding": [1.0] * 1024}]}
        with pytest.raises(ValueError):
            _validate_vectors(broken, 2, "model")


def test_legacy_zip_never_executes_pickle_and_rejects_unsafe_paths():
    stream = io.BytesIO()
    with ZipFile(stream, "w") as archive:
        archive.writestr("outputs/joint_bge_m3/document_topics.json", "[]")
        archive.writestr("outputs/joint_bge_m3/bertopic_model.pkl", b"not a model")
    assert set(topic_legacy.read_archive(stream)) == {"joint_bge_m3/document_topics.json"}
    stream = io.BytesIO()
    with ZipFile(stream, "w") as archive:
        archive.writestr("../outputs/joint_bge_m3/document_topics.json", "[]")
    with pytest.raises(ValueError, match="路径"):
        topic_legacy.read_archive(stream)


def test_matrix_header_is_checked_before_numpy_allocation():
    stream = io.BytesIO()
    np.save(stream, np.ones((2, 1024), dtype=np.float32) / 32, allow_pickle=False)
    assert topic_legacy.load_matrix(stream.getvalue(), 2).shape == (2, 1024)
    with pytest.raises(ValueError, match="尺寸"):
        topic_legacy.load_matrix(stream.getvalue(), 3)


def test_semantic_text_preserves_entities_and_representation_language_is_actual_text():
    doc = prepare_document(
        "a",
        "a",
        {
            "title": "RISBZ1 regulates ER stress",
            "abstract": "<正>水稻胚乳中 RISBZ1 与 OsbZIP58 的表达。",
            "language": "en",
        },
    )
    assert "RISBZ1" in doc["text"] and "OsbZIP58" in doc["text"] and "<正>" not in doc["text"]
    assert doc["language_of_text"] == "zh"


async def test_record_database_filter_does_not_pull_cross_database_duplicate_occurrences(environment):
    await populate(environment)
    async with environment.scope():
        await reference_import.import_references(
            1,
            b"Publication Type,Authors,Article Title,Abstract,Publication Year,Pubmed Id\n"
            b"J,Alice,Rice topic 0,Abstract on grain and stress 0,2020,10000\n",
            source_name="cross-database.csv",
            fmt="wos-csv",
        )
    async with environment.scope():
        pubmed = await topic_analysis.snapshot(1, mode="records", database="pubmed")
        wos = await topic_analysis.snapshot(1, mode="records", database="wos")
    assert len(pubmed) == 20 and all(d["source"] == "pubmed-nbib" for d in pubmed)
    assert len(wos) == 1 and wos[0]["source"] == "wos-csv"
    assert pubmed[0]["rid"] == wos[0]["rid"]  # Same paper, independently scoped occurrences.


@pytest.mark.parametrize(
    "attempts,user_cancel,expected", [(0, False, "PENDING"), (2, False, "FAILED"), (0, True, "CANCELLED")]
)
async def test_worker_interruption_is_not_a_user_cancel(environment, monkeypatch, attempts, user_cancel, expected):
    await populate(environment)
    async with environment.scope():
        dto = await topic_analysis.start_run(1, {"allow_remote": True, "embedding_spec": "fixture:bge"}, {})
    async with environment.sessions() as db:
        run = await db.get(LiteratureTopicRun, dto["run_id"])
        run.attempts = attempts
        await db.commit()

    async def interrupted(run, token):
        if user_cancel:
            async with environment.sessions() as db:
                current = await db.get(LiteratureTopicRun, run.run_id)
                current.cancel_requested = True
                await db.commit()
        raise asyncio.CancelledError()

    monkeypatch.setattr(topic_worker, "_execute", interrupted)
    result = await topic_worker.process_topic_run({}, dto["run_id"])
    async with environment.sessions() as db:
        run = await db.get(LiteratureTopicRun, dto["run_id"])
    assert result["status"] == run.status == expected
    assert run.lease_token is None and run.attempts == attempts + 1
    assert run.error is None if expected == "PENDING" else run.error


async def test_reconciler_handles_missing_heartbeat_cancellation_and_retry_budget(environment, monkeypatch):
    old = utc_now() - timedelta(minutes=5)
    async with environment.sessions() as db:
        for run_id, attempts, cancelled, heartbeat in (
            ("recover", 1, False, old),
            ("exhausted", 3, False, old),
            ("no-heartbeat", 1, False, None),
            ("cancelled", 1, True, utc_now()),
        ):
            db.add(
                LiteratureTopicRun(
                    run_id=run_id,
                    tenant_id=1,
                    created_by="alice",
                    name=run_id,
                    status="RUNNING",
                    mode="papers",
                    input_digest="0" * 64,
                    config_json={},
                    progress_json={},
                    attempts=attempts,
                    cancel_requested=cancelled,
                    lease_token="old",
                    heartbeat_at=heartbeat,
                    created_at=old,
                    updated_at=old,
                )
            )
        await db.commit()
    scheduled = []

    async def dispatch(run_id, *, attempt):
        scheduled.append((run_id, attempt))

    async def redis_set(*args, **kwargs):
        pass

    monkeypatch.setattr(topic_analysis, "dispatch", dispatch)
    await topic_worker.reconcile({"redis": SimpleNamespace(set=redis_set)})
    async with environment.sessions() as db:
        runs = {r.run_id: r for r in (await db.execute(select(LiteratureTopicRun))).scalars()}
    assert runs["recover"].status == runs["no-heartbeat"].status == "PENDING"
    assert runs["exhausted"].status == "FAILED" and runs["cancelled"].status == "CANCELLED"
    assert sorted(scheduled) == [("no-heartbeat", 1), ("recover", 1)]
    assert all(run.lease_token is None for run in runs.values())


async def test_stale_worker_cannot_switch_verified_result_pointer(environment):
    await populate(environment)
    async with environment.scope():
        dto = await topic_analysis.start_run(1, {"allow_remote": True, "embedding_spec": "fixture:bge"}, {})
        run, docs = await topic_analysis.authorized_run(1, dto["run_id"], full=True)
    async with environment.sessions() as db:
        current = await db.get(LiteratureTopicRun, run.run_id)
        current.status, current.lease_token = "RUNNING", "new-lease"
        await db.commit()
    with pytest.raises(asyncio.CancelledError):
        await topic_analysis.publish_result(run, docs, result_for(docs), lease_token="expired-lease")
    async with environment.sessions() as db:
        current = await db.get(LiteratureTopicRun, run.run_id)
        assert current.status == "RUNNING" and current.result_object is None
    await topic_analysis.publish_result(run, docs, result_for(docs), lease_token="new-lease")
    async with environment.sessions() as db:
        pointer = (await db.get(LiteratureTopicRun, run.run_id)).result_object
    with pytest.raises(asyncio.CancelledError):
        await topic_analysis.publish_result(run, docs, result_for(docs), lease_token="new-lease")
    async with environment.sessions() as db:
        assert (await db.get(LiteratureTopicRun, run.run_id)).result_object == pointer


async def test_dispatch_uses_bounded_stable_attempt_ids(monkeypatch):
    from yuxi.services import run_queue_service

    calls = []

    async def enqueue(*args, **kwargs):
        calls.append(kwargs["_job_id"])

    async def pool():
        return SimpleNamespace(enqueue_job=enqueue)

    monkeypatch.setattr(run_queue_service, "get_arq_pool", pool)
    for attempt in (0, 0, 1):
        assert await topic_analysis.dispatch("fixture", attempt=attempt)
    assert calls == ["topic:fixture:0", "topic:fixture:0", "topic:fixture:1"]


def test_malformed_embedding_payload_does_not_echo_provider_content():
    private = "private-provider-body-credential"
    with pytest.raises(ValueError) as failure:
        _validate_vectors({"data": [{"index": 0, "embedding": [private]}]}, 1, "model")
    assert private not in str(failure.value)


@pytest.mark.parametrize(
    "change",
    [
        lambda m: m.update(similarity=[{"a": 0, "b": -1, "cosine": 0.7}]),
        lambda m: m.update(hierarchy=[[0, 1, 0.2, 2]]),
        lambda m: m["yearly"].pop(),
        lambda m: m["yearly"][0].update(denominator=1),
        lambda m: m["yearly"].append(dict(m["yearly"][0])),
        lambda m: [r.update(year=2021) for r in m["yearly"]],
        lambda m: m["topics"][0].update(word_frequencies=[["grain", 0.3]]),
        lambda m: m["topics"][0].update(word_frequencies=[["grain", True]]),
        lambda m: m["topics"][0].update(word_frequencies=[["grain", 2], ["grain", 1]]),
        lambda m: m.update(analysis_year=True),
    ],
)
def test_chart_contract_rejects_misleading_analytics(change):
    docs = [prepare_document(k, k, {"title": "Rice", "year": 2020}) for k in ("a", "b")]
    result = result_for(docs)
    model = result["models"]["joint_bge_m3"]
    model.update(
        similarity=[],
        hierarchy=[],
        analysis_year=2026,
        yearly=[
            {"year": 2020, "topic": -1, "count": 1, "denominator": 2},
            {"year": 2020, "topic": 0, "count": 1, "denominator": 2},
        ],
    )
    model["topics"][0]["word_frequencies"] = [["grain", 2]]
    validate_result(result, docs)
    change(model)
    with pytest.raises(ValueError):
        validate_result(result, docs)


async def test_cached_refit_has_distinct_origin_and_keeps_source_run(environment):
    await populate(environment)
    async with environment.scope():
        dto = await topic_analysis.start_run(1, {"allow_remote": True, "embedding_spec": "fixture:bge"}, {})
        run, docs = await topic_analysis.authorized_run(1, dto["run_id"], full=True)
        await topic_analysis.publish_result(run, docs, result_for(docs))
        async with environment.sessions() as db:
            original = await db.get(LiteratureTopicRun, run.run_id)
            original.config_json = {
                **original.config_json,
                "origin": "legacy_offline",
                "archive_fingerprint": "0" * 64,
                "embedding_objects": {"joint_bge_m3": {"object": "fixture/matrix", "sha256": "0" * 64}},
            }
            await db.commit()
        refit = await topic_analysis.start_run(1, {"seed": 7}, {}, reuse_run_id=run.run_id)
        original_dto = await topic_analysis.get_run(1, run.run_id)
    assert refit["config"]["origin"] == "cached_refit" and not refit["config"]["allow_remote"]
    assert refit["config"]["source_run_id"] == run.run_id and refit["run_id"] != run.run_id
    assert original_dto["status"] == "SUCCEEDED" and original_dto["config"]["origin"] == "legacy_offline"


async def test_publication_budget_matches_reader_budget(environment, monkeypatch):
    await populate(environment)
    async with environment.scope():
        dto = await topic_analysis.start_run(1, {"allow_remote": True, "embedding_spec": "fixture:bge"}, {})
        run, docs = await topic_analysis.authorized_run(1, dto["run_id"], full=True)
    monkeypatch.setattr(topic_analysis, "MAX_RESULT_BYTES", 32)
    with pytest.raises(ValueError, match="安全预算"):
        await topic_analysis.publish_result(run, docs, result_for(docs))
    async with environment.sessions() as db:
        current = await db.get(LiteratureTopicRun, run.run_id)
        assert current.status == "PENDING" and current.result_object is None
    assert not environment.store.values  # Never mark an unreadable object as a successful result.
