"""Read-only operational gate; exit 1 means the monitoring system should alert."""

import asyncio
import json
from contextlib import suppress
from datetime import timedelta

import httpx
from sqlalchemy import text

from yuxi.storage.postgres.manager import pg_manager
from yuxi.utils.datetime_utils import utc_now


async def main():
    pg_manager.initialize()
    async with pg_manager.get_async_session_context() as db:
        url = await db.scalar(text("SELECT url FROM mcp_servers WHERE slug='genoffice' AND enabled = 1"))
        stats = (
            (
                await db.execute(
                    text("""
            SELECT count(*) AS calls,
                   count(*) FILTER (WHERE status <> 'success') AS errors,
                   count(*) FILTER (WHERE error_class = 'MCP_DELIVERY_FAILED') AS delivery_errors,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95_ms
            FROM mcp_call_audit WHERE server_slug='genoffice' AND created_at >= :since
        """),
                    {"since": utc_now() - timedelta(minutes=15)},
                )
            )
            .mappings()
            .one()
        )
    result = {"checked_at": utc_now().isoformat(), "window_minutes": 15, **dict(stats), "healthy": False}
    if url:
        try:
            async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
                response = await client.get(url.rsplit("/mcp", 1)[0] + "/health")
                result["healthy"] = response.status_code == 200 and response.json().get("status") == "ok"
        except Exception:
            pass
    result["alert"] = (
        not result["healthy"]
        or result["delivery_errors"] > 0
        or (result["calls"] >= 5 and result["errors"] / result["calls"] > 0.1)
        or (result["p95_ms"] or 0) > 30000
    )
    print(json.dumps(result, ensure_ascii=False))
    return int(result["alert"])


async def run():
    try:
        return await main()
    except Exception as error:
        # Do not expose connection strings, document contents, or credentials.
        print(
            json.dumps(
                {
                    "checked_at": utc_now().isoformat(),
                    "healthy": False,
                    "alert": True,
                    "error_class": type(error).__name__,
                }
            )
        )
        return 1
    finally:
        with suppress(Exception):
            await pg_manager.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
