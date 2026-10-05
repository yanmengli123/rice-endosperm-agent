"""Milvus 启动连接退避重试测试（阶段 0 环境加固的代码侧兜底）。

背景：compose 已等 9091 healthz，但 healthz 通过 ≠ gRPC 19530 Proxy 就绪
（Docker Desktop 重启后常见）；此前硬失败会让 uvicorn 直接退出。
覆盖：退避后成功、耗尽后上抛、退避参数走环境变量。
"""

from __future__ import annotations

import pytest

from yuxi.knowledge.implementations import milvus as milvus_mod


def _bare_kb():
    obj = object.__new__(milvus_mod.MilvusKB)
    obj.connection_alias = "test-alias"
    obj.milvus_uri = "http://milvus:19530"
    obj.milvus_token = ""
    obj.milvus_db = "default"
    return obj


def test_init_connection_retries_until_ready(monkeypatch):
    kb = _bare_kb()
    calls = {"n": 0}
    sleeps: list[float] = []

    def fake_connect(**_kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("Milvus Proxy is not ready yet")

    monkeypatch.setattr(milvus_mod.connections, "connect", fake_connect)
    monkeypatch.setattr(milvus_mod.db, "list_database", lambda using: ["default"])
    monkeypatch.setattr(milvus_mod.db, "using_database", lambda *a, **k: None)
    monkeypatch.setattr(milvus_mod.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setenv("MILVUS_CONNECT_RETRIES", "5")
    monkeypatch.setenv("MILVUS_CONNECT_RETRY_DELAY", "1")

    kb._init_connection()

    assert calls["n"] == 3, "前两次失败第三次成功"
    assert sleeps == [1.0, 1.0], "失败间隔按环境变量退避"


def test_init_connection_raises_after_budget_exhausted(monkeypatch):
    kb = _bare_kb()
    attempts = {"n": 0}

    def always_fail(**_kwargs):
        attempts["n"] += 1
        raise RuntimeError("Proxy is not ready yet")

    monkeypatch.setattr(milvus_mod.connections, "connect", always_fail)
    monkeypatch.setattr(milvus_mod.time, "sleep", lambda s: None)
    monkeypatch.setenv("MILVUS_CONNECT_RETRIES", "3")
    monkeypatch.setenv("MILVUS_CONNECT_RETRY_DELAY", "1")

    with pytest.raises(RuntimeError, match="Proxy is not ready"):
        kb._init_connection()

    assert attempts["n"] == 3, "有界重试：恰好尝试预算次数后上抛，不无限循环"
