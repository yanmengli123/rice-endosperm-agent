"""钉钉企业内部机器人适配器（HTTP 回调：明文 + 可选签名 / 可选加密报文）。

入站（明文模式）：POST JSON（senderStaffId/conversationId/text/sessionWebhook）；
配置了 app_secret 时校验 headers 的 sign + timestamp（HMAC-SHA256）。

入站（加密模式，可选启用）：凭据含 ``encoding_aes_key`` + ``token`` + ``corp_id``
时按官方《回调事件消息体加解密》协议处理——
``signature = sha1(sort(token, timestamp, nonce, encrypt))``（与微信系同构），
AES-256-CBC（IV = AESKey 前 16 字节，明文 = random(16B) + msg_len(4B) + msg + corpid），
封包 ``{"encrypt": ...}``，URL 参数 ``signature/timestamp(ms)/nonce``；
URL 校验 challenge 回加密封包 ``{msg_signature,timeStamp,nonce,encrypt}``。
**该模式 opt-in（默认明文），上线前须在钉钉后台做一次真实 URL 校验确认。**

出站：sessionWebhook（随消息下发、时效有限约 2 小时）推送 markdown，超期即
``undeliverable`` 终态；Stream 模式（零公网）属后续增强。
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from typing import Any

from yuxi.channels.adapters.base import ChannelAdapter, ChannelPushError, get_http_client
from yuxi.channels.contracts import ChannelChallenge, ChannelEnvelope, InboundParseResult, OutboundPayload
from yuxi.channels.signing import (
    ChannelSignatureError,
    WeChatCrypto,
    constant_time_equals,
    dingtalk_robot_signature,
    wechat_signature,
)


class DingTalkAdapter(ChannelAdapter):
    channel_type = "dingtalk"
    label = "钉钉企业内部机器人"
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
        if method != "POST":
            return InboundParseResult(ack_body='{"code":0,"success":true}')

        if str(credentials.get("encoding_aes_key") or ""):
            # 加密模式（opt-in）：验签 + 解密后与明文路径共用事件处理
            event, challenge_ack = self._decode_encrypted(
                query=query, headers=headers, body=body, credentials=credentials
            )
            if challenge_ack is not None:
                return InboundParseResult(ack_body=challenge_ack, ack_media_type="application/json")
            return self._result_from_event(event, platform_app_id)

        app_secret = str(credentials.get("app_secret") or "")
        if app_secret:
            timestamp = str(headers.get("timestamp") or headers.get("Timestamp") or "")
            sign = str(headers.get("sign") or headers.get("Sign") or "")
            if (
                not timestamp
                or not sign
                or not constant_time_equals(dingtalk_robot_signature(app_secret, timestamp), sign)
            ):
                raise ChannelSignatureError("钉钉回调签名失败")

        try:
            event = json.loads(body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise ChannelSignatureError("钉钉报文 JSON 非法") from exc

        return self._result_from_event(event, platform_app_id)

    def _decode_encrypted(
        self, *, query: dict[str, str], headers: dict[str, str], body: bytes, credentials: dict[str, Any]
    ) -> tuple[dict[str, Any], str | None]:
        """钉钉加密回调解密：验签 → AES 解密 → challenge 回加密封包。

        返回 ``(event, challenge_ack)``：challenge 有值时调用方直接回包。
        """
        token = str(credentials.get("token") or "")
        corp_id = str(credentials.get("corp_id") or "")
        if not token or not corp_id:
            raise ChannelSignatureError("钉钉加密回调缺少 token/corp_id 凭据")

        timestamp = str(query.get("timestamp") or headers.get("timestamp") or "")
        nonce = str(query.get("nonce") or headers.get("nonce") or "")
        signature = str(query.get("signature") or headers.get("signature") or "")
        try:
            outer = json.loads(body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise ChannelSignatureError("钉钉加密报文 JSON 非法") from exc
        encrypt = str(outer.get("encrypt") or "")
        if not encrypt:
            raise ChannelSignatureError("钉钉加密报文缺少 encrypt")
        # 签名用平台原始 timestamp 字符串（钉钉为毫秒），不做归一化
        if (
            not timestamp
            or not nonce
            or not constant_time_equals(wechat_signature(token, timestamp, nonce, encrypt), signature)
        ):
            raise ChannelSignatureError("钉钉加密回调签名失败")

        crypto = WeChatCrypto(str(credentials.get("encoding_aes_key") or ""), corp_id)
        try:
            event = json.loads(crypto.decrypt(encrypt))
        except (ValueError, json.JSONDecodeError) as exc:
            raise ChannelSignatureError("钉钉加密回调解密失败") from exc

        if str(event.get("type") or "") == "url_verification" or "challenge" in event:
            return {}, self._encrypted_challenge(crypto, token, str(event.get("challenge") or ""))
        return event, None

    @staticmethod
    def _encrypted_challenge(crypto: WeChatCrypto, token: str, challenge: str) -> str:
        """URL 校验 challenge 的加密回包（DingTalkEncryptor.getEncryptedMap 同构）。"""
        timestamp = str(int(time.time()))
        nonce = secrets.token_hex(8)
        encrypt = crypto.encrypt(json.dumps({"challenge": challenge}, ensure_ascii=False))
        signature = wechat_signature(token, timestamp, nonce, encrypt)
        return json.dumps(
            {"msg_signature": signature, "timeStamp": timestamp, "nonce": nonce, "encrypt": encrypt},
            ensure_ascii=False,
        )

    def _result_from_event(self, event: dict[str, Any], platform_app_id: str) -> InboundParseResult:
        if str(event.get("type") or "") == "url_verification" or "challenge" in event:
            return InboundParseResult(
                challenge=ChannelChallenge(body=json.dumps({"challenge": event.get("challenge", "")})),
                ack_body='{"code":0,"success":true}',
            )

        if event.get("msgtype") != "text":
            return InboundParseResult(ack_body='{"code":0,"success":true}')

        conversation_type = str(event.get("conversationType") or "1")
        chat_type = "group" if conversation_type != "1" else "p2p"
        text = str((event.get("text") or {}).get("content") or "").strip()
        sender_id = str(event.get("senderStaffId") or event.get("senderId") or "")

        envelope = ChannelEnvelope(
            channel_type=self.channel_type,
            platform_app_id=platform_app_id,
            platform_message_id=str(event.get("msgId") or event.get("createAt") or "") or None,
            chat_id=str(event.get("conversationId") or sender_id),
            chat_type=chat_type,
            user_id=sender_id or None,
            user_display=str(event.get("senderNick") or "") or None,
            text=text,
            mentioned_me=bool(event.get("isInAtList")) or chat_type == "p2p",
            raw={"session_webhook": str(event.get("sessionWebhook") or "")},
        )
        return InboundParseResult(envelopes=[envelope], ack_body='{"code":0,"success":true}')

    def render_outbound(self, markdown_text: str, *, web_url: str | None = None) -> OutboundPayload:
        payload = super().render_outbound(markdown_text, web_url=web_url)
        payload.kind = "markdown"
        return payload

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
        """钉钉出站：经入站消息携带的 sessionWebhook 推送 markdown。

        sessionWebhook 随消息下发、时效有限（约 2 小时），过期即不可重试。
        """
        webhook = str(payload.extra.get("session_webhook") or "")
        if not webhook:
            raise ChannelPushError("钉钉推送缺少 sessionWebhook", retryable=False)
        sent = 0
        for index, chunk in enumerate(payload.chunks[start_chunk:], start=start_chunk):
            response = await get_http_client().post(
                webhook,
                json={"msgtype": "markdown", "markdown": {"title": "Yuxi 智能体回复", "text": chunk}},
            )
            if response.status_code != 200:
                raise ChannelPushError(
                    f"钉钉 sessionWebhook 推送失败：HTTP {response.status_code}", retryable=False, chunks_sent=sent
                )
            try:
                data = response.json()
            except ValueError:
                sent += 1
                continue
            if data.get("errcode") not in (0, None):
                # 进度只在平台确认后前进（chunks_sent 是续传游标，必须与真实送达一致）
                raise ChannelPushError(
                    f"钉钉 sessionWebhook 推送失败：{data.get('errcode')} {data.get('errmsg')}",
                    retryable=False,
                    chunks_sent=sent,
                )
            sent += 1
            if index < len(payload.chunks) - 1:
                await asyncio.sleep(1.0 / (self.outbound_rate_per_second or 2))
