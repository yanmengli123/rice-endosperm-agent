"""Token 流式 tap（ADR-0011 附录）：观感通道的纯逻辑与降级行为。

不依赖真实 Redis：用假客户端记录 XADD/EXPIRE/DELETE/XREAD 调用；
关键断言是"观感通道故障绝不影响受治轮次"（发布异常被吞、读取可被取消清理）。
"""

import asyncio

import pytest

from yuxi.agents.buildin.dsh import stream as token_stream


class _FakeRedis:
    def __init__(self, *, fail=False, entries=None):
        self.fail = fail
        self.entries = entries or []
        self.xadd_calls = []
        self.expire_calls = []
        self.deleted = []

    async def xadd(self, key, fields, **kwargs):
        if self.fail:
            raise RuntimeError("redis down")
        self.xadd_calls.append((key, fields, kwargs))

    async def expire(self, key, ttl):
        if self.fail:
            raise RuntimeError("redis down")
        self.expire_calls.append((key, ttl))

    async def delete(self, *keys):
        self.deleted.extend(keys)

    async def xread(self, streams, block=None, count=None):
        if self.fail:
            raise RuntimeError("redis down")
        if not self.entries:
            await asyncio.sleep(3600)
        batch, self.entries = self.entries, []
        return batch


@pytest.fixture
def fake_redis(monkeypatch):
    holder = []

    async def get_client():
        return holder[0]

    import yuxi.storage.redis as redis_module

    monkeypatch.setattr(redis_module, "get_async_redis_client", get_client)

    def install(client):
        holder.clear()
        holder.append(client)
        return client

    return install


def test_token_delta_event_uses_the_existing_message_delta_seam():
    mode, payload = token_stream.token_delta_event("水稻", thread_id="t-1")
    assert mode == "stream_event"
    assert payload["type"] == "message_delta"
    assert payload["content"] == "水稻"
    assert payload["message_id"] == token_stream.DESKTOP_MESSAGE_ID
    assert payload["namespace"] == []
    assert payload["thread_id"] == "t-1"


def test_token_stream_toggle():
    assert token_stream.token_stream_enabled() is True
    import os

    old = os.environ.get("YUXI_DSH_TOKEN_STREAM")
    try:
        os.environ["YUXI_DSH_TOKEN_STREAM"] = "off"
        assert token_stream.token_stream_enabled() is False
        os.environ["YUXI_DSH_TOKEN_STREAM"] = "true"
        assert token_stream.token_stream_enabled() is True
    finally:
        if old is None:
            os.environ.pop("YUXI_DSH_TOKEN_STREAM", None)
        else:
            os.environ["YUXI_DSH_TOKEN_STREAM"] = old


async def test_publish_writes_run_scoped_stream_with_caps(fake_redis):
    client = fake_redis(_FakeRedis())
    await token_stream.publish_token_delta("run-1", "胚乳")
    assert client.xadd_calls == [
        (token_stream.stream_key("run-1"), {"t": "胚乳"}, {"maxlen": token_stream.STREAM_MAXLEN, "approximate": True})
    ]
    assert client.expire_calls == [(token_stream.stream_key("run-1"), token_stream.STREAM_TTL_SECONDS)]
    await token_stream.publish_token_delta("run-1", "")
    assert len(client.xadd_calls) == 1


async def test_publish_never_raises_when_redis_fails(fake_redis):
    fake_redis(_FakeRedis(fail=True))
    await token_stream.publish_token_delta("run-1", "文本")  # 不得抛出


async def test_reader_forwards_entries_until_cancelled(fake_redis):
    key = token_stream.stream_key("run-1")
    client = fake_redis(
        _FakeRedis(entries=[(key, [("1700000000000-0", {"t": "稻"}), ("1700000000001-0", {"t": "芯"})])])
    )
    queue: asyncio.Queue[str] = asyncio.Queue()
    task = asyncio.create_task(token_stream.read_token_deltas("run-1", queue))
    assert await asyncio.wait_for(queue.get(), timeout=2) == "稻"
    assert await asyncio.wait_for(queue.get(), timeout=2) == "芯"
    await token_stream.stop_token_stream("run-1", task)
    assert client.deleted == [token_stream.stream_key("run-1")]
    assert task.cancelled() or task.done()


async def test_stop_survives_redis_failure(fake_redis):
    fake_redis(_FakeRedis(fail=True))
    await token_stream.stop_token_stream("run-1", None)  # 不得抛出
