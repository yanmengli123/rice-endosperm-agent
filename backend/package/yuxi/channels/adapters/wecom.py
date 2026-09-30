"""企业微信自建应用适配器（webhook 模式）。

入站：GET 首次 URL 校验（echostr 解密回显）+ POST 加密 XML（msg_signature
四元组 sha1 + AES-256-CBC，receiveid = corp_id）。超时 5s 重试 3 次的平台行为
由「立即空串回包 + 异步推送」化解；MsgId 幂等由消息表唯一索引兜底。
出站：access_token（corpid+corpsecret）+ message/send 应用消息。
"""

from __future__ import annotations

import asyncio
from typing import Any

from yuxi.channels.adapters.base import ChannelAdapter, ChannelPushError, get_http_client, token_cache
from yuxi.channels.contracts import ChannelChallenge, ChannelEnvelope, InboundParseResult, OutboundPayload
from yuxi.channels.signing import (
    ChannelSignatureError,
    WeChatCrypto,
    parse_wechat_xml,
    wechat_signature,
)

WECOM_BASE = "https://qyapi.weixin.qq.com"


class WeComAdapter(ChannelAdapter):
    channel_type = "wecom"
    label = "企业微信自建应用"
    token_expired_codes = (40014, 42001)
    outbound_rate_per_second = 2

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
        aes_key = str(credentials.get("encoding_aes_key") or "")
        crypto = WeChatCrypto(aes_key, platform_app_id)

        if method == "GET":
            echostr = query.get("echostr", "")
            signature = query.get("msg_signature", "")
            if not wechat_signature_ok(token, query.get("timestamp", ""), query.get("nonce", ""), echostr, signature):
                raise ChannelSignatureError("企业微信 URL 校验签名失败")
            return InboundParseResult(
                challenge=ChannelChallenge(body=crypto.decrypt(echostr), media_type="text/plain"),
                ack_body="",
            )

        body_xml = body.decode("utf-8", errors="replace")
        outer = parse_wechat_xml(body_xml)
        encrypt = outer.get("Encrypt", "")
        signature = query.get("msg_signature", "")
        if not wechat_signature_ok(token, query.get("timestamp", ""), query.get("nonce", ""), encrypt, signature):
            raise ChannelSignatureError("企业微信回调签名失败")
        inner = parse_wechat_xml(crypto.decrypt(encrypt))

        msg_type = inner.get("MsgType", "")
        if msg_type == "event":
            envelope = ChannelEnvelope(
                channel_type=self.channel_type,
                platform_app_id=platform_app_id,
                # 事件无 MsgId：合成幂等键（平台重推 3 次靠它去重，与公众号同法）
                platform_message_id=(f"evt:{inner.get('Event')}:{inner.get('CreateTime')}:{inner.get('FromUserName')}"),
                chat_id=inner.get("FromUserName", ""),
                chat_type="p2p",
                user_id=inner.get("FromUserName") or None,
                text="",
                is_event=True,
                event_name=inner.get("Event"),
                event_key=inner.get("EventKey"),
                raw={"agent_id": inner.get("AgentID")},
            )
            return InboundParseResult(envelopes=[envelope], ack_body="", ack_media_type="text/plain")

        chat_type = "group" if inner.get("ChatType") == "group" else "p2p"
        text = inner.get("Content", "") if msg_type == "text" else ""
        mentioned = chat_type == "group" and "@全部" not in text and bool(text.strip())
        raw = {"agent_id": inner.get("AgentID")}
        if msg_type != "text":
            raw["unsupported_media"] = True  # 服务层回「暂仅支持文本」提示
        envelope = ChannelEnvelope(
            channel_type=self.channel_type,
            platform_app_id=platform_app_id,
            platform_message_id=inner.get("MsgId") or None,
            chat_id=inner.get("FromUserName", ""),
            chat_type=chat_type,
            user_id=inner.get("FromUserName") or None,
            text=text.strip(),
            mentioned_me=mentioned,
            raw=raw,
        )
        return InboundParseResult(envelopes=[envelope], ack_body="", ack_media_type="text/plain")

    def render_outbound(self, markdown_text: str, *, web_url: str | None = None) -> OutboundPayload:
        payload = super().render_outbound(markdown_text, web_url=web_url)
        payload.kind = "markdown"
        return payload

    async def _get_access_token(self, credentials: dict[str, Any], platform_app_id: str, *, force: bool = False) -> str:
        self._validate_credentials(credentials, ("corp_id", "corp_secret"))
        if not force:
            cached = await token_cache.get(self.channel_type, platform_app_id)
            if cached:
                return cached
        response = await get_http_client().get(
            f"{WECOM_BASE}/cgi-bin/gettoken",
            params={"corpid": credentials["corp_id"], "corpsecret": credentials["corp_secret"]},
        )
        data = response.json()
        if data.get("errcode") != 0:
            raise ChannelPushError(
                f"企业微信 access_token 获取失败：{data.get('errcode')} {data.get('errmsg')}", retryable=False
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
        self._validate_credentials(credentials, ("corp_id", "corp_secret"))
        agent_id = int(credentials.get("agent_id") or 0)
        sent = 0
        position = start_chunk  # F5：跨 token 重取保持进度，刷新后不重发已成功片
        for attempt in range(2):
            token = await self._get_access_token(credentials, platform_app_id, force=attempt > 0)
            while position < len(payload.chunks):
                chunk = payload.chunks[position]
                response = await get_http_client().post(
                    f"{WECOM_BASE}/cgi-bin/message/send",
                    params={"access_token": token},
                    json={
                        "touser": user_id or chat_id,
                        "msgtype": "markdown",
                        "agentid": agent_id,
                        "markdown": {"content": chunk},
                    },
                )
                data = response.json()
                errcode = int(data.get("errcode") or 0)
                if errcode != 0:
                    if errcode in self.token_expired_codes and attempt == 0:
                        await token_cache.clear(self.channel_type, platform_app_id)
                        break  # 换新 token 从 position 续发，不回卷
                    # 45009 等频控错误不可盲目重试
                    raise ChannelPushError(
                        f"企业微信消息发送失败：{errcode} {data.get('errmsg')}",
                        retryable=errcode not in (45009, 45011),
                        chunks_sent=sent,
                    )
                sent += 1
                position += 1
                if position < len(payload.chunks):
                    await asyncio.sleep(1.0 / (self.outbound_rate_per_second or 2))
            else:
                return  # 全部送达
        raise ChannelPushError("企业微信消息发送失败：token 重取后仍无效", retryable=False, chunks_sent=sent)


def wechat_signature_ok(token: str, timestamp: str, nonce: str, encrypt: str, signature: str) -> bool:
    from yuxi.channels.signing import constant_time_equals

    return bool(signature) and constant_time_equals(wechat_signature(token, timestamp, nonce, encrypt), signature)
