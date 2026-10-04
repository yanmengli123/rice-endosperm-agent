"""Governed optional reranking and permission/version-bound navigation caching."""

from __future__ import annotations

import hashlib
import json
import math
import uuid

from sqlalchemy import select
from redis.exceptions import RedisError
from yuxi.literature.access import build_access, current_access
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    LiteraturePaper,
    LiteratureCompileJob,
    LiteratureNavigationIndex,
    LiteratureNoteSearch,
)


async def search_identity(tenant_id: int, request: dict) -> str | None:
    scope = current_access()
    if scope is None or scope.tenant_id != tenant_id:
        return None
    fresh = await build_access(scope.user, tenant_id)
    # Reject a request already invalidated while it was running; do not serve
    # an old scope merely because it was authorized at HTTP entry.
    if scope.rids != fresh.rids or scope.sources != fresh.sources:
        raise ValueError("检索来源或权限已变更，请重新检索")
    async with pg_manager.get_async_session_context() as db:
        papers = (
            await db.execute(
                select(
                    LiteraturePaper.rid,
                    LiteraturePaper.source_revision_id,
                    LiteraturePaper.reading_version,
                    LiteraturePaper.card_revision,
                    LiteraturePaper.updated_at,
                ).where(LiteraturePaper.tenant_id == tenant_id, LiteraturePaper.rid.in_(fresh.rids))
            )
        ).all()
        notes = (
            await db.execute(
                select(
                    LiteratureCompileJob.id,
                    LiteratureCompileJob.status,
                    LiteratureCompileJob.note_uri,
                    LiteratureCompileJob.updated_at,
                ).where(LiteratureCompileJob.tenant_id == tenant_id, LiteratureCompileJob.paper_id.in_(fresh.paper_ids))
            )
        ).all()
        indexes = (
            await db.execute(
                select(
                    LiteratureNavigationIndex.index_id,
                    LiteratureNavigationIndex.status,
                    LiteratureNavigationIndex.updated_at,
                ).where(
                    LiteratureNavigationIndex.tenant_id == tenant_id,
                    LiteratureNavigationIndex.requested_by == scope.user.uid,
                )
            )
        ).all()
        fulltext = (
            await db.execute(
                select(
                    LiteratureNoteSearch.job_id, LiteratureNoteSearch.content_digest, LiteratureNoteSearch.updated_at
                )
                .join(LiteratureCompileJob, LiteratureCompileJob.id == LiteratureNoteSearch.job_id)
                .where(LiteratureNoteSearch.tenant_id == tenant_id, LiteratureCompileJob.paper_id.in_(fresh.paper_ids))
            )
        ).all()
    manifest = {
        "version": "navigation-cache-v1",
        "uid": scope.user.uid,
        "tenant": tenant_id,
        "sources": fresh.sources,
        "rids": sorted(fresh.rids),
        "request": request,
        "papers": sorted(map(tuple, papers)),
        "notes": sorted(map(tuple, notes)),
        "indexes": sorted(map(tuple, indexes)),
        "fulltext": sorted(map(tuple, fulltext)),
    }
    return "yuxi:lit:search:" + hashlib.sha256(json.dumps(manifest, sort_keys=True, default=str).encode()).hexdigest()


async def read_cache(key: str | None):
    if not key:
        return None
    from yuxi.storage.redis import get_async_redis_client

    try:
        client = await get_async_redis_client()
        raw = await client.get(key) if client else None
        return json.loads(raw) if raw else None
    except (RedisError, ConnectionError, TimeoutError, json.JSONDecodeError):
        return None


async def write_cache(key: str | None, value) -> None:
    if not key:
        return
    from yuxi.storage.redis import get_async_redis_client

    try:
        client = await get_async_redis_client()
        if client:
            await client.setex(key, 300, json.dumps(value, ensure_ascii=False))
    except (RedisError, ConnectionError, TimeoutError):
        return


async def rerank(tenant_id: int, query: str, hits: list[dict], model_spec: str) -> list[dict]:
    if not model_spec or not hits:
        return hits
    if len(hits) > 100:
        raise ValueError("单次重排候选不得超过 100 条")
    from yuxi.agents.models import apply_credential_override
    from yuxi.literature import llm_gateway
    from yuxi.models.providers.cache import model_cache
    from yuxi.models.rerank import OpenAIReranker, DashscopeReranker

    scope = current_access()
    if scope is None or scope.tenant_id != tenant_id:
        raise ValueError("重排必须绑定已认证的请求用户")
    if not {hit["rid"] for hit in hits} <= (await build_access(scope.user, tenant_id)).rids:
        raise ValueError("重排候选来源权限已变更")
    async with pg_manager.get_async_session_context() as db:
        policy = await llm_gateway.freeze_literature_policy(
            db,
            tenant_id=tenant_id,
            uid=scope.user.uid,
            operation="navigation_rerank",
            resource_ref=f"rerank:{uuid.uuid4().hex}",
            model_spec=model_spec,
            model_type="rerank",
        )
        await db.commit()
    async with llm_gateway.governed_runtime(policy):
        info = model_cache.get_model_info(policy.model_spec)
        if info is None or info.model_type != "rerank":
            raise ValueError("冻结的重排模型不可用")
        info = apply_credential_override(info)
        adapter = (
            DashscopeReranker
            if (info.extra.get("rerank_protocol") or info.extra.get("protocol")) == "dashscope"
            else OpenAIReranker
        )
        model = adapter(
            model_name=info.model_id,
            api_key=info.api_key,
            base_url=info.base_url,
            parameters=dict(info.extra.get("parameters") or {}),
        )
        documents = [
            (str(hit.get("title") or "") + "\n" + str(hit.get("note_excerpt") or ""))[:4000] for hit in hits[:100]
        ]
        extract = model._extract_results

        def strict_extract(payload):
            entries = extract(payload)
            indices = [entry.get("index") for entry in entries]
            if any(type(index) is not int for index in indices) or sorted(indices) != list(range(len(documents))):
                raise ValueError("重排响应索引缺失或重复")
            if any("relevance_score" not in entry for entry in entries):
                raise ValueError("重排响应缺少分数")
            return entries

        model._extract_results = strict_extract
        try:
            # acompute_score silently substitutes 0.5 after provider errors.
            # The strict batch boundary propagates failure instead of claiming
            # a model reranking channel that did not actually execute.
            scores = await model._batch_rerank(query, documents, max_length=512)
            if len(scores) != len(documents) or not all(math.isfinite(score) for score in scores):
                raise ValueError("重排结果数量或数值无效")
            usage = llm_gateway._usage_context.get()
            usage.input_tokens += sum(max(1, math.ceil(len(text) / 4)) for text in [query, *documents])
            usage.estimated = True
        finally:
            await model.aclose()
    fresh = await build_access(scope.user, tenant_id)
    if not {hit["rid"] for hit in hits} <= fresh.rids:
        raise ValueError("重排期间来源权限已变更")
    ordered = [
        {**hit, "rerank_score": score, "rerank_model": model_spec} for hit, score in zip(hits, scores, strict=True)
    ]
    return sorted(ordered, key=lambda hit: (-hit["rerank_score"], -hit.get("rrf_score", 0), hit["rid"]))
