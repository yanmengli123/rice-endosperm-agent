"""渠道可观测最小平面：Redis 日桶计数器（不引入外部指标栈依赖）。

设计取舍：不新增 Prometheus/OTel 依赖，按「天 : 应用 : 指标」三层分桶的
Redis 计数器足以支撑运营告警（DEAD>0、验签失败率、重放、限流、绑定失败），
读取面是 admin 端点 ``GET /api/channels/metrics``；接入正式指标平面时
只需把这些计数转成 gauge/counter 导出，口径不变。

纪律：
- 所有写入 **fail-open**——可观测性故障不得影响消息通路（与 outbox/限流一致）；
- 桶键带 TTL（保留 8 天），不做无限增长；
- 只记计数与维度，不记消息内容（PII 纪律与 ``content_digest`` 同源）。
"""

from __future__ import annotations

import time
from typing import Any

from yuxi.utils.logging_config import logger

PREFIX = "channel:metric"
KEEP_DAYS = 8
KEY_TTL_SECONDS = KEEP_DAYS * 24 * 3600


class Metrics:
    """指标名闭集：新增指标在此登记，避免拼写漂移导致看板/告警静默失效。"""

    INBOUND_RECEIVED = "inbound_received"
    INBOUND_IP_BLOCKED = "inbound_ip_blocked"
    INBOUND_SIGNATURE_REJECTED = "inbound_signature_rejected"
    INBOUND_STALE_REJECTED = "inbound_stale_rejected"
    INBOUND_NONCE_DUPLICATE = "inbound_nonce_duplicate"
    INBOUND_INTERNAL_ERROR = "inbound_internal_error"
    INBOUND_INGEST_FAILED = "inbound_ingest_failed"
    GATE_REJECTED = "gate_rejected"
    GATE_IGNORED = "gate_ignored"
    BIND_SUCCEEDED = "bind_succeeded"
    BIND_FAILED = "bind_failed"
    BIND_THROTTLED = "bind_throttled"
    RUN_DISPATCHED = "run_dispatched"
    OUTBOUND_PUSHED = "outbound_pushed"
    OUTBOUND_DEFERRED = "outbound_deferred"
    OUTBOUND_DEAD = "outbound_dead"
    RETENTION_PURGED = "retention_purged"


def metric_day(offset: int = 0) -> str:
    """UTC 日桶（与 TIMESTAMPTZ 纪律一致，不用本地时区）。"""
    return time.strftime("%Y%m%d", time.gmtime(time.time() - offset * 86400))


def metric_key(name: str, app_id: int | None, day: str | None = None) -> str:
    return f"{PREFIX}:{day or metric_day()}:{int(app_id) if app_id else 'all'}:{name}"


async def incr_metric(name: str, *, app_id: int | None = None, delta: int = 1) -> None:
    """计数（fail-open；首次写入设置 TTL）。"""
    from yuxi.storage.redis.manager import get_async_redis_client

    try:
        client = await get_async_redis_client()
        key = metric_key(name, app_id)
        count = int(await client.incr(key, int(delta)) or 0)
        if count <= int(delta):  # 首次写入：挂 TTL，避免键无限增长
            await client.expire(key, KEY_TTL_SECONDS)
    except Exception as error:  # noqa: BLE001 - 观测失败不得影响消息通路
        logger.debug(f"channel metric write skipped ({name}): {type(error).__name__}")


async def read_metrics(days: int = 2) -> dict[str, Any]:
    """读取最近 N 天计数：``by_day``（天 → 指标 → 计数）与 ``by_app``（应用 → 指标）。"""
    from yuxi.storage.redis.manager import get_async_redis_client

    wanted = {metric_day(offset) for offset in range(max(1, min(int(days), KEEP_DAYS)))}
    result: dict[str, Any] = {"days": sorted(wanted, reverse=True), "by_day": {}, "by_app": {}, "errors": []}
    try:
        client = await get_async_redis_client()
        async for key in client.scan_iter(match=f"{PREFIX}:*", count=200):
            parts = str(key).split(":")
            if len(parts) != 5:
                continue
            _, _, day, scope, name = parts
            if day not in wanted:
                continue
            value = int(await client.get(key) or 0)
            result["by_day"].setdefault(day, {})
            result["by_day"][day][name] = result["by_day"][day].get(name, 0) + value
            if scope != "all":
                result["by_app"].setdefault(scope, {})
                result["by_app"][scope][name] = result["by_app"][scope].get(name, 0) + value
    except Exception as error:  # noqa: BLE001 - 读取失败返回空集 + 错误说明
        result["errors"].append(f"{type(error).__name__}: {error}")
    return result
