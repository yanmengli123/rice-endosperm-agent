"""答案缓存（P11 §八；方案：缓存键含权限范围/来源版本/模型/策略）。

上游 answer_cache（256 条 LRU）在 Yuxi 的等价物：Redis TTL 缓存，
键 = sha256(权限范围 + 来源版本 + 模型 + 查询)；撤权或输入变化即失效
（TTL + 显式 invalidate 双保险）。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

_CACHE_PREFIX = "yuxi:lit:answer:"
_DEFAULT_TTL = 3600  # 1 小时


def _cache_key(query: str, *, rid: str, scope_fingerprint: str, model_spec: str, policy_version: str) -> str:
    """缓存键 = sha256(权限范围 + 来源版本 + 模型 + 查询)——缺一不可。"""
    raw = json.dumps(
        {"q": query, "rid": rid, "scope": scope_fingerprint, "model": model_spec, "policy": policy_version},
        sort_keys=True,
        ensure_ascii=False,
    )
    return _CACHE_PREFIX + hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def cache_get(query: str, *, rid: str, scope_fingerprint: str, model_spec: str, policy_version: str) -> str | None:
    """取缓存答案（权限/模型/策略任一变化即 miss）。"""
    import redis.asyncio as aioredis

    from yuxi.storage.redis import get_redis_client

    try:
        client = get_redis_client()
        if client is None:
            return None
        value = await client.get(_cache_key(query, rid=rid, scope_fingerprint=scope_fingerprint, model_spec=model_spec, policy_version=policy_version))
        return value.decode("utf-8") if value else None
    except Exception:  # noqa: BLE001 - 缓存不可用静默降级
        return None


async def cache_set(
    query: str,
    answer: str,
    *,
    rid: str,
    scope_fingerprint: str,
    model_spec: str,
    policy_version: str,
    ttl: int = _DEFAULT_TTL,
) -> None:
    """写缓存（TTL 兜底失效）。"""
    import json as _json

    from yuxi.storage.redis import get_redis_client

    try:
        client = get_redis_client()
        if client is None:
            return
        await client.setex(
            _cache_key(query, rid=rid, scope_fingerprint=scope_fingerprint, model_spec=model_spec, policy_version=policy_version),
            ttl,
            answer.encode("utf-8"),
        )
    except Exception:  # noqa: BLE001
        pass


async def invalidate_for_paper(rid: str) -> int:
    """文献变更（裁决回放/重编译）时失效全部相关缓存。"""
    from yuxi.storage.redis import get_redis_client

    try:
        client = get_redis_client()
        if client is None:
            return 0
        # 扫描前缀并删除（rid 在键哈希内，用 tag 索引做精确失效）
        tags_key = f"{_CACHE_PREFIX}tags:{rid}"
        keys = await client.smembers(tags_key)
        if keys:
            pipe = client.pipeline()
            for key in keys:
                pipe.delete(key)
            pipe.delete(tags_key)
            await pipe.execute()
            return len(keys)
        return 0
    except Exception:  # noqa: BLE001
        return 0
