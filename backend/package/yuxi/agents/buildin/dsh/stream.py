"""Gateway token-delta tap (ADR-0011 附录：DSH token 流式 PoC).

架构：内部 LLM 网关（api 进程）在流式补全经过时把文本 delta 写入按 run 隔离的
Redis Stream；DSH 轮次循环（worker 进程）并发读取并转成既有的
``stream_event(message_delta)`` 接缝事件。桌面端与 AgentRun wire 协议**零改动**
（yuxi.rs 已按 message_id 累积 message_delta，最终文本以 run 结果为准）。

边界：
- 观感通道，绝不影响受治轮次：任何 Redis 故障静默降级（网关侧吞异常、worker 侧
  取消读取任务），权威回答/用量仍只来自 PG 与 ``assistant/message`` 事件。
- 只转发正文 delta；reasoning 不外发（worker 侧 ``_sanitize_stream_event``
  本就会剥离），工具参数流不展示。
- run 结束即清理 stream key；MAXLEN + TTL 双保险防 worker 崩溃残留。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from yuxi.utils import logger

STREAM_KEY_PREFIX = "yuxi:dsh:tokens:"
STREAM_MAXLEN = 8192
STREAM_TTL_SECONDS = 900
DESKTOP_MESSAGE_ID = "dsh-stream"


def token_stream_enabled() -> bool:
    """双方（api 网关与 worker）读同一开关；默认开启，值为 0/false/off 时关闭。"""
    return os.getenv("YUXI_DSH_TOKEN_STREAM", "true").strip().lower() not in {"0", "false", "off"}


def stream_key(run_id: str) -> str:
    return f"{STREAM_KEY_PREFIX}{run_id}"


def token_delta_event(text: str, *, thread_id: str | None) -> tuple[str, dict[str, Any]]:
    """映射为 chat_service 既有 stream_event 接缝；message_id 恒定保持单段累积。"""
    return (
        "stream_event",
        {
            "type": "message_delta",
            "message_id": DESKTOP_MESSAGE_ID,
            "content": text,
            "namespace": [],
            "thread_id": thread_id,
        },
    )


async def publish_token_delta(run_id: str, text: str) -> None:
    """网关侧发布（尽力而为）：观感通道故障绝不让受治补全失败。"""
    if not text:
        return
    try:
        from yuxi.storage.redis import get_async_redis_client

        client = await get_async_redis_client()
        key = stream_key(run_id)
        await client.xadd(key, {"t": text}, maxlen=STREAM_MAXLEN, approximate=True)
        await client.expire(key, STREAM_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001 - cosmetic path must never fail the turn
        logger.warning(f"DSH token stream tap skipped ({type(exc).__name__})")


async def read_token_deltas(run_id: str, into_queue: asyncio.Queue[str]) -> None:
    """worker 侧读取：从轮次起点消费，直到被取消（轮次结束）。"""
    from yuxi.storage.redis import get_async_redis_client

    client = await get_async_redis_client()
    key = stream_key(run_id)
    last_id = "0-0"
    while True:
        entries = await client.xread({key: last_id}, block=1000, count=64)
        if not entries:
            continue
        for _, messages in entries:
            for entry_id, fields in messages:
                last_id = entry_id
                text = fields.get("t")
                if text:
                    await into_queue.put(text)


async def stop_token_stream(run_id: str, task: asyncio.Task | None) -> None:
    """轮次收尾：取消读取并清理 stream key（尽力而为）。"""
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    try:
        from yuxi.storage.redis import get_async_redis_client

        await (await get_async_redis_client()).delete(stream_key(run_id))
    except Exception:  # noqa: BLE001
        pass
