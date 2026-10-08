"""GenOffice admission control shared by API and worker processes."""

from __future__ import annotations

import hashlib
import os
import uuid
from copy import deepcopy
from contextlib import asynccontextmanager

from yuxi.storage.redis import get_async_redis_client

# Redis time avoids host clock skew; expired leases recover worker crashes.
_ACQUIRE = """
local t = redis.call('TIME')
local now = t[1] * 1000 + math.floor(t[2] / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[1]) then return 0 end
redis.call('ZADD', KEYS[1], now + tonumber(ARGV[2]), ARGV[3])
redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[2]) + 1000)
return 1
"""

DISABLED_TOOLS = frozenset({"search", "image", "media", "deck_start", "deck_page", "deck_build", "deck_replace"})


def platform_tool_schema(tool_name, schema):
    """Only advertise input shapes that work across the container boundary."""
    projected = deepcopy(schema)
    unsupported = {"out"}
    if tool_name == "create_pptx":
        unsupported.update({"spec_dir", "outline"})
    for name in unsupported:
        projected.get("properties", {}).pop(name, None)
    if "required" in projected:
        projected["required"] = [name for name in projected["required"] if name not in unsupported]
    return projected


@asynccontextmanager
async def genoffice_slot(url: str, *, deadline_seconds: float):
    limit = int(os.getenv("YUXI_GENOFFICE_MAX_CONCURRENT", "2"))
    if not 1 <= limit <= 16:
        raise ValueError("YUXI_GENOFFICE_MAX_CONCURRENT must be between 1 and 16")
    key = "yuxi:mcp:genoffice:slots:" + hashlib.sha256(url.encode()).hexdigest()[:24]
    member = uuid.uuid4().hex
    client = await get_async_redis_client()
    acquired = await client.eval(_ACQUIRE, 1, key, limit, int((deadline_seconds + 30) * 1000), member)
    if not acquired:
        raise RuntimeError("MCP_RATE_LIMITED: too many requests; GenOffice capacity is busy")
    try:
        yield
    finally:
        # The lease also expires if Redis or the process disappears during cleanup.
        from yuxi.utils import logger

        try:
            await client.zrem(key, member)
        except Exception:
            logger.warning("GenOffice admission lease cleanup failed; expiry will recover the slot")
