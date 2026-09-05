"""Temporary migration drill: run full versioned migrations against a scratch database.

Usage (inside api-dev): python scripts/migration_drill.py
Creates no objects in the main database; the scratch database must exist beforehand.
"""

import asyncio
import os

from sqlalchemy import text

from yuxi.storage.postgres.manager import pg_manager

SCRATCH_DB = "yuxi_mig_drill"


async def main() -> None:
    base, _, _ = os.environ["POSTGRES_URL"].rpartition("/")
    os.environ["POSTGRES_URL"] = f"{base}/{SCRATCH_DB}"
    pg_manager.initialize()
    await pg_manager.create_tables()
    await pg_manager.ensure_business_schema()
    async with pg_manager.get_async_session_context() as session:
        versions = [
            row[0]
            for row in (await session.execute(text("SELECT version FROM schema_migrations ORDER BY version"))).all()
        ]
        tables = (
            (
                await session.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename LIKE 'knowledge_%'")
                )
            )
            .scalars()
            .all()
        )
    print("MIGRATIONS_APPLIED:", versions)
    print("KNOWLEDGE_TABLES:", sorted(tables))
    missing = [v for v, _ in pg_manager._VERSIONED_MIGRATIONS if v not in versions]
    print("MISSING:", missing)
    if missing:
        raise SystemExit(f"migration drill failed, missing: {missing}")
    print("FRESH_INSTALL_DRILL: PASS")


asyncio.run(main())
