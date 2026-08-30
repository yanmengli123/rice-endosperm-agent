"""Verify every installed BioinfoMCP image through Yuxi's real MCP host path."""

from __future__ import annotations

import asyncio

from yuxi.agents.mcp.bioinfomcp_catalog import BIOINFOMCP_EXPECTED_TOOLS
from yuxi.agents.mcp.service import (
    ensure_builtin_mcp_servers_in_db,
    get_mcp_server,
    probe_mcp_server,
)
from yuxi.storage.postgres.manager import pg_manager

EXPECTED_TOOLS = {"bioinfomcp-fastqc": ("fastqc",), **BIOINFOMCP_EXPECTED_TOOLS}


async def main() -> int:
    await ensure_builtin_mcp_servers_in_db()
    failures: list[str] = []

    for index, (slug, expected) in enumerate(sorted(EXPECTED_TOOLS.items()), start=1):
        result = await probe_mcp_server(slug, persist=True)
        if not result.ok:
            failures.append(f"{slug}: {result.code or result.stage}: {result.message}")
            print(f"[{index:02d}/38] FAILED {slug}: {result.code or result.stage}", flush=True)
            continue

        async with pg_manager.get_async_session_context() as session:
            server = await get_mcp_server(session, slug)
        snapshot = (server.capability_snapshot if server is not None else None) or {}
        discovered = tuple(item.get("name", "") for item in snapshot.get("tools", []))
        missing = sorted(set(expected) - set(discovered))
        unexpected = sorted(set(discovered) - set(expected))
        if missing or unexpected:
            failures.append(
                f"{slug}: tools/list mismatch; missing={missing or '-'} unexpected={unexpected or '-'}"
            )
            print(f"[{index:02d}/38] FAILED {slug}: tools/list mismatch", flush=True)
            continue

        print(f"[{index:02d}/38] OK {slug}: {len(discovered)} tools", flush=True)

    print(f"BioinfoMCP verification: {len(EXPECTED_TOOLS) - len(failures)}/38 ready")
    if failures:
        print("Failures:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
