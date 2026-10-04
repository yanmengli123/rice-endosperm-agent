"""Embedding navigation must use the same governed runtime as compilation."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from yuxi.literature import embedding_gateway, llm_gateway

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("query,fail_later", [(False, False), (True, False), (False, True)])
async def test_batches_use_credential_override_and_account_partial_usage(monkeypatch, query, fail_later):
    from yuxi.agents import models
    from yuxi.models import embed
    from yuxi.models.providers.cache import model_cache

    events = []
    usage = llm_gateway._Usage()
    policy = SimpleNamespace(model_spec="fixture:embedding")
    info = SimpleNamespace(
        model_type="embedding",
        model_id="platform",
        base_url="https://fixture.invalid",
        api_key="fixture-placeholder",
        dimension=2,
        batch_size=1,
    )

    @asynccontextmanager
    async def governed(actual):
        assert actual is policy
        token = llm_gateway._usage_context.set(usage)
        events.append("governed")
        try:
            yield
        finally:
            events.append("settled")
            llm_gateway._usage_context.reset(token)

    def credential(actual):
        assert actual is info
        events.append("override")
        return SimpleNamespace(**{**vars(info), "model_id": "user-model"})

    class FakeEmbedding:
        def __init__(self, **kwargs):
            assert kwargs["model"] == "user-model"
            self.batch_size = kwargs["batch_size"]

        async def aencode(self, batch):
            events.append(("document", batch))
            if fail_later and batch == ["second"]:
                raise RuntimeError("fixture later batch failure")
            return [[1.0, 0.0]]

        async def aencode_queries(self, batch):
            events.append(("query", batch))
            return [[1.0, 0.0]]

    monkeypatch.setattr(llm_gateway, "governed_runtime", governed)
    monkeypatch.setattr(model_cache, "get_model_info", lambda spec: info)
    monkeypatch.setattr(models, "apply_credential_override", credential)
    monkeypatch.setattr(embed, "OtherEmbedding", FakeEmbedding)
    if fail_later:
        with pytest.raises(RuntimeError, match="later batch"):
            await embedding_gateway.encode(policy, ["first", "second"], query=query)
        assert usage.input_tokens == 2
    else:
        assert await embedding_gateway.encode(policy, ["first", "second"], query=query) == [[1.0, 0.0], [1.0, 0.0]]
        assert usage.input_tokens == 4
    assert usage.estimated and events[:2] == ["governed", "override"] and events[-1] == "settled"


async def test_query_cannot_use_anonymous_or_cross_tenant_platform_model():
    from dataclasses import replace
    from yuxi.literature.access import LiteratureAccess, _access

    scope = LiteratureAccess(7, frozenset(), frozenset(), {}, {}, SimpleNamespace(uid="u1"))
    token = _access.set(None)
    try:
        with pytest.raises(ValueError, match="绑定请求者"):
            await embedding_gateway.encode_query(7, "fixture:embedding", "query")
        _access.set(replace(scope, tenant_id=8))
        with pytest.raises(ValueError, match="绑定请求者"):
            await embedding_gateway.encode_query(7, "fixture:embedding", "query")
    finally:
        _access.reset(token)
