"""Opt-in Redis Lua/LRU verification in an isolated synthetic owner namespace."""

import os
import uuid
from types import SimpleNamespace

import pytest

from yuxi.literature import answer_cache
from yuxi.literature.access import LiteratureAccess, _access
from yuxi.storage.redis import get_async_redis_client

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("YUXI_LIVE_CACHE_TEST") != "1", reason="Requires isolated live cache opt-in"),
]


async def test_native_candidate_cache_is_atomic_owner_lru_and_version_bound():
    owner = "cache-probe-" + uuid.uuid4().hex
    rid = "cache-probe-" + uuid.uuid4().hex
    scope = LiteratureAccess(1, frozenset({rid}), frozenset(), {}, {}, SimpleNamespace(uid=owner))
    token = _access.set(scope)
    client = await get_async_redis_client()
    keys = []
    options = {"rid": rid, "scope_fingerprint": "v1", "model_spec": "probe:model", "policy_version": "p1"}
    lru, tag = answer_cache._lru_key(), f"yuxi:lit:answer:tags:1:{rid}"
    try:
        assert client is not None
        for index in range(256):
            query = f"Question {index}"
            keys.append(answer_cache._cache_key(query, **options))
            await answer_cache.cache_set(query, f"Candidate {index}", **options)
        assert await answer_cache.cache_get("Question 0", **options) == "Candidate 0"
        for index in (256, 257):
            query = f"Question {index}"
            keys.append(answer_cache._cache_key(query, **options))
            await answer_cache.cache_set(query, f"Candidate {index}", **options)
        assert await client.zcard(lru) == 256
        assert await client.scard(tag) == 256
        assert await answer_cache.cache_get("Question 1", **options) is None
        assert await answer_cache.cache_get("Question 2", **options) is None
        assert await answer_cache.cache_get("Question 0", **options) == "Candidate 0"
        assert await answer_cache.cache_get("Question 0", **{**options, "scope_fingerprint": "v2"}) is None
        alternate = _access.set(
            LiteratureAccess(1, scope.rids, frozenset(), {}, {}, SimpleNamespace(uid=owner + "other"))
        )
        try:
            assert await answer_cache.cache_get("Question 0", **options) is None
        finally:
            _access.reset(alternate)
        assert await answer_cache.invalidate_for_paper(rid) == 256
        assert await answer_cache.cache_get("Question 0", **options) is None
    finally:
        if client is not None:
            # All targets are the exact random synthetic namespace created above.
            await client.delete(*keys, *(key + ":tag" for key in keys), lru, tag)
        _access.reset(token)
