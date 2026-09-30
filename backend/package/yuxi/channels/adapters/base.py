"""适配器基座：共享 httpx 客户端与平台 access_token 的 Redis 缓存。

token 缓存键 ``channel:token:{channel_type}:{platform_app_id}``，TTL 取平台
过期时间减 5 分钟（封顶 2 小时）；平台返回 token 失效错误码时由各适配器清缓存
重试一次。
"""

from __future__ import annotations

from typing import Any

import httpx

from yuxi.channels.contracts import InboundParseResult, OutboundPayload
from yuxi.utils.logging_config import logger

HTTP_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
_shared_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True)
    return _shared_client


async def close_http_client() -> None:
    global _shared_client
    if _shared_client is not None and not _shared_client.is_closed:
        await _shared_client.aclose()
    _shared_client = None


class ChannelPushError(Exception):
    """出站推送失败：携带投递进度与可重试语义，outbox relay 据此续传/退避/死信。

    - ``chunks_sent``：本次调用内已成功发出的分片数（部分成功时报告，relay
      据此推进 outbox 的 ``delivered_chunks`` 游标，重试不重复推送已发片）；
    - ``retry_after``：平台明示的等待秒数（如 Telegram 429 的
      ``parameters.retry_after``），relay 取 max(指数退避, retry_after)。
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = True,
        chunks_sent: int = 0,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.chunks_sent = int(chunks_sent)
        self.retry_after = retry_after


class token_cache:  # noqa: N801 - 类式命名空间，避免与函数式缓存装饰器混淆
    """平台 access_token 的 Redis 缓存（异步）。"""

    @staticmethod
    def _key(channel_type: str, platform_app_id: str) -> str:
        return f"channel:token:{channel_type}:{platform_app_id}"

    @staticmethod
    async def get(channel_type: str, platform_app_id: str) -> str | None:
        from yuxi.storage.redis.manager import get_async_redis_client

        try:
            client = await get_async_redis_client()
            return await client.get(token_cache._key(channel_type, platform_app_id))
        except Exception as error:  # noqa: BLE001 - 缓存不可用时退化为直连获取
            logger.warning(f"channel token cache read failed: {type(error).__name__}")
            return None

    @staticmethod
    async def set(channel_type: str, platform_app_id: str, token: str, expire_seconds: int) -> None:
        from yuxi.storage.redis.manager import get_async_redis_client

        ttl = max(60, min(expire_seconds - 300, 7200))
        try:
            client = await get_async_redis_client()
            await client.set(token_cache._key(channel_type, platform_app_id), token, ex=ttl)
        except Exception as error:  # noqa: BLE001
            logger.warning(f"channel token cache write failed: {type(error).__name__}")

    @staticmethod
    async def clear(channel_type: str, platform_app_id: str) -> None:
        from yuxi.storage.redis.manager import get_async_redis_client

        try:
            client = await get_async_redis_client()
            await client.delete(token_cache._key(channel_type, platform_app_id))
        except Exception as error:  # noqa: BLE001
            logger.warning(f"channel token cache clear failed: {type(error).__name__}")


class bot_identity_cache:  # noqa: N801 - 与 token_cache 同风格的类式命名空间
    """平台 bot 自身身份（飞书 open_id / Telegram username）的 Redis 缓存。

    回调热路径只读缓存（命中即一次 Redis GET），外部调用只在
    ``prewarm``（管理路径）或冷缓存时发生——避免在飞书 3s / 微信系 5s 的
    回调预算内打外部 HTTP。
    """

    TTL_SECONDS = 3600

    @staticmethod
    def _key(channel_type: str, platform_app_id: str) -> str:
        return f"channel:bot:{channel_type}:{platform_app_id}"

    @staticmethod
    async def get(channel_type: str, platform_app_id: str) -> str | None:
        from yuxi.storage.redis.manager import get_async_redis_client

        try:
            client = await get_async_redis_client()
            value = await client.get(bot_identity_cache._key(channel_type, platform_app_id))
            return str(value) if value else None
        except Exception as error:  # noqa: BLE001 - 缓存不可用退化为现场获取
            logger.warning(f"channel bot identity cache read failed: {type(error).__name__}")
            return None

    @staticmethod
    async def set(channel_type: str, platform_app_id: str, value: str) -> None:
        from yuxi.storage.redis.manager import get_async_redis_client

        try:
            client = await get_async_redis_client()
            await client.set(
                bot_identity_cache._key(channel_type, platform_app_id), value, ex=bot_identity_cache.TTL_SECONDS
            )
        except Exception as error:  # noqa: BLE001
            logger.warning(f"channel bot identity cache write failed: {type(error).__name__}")


class ChannelAdapter:
    """平台适配器契约：子类按平台实现验签解析、渲染与推送。"""

    channel_type: str = ""
    label: str = ""
    inbound_modes: tuple[str, ...] = ("webhook",)
    supports_pairing_qr: bool = False
    # 平台 token 失效错误码（触发清缓存重取一次）
    token_expired_codes: tuple[int, ...] = ()
    # 出站保守限速（次/秒，按 app 级令牌桶执行）；None 表示不限。
    # 具体频控以官方文档为准，此处取安全值防触发平台 4xx/频控封禁。
    outbound_rate_per_second: int | None = None

    async def parse_inbound(
        self,
        *,
        method: str,
        query: dict[str, str],
        headers: dict[str, str],
        body: bytes,
        credentials: dict[str, Any],
        platform_app_id: str,
    ) -> InboundParseResult:
        raise NotImplementedError

    def render_outbound(self, markdown_text: str, *, web_url: str | None = None) -> OutboundPayload:
        from yuxi.channels.render import render_outbound

        text, chunks = render_outbound(markdown_text, self.channel_type, web_url=web_url)
        return OutboundPayload(kind="text", chunks=chunks, fallback_text=text[:200], web_url=web_url)

    async def push(
        self,
        *,
        credentials: dict[str, Any],
        platform_app_id: str,
        chat_id: str,
        user_id: str | None,
        payload: OutboundPayload,
        start_chunk: int = 0,
    ) -> None:
        """按 ``start_chunk`` 续传分片；部分成功时以 ChannelPushError.chunks_sent 报告进度。"""
        raise NotImplementedError

    async def create_pairing_qr(self, *, credentials: dict[str, Any], code: str) -> str | None:
        """支持带参二维码的平台（公众号）返回二维码图片 URL，其余返回 None。"""
        return None

    def _validate_credentials(self, credentials: dict[str, Any], required: tuple[str, ...]) -> None:
        missing = [key for key in required if not str(credentials.get(key) or "").strip()]
        if missing:
            raise ChannelPushError(f"渠道凭据缺失：{','.join(missing)}", retryable=False)
