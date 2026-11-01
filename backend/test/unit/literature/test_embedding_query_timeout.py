"""导航 Embedding 查询必须有界：外部调用挂起时超时抛出，由检索层降级关键词通道。

回归 2026-11-01 验收：无超时的 encode_query 把文献检索工具整体挂死到 300s 全局看门狗。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from yuxi.literature import embedding_gateway as gateway


async def test_encode_query_times_out_instead_of_hanging(monkeypatch):
    monkeypatch.setattr(gateway, "EMBEDDING_QUERY_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(
        gateway, "current_access", lambda: SimpleNamespace(tenant_id=1, user=SimpleNamespace(uid="u1"))
    )

    async def hang(_tenant_id, _model_spec, _query):
        await asyncio.sleep(30)
        raise AssertionError("不应执行到此处")

    monkeypatch.setattr(gateway, "_encode_query_governed", hang)
    with pytest.raises(TimeoutError, match="回退关键词通道"):
        await gateway.encode_query(1, "provider:model", "查询词")
