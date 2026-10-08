"""Cross-process capacity and stale lease recovery using the real Redis service."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path

import pytest

from yuxi.agents.mcp.genoffice import genoffice_slot
from yuxi.storage.redis import get_async_redis_client

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]


async def test_capacity_shared_across_processes_and_recovers_expired_lease(monkeypatch):
    if os.getenv("RUN_GENOFFICE_ACCEPTANCE") != "1":
        pytest.skip("Live Redis capacity test is opt-in")
    monkeypatch.setenv("YUXI_GENOFFICE_MAX_CONCURRENT", "2")
    url = f"http://acceptance-{uuid.uuid4().hex}:3093/mcp"
    key = "yuxi:mcp:genoffice:slots:" + hashlib.sha256(url.encode()).hexdigest()[:24]
    child_code = """
import asyncio,sys
from yuxi.agents.mcp.genoffice import genoffice_slot
async def main():
    async with genoffice_slot(sys.argv[1], deadline_seconds=300):
        print('READY', flush=True)
        await asyncio.to_thread(sys.stdin.readline)
asyncio.run(main())
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        child_code,
        url,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    client = await get_async_redis_client()
    try:
        async with asyncio.timeout(30):
            while (await process.stdout.readline()).strip() != b"READY":
                assert process.returncode is None, "capacity child failed to start"
        async with genoffice_slot(url, deadline_seconds=300):
            assert await client.zcard(key) == 2
            with pytest.raises(RuntimeError, match="MCP_RATE_LIMITED"):
                async with genoffice_slot(url, deadline_seconds=300):
                    pytest.fail("third concurrent operation was admitted")
        process.stdin.write(b"release\n")
        await process.stdin.drain()
        assert await process.wait() == 0
        assert await client.zcard(key) == 0
        # A crashed worker's lease is removed using Redis time on the next admission.
        await client.zadd(key, {"expired-worker": 1})
        async with genoffice_slot(url, deadline_seconds=300):
            assert await client.zcard(key) == 1
        assert await client.zcard(key) == 0
        report = {
            "capacity": 2,
            "cross_process_shared": True,
            "third_call_rejected": True,
            "release_count": 0,
            "expired_lease_recovered": True,
        }
        output = Path("saves/acceptance/genoffice/admission.json")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        await client.delete(key)
