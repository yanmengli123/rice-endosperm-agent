"""Topic snapshots, immutable runs, ACL revocation and offline format safety."""

import io
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from zipfile import ZipFile

import numpy as np
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.literature import reference_import, topic_analysis, topic_legacy
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
