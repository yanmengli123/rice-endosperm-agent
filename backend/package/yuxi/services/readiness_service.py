"""Traffic readiness is separate from process liveness."""

import asyncio
import os
import time

from sqlalchemy import text

WORKER_HEARTBEAT_TTL = 200


def worker_heartbeat_key():
    return "yuxi:worker:heartbeat:" + os.getenv("YUXI_INSTANCE_ID", "default")


async def check_readiness(*, initialized, db_context, redis):
    checks = {"initialization": bool(initialized), "database": False, "schema": False, "queue": False, "worker": False}
    try:
        async with asyncio.timeout(3):
            async with db_context() as db:
                await db.execute(text("SELECT 1"))
                checks["database"] = True
                result = await db.execute(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                        "WHERE table_schema = current_schema() AND table_name = 'skills' "
                        "AND column_name = 'bound_agent_id') "
                        "AND to_regclass('skill_versions') IS NOT NULL "
                        "AND to_regclass('mcp_oauth_configs') IS NOT NULL "
                        "AND to_regclass('mcp_oauth_connections') IS NOT NULL "
                        "AND to_regclass('mcp_oauth_attempts') IS NOT NULL"
                        " AND to_regclass('collaboration_messages') IS NOT NULL"
                        " AND to_regclass('collaboration_stops') IS NOT NULL"
                        " AND to_regclass('agent_run_waitpoints') IS NOT NULL"
                        " AND to_regclass('answer_source_annotations') IS NOT NULL"
                        " AND to_regclass('public_apps') IS NOT NULL"
                        " AND to_regclass('public_app_keys') IS NOT NULL"
                        " AND to_regclass('public_end_users') IS NOT NULL"
                        " AND to_regclass('public_sessions') IS NOT NULL"
                        " AND to_regclass('public_inputs') IS NOT NULL"
                        " AND to_regclass('public_turns') IS NOT NULL"
                        " AND to_regclass('public_turn_runs') IS NOT NULL"
                        " AND to_regclass('public_receipts') IS NOT NULL"
                        " AND to_regclass('public_session_events') IS NOT NULL"
                        " AND to_regclass('public_files') IS NOT NULL"
                        " AND EXISTS (SELECT 1 FROM information_schema.columns"
                        " WHERE table_schema = current_schema() AND table_name = 'users'"
                        " AND column_name = 'user_kind')"
                        " AND EXISTS (SELECT 1 FROM information_schema.columns"
                        " WHERE table_schema = current_schema() AND table_name = 'conversations'"
                        " AND column_name = 'public_session')"
                        " AND to_regprocedure('yuxi_public_budget(text,bigint,timestamp without time zone,"
                        "timestamp with time zone)') IS NOT NULL"
                    )
                )
                checks["schema"] = bool(result.scalar())
    except Exception:
        pass
    try:
        async with asyncio.timeout(3):
            checks["queue"] = bool(await redis.ping())
            heartbeat = await redis.get(worker_heartbeat_key())
            if heartbeat:
                age = time.time() - float(heartbeat)
                checks["worker"] = -5 <= age < WORKER_HEARTBEAT_TTL
    except Exception:
        pass
    ready = all(checks.values())
    return {"status": "ready" if ready else "not_ready", "checks": checks}
