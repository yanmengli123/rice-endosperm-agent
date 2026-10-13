"""Read-only schema gate; run with --confcutdir=test/integration/literature.

This deliberately avoids the parent integration cleanup fixtures. No rows,
permissions or service state are mutated in the user's running database.
"""

import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from yuxi.storage.postgres.models_knowledge import (
    LiteratureReferenceBatch,
    LiteratureReferenceRecord,
    LiteratureTopicDocument,
    LiteratureTopicRun,
)

pytestmark = pytest.mark.integration


async def test_live_topic_schema_rls_and_tenant_foreign_keys():
    url = os.environ.get("POSTGRES_URL")
    if not url:
        pytest.skip("POSTGRES_URL is not configured")
    engine = create_async_engine(url)
    models = (LiteratureReferenceBatch, LiteratureReferenceRecord, LiteratureTopicRun, LiteratureTopicDocument)
    tables = [model.__tablename__ for model in models]
    protected = tables[1:]
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SET TRANSACTION READ ONLY"))
            assert (
                await conn.scalar(
                    text("SELECT COUNT(*) FROM schema_migrations WHERE version = '0112_literature_topic_analysis'")
                )
                == 1
            )
            rows = (
                await conn.execute(
                    text(
                        "SELECT table_name, column_name, data_type FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = ANY(:tables)"
                    ),
                    {"tables": tables},
                )
            ).all()
            actual = {(table, column): data_type for table, column, data_type in rows}
            for model in models:
                assert {column for table, column in actual if table == model.__tablename__} == set(
                    model.__table__.columns.keys()
                )
            for column in ("created_at", "updated_at", "heartbeat_at"):
                assert actual["literature_topic_runs", column] == "timestamp with time zone"
            rls = (
                await conn.execute(
                    text(
                        "SELECT relname, relrowsecurity FROM pg_class WHERE relnamespace = 'public'::regnamespace "
                        "AND relname = ANY(:tables)"
                    ),
                    {"tables": protected},
                )
            ).all()
            assert len(rls) == 3 and all(enabled for _, enabled in rls)
            policies = (
                await conn.execute(
                    text(
                        "SELECT tablename, policyname, qual FROM pg_policies "
                        "WHERE schemaname = 'public' AND tablename = ANY(:tables)"
                    ),
                    {"tables": protected},
                )
            ).all()
            owners = [(table, name, rule) for table, name, rule in policies if name.endswith("_owner")]
            assert {(table, name) for table, name, _ in owners} == {(table, f"{table}_owner") for table in protected}
            assert all("yuxi.tenant_id" in rule and "yuxi.uid" in rule for _, _, rule in owners)
            assert {(table, name) for table, name, _ in policies} - {(table, name) for table, name, _ in owners} <= {
                ("literature_topic_runs", "literature_topic_scheduler_select"),
                ("literature_topic_runs", "literature_topic_scheduler_update"),
            }
            for model in (LiteratureReferenceRecord, LiteratureTopicDocument):
                definitions = (
                    (
                        await conn.execute(
                            text(
                                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                                "WHERE conrelid = CAST(:table AS regclass) AND contype = 'f'"
                            ),
                            {"table": model.__tablename__},
                        )
                    )
                    .scalars()
                    .all()
                )
                assert any("FOREIGN KEY (tenant_id," in ddl for ddl in definitions)
            assert (
                await conn.scalar(
                    text(
                        "SELECT COUNT(*) FROM pg_indexes WHERE schemaname = 'public' "
                        "AND indexname = 'ix_topic_runs_owner'"
                    )
                )
                == 1
            )
            active = await conn.scalar(
                text("SELECT COUNT(*) FROM literature_topic_runs WHERE status IN ('PENDING', 'RUNNING')")
            )
            print(f"Topic schema/RLS/tenant FK verified; active topic runs: {active}")
    finally:
        await engine.dispose()
