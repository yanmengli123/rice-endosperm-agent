"""Read-only checks against an explicitly opted-in migrated PostgreSQL database."""

import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from yuxi.storage.postgres.models_knowledge import Base

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("YUXI_LIVE_SCHEMA_TEST") != "1", reason="Requires explicit live schema opt-in"),
]


async def test_literature_orm_columns_migrations_and_rls_match_live_database():
    # No credential values or provider endpoints are printed or persisted.
    engine = create_async_engine(os.environ["POSTGRES_URL"])
    try:
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT table_name, column_name FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND "
                        "(table_name LIKE 'literature_%' OR table_name = 'knowledge_parse_revisions')"
                    )
                )
            ).all()
            actual = {(table, column) for table, column in rows}
            expected = {
                (table.name, column.name)
                for table in Base.metadata.tables.values()
                if table.name.startswith("literature_") or table.name == "knowledge_parse_revisions"
                for column in table.columns
            }
            assert expected <= actual, f"Missing literature columns: {sorted(expected - actual)}"
            unmapped = {(table, column) for table, column in actual - expected if table.startswith("literature_")}
            assert not unmapped, f"Migrated literature columns missing ORM: {sorted(unmapped)}"
            versions = set((await connection.execute(text("SELECT version FROM schema_migrations"))).scalars())
            assert {
                "0085_literature_revision_contracts",
                "0086_literature_embedding_governance",
                "0087_literature_card_context",
                "0088_scientific_pdf_parse_only",
                "0089_literature_history_import",
                "0090_literature_manual_products",
                "0091_literature_fulltext",
                "0092_literature_review_artifacts",
                "0093_literature_fulltext_retry",
                "0096_literature_topics",
                "0097_literature_original_history_search",
                "0098_literature_attachment_search",
                "0099_literature_translation_workspace",
                "0100_literature_trash_reason",
                "0101_literature_translation_generation",
                "0102_literature_reader_generation",
                "0103_literature_paper_session_unique",
            } <= versions
            protected = (
                await connection.execute(
                    text(
                        "SELECT relname, relrowsecurity FROM pg_class WHERE relname IN "
                        "('literature_navigation_indexes', 'literature_library_runs', 'literature_cleaning_plans', "
                        "'literature_history_items', 'literature_note_search', 'literature_topic_sets', "
                        "'literature_original_search', 'literature_translation_settings', "
                        "'literature_translation_tasks')"
                    )
                )
            ).all()
            assert len(protected) == 9 and all(rls for _, rls in protected)
            policies = set(
                (
                    await connection.execute(
                        text(
                            "SELECT tablename FROM pg_policies WHERE tablename IN "
                            "('literature_navigation_indexes', 'literature_library_runs', 'literature_cleaning_plans', "
                            "'literature_history_items', 'literature_note_search', 'literature_topic_sets', "
                            "'literature_original_search', 'literature_translation_settings', "
                            "'literature_translation_tasks')"
                        )
                    )
                ).scalars()
            )
            assert len(policies) == 9
            indexes = dict(
                (
                    await connection.execute(
                        text(
                            "SELECT indexname, indexdef FROM pg_indexes WHERE indexname IN "
                            "('uq_literature_compile_jobs_key', 'uq_literature_global_cards', "
                            "'ix_literature_note_search_fts', 'uq_conversations_literature_paper_session', "
                            "'uq_literature_translation_jobs_key')"
                        )
                    )
                ).all()
            )
            assert "card_revision" in indexes.get("uq_literature_compile_jobs_key", "")
            assert "WHERE (paper_id IS NULL)" in indexes.get("uq_literature_global_cards", "")
            assert "USING gin" in indexes.get("ix_literature_note_search_fts", "")
            session_index = indexes.get("uq_conversations_literature_paper_session", "")
            assert all(
                part in session_index for part in ("UNIQUE", "tenant_id", "uid", "literature_session", "rid", "deleted")
            )
            assert all(
                part in indexes.get("uq_literature_translation_jobs_key", "")
                for part in ("UNIQUE", "config_digest", "generation")
            )
    finally:
        await engine.dispose()


async def test_postgres_fulltext_uses_gin_for_large_synthetic_corpus():
    """10,000 generated rows in a transaction-local table, not user data."""
    engine = create_async_engine(os.environ["POSTGRES_URL"])
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("CREATE TEMP TABLE lit_fts_probe (id INTEGER, lexical_text TEXT) ON COMMIT DROP")
            )
            await connection.execute(
                text(
                    "INSERT INTO lit_fts_probe SELECT n, CASE WHEN n = 9999 "
                    "THEN 'starch 淀粉 合成' ELSE 'background science content ' || n::text END "
                    "FROM generate_series(1, 10000) AS n"
                )
            )
            await connection.execute(
                text(
                    "CREATE INDEX lit_fts_probe_gin ON lit_fts_probe "
                    "USING gin (to_tsvector('simple'::regconfig, lexical_text))"
                )
            )
            await connection.execute(text("ANALYZE lit_fts_probe"))
            query = (
                "SELECT id FROM lit_fts_probe WHERE to_tsvector('simple'::regconfig, lexical_text) "
                "@@ plainto_tsquery('simple'::regconfig, :term)"
            )
            for term in ("starch", "淀粉"):
                assert (await connection.execute(text(query), {"term": term})).scalars().all() == [9999]
                plan = (
                    await connection.execute(text("EXPLAIN (ANALYZE, FORMAT JSON) " + query), {"term": term})
                ).scalar_one()
                assert "lit_fts_probe_gin" in str(plan)
    finally:
        await engine.dispose()


async def test_actual_fulltext_query_compiles_on_postgres_with_source_and_latest_guards(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from sqlalchemy.ext.asyncio import AsyncSession
    from yuxi.literature import fulltext
    from yuxi.literature.access import LiteratureAccess, _access

    engine = create_async_engine(os.environ["POSTGRES_URL"])
    token = _access.set(LiteratureAccess(-1, frozenset(), frozenset(), {}, {}, SimpleNamespace(uid="schema-probe")))
    try:
        async with engine.connect() as connection:

            @asynccontextmanager
            async def database():
                async with AsyncSession(bind=connection) as db:
                    yield db

            monkeypatch.setattr(fulltext.pg_manager, "get_async_session_context", database)
            assert await fulltext.search_notes(-1, ["starch", "淀粉"]) == []
    finally:
        _access.reset(token)
        await engine.dispose()
