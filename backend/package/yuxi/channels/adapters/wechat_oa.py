"""微信公众号适配器（webhook 模式）。

入站：GET echostr 校验 + POST 加密 XML（与企微同密码学体系，receiveid = appid）。
被动回复 5s 时限以「回 success 空包」化解，结果走客服消息接口（48h 互动窗口）。
带参二维码（subscribe/SCAN 事件）是渠道身份绑定的扫码入口。
"""

from __future__ import annotations

import asyncio
from typing import Any

from yuxi.channels.adapters.base import ChannelAdapter, ChannelPushError, get_http_client, token_cache
from yuxi.channels.adapters.wecom import wechat_signature_ok
from yuxi.channels.contracts import ChannelChallenge, ChannelEnvelope, InboundParseResult, OutboundPayload
from yuxi.channels.signing import ChannelSignatureError, WeChatCrypto, parse_wechat_xml

WECHAT_BASE = "https://api.weixin.qq.com"


class WeChatOfficialAccountAdapter(ChannelAdapter):
    channel_type = "wechat_oa"
    label = "微信公众号"
    supports_pairing_qr = True
    outbound_rate_per_second = 1
    token_expired_codes = (40001, 42001)

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
        token = str(credentials.get("token") or "")
        crypto = WeChatCrypto(str(credentials.get("encoding_aes_key") or ""), platform_app_id)

        if method == "GET":
            echostr = query.get("echostr", "")
            if not wechat_signature_ok(
                token, query.get("timestamp", ""), query.get("nonce", ""), echostr, query.get("msg_signature", "")
            ):
                raise ChannelSignatureError("公众号 URL 校验签名失败")
            return InboundParseResult(
                challenge=ChannelChallenge(body=crypto.decrypt(echostr), media_type="text/plain"),
                ack_body="",
            )

        outer = parse_wechat_xml(body.decode("utf-8", errors="replace"))
        encrypt = outer.get("Encrypt", "")
        if not wechat_signature_ok(
            token, query.get("timestamp", ""), query.get("nonce", ""), encrypt, query.get("msg_signature", "")
        ):
            raise ChannelSignatureError("公众号回调签名失败")
        inner = parse_wechat_xml(crypto.decrypt(encrypt))
        from_user = inner.get("FromUserName", "")
        msg_type = inner.get("MsgType", "")

        if msg_type == "event":
            event = inner.get("Event", "")
            event_key = inner.get("EventKey", "")
            envelope = ChannelEnvelope(
                channel_type=self.channel_type,
                platform_app_id=platform_app_id,
                # subscribe/SCAN 无 MsgId，用 FromUserName+CreateTime 合成幂等键
                platform_message_id=f"evt:{from_user}:{inner.get('CreateTime', '')}:{event}:{event_key}",
                chat_id=from_user,
                chat_type="p2p",
                user_id=from_user or None,
                text="",
                is_event=True,
                event_name=event,
                event_key=event_key.removeprefix("qrscene_") or None,
                raw={},
            )
            return InboundParseResult(envelopes=[envelope], ack_body="success", ack_media_type="text/plain")

        if msg_type != "text":
            # 非文本（图片/语音等）：服务层回「暂仅支持文本」提示
            envelope = ChannelEnvelope(
                channel_type=self.channel_type,
                platform_app_id=platform_app_id,
                platform_message_id=inner.get("MsgId") or None,
                chat_id=from_user,
                chat_type="p2p",
                user_id=from_user or None,
                text="",
                mentioned_me=True,
                raw={"unsupported_media": True},
            )
            return InboundParseResult(envelopes=[envelope], ack_body="success", ack_media_type="text/plain")

        envelope = ChannelEnvelope(
            channel_type=self.channel_type,
            platform_app_id=platform_app_id,
            platform_message_id=inner.get("MsgId") or None,
            chat_id=from_user,
            chat_type="p2p",
            user_id=from_user or None,
            text=inner.get("Content", "").strip(),
            mentioned_me=True,
            raw={},
        )
        return InboundParseResult(envelopes=[envelope], ack_body="success", ack_media_type="text/plain")

    async def _get_access_token(self, credentials: dict[str, Any], platform_app_id: str, *, force: bool = False) -> str:
        self._validate_credentials(credentials, ("app_id", "app_secret"))
        if not force:
            cached = await token_cache.get(self.channel_type, platform_app_id)
            if cached:
                return cached
        response = await get_http_client().post(
            f"{WECHAT_BASE}/cgi-bin/stable_token",
            json={
                "grant_type": "client_credential",
                "appid": credentials["app_id"],
                "secret": credentials["app_secret"],
                "force_refresh": force,
            },
        )
        data = response.json()
        if not data.get("access_token"):
            raise ChannelPushError(
                f"公众号 access_token 获取失败：{data.get('errcode')} {data.get('errmsg')}", retryable=False
            )
        token = str(data["access_token"])
        await token_cache.set(self.channel_type, platform_app_id, token, int(data.get("expires_in") or 7200))
        return token

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
        target = user_id or chat_id
        sent = 0
        position = start_chunk  # F5：跨 token 重取保持进度，刷新后不重发已成功片
        for attempt in range(2):
            token = await self._get_access_token(credentials, platform_app_id, force=attempt > 0)
            while position < len(payload.chunks):
                chunk = payload.chunks[position]
                response = await get_http_client().post(
                    f"{WECHAT_BASE}/cgi-bin/message/custom/send",
                    params={"access_token": token},
                    json={"touser": target, "msgtype": "text", "text": {"content": chunk}},
                )
                data = response.json()
                errcode = int(data.get("errcode") or 0)
                if errcode != 0:
                    if errcode in self.token_expired_codes and attempt == 0:
                        await token_cache.clear(self.channel_type, platform_app_id)
                        break  # 换新 token 从 position 续发，不回卷
                    # 45009/45002 等窗口/频控错误不可盲目重试
                    raise ChannelPushError(
                        f"公众号客服消息发送失败：{errcode} {data.get('errmsg')}",
                        retryable=errcode not in (45002, 45009, 48004, 48002),
                        chunks_sent=sent,
                    )
                sent += 1
                position += 1
                if position < len(payload.chunks):
                    await asyncio.sleep(1.0 / (self.outbound_rate_per_second or 1))
            else:
                return
        raise ChannelPushError("公众号消息发送失败：token 重取后仍无效", retryable=False, chunks_sent=sent)

    async def create_pairing_qr(self, *, credentials: dict[str, Any], code: str) -> str | None:
        """创建带参临时二维码（scene_str = 绑定码，30 天有效取上限内 10 分钟由绑定码控制）。"""
        token = await self._get_access_token(credentials, str(credentials.get("app_id") or ""))
        response = await get_http_client().post(
            f"{WECHAT_BASE}/cgi-bin/qrcode/create",
            params={"access_token": token},
            json={"expire_seconds": 1800, "action_name": "QR_STR_SCENE", "action_info": {"scene": {"scene_str": code}}},
        )
        data = response.json()
        ticket = data.get("ticket")
        if not ticket:
            raise ChannelPushError(
                f"公众号带参二维码创建失败：{data.get('errcode')} {data.get('errmsg')}", retryable=False
            )
        return f"https://mp.weixin.qq.com/cgi-bin/showqrcode?ticket={ticket}"
