"""Real PostgreSQL RLS/dispatch/restore on an explicitly isolated fixture only.

Run with --confcutdir=test/integration/literature; no parent cleanup fixtures.
The fixture network has no public ports and all Docker resources carry a task label.
"""

import asyncio
import io
import os
from datetime import timedelta
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import insert, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from yuxi.literature import topic_analysis, topic_db, topic_legacy, topic_security, topic_worker
from yuxi.literature.access import _access
from yuxi.literature.topic_contract import corpus_digest, prepare_document
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import Tenant
from yuxi.storage.postgres.models_knowledge import (
    LiteraturePaper,
    LiteratureReferenceBatch,
    LiteratureReferenceMember,
    LiteratureReferenceRecord,
    LiteratureTopicDocument,
    LiteratureTopicRun,
)
from yuxi.utils.datetime_utils import utc_now

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("YUXI_TOPIC_SECURITY_FIXTURE") != "1",
        reason="Requires isolated PostgreSQL fixture opt-in",
    ),
]


@pytest_asyncio.fixture
async def fixture_db(monkeypatch):
    url = make_url(os.environ["YUXI_TOPIC_SECURITY_DATABASE_URL"])
    assert url.database == "topic_fixture" and url.host == "topic-postgres", "Refuse any non-fixture target"
    admin = create_async_engine(url, hide_parameters=True)
    async with admin.begin() as conn:
        assert await conn.scalar(text("SELECT current_database()")) == "topic_fixture"
        for model in (
            Tenant,
            LiteraturePaper,
            LiteratureReferenceBatch,
            LiteratureReferenceMember,
            LiteratureReferenceRecord,
            LiteratureTopicRun,
            LiteratureTopicDocument,
        ):
            await conn.run_sync(model.__table__.create)
        await pg_manager._migration_0111_literature_reference_batches(conn)
        await pg_manager._migration_0112_literature_topic_analysis(conn)
        await conn.execute(text("ALTER TABLE literature_papers ENABLE ROW LEVEL SECURITY"))
        await conn.execute(
            text(
                "CREATE POLICY paper_tenant ON literature_papers USING "
                "(tenant_id::text=current_setting('yuxi.tenant_id',true))"
            )
        )
        await topic_security.migrate(conn)
        await conn.execute(text("CREATE ROLE topic_fixture_app LOGIN PASSWORD 'fixture' IN ROLE yuxi_topic_runtime"))
        await conn.execute(
            text("CREATE ROLE topic_fixture_dispatch LOGIN PASSWORD 'fixture' IN ROLE yuxi_topic_scheduler")
        )
        await conn.execute(insert(Tenant), [{"id": i, "name": f"Fixture {i}", "created_at": utc_now()} for i in (1, 2)])
        docs = [
            prepare_document(
                f"FIX_{i:06d}",
                f"p{i}",
                {
                    "title": f"水稻胚乳灌浆淀粉合成与基因表达 {i}" if i < 2 else f"Rice stress {i}",
                    "abstract": "Grain development and ER stress",
                    "year": 2020,
                    "pmid": str(10000 + i),
                },
            )
            for i in range(10)
        ]
        await conn.execute(
            insert(LiteraturePaper),
            [
                {
                    "id": i + 1,
                    "tenant_id": 1,
                    "rid": d["rid"],
                    "title": d["title"],
                    "abstract": d["abstract"],
                    "pub_year": 2020,
                    "pmid": d["pmid"],
                    "reading_version": 1,
                    "is_reference": False,
                    "meta_locked": False,
                    "first_seen": utc_now(),
                    "updated_at": utc_now(),
                }
                for i, d in enumerate(docs)
            ],
        )
        for run_id, tenant, uid in (("alice", 1, "alice"), ("bob", 1, "bob"), ("other-tenant", 2, "alice")):
            await conn.execute(
                insert(LiteratureTopicRun),
                [
                    {
                        "run_id": run_id,
                        "tenant_id": tenant,
                        "created_by": uid,
                        "name": run_id,
                        "status": "SUCCEEDED",
                        "mode": "records",
                        "input_digest": "0" * 64,
                        "config_json": {},
                        "progress_json": {},
                        "attempts": 0,
                        "cancel_requested": False,
                        "created_at": utc_now(),
                        "updated_at": utc_now(),
                    }
                ],
            )

    def role_url(name):
        return url.set(username=name, password="fixture").render_as_string(hide_password=False)

    monkeypatch.setenv("YUXI_TOPIC_DATABASE_URL", role_url("topic_fixture_app"))
    monkeypatch.setenv("YUXI_TOPIC_SCHEDULER_DATABASE_URL", role_url("topic_fixture_dispatch"))
    monkeypatch.setenv("YUXI_TOPIC_REQUIRE_RLS", "true")
    objects = {}

    class Store:
        async def aupload_file(self, bucket, name, data, content_type=None):
            objects[name] = data

        async def adownload_file(self, bucket, name):
            return objects[name]

    monkeypatch.setattr(topic_analysis, "get_minio_client", Store)
    await topic_db.close()
    yield SimpleNamespace(admin=admin, docs=docs, objects=objects, url=url, role_url=role_url)
    await topic_db.close()
    await admin.dispose()


@asynccontextmanager
async def scope(tenant=1, uid="alice"):
    token = _access.set(
        SimpleNamespace(tenant_id=tenant, user=SimpleNamespace(uid=uid), rids=frozenset(f"p{i}" for i in range(10)))
    )
    try:
        yield
    finally:
        _access.reset(token)


async def test_real_rls_pool_dispatch_concurrent_import_and_restore(fixture_db, monkeypatch):
    assert (await topic_db.readiness())["ready"]
    # Same pooled DB login, different authenticated actors; no cross-checkout leakage.
    for tenant, uid, expected in ((1, "alice", "alice"), (1, "bob", "bob"), (2, "alice", "other-tenant")):
        with topic_db.actor(tenant, uid):
            async with topic_db.session() as db:
                assert list((await db.execute(select(LiteratureTopicRun.run_id))).scalars()) == [expected]
                changed = await db.execute(
                    text("UPDATE literature_topic_runs SET name='forbidden' WHERE run_id=:id"),
                    {"id": "bob" if uid == "alice" else "alice"},
                )
                assert changed.rowcount == 0
    with topic_db.actor(1, "alice"), pytest.raises(RuntimeError):
        async with topic_db.session() as db:
            await db.execute(
                text(
                    "INSERT INTO literature_topic_runs SELECT 'forged',2,created_by,name,status,mode,input_digest,"
                    "config_json,progress_json,result_object,error,attempts,lease_token,heartbeat_at,"
                    "cancel_requested,created_at,updated_at FROM literature_topic_runs WHERE run_id='alice'"
                )
            )
    with pytest.raises(PermissionError):
        async with topic_db.session():
            pass
    async with topic_db.session(scheduler=True) as db:
        assert len(list((await db.execute(select(LiteratureTopicRun.run_id))).scalars())) == 3
    for sql in ("SELECT * FROM literature_topic_documents", "UPDATE literature_topic_runs SET result_object='forged'"):
        with pytest.raises(RuntimeError):
            async with topic_db.session(scheduler=True) as db:
                await db.execute(text(sql))

    # Concurrent, identical offline requests serialize in the real PG advisory lock.
    from test.unit.literature.test_topic_legacy_import import _mini_zip

    payload = _mini_zip(fixture_db.docs)

    async def import_one():
        async with scope():
            return await topic_legacy.import_archive(1, io.BytesIO(payload))

    first, second = await asyncio.gather(import_one(), import_one())
    assert first["run_id"] == second["run_id"] and sorted((first["reused"], second["reused"])) == [False, True]
    async with scope(uid="bob"):
        with pytest.raises(PermissionError):
            await topic_analysis.get_run(1, first["run_id"])
    async with scope():
        dto = await topic_analysis.get_run(1, first["run_id"])
        assert dto["status"] == "SUCCEEDED" and dto["progress"]["documents"] == 10

    # A superuser DSN must not pass the production readiness gate.
    monkeypatch.setenv("YUXI_TOPIC_DATABASE_URL", fixture_db.url.render_as_string(hide_password=False))
    assert not (await topic_db.readiness())["ready"]
    monkeypatch.setenv("YUXI_TOPIC_DATABASE_URL", fixture_db.role_url("topic_fixture_app"))

    # Real PG recovery and publication at the user's corpus scale. The CPU/model
    # computation is a deterministic stub here, not claimed as a 22k full refit.
    docs = [{**fixture_db.docs[0], "document_key": f"large_{i}"} for i in range(22549)]
    now = utc_now()
    async with fixture_db.admin.begin() as conn:
        await conn.execute(
            insert(LiteratureTopicRun),
            [
                {
                    "run_id": "large",
                    "tenant_id": 1,
                    "created_by": "alice",
                    "name": "Large recovery fixture",
                    "status": "RUNNING",
                    "mode": "records",
                    "input_digest": corpus_digest(docs),
                    "config_json": {},
                    "progress_json": {},
                    "attempts": 1,
                    "cancel_requested": False,
                    "lease_token": "expired",
                    "heartbeat_at": now - timedelta(minutes=5),
                    "created_at": now,
                    "updated_at": now,
                }
            ],
        )
        for start in range(0, len(docs), 500):
            await conn.execute(
                insert(LiteratureTopicDocument),
                [
                    {
                        "run_id": "large",
                        "tenant_id": 1,
                        "document_key": d["document_key"],
                        "rid": d["rid"],
                        "ordinal": start + i,
                        "text_sha256": d["text_sha256"],
                        "payload_json": d,
                    }
                    for i, d in enumerate(docs[start : start + 500])
                ],
            )
    scheduled = []

    async def dispatch(run_id, *, attempt):
        scheduled.append((run_id, attempt))

    async def heartbeat(*args, **kwargs):
        pass

    async def execute(run, token):
        assert topic_db._identity.get() == (1, "alice")
        frozen = await topic_analysis.load_documents(run.run_id)
        assert len(frozen) == 22549
        result = {
            "schema_version": "literature-topics-v1",
            "models": {
                "joint_bge_m3": {
                    "topics": [{"topic": 0, "count": len(frozen), "label": "Fixture", "keywords": []}],
                    "assignments": [
                        {
                            "document_key": d["document_key"],
                            "topic": 0,
                            "membership": 0.5,
                            "centroid_cosine": 0.7,
                            "x": 1.0,
                            "y": 2.0,
                        }
                        for d in frozen
                    ],
                }
            },
        }
        await topic_analysis.publish_result(run, frozen, result, lease_token=token)

    monkeypatch.setattr(topic_analysis, "dispatch", dispatch)
    monkeypatch.setattr(topic_worker, "_execute", execute)
    await topic_worker.reconcile({"redis": SimpleNamespace(set=heartbeat)})
    assert scheduled == [("large", 1)]
    with topic_db.actor(1, "alice"), pytest.raises(asyncio.CancelledError):
        await topic_worker._update("large", "expired", progress={"stage": "forged"})
    assert (await topic_worker.process_topic_run({}, "large"))["status"] == "SUCCEEDED"
    async with scope():
        large = await topic_analysis.get_run(1, "large")
        assert large["attempts"] == 2 and large["models"]["joint_bge_m3"]["documents"] == 22549

    # A real dump/restore of this fixture, never the user's PostgreSQL container.
    import httpx

    container = os.environ["YUXI_TOPIC_FIXTURE_CONTAINER"]
    assert container.startswith("topic-prod-fixture-")
    async with httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(uds="/var/run/docker.sock"), base_url="http://docker", timeout=180
    ) as client:
        info = (await client.get(f"/containers/{container}/json")).json()
        assert info["Config"]["Labels"]["yuxi.topic-fixture"] == "true"
        for command in (
            ["pg_dump", "-U", "fixture", "-d", "topic_fixture", "-Fc", "-f", "/tmp/topic-fixture.dump"],
            ["createdb", "-U", "fixture", "topic_restore"],
            ["pg_restore", "-U", "fixture", "-d", "topic_restore", "--exit-on-error", "/tmp/topic-fixture.dump"],
        ):
            response = await client.post(
                f"/containers/{container}/exec",
                json={
                    "Cmd": command,
                    "AttachStdout": True,
                    "AttachStderr": True,
                    "Tty": True,
                },
            )
            response.raise_for_status()
            execution = response.json()["Id"]
            result = await client.post(f"/exec/{execution}/start", json={"Detach": False, "Tty": True})
            result.raise_for_status()
            assert (await client.get(f"/exec/{execution}/json")).json()["ExitCode"] == 0, result.text[-1000:]
    restored_url = make_url(fixture_db.role_url("topic_fixture_app")).set(database="topic_restore")
    monkeypatch.setenv("YUXI_TOPIC_DATABASE_URL", restored_url.render_as_string(hide_password=False))
    async with scope():
        restored = await topic_analysis.get_run(1, first["run_id"])
        assert restored["input_digest"] == dto["input_digest"] and restored["models"] == dto["models"]
        recovered = await topic_analysis.get_run(1, "large")
        assert recovered["input_digest"] == large["input_digest"] and recovered["models"] == large["models"]
    async with scope(uid="bob"):
        with pytest.raises(PermissionError):
            await topic_analysis.get_run(1, first["run_id"])
