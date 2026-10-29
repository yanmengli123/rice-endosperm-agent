"""One-shot administrator command, separate from business process startup."""

import asyncio
from sqlalchemy import text

from yuxi.storage.postgres.manager import SCHEMA_LOCK_SQL, pg_manager


async def migrate():
    pg_manager.initialize()
    try:
        await pg_manager.create_tables()
        await pg_manager.ensure_business_schema()
        await pg_manager.ensure_knowledge_schema()
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        await AsyncPostgresSaver(pg_manager.langgraph_pool).setup()
        # Fresh checkpoint tables are created after versioned migrations. Grant
        # the established runtime roles only after all administrator DDL finishes.
        async with pg_manager.async_engine.begin() as conn:
            await conn.execute(text(SCHEMA_LOCK_SQL))
            await pg_manager._migration_0123_lightweight_runtime_security(conn)
    finally:
        await pg_manager.close()


if __name__ == "__main__":
    asyncio.run(migrate())
