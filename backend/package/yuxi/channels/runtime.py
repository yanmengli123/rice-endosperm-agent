"""channels 运行时：持有「长连接/轮询」型入站传输的常驻进程。

当前实现 Telegram 长轮询（getUpdates offset 游标）。webhook 型平台不经过本
进程；飞书 WebSocket 长连接 / 钉钉 Stream 属后续增强（在监督循环里为新的
传输形态登记 runner 即可）。

单活与可靠性（ADR-0009）：
- **单活锁（A3）**：每个 app 一把 Redis 锁（TTL 60s，每次轮询前续期），
  多副本部署时仅持锁者拉取，其余 standby——Telegram getUpdates 双实例互拉
  会 409 抢占，单活是运行时强制而非注释约定；
- **游标持久化（A1）**：offset 存 ``channel_apps.config.transport_cursor``，
  仅在整批 ingest 落库成功后前进；进程重启从游标恢复，崩溃窗口内已处理
  的 update 重放由消息表幂等去重兜住（零丢失、零重复处理）；
- **动态 reconcile（A2）**：监督循环每 30s 对账启用应用集合，新建/停用
  免重启容器即生效。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from yuxi.channels.adapters.telegram import TelegramAdapter
from yuxi.utils.logging_config import logger

HEARTBEAT_KEY = "channel:runtime:heartbeat"
HEARTBEAT_FILE = "/tmp/channels_heartbeat"  # 容器健康检查锚点（文件年龄 < 200s）
HEARTBEAT_INTERVAL_SECONDS = 60
POLL_TIMEOUT_SECONDS = 30
ERROR_BACKOFF_SECONDS = 5
SUPERVISOR_INTERVAL_SECONDS = 30
LOCK_TTL_SECONDS = 60
STANDBY_SLEEP_SECONDS = 15
# N2：失败批退避——游标不前进时 getUpdates 会立刻返回同一批，无退避即成
# 对 Telegram + PG 的紧循环；退避升级到 ERROR 作为告警信号，但**绝不跳批**。
FAILED_BATCH_BACKOFF_SECONDS = 10
FAILED_BATCH_BACKOFF_CAP_SECONDS = 300
FAILED_BATCH_ALERT_ROUNDS = 3


async def _heartbeat_loop() -> None:
    from pathlib import Path

    from yuxi.storage.redis.manager import get_async_redis_client

    while True:
        stamp = str(int(time.time()))
        try:
            client = await get_async_redis_client()
            await client.set(HEARTBEAT_KEY, stamp, ex=HEARTBEAT_INTERVAL_SECONDS * 3)
        except Exception as error:  # noqa: BLE001 - 心跳失败不终止运行时
            logger.warning(f"channel runtime heartbeat failed: {type(error).__name__}")
        try:  # 双通道：Redis 供监控读，文件供容器 healthcheck 判活
            Path(HEARTBEAT_FILE).write_text(stamp)
        except Exception as error:  # noqa: BLE001
            logger.warning(f"channel runtime heartbeat file failed: {type(error).__name__}")
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# 单活锁（A3）
# ---------------------------------------------------------------------------

_INSTANCE_ID = f"{time.time_ns():x}-{id(object()):x}"


def _lock_key(app_id: int) -> str:
    return f"channel:runtime:lock:{app_id}"


async def _acquire_or_renew_lock(app_id: int) -> bool:
    """SET NX 抢锁 / 同值续期（GET 确认后 EXPIRE 刷新 TTL）。失败即 standby
    （另一副本在拉）。Redis 不可用 fail-open——锁防双活抖动，正确性由
    「游标 + 消息幂等」保证，不依赖锁。"""
    from yuxi.storage.redis.manager import get_async_redis_client

    try:
        client = await get_async_redis_client()
        if await client.set(_lock_key(app_id), _INSTANCE_ID, ex=LOCK_TTL_SECONDS, nx=True):
            return True
        if await client.get(_lock_key(app_id)) == _INSTANCE_ID:
            # 续期（F4）：不刷新 TTL 的锁会在 60s 后过期，双副本轮换互抢
            await client.expire(_lock_key(app_id), LOCK_TTL_SECONDS)
            return True
        return False
    except Exception as error:  # noqa: BLE001
        logger.warning(f"channel runtime lock unavailable app={app_id}: {type(error).__name__}")
        return True


# ---------------------------------------------------------------------------
# 游标持久化（A1）
# ---------------------------------------------------------------------------


async def _read_cursor(app_id: int) -> int:
    from sqlalchemy import select

    from yuxi.storage.postgres.manager import pg_manager
    from yuxi.storage.postgres.models_business import ChannelApp

    async with pg_manager.get_async_session_context() as db:
        app = (await db.execute(select(ChannelApp).where(ChannelApp.id == app_id))).scalar_one_or_none()
        return int((app.config or {}).get("transport_cursor") or 0) if app is not None else 0


async def _persist_cursor(app_id: int, offset: int) -> None:
    """整批 ingest 成功后前进游标（存 config JSONB，无需新迁移）。"""
    from sqlalchemy import select

    from yuxi.storage.postgres.manager import pg_manager
    from yuxi.storage.postgres.models_business import ChannelApp

    async with pg_manager.get_async_session_context() as db:
        app = (await db.execute(select(ChannelApp).where(ChannelApp.id == app_id))).scalar_one_or_none()
        if app is None:
            return
        config = dict(app.config or {})
        config["transport_cursor"] = int(offset)
        app.config = config
        await db.commit()


# ---------------------------------------------------------------------------
# Telegram 长轮询 runner
# ---------------------------------------------------------------------------


async def _run_telegram_long_poll(app_row: dict[str, Any]) -> None:
    """单个 Telegram 渠道应用的长轮询循环：锁 → 取批 → 逐条入站 → 前进游标。"""
    from yuxi.services.channel_service import ingest_channel_envelopes, load_channel_credentials

    app_id = int(app_row["id"])
    adapter = TelegramAdapter()
    credentials = await load_channel_credentials(app_id)
    offset = await _read_cursor(app_id)
    consecutive_failures = 0
    logger.info(f"[channels] telegram long-poll started for app {app_row['platform_app_id']} offset={offset}")
    while True:
        try:
            if not await _acquire_or_renew_lock(app_id):
                # 另一副本持锁：standby 等待（锁过期或对端下线后接管）
                await asyncio.sleep(STANDBY_SLEEP_SECONDS)
                continue
            updates = await adapter.fetch_updates(
                credentials=credentials,
                platform_app_id=str(app_row["platform_app_id"]),
                offset=offset,
                timeout=POLL_TIMEOUT_SECONDS,
            )
            if not updates:
                continue
            next_offset = max(int(update.get("update_id") or 0) for update in updates) + 1
            bot_username = await adapter.resolve_bot_username(credentials, str(app_row["platform_app_id"]))
            envelopes = [
                envelope
                for update in updates
                for envelope in adapter.parse_update(
                    update, platform_app_id=str(app_row["platform_app_id"]), bot_username=bot_username
                ).envelopes
            ]
            failures = await ingest_channel_envelopes(app_row, envelopes)
            # F3：仅整批零 hard-failure 才前进游标；失败时本批重拉
            # （已入库的靠消息表幂等吞掉），保证零丢失。
            if failures == 0:
                await _persist_cursor(app_id, next_offset)
                offset = next_offset  # 持久化成功后才推进内存游标（崩溃不丢窗口）
                consecutive_failures = 0
            else:
                # N2：退避后重拉本批（已入库的靠消息表幂等吞掉，保证零丢失）。
                consecutive_failures += 1
                exponent = min(consecutive_failures, 6) - 1
                backoff = min(FAILED_BATCH_BACKOFF_CAP_SECONDS, FAILED_BATCH_BACKOFF_SECONDS * (2**exponent))
                log = logger.error if consecutive_failures >= FAILED_BATCH_ALERT_ROUNDS else logger.warning
                log(
                    f"[channels] batch had {failures} failure(s) for app {app_row['platform_app_id']} "
                    f"(round {consecutive_failures}); cursor not advanced, re-fetch in {backoff}s — "
                    "绝不跳批（跳过=静默丢失）"
                )
                await asyncio.sleep(backoff)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - 单应用循环崩溃退避后继续
            logger.warning(f"[channels] telegram poll error for app {app_row['platform_app_id']}: {error}")
            await asyncio.sleep(ERROR_BACKOFF_SECONDS)


# ---------------------------------------------------------------------------
# 监督循环（A2）：按启用应用集合动态增删 runner
# ---------------------------------------------------------------------------


async def _load_long_poll_apps() -> list[dict[str, Any]]:
    """加载启用的 telegram 渠道应用（长轮询型）。"""
    from sqlalchemy import select

    from yuxi.storage.postgres.manager import pg_manager
    from yuxi.storage.postgres.models_business import ChannelApp

    async with pg_manager.get_async_session_context() as db:
        rows = (
            (
                await db.execute(
                    select(ChannelApp).where(
                        ChannelApp.channel_type == "telegram",
                        ChannelApp.is_enabled == True,  # noqa: E712
                    )
                )
            )
            .scalars()
            .all()
        )
        return [
            {
                "id": int(row.id),
                "channel_type": row.channel_type,
                "platform_app_id": row.platform_app_id,
            }
            for row in rows
        ]


async def _supervise_long_poll_runners() -> None:
    """每 30s 对账：新增启用应用 → 起 runner；停用/删除 → 取消 task。

    状态变化时输出一行日志（S7）：运维据此确认 reconcile 存活，无需重启容器。
    """
    runners: dict[int, asyncio.Task] = {}
    idle_logged = False
    while True:
        try:
            apps = {int(app["id"]): app for app in await _load_long_poll_apps()}
            if not apps and not idle_logged:
                logger.info("[channels] supervisor alive: no long-poll apps enabled, idling (webhook apps unaffected)")
                idle_logged = True
            if apps and idle_logged:
                idle_logged = False
            for app_id, task in list(runners.items()):
                if app_id not in apps or task.done():
                    if not task.done():
                        task.cancel()
                        logger.info(f"[channels] long-poll runner cancelled for app {app_id}")
                    del runners[app_id]
            for app_id, app in apps.items():
                if app_id not in runners:
                    runners[app_id] = asyncio.create_task(_run_telegram_long_poll(app))
                    logger.info(f"[channels] long-poll runner started for app {app_id}")
        except asyncio.CancelledError:
            for task in runners.values():
                task.cancel()
            raise
        except Exception as error:  # noqa: BLE001 - 对账失败不终止监督
            logger.warning(f"[channels] supervisor reconcile failed: {error}")
        await asyncio.sleep(SUPERVISOR_INTERVAL_SECONDS)


async def run_channels_runtime() -> None:
    """channels 进程主入口：初始化存储后并发驱动监督循环 + 心跳。"""
    from yuxi.storage.postgres.manager import pg_manager

    pg_manager.initialize()
    await pg_manager.create_business_tables()
    await pg_manager.ensure_business_schema()

    tasks = [
        asyncio.create_task(_heartbeat_loop()),
        asyncio.create_task(_supervise_long_poll_runners()),
    ]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
