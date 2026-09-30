"""Telegram Bot 适配器（webhook + 长轮询双模式）。

webhook：``X-Telegram-Bot-Api-Secret-Token`` 常量时间比对（可选，凭据里配置
webhook_secret 时强制）；长轮询由 channels 运行时驱动 ``fetch_updates``。
出站：sendMessage 纯文本（4096 字符限制由渲染层分片；Telegram 侧渲染层已
把 Markdown 剥离为纯文本——sendMessage 不设 parse_mode，避免转义地狱）。

群聊语义（二轮修复）：@ 判定按 bot 自身 username 权威比对（getMe 缓存 1h，
冷缓存 fail-open 到旧启发式），并归一化 ``/help@bot`` / ``@bot /help`` 两种
写法——否则群内指令会被 mention_only 策略门静默丢弃。
"""

from __future__ import annotations

import json
import asyncio
from typing import Any

from yuxi.channels.adapters.base import ChannelAdapter, ChannelPushError, bot_identity_cache, get_http_client
from yuxi.channels.contracts import ChannelEnvelope, InboundParseResult, OutboundPayload
from yuxi.channels.signing import ChannelSignatureError, constant_time_equals
from yuxi.utils.logging_config import logger


class TelegramAdapter(ChannelAdapter):
    channel_type = "telegram"
    label = "Telegram Bot"
    inbound_modes = ("webhook", "long_poll")
    outbound_rate_per_second = 20

    def _api_base(self, credentials: dict[str, Any]) -> str:
        self._validate_credentials(credentials, ("bot_token",))
        return f"https://api.telegram.org/bot{credentials['bot_token']}"

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
        webhook_secret = str(credentials.get("webhook_secret") or "")
        if webhook_secret:
            provided = str(headers.get("x-telegram-bot-api-secret-token") or "")
            if not constant_time_equals(webhook_secret, provided):
                raise ChannelSignatureError("Telegram webhook secret_token 校验失败")
        try:
            update = json.loads(body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise ChannelSignatureError("Telegram 报文 JSON 非法") from exc
        bot_username = await self.resolve_bot_username(credentials, platform_app_id)
        return self.parse_update(update, platform_app_id=platform_app_id, bot_username=bot_username)

    async def resolve_bot_username(self, credentials: dict[str, Any] | None, platform_app_id: str) -> str | None:
        """bot 自身 username（群聊 @ 判定与指令归一化的权威依据）。

        ``getMe`` 获取并缓存 1h；凭据缺失/网络异常返回 None（调用方回退旧启发式
        ——@ 判定宁退勿断）。
        """
        cached = await bot_identity_cache.get(self.channel_type, platform_app_id)
        if cached:
            return cached
        if not credentials:
            return None
        try:
            response = await get_http_client().post(f"{self._api_base(credentials)}/getMe")
            data = response.json()
            username = str(((data.get("result") or {}).get("username")) or "") if data.get("ok") else ""
            if username:
                await bot_identity_cache.set(self.channel_type, platform_app_id, username)
                return username
        except Exception as error:  # noqa: BLE001 - 冷缓存回退为启发式，不阻断消息
            logger.warning(f"telegram getMe failed app={platform_app_id}: {type(error).__name__}")
        return None

    @staticmethod
    def normalize_group_text(text: str, bot_username: str | None) -> str:
        """群聊指令归一化：``/help@mybot`` → ``/help``；``@mybot /help`` → ``/help``。

        不归一化时两种写法都会落到默认指令解析之外（前者解析成
        ``/help@mybot``，后者不以 ``/`` 开头）→ 群内指令实质不可用。
        """
        stripped = (text or "").strip()
        if not stripped or not bot_username:
            return stripped
        handle = f"@{bot_username}".lower()
        if stripped.startswith("/"):
            head, separator, rest = stripped.partition(" ")
            lowered_head = head.lower()
            if handle in lowered_head:
                head = head[: lowered_head.index(handle)]
            return (head + separator + rest).strip()
        if stripped.lower().startswith(handle):
            return stripped[len(handle) :].strip()
        return stripped

    @staticmethod
    def detect_mention(message: dict[str, Any], bot_username: str | None) -> bool:
        """群聊 @ 判定：``bot_command`` 实体、按 bot username 比对的 mention、回复本 bot。

        旧实现是「任意 mention 实体即算 @ 我」——群里 @ 别人也会误触发。
        冷缓存（bot_username 为 None）时维持旧启发式：宁多答勿失答。
        """
        entities = message.get("entities") or []
        entity_types = {str(entity.get("type") or "") for entity in entities}
        if "bot_command" in entity_types:
            return True
        if "mention" in entity_types:
            if not bot_username:
                return True  # fail-open：与旧语义一致
            raw_text = str(message.get("text") or "")
            return f"@{bot_username}".lower() in raw_text.lower()
        reply_from = ((message.get("reply_to_message") or {}).get("from") or {}).get("username")
        return bool(bot_username) and str(reply_from or "").lower() == bot_username.lower()

    def parse_update(
        self, update: dict[str, Any], *, platform_app_id: str, bot_username: str | None = None
    ) -> InboundParseResult:
        """长轮询与 webhook 共用的 update 解析（纯同步；username 由调用方预解析）。"""
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        from_user = message.get("from") or {}
        chat_type = str(chat.get("type") or "private")
        text = self.normalize_group_text(str(message.get("text") or "").strip(), bot_username)
        mentioned = chat_type != "private" and self.detect_mention(message, bot_username)
        envelope = ChannelEnvelope(
            channel_type=self.channel_type,
            platform_app_id=platform_app_id,
            platform_message_id=str(update.get("update_id") or message.get("message_id") or "") or None,
            chat_id=str(chat.get("id") or ""),
            chat_type="group" if chat_type in ("group", "supergroup") else "p2p",
            user_id=str(from_user.get("id") or "") or None,
            user_display=" ".join(filter(None, [from_user.get("first_name"), from_user.get("last_name")])) or None,
            text=text,
            mentioned_me=mentioned,
            raw={},
        )
        return InboundParseResult(envelopes=[envelope], ack_body='{"ok":true}')

    async def fetch_updates(
        self, *, credentials: dict[str, Any], platform_app_id: str, offset: int, timeout: int
    ) -> list[dict[str, Any]]:
        """长轮询取一批 update（channels 运行时驱动）。"""
        response = await get_http_client().post(
            f"{self._api_base(credentials)}/getUpdates",
            json={"offset": offset, "timeout": timeout, "allowed_updates": ["message"]},
        )
        if response.status_code != 200:
            raise ChannelPushError(f"Telegram getUpdates 失败：HTTP {response.status_code}")
        data = response.json()
        if not data.get("ok"):
            raise ChannelPushError(f"Telegram getUpdates 失败：{data.get('description')}", retryable=False)
        return list(data.get("result") or [])

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
        sent = 0
        for index, chunk in enumerate(payload.chunks[start_chunk:], start=start_chunk):
            response = await get_http_client().post(
                f"{self._api_base(credentials)}/sendMessage",
                json={"chat_id": chat_id, "text": chunk},
            )
            data = {}
            try:
                data = response.json()
            except ValueError:
                pass
            if not data.get("ok"):
                error_code = int(data.get("error_code") or 0)
                # 429 携带 parameters.retry_after：relay 取 max(退避, retry_after)
                retry_after = None
                parameters = data.get("parameters")
                if isinstance(parameters, dict) and parameters.get("retry_after") is not None:
                    retry_after = float(parameters["retry_after"])
                raise ChannelPushError(
                    f"Telegram 消息发送失败：{error_code} {data.get('description')}",
                    retryable=error_code not in (400, 403, 404),
                    chunks_sent=sent,
                    retry_after=retry_after,
                )
            sent += 1
            if index < len(payload.chunks) - 1:
                await asyncio.sleep(1.0 / (self.outbound_rate_per_second or 20))
