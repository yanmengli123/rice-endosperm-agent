"""Literature embeddings inherit requester credentials, quotas and usage ledger."""

from __future__ import annotations

import math
import uuid

from yuxi.literature import llm_gateway
from yuxi.literature.access import current_access
from yuxi.storage.postgres.manager import pg_manager


async def encode(policy, texts: list[str], *, query: bool = False) -> list[list[float]]:
    from yuxi.agents.models import apply_credential_override
    from yuxi.models.embed import OtherEmbedding
    from yuxi.models.providers.cache import model_cache

    async with llm_gateway.governed_runtime(policy):
        info = model_cache.get_model_info(policy.model_spec)
        if info is None or info.model_type != "embedding":
            raise ValueError("冻结的导航 Embedding 模型已不可用")
        # Construct a per-call instance; never mutate a cached platform model.
        info = apply_credential_override(info)
        model = OtherEmbedding(
            model=info.model_id,
            base_url=info.base_url,
            api_key=info.api_key,
            dimension=info.dimension,
            batch_size=info.batch_size,
        )
        vectors = []
        for start in range(0, len(texts), max(1, model.batch_size)):
            batch = texts[start : start + max(1, model.batch_size)]
            result = await (model.aencode_queries(batch) if query else model.aencode(batch))
            if len(result) != len(batch) or any(not v or not all(math.isfinite(float(n)) for n in v) for v in result):
                raise ValueError("Embedding 返回数量或数值无效")
            usage = llm_gateway._usage_context.get()
            # Existing embedding adapter does not expose provider usage. Record
            # successful batches conservatively as estimated, including batches
            # completed before a later batch fails.
            usage.input_tokens += sum(max(1, math.ceil(len(text) / 4)) for text in batch)
            usage.estimated = True
            vectors.extend(result)
        return vectors


async def encode_query(tenant_id: int, model_spec: str, query: str) -> list[list[float]]:
    scope = current_access()
    if scope is None or scope.tenant_id != tenant_id:
        raise ValueError("导航查询必须绑定请求者")
    async with pg_manager.get_async_session_context() as session:
        policy = await llm_gateway.freeze_literature_policy(
            session,
            tenant_id=tenant_id,
            uid=scope.user.uid,
            operation="navigation_query",
            resource_ref=f"nav-query:{uuid.uuid4().hex}",
            model_spec=model_spec,
            model_type="embedding",
        )
        await session.commit()
    return await encode(policy, [query], query=True)
