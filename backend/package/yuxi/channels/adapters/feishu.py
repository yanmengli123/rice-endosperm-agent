"""飞书自建应用适配器（webhook 模式）。

入站：事件订阅 v2.0——URL 校验挑战、可选 encrypt_key 加密（sha256 签名 +
AES-256-CBC）、event_id 幂等键、群聊 @ 识别（mentions 含本应用 open_id）。
出站：tenant_access_token（内部应用自取）+ im/v1/messages 发送。

长连接（WebSocket）模式属后续增强：入站传输换成 SDK 客户端，本适配器的
解析与推送层不变。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from yuxi.channels.adapters.base import (
    ChannelAdapter,
    ChannelPushError,
    bot_identity_cache,
    get_http_client,
    token_cache,
)
from yuxi.channels.contracts import ChannelChallenge, ChannelEnvelope, InboundParseResult, OutboundPayload
from yuxi.channels.render import markdown_mode
from yuxi.channels.signing import ChannelSignatureError, feishu_decrypt, feishu_signature

FEISHU_BASE = "https://open.feishu.cn"


def _extract_text(message: dict[str, Any]) -> str:
    """message.content 是 JSON 字符串；text 类型取 text 并剥离 @ 段。"""
    if message.get("message_type") != "text":
        return ""
    try:
        content = json.loads(message.get("content") or "{}")
    except json.JSONDecodeError:
        return ""
    return str(content.get("text") or "")


class FeishuAdapter(ChannelAdapter):
    channel_type = "feishu"
    label = "飞书自建应用"
    token_expired_codes = (99991661, 99991663, 99991668)
    outbound_rate_per_second = 5

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
        if method != "POST":
            return InboundParseResult(ack_body="{}", ack_media_type="application/json")

        body_text = body.decode("utf-8", errors="replace")
        encrypt_key = str(credentials.get("encrypt_key") or "")
        if encrypt_key:
            signature = str(headers.get("x-lark-signature") or headers.get("X-Lark-Signature") or "")
            timestamp = str(headers.get("x-lark-request-timestamp") or headers.get("X-Lark-Request-Timestamp") or "")
            nonce = str(headers.get("x-lark-request-nonce") or headers.get("X-Lark-Request-Nonce") or "")
            expected = feishu_signature(encrypt_key, timestamp, nonce, body_text)
            if not signature or not hmac_compare(signature, expected):
                raise ChannelSignatureError("飞书签名校验失败")
            try:
                outer = json.loads(body_text)
                event_json = feishu_decrypt(encrypt_key, str(outer.get("encrypt") or ""))
            except (json.JSONDecodeError, KeyError) as exc:
                raise ChannelSignatureError("飞书加密报文结构非法") from exc
            event = json.loads(event_json)
        else:
            try:
                event = json.loads(body_text)
            except json.JSONDecodeError as exc:
                raise ChannelSignatureError("飞书报文 JSON 非法") from exc

        verification_token = str(credentials.get("verification_token") or "")
        if verification_token:
            # v2 事件 token 在 header；v1 兼容顶层
            event_token = str((event.get("header") or {}).get("token") or event.get("token") or "")
            if event_token != verification_token:
                raise ChannelSignatureError("飞书 verification_token 不匹配")

        if event.get("type") == "url_verification" or "challenge" in event:
            return InboundParseResult(
                challenge=ChannelChallenge(body=json.dumps({"challenge": event.get("challenge", "")})),
                ack_body='{"challenge":""}',
            )

        header = event.get("header") or {}
        event_id = str(header.get("event_id") or "")
        event_type = str(header.get("event_type") or "")
        if event_type != "im.message.receive_v1":
            return InboundParseResult(ack_body='{"code":0}')

        message_event = event.get("event") or {}
        message = message_event.get("message") or {}
        sender = message_event.get("sender") or {}
        sender_id = (sender.get("sender_id") or {}).get("open_id") or ""
        chat_id = str(message.get("chat_id") or "")
        chat_type = "group" if str(message.get("chat_type") or "") == "group" else "p2p"

        mentions = message_event.get("mentions") or []
        bot_open_id = await self._get_bot_open_id(credentials, platform_app_id)
        if bot_open_id:
            # 权威判定：mention.id.open_id 与 bot 自身 open_id 比对
            # （bot/v3/info 获取并缓存），不依赖事件订阅范围配置。
            mentioned = chat_type == "group" and any(
                str(((mention.get("id") or {}).get("open_id")) or "") == bot_open_id for mention in mentions
            )
        else:
            # 回退启发式：@ 机器人的 mention 恒为第一个占位符（要求事件订阅
            # 配置为「仅接收@机器人消息」，管理页有说明）。
            mentioned = chat_type == "group" and any(
                str(mention.get("key") or "") == "@_user_1" for mention in mentions
            )

        text = _extract_text(message)
        # 群聊剥离 @ 文本段（@_user_1 占位）
        if chat_type == "group":
            text = " ".join(part for part in text.split(" ") if not part.startswith("@_user"))

        raw = {"event_id": event_id, "event_type": event_type}
        if message.get("message_type") not in (None, "text"):
            raw["unsupported_media"] = True  # 服务层回「暂仅支持文本」提示
        envelope = ChannelEnvelope(
            channel_type=self.channel_type,
            platform_app_id=platform_app_id,
            platform_message_id=str(message.get("message_id") or event_id) or None,
            chat_id=chat_id,
            chat_type=chat_type,
            user_id=sender_id or None,
            text=text.strip(),
            mentioned_me=mentioned,
            raw=raw,
        )
        return InboundParseResult(envelopes=[envelope], ack_body='{"code":0}')

    def render_outbound(self, markdown_text: str, *, web_url: str | None = None) -> OutboundPayload:
        payload = super().render_outbound(markdown_text, web_url=web_url)
        # card 模式（render.markdown_mode）：Markdown 交卡片 markdown 组件渲染
        payload.kind = "card"
        return payload

    async def _get_tenant_access_token(
        self, credentials: dict[str, Any], platform_app_id: str, *, force: bool = False
    ) -> str:
        self._validate_credentials(credentials, ("app_id", "app_secret"))
        if not force:
            cached = await token_cache.get(self.channel_type, platform_app_id)
            if cached:
                return cached
        response = await get_http_client().post(
            f"{FEISHU_BASE}/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": credentials["app_id"], "app_secret": credentials["app_secret"]},
        )
        data = response.json()
        if data.get("code") != 0:
            raise ChannelPushError(f"飞书 tenant_access_token 获取失败：{data.get('msg')}", retryable=False)
        token = str(data["tenant_access_token"])
        await token_cache.set(self.channel_type, platform_app_id, token, int(data.get("expire") or 7200))
        return token

    async def _get_bot_open_id(self, credentials: dict[str, Any], platform_app_id: str) -> str | None:
        """bot 自身 open_id（@ 判定权威依据）：bot/v3/info 获取，Redis 缓存 1h。

        凭据/网络异常时返回 None，调用方回退到占位符启发式——@ 判定宁退勿断。
        回调热路径命中缓存即一次 Redis GET；创建/更新凭据路径会预热（S5）。
        """
        cached = await bot_identity_cache.get(self.channel_type, platform_app_id)
        if cached:
            return cached
        try:
            token = await self._get_tenant_access_token(credentials, platform_app_id)
            response = await get_http_client().get(
                f"{FEISHU_BASE}/open-apis/bot/v3/info", headers={"Authorization": f"Bearer {token}"}
            )
            data = response.json()
            open_id = str(((data.get("bot") or {}).get("open_id")) or "") or None
            if open_id:
                await bot_identity_cache.set(self.channel_type, platform_app_id, open_id)
            return open_id
        except Exception:  # noqa: BLE001 - @ 判定降级为启发式，不阻断消息
            return None

    @staticmethod
    def _card_payload(chunk: str) -> dict[str, Any]:
        """飞书 interactive 卡片的 markdown 元素（渲染层把飞书判为 card 模式）。"""
        return {"config": {"wide_screen_mode": True}, "elements": [{"tag": "markdown", "content": chunk}]}

    async def _post_chunk(self, *, token: str, chat_id: str, chunk: str, card: bool) -> tuple[int, str]:
        content = (
            json.dumps(self._card_payload(chunk), ensure_ascii=False)
            if card
            else json.dumps({"text": chunk}, ensure_ascii=False)
        )
        response = await get_http_client().post(
            f"{FEISHU_BASE}/open-apis/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            headers={"Authorization": f"Bearer {token}"},
            json={
                "receive_id": chat_id,
                "msg_type": "interactive" if card else "text",
                "content": content,
            },
        )
        try:
            data = response.json()
        except ValueError:
            return -1, f"HTTP {response.status_code}"
        return int(data.get("code") or 0), str(data.get("msg") or "")

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
        position = start_chunk  # F5：跨 token 重取保持进度，刷新后不重发已成功片
        card = markdown_mode(self.channel_type) == "card"
        for attempt in range(2):
            token = await self._get_tenant_access_token(credentials, platform_app_id, force=attempt > 0)
            while position < len(payload.chunks):
                chunk = payload.chunks[position]
                code, message = await self._post_chunk(token=token, chat_id=chat_id, chunk=chunk, card=card)
                if code != 0:
                    if code in self.token_expired_codes and attempt == 0:
                        await token_cache.clear(self.channel_type, platform_app_id)
                        break  # 换新 token 从 position 续发，不回卷
                    if card:
                        # 卡片被拒（平台策略/版本差异）：降级为纯文本重发同一片，
                        # 不重发已成功片（position 未前进）。
                        code, message = await self._post_chunk(token=token, chat_id=chat_id, chunk=chunk, card=False)
                        if code == 0:
                            sent += 1
                            position += 1
                            if position < len(payload.chunks):
                                await asyncio.sleep(1.0 / (self.outbound_rate_per_second or 5))
                            continue
                    raise ChannelPushError(f"飞书消息发送失败：{code} {message}", chunks_sent=sent)
                sent += 1
                position += 1
                if position < len(payload.chunks):
                    await asyncio.sleep(1.0 / (self.outbound_rate_per_second or 5))
            else:
                return
        raise ChannelPushError("飞书消息发送失败：token 重取后仍无效", retryable=False, chunks_sent=sent)


def hmac_compare(expected: str, actual: str) -> bool:
    import hmac as _hmac

    return _hmac.compare_digest(expected.encode(), actual.encode())
