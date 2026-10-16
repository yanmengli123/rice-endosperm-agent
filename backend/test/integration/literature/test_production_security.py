"""Real PostgreSQL runtime grants and pooled identities, on a labelled fixture only."""

import os
import asyncio
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.exc import DBAPIError

from yuxi.literature.production_security import migrate
from yuxi.storage.postgres.models_business import Base as BusinessBase, Tenant, AgentRun
from yuxi.storage.postgres.models_knowledge import Base as KnowledgeBase
from yuxi.storage.postgres.models_knowledge import LiteraturePaper, LiteratureTranslationJob, LiteraturePdfModelCall
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.runtime_security import RuntimeAsyncSession, actor
from yuxi.literature import pdf_translation as service

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("YUXI_PDF_SECURITY_FIXTURE") != "1", reason="Requires isolated production fixture"),
]


@pytest_asyncio.fixture
async def protected_database():
    from sqlalchemy.engine import make_url

    url = make_url(os.environ["YUXI_PDF_SECURITY_DATABASE_URL"])
    assert url.host == "pdf-postgres" and url.database == "pdf_fixture"
    admin = create_async_engine(url, hide_parameters=True)
    async with admin.begin() as conn:
        assert await conn.scalar(text("SELECT current_database()")) == "pdf_fixture"
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
        for role in ("pdf_fixture_api", "pdf_fixture_worker"):
            await conn.execute(text(f"DROP ROLE IF EXISTS {role}"))
        await conn.run_sync(KnowledgeBase.metadata.create_all)
        await conn.run_sync(BusinessBase.metadata.create_all)
        await conn.execute(text("CREATE TABLE IF NOT EXISTS schema_migrations(version varchar PRIMARY KEY)"))
        await conn.execute(text("ALTER TABLE agent_runs ENABLE ROW LEVEL SECURITY"))
        await conn.execute(text("CREATE POLICY own_run ON agent_runs USING (uid=current_setting('yuxi.uid',true))"))
        await migrate(conn)
        await pg_manager._migration_0117_pdf_usage_resolution(conn)
        await conn.execute(text("CREATE ROLE pdf_fixture_api LOGIN PASSWORD 'fixture' IN ROLE yuxi_api_runtime"))
        await conn.execute(text("CREATE ROLE pdf_fixture_worker LOGIN PASSWORD 'fixture' IN ROLE yuxi_pdf_runtime"))
        await conn.execute(insert(Tenant), [{"id": 1, "name": "Fixture one"}, {"id": 2, "name": "Fixture two"}])
        await conn.execute(
            insert(LiteraturePaper),
            [
                {"id": 1, "tenant_id": 1, "rid": "one", "title": "Fixture one"},
                {"id": 2, "tenant_id": 2, "rid": "two", "title": "Fixture two"},
            ],
        )
        await conn.execute(
            insert(AgentRun),
            [
                {
                    "id": "fixture-run",
                    "uid": "fixture-user",
                    "tenant_id": 1,
                    "agent_slug": "literature",
                    "status": "failed",
                    "conversation_thread_id": "fixture-thread",
                    "request_id": "fixture-request",
                }
            ],
        )
        await conn.execute(
            insert(LiteratureTranslationJob),
            [
                {
                    "id": 1,
                    "paper_id": 1,
                    "tenant_id": 1,
                    "requested_by": "fixture-user",
                    "variant_kind": "pdf_zh",
                    "status": "FAILED",
                    "execution_json": {"cleaned": True},
                    "llm_run_id": "fixture-run",
                }
            ],
        )
        await conn.execute(
            insert(LiteraturePdfModelCall),
            [
                {
                    "id": "fixture-call",
                    "job_id": 1,
                    "run_id": "fixture-run",
                    "tenant_id": 1,
                    "attempt": 1,
                    "request_digest": "a" * 64,
                    "reserved_tokens": 10,
                    "status": "resolved",
                    "resolution_json": {"receipt_ref": "fixture-receipt"},
                }
            ],
        )
    pools = {
        role: create_async_engine(
            url.set(username=role, password="fixture"), pool_size=1, max_overflow=0, hide_parameters=True
        )
        for role in ("pdf_fixture_api", "pdf_fixture_worker")
    }
    yield admin, pools
    for pool in pools.values():
        await pool.dispose()
    await admin.dispose()


async def test_readiness_rejects_disabled_rls_even_when_policy_still_exists(protected_database, monkeypatch):
    admin, pools = protected_database
    monkeypatch.setenv("YUXI_DB_SKIP_MIGRATIONS", "true")
    monkeypatch.setattr(pg_manager, "async_engine", pools["pdf_fixture_api"])
    async with admin.begin() as conn:
        await conn.execute(text("INSERT INTO schema_migrations(version) VALUES (:version)"),
                           {"version": pg_manager._VERSIONED_MIGRATIONS[-1][0]})
    assert await pg_manager._runtime_schema_ready()
    try:
        async with admin.begin() as conn:
            await conn.execute(text("ALTER TABLE literature_papers DISABLE ROW LEVEL SECURITY"))
        with pytest.raises(RuntimeError, match="production admission"):
            await pg_manager._runtime_schema_ready()
    finally:
        async with admin.begin() as conn:
            await conn.execute(text("ALTER TABLE literature_papers ENABLE ROW LEVEL SECURITY"))


async def test_real_runtime_rls_survives_commit_rollback_and_pool_reuse(protected_database):
    _admin, pools = protected_database
    factory = async_sessionmaker(pools["pdf_fixture_api"], class_=RuntimeAsyncSession)
    for tenant, expected in ((1, "one"), (2, "two"), (1, "one")):
        with actor(tenant, "fixture-user"):
            async with factory() as db:
                assert list((await db.execute(text("SELECT rid FROM literature_papers"))).scalars()) == [expected]
                await db.commit()
                assert list((await db.execute(text("SELECT rid FROM literature_papers"))).scalars()) == [expected]
                await db.rollback()
                assert list((await db.execute(text("SELECT rid FROM literature_papers"))).scalars()) == [expected]
    async with factory() as db:
        assert not (await db.execute(text("SELECT rid FROM literature_papers"))).first()
    with actor(1, "fixture-user"):
        async with factory() as db:
            assert (
                await db.execute(text("UPDATE literature_papers SET title='forged' WHERE tenant_id=2"))
            ).rowcount == 0


async def test_runtime_accounts_cannot_migrate_escalate_or_rewrite_billing(protected_database):
    _admin, pools = protected_database
    for pool in pools.values():
        for query in (
            "CREATE TABLE forbidden_schema(id int)",
            "SET ROLE fixture",
            "UPDATE usage_ledger SET total_tokens=0",
            "DELETE FROM usage_ledger",
            "DROP TABLE literature_papers",
        ):
            with pytest.raises(DBAPIError) as denied:
                async with pool.begin() as conn:
                    await conn.execute(text(query))
            assert denied.value.orig.sqlstate == "42501"
    async with pools["pdf_fixture_worker"].connect() as conn:
        assert not (await conn.execute(text("SELECT * FROM yuxi_pdf_queue_candidates()"))).first()
    for table in ("api_keys", "model_user_credentials", "model_providers", "users"):
        with pytest.raises(DBAPIError) as denied:
            async with pools["pdf_fixture_worker"].begin() as conn:
                await conn.execute(text(f"SELECT * FROM {table}"))
        assert denied.value.orig.sqlstate == "42501"


async def test_scoped_source_lock_and_receipt_immutability(protected_database):
    _admin, pools = protected_database
    factory = async_sessionmaker(pools["pdf_fixture_worker"], class_=RuntimeAsyncSession)
    for tenant, uid in ((1, "wrong-user"), (2, "fixture-user")):
        with actor(tenant, uid):
            with pytest.raises(DBAPIError) as denied:
                async with factory() as db:
                    await db.execute(text("SELECT yuxi_pdf_lock_input(1)"))
            assert denied.value.orig.sqlstate == "42501"
    with actor(1, "fixture-user"):
        async with factory() as db:
            await db.execute(text("SELECT yuxi_pdf_lock_input(1)"))
            assert (await db.execute(text("SELECT rid FROM literature_papers"))).scalar_one() == "one"
        with pytest.raises(DBAPIError) as denied:
            async with factory() as db:
                await db.execute(text("UPDATE literature_papers SET title='forged' WHERE id=1"))
        assert denied.value.orig.sqlstate == "42501"
        with pytest.raises(DBAPIError) as immutable:
            async with factory() as db:
                await db.execute(
                    text("UPDATE literature_pdf_model_calls SET resolution_json='{}' WHERE id='fixture-call'")
                )
        assert immutable.value.orig.sqlstate == "P0001"
        api = async_sessionmaker(pools["pdf_fixture_api"], class_=RuntimeAsyncSession)
        with pytest.raises(DBAPIError) as denied:
            async with api() as db:
                await db.execute(insert(LiteraturePaper).values(id=3, tenant_id=2, rid="forged", title="Forged"))
        assert denied.value.orig.sqlstate == "42501"


async def test_concurrent_tenant_capacity_admission_never_exceeds_64(protected_database, monkeypatch):
    import fitz

    admin, pools = protected_database
    async with admin.begin() as conn:
        await conn.execute(text("SELECT setval(pg_get_serial_sequence('literature_translation_jobs','id'),1)"))
        await conn.execute(
            insert(LiteraturePaper),
            [
                {"id": 100 + index, "tenant_id": 1, "rid": f"load-{index}", "title": "Synthetic admission fixture"}
                for index in range(80)
            ],
        )
    pool = create_async_engine(pools["pdf_fixture_api"].url, pool_size=8, max_overflow=0, hide_parameters=True)
    factory = async_sessionmaker(pool, class_=RuntimeAsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def context():
        async with factory() as db:
            yield db

    with fitz.open() as document:
        document.new_page()
        source = document.tobytes()

    async def options(*_args):
        return {"model_spec": "fixture:model"}

    async def original(*_args):
        return source, "fixture/source.pdf"

    async def available():
        return {
            "enabled": True,
            "runtime": {key: "a" * 64 for key in ("engine_revision", "font_digest", "corpus_digest")},
        }

    async def precheck(*_args, **_kwargs):
        pass

    async def freeze(db, *, tenant_id, uid, **_kwargs):
        run_id = uuid.uuid4().hex
        db.add(
            AgentRun(
                id=run_id,
                uid=uid,
                tenant_id=tenant_id,
                agent_slug="literature",
                status="pending",
                request_id=run_id,
                conversation_thread_id=run_id,
            )
        )
        await db.flush()
        return SimpleNamespace(
            run_id=run_id,
            model_spec="fixture:model",
            credential_source="platform",
            credential_id=None,
            provider_id="fixture",
            policy_version=1,
        )

    class Queue:
        async def enqueue_job(self, *_args, **_kwargs):
            pass

    async def queue():
        return Queue()

    monkeypatch.setattr(pg_manager, "get_async_session_context", context)
    monkeypatch.setattr(service, "capabilities", available)
    monkeypatch.setattr("yuxi.services.run_queue_service.get_arq_pool", queue)
    monkeypatch.setattr("yuxi.literature.translate.settings.translation_options", options)
    monkeypatch.setattr("yuxi.literature.translate.service._precheck_translation_model", precheck)
    monkeypatch.setattr("yuxi.literature.llm_gateway.freeze_literature_policy", freeze)
    monkeypatch.setattr("yuxi.literature.reader_bundle.get_source_pdf", original)

    async def submit(index):
        with actor(1, f"load-user-{index}"):
            try:
                return await service.enqueue_pdf_translation(paper_id=100 + index, requested_by=f"load-user-{index}")
            except ValueError as error:
                assert "容量上限" in str(error)
                return None

    try:
        results = await asyncio.gather(*(submit(index) for index in range(80)))
        assert sum(result is not None for result in results) == 64
        async with admin.connect() as conn:
            assert (
                await conn.scalar(text("SELECT count(*) FROM literature_translation_jobs WHERE status='PENDING'")) == 64
            )
            assert await conn.scalar(text("SELECT count(*) FROM literature_pdf_model_calls")) == 1
    finally:
        await pool.dispose()
