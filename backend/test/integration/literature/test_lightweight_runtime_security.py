"""Real API/worker permissions, only on an explicitly isolated fixture database."""

import os

import pytest
import pytest_asyncio
from sqlalchemy import insert, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.literature.lightweight_security import migrate
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import Base as BusinessBase, Conversation, Tenant, UsageLedger
from yuxi.storage.postgres.models_knowledge import Base as KnowledgeBase, LiteraturePaper, LiteratureReferenceBatch
from yuxi.storage.postgres.runtime_security import RuntimeAsyncSession, actor

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.getenv("YUXI_LIGHT_SECURITY_FIXTURE") != "1", reason="Requires explicitly isolated PostgreSQL fixture"
)]


@pytest_asyncio.fixture
async def database():
    url = make_url(os.environ["YUXI_LIGHT_SECURITY_DATABASE_URL"])
    assert url.host == "literature-pg" and url.database == "literature_fixture"
    admin = create_async_engine(url, hide_parameters=True)
    async with admin.begin() as conn:
        assert await conn.scalar(text("SELECT current_database()")) == "literature_fixture"
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
        for role in ("light_fixture_api", "light_fixture_worker"):
            await conn.execute(text(f"DROP ROLE IF EXISTS {role}"))
        await conn.run_sync(KnowledgeBase.metadata.create_all)
        await conn.run_sync(BusinessBase.metadata.create_all)
        await conn.execute(text("CREATE TABLE schema_migrations(version varchar PRIMARY KEY)"))
        await conn.execute(text("INSERT INTO schema_migrations VALUES (:version)"),
                           {"version": pg_manager._VERSIONED_MIGRATIONS[-1][0]})
        await conn.execute(text("ALTER TABLE agent_runs ENABLE ROW LEVEL SECURITY"))
        await conn.execute(text("CREATE POLICY own_run ON agent_runs USING (uid=current_setting('yuxi.uid',true))"))
        await conn.execute(text("ALTER TABLE conversations ENABLE ROW LEVEL SECURITY"))
        await conn.execute(text("CREATE POLICY own_conversation ON conversations USING "
                                "(uid=current_setting('yuxi.uid',true))"))
        await conn.execute(text("ALTER TABLE literature_reference_batches ENABLE ROW LEVEL SECURITY"))
        await conn.execute(text("CREATE POLICY own_batch ON literature_reference_batches USING "
                                "(created_by=current_setting('yuxi.uid',true))"))
        await migrate(conn)
        await migrate(conn)  # Reapplying grants must preserve boundaries and audit restrictions.
        await conn.execute(text("CREATE ROLE light_fixture_api LOGIN IN ROLE yuxi_api_runtime"))
        await conn.execute(text("CREATE ROLE light_fixture_worker LOGIN IN ROLE yuxi_worker_runtime"))
        await conn.execute(insert(Tenant), [{"id": 1, "name": "One"}, {"id": 2, "name": "Two"}])
        await conn.execute(insert(LiteraturePaper), [
            {"id": 1, "tenant_id": 1, "rid": "one"}, {"id": 2, "tenant_id": 2, "rid": "two"},
        ])
        await conn.execute(insert(UsageLedger), [
            {"run_id": "run-one", "uid": "one", "tenant_id": 1, "total_tokens": 10},
            {"run_id": "run-two", "uid": "two", "tenant_id": 2, "total_tokens": 20},
        ])
        await conn.execute(insert(LiteratureReferenceBatch), [
            {"batch_id": uid, "tenant_id": 1, "created_by": uid, "source_name": uid,
             "source_sha256": "a" * 64, "format": "fixture", "report_json": {}}
            for uid in ("one", "other")
        ])
        await conn.execute(insert(Conversation), [
            {"uid": uid, "tenant_id": 1, "thread_id": uid, "agent_id": "fixture", "title": uid}
            for uid in ("one", "other")
        ])
    pools = {role: create_async_engine(url.set(username=role), pool_size=1, max_overflow=0, hide_parameters=True)
             for role in ("light_fixture_api", "light_fixture_worker")}
    try:
        yield admin, pools
    finally:
        for pool in pools.values():
            await pool.dispose()
        await admin.dispose()


async def test_api_actor_survives_transactions_and_pool_reuse(database):
    _admin, pools = database
    factory = async_sessionmaker(pools["light_fixture_api"], class_=RuntimeAsyncSession)
    for tenant, uid in ((1, "one"), (2, "two"), (1, "one")):
        with actor(tenant, uid):
            async with factory() as db:
                for action in (db.commit, db.rollback):
                    assert list((await db.execute(text("SELECT rid FROM literature_papers"))).scalars()) == [uid]
                    assert await db.scalar(text("SELECT total_tokens FROM usage_ledger")) == tenant * 10
                    await action()
                assert (await db.execute(text("UPDATE literature_papers SET title='forged' WHERE tenant_id<>:t"),
                                         {"t": tenant})).rowcount == 0
    async with factory() as db:
        assert not (await db.execute(text("SELECT rid FROM literature_papers"))).first()


async def test_worker_trusted_maintenance_and_bound_actor_are_distinct(database):
    _admin, pools = database
    factory = async_sessionmaker(pools["light_fixture_worker"], class_=RuntimeAsyncSession)
    async with factory() as db:
        assert set((await db.execute(text("SELECT rid FROM literature_papers"))).scalars()) == {"one", "two"}
    for tenant, uid in ((1, "one"), (2, "two")):
        with actor(tenant, uid):
            async with factory() as db:
                assert list((await db.execute(text("SELECT rid FROM literature_papers"))).scalars()) == [uid]
                await db.commit()
                assert await db.scalar(text("SELECT total_tokens FROM usage_ledger")) == tenant * 10
    with actor(1, "wrong-user"):
        async with factory() as db:
            assert not (await db.execute(text("SELECT total_tokens FROM usage_ledger"))).first()


async def test_both_accounts_cannot_escalate_migrate_or_rewrite_billing(database):
    _admin, pools = database
    for pool in pools.values():
        async with pool.connect() as db:
            assert not await db.scalar(text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user"))
        for sql in (
            "CREATE TABLE forbidden(id int)", "SET ROLE fixture", "DROP TABLE literature_papers",
            "UPDATE usage_ledger SET total_tokens=0", "DELETE FROM usage_ledger",
            "DELETE FROM literature_pdf_model_calls", "DELETE FROM schema_migrations",
        ):
            with pytest.raises(DBAPIError) as error:
                async with pool.begin() as db:
                    await db.execute(text(sql))
            assert error.value.orig.sqlstate == "42501"


async def test_existing_same_tenant_owner_policies_survive_for_api_and_worker(database):
    _admin, pools = database
    for pool in pools.values():
        factory = async_sessionmaker(pool, class_=RuntimeAsyncSession)
        with actor(1, "one"):
            async with factory() as db:
                assert list((await db.execute(text("SELECT created_by FROM literature_reference_batches")))
                            .scalars()) == ["one"]
                assert list((await db.execute(text("SELECT uid FROM conversations"))).scalars()) == ["one"]


async def test_runtime_startup_and_disabled_rls_are_checked(database, monkeypatch):
    admin, pools = database
    monkeypatch.setenv("YUXI_DB_SKIP_MIGRATIONS", "true")
    for pool in pools.values():
        monkeypatch.setattr(pg_manager, "async_engine", pool)
        assert await pg_manager._runtime_schema_ready()
    try:
        async with admin.begin() as conn:
            await conn.execute(text("ALTER TABLE literature_papers DISABLE ROW LEVEL SECURITY"))
        with pytest.raises(RuntimeError, match="production admission"):
            await pg_manager._runtime_schema_ready()
    finally:
        async with admin.begin() as conn:
            await conn.execute(text("ALTER TABLE literature_papers ENABLE ROW LEVEL SECURITY"))
    async with admin.begin() as conn:
        await conn.execute(text("DROP POLICY lightweight_worker_boundary ON literature_papers"))
    with pytest.raises(RuntimeError, match="production admission"):
        await pg_manager._runtime_schema_ready()


async def test_runtime_startup_rejects_boundary_for_unrelated_role(database, monkeypatch):
    admin, pools = database
    monkeypatch.setenv("YUXI_DB_SKIP_MIGRATIONS", "true")
    for login, policy, group in (
        ("light_fixture_api", "runtime_tenant_boundary", "yuxi_api_runtime"),
        ("light_fixture_worker", "lightweight_worker_boundary", "yuxi_worker_runtime"),
    ):
        monkeypatch.setattr(pg_manager, "async_engine", pools[login])
        assert await pg_manager._runtime_schema_ready()
        try:
            async with admin.begin() as conn:
                await conn.execute(text(f"ALTER POLICY {policy} ON literature_papers TO fixture"))
            with pytest.raises(RuntimeError, match="production admission"):
                await pg_manager._runtime_schema_ready()
        finally:
            async with admin.begin() as conn:
                await conn.execute(text(f"ALTER POLICY {policy} ON literature_papers TO {group}"))
