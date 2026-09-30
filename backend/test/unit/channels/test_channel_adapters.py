"""适配器入站解析契约：加密回调全路径（加密→验签→解密→归一化信封）。"""

from __future__ import annotations

import base64
import hashlib
import json
import os

import pytest
from yuxi.channels.adapters.feishu import FeishuAdapter
from yuxi.channels.adapters.telegram import TelegramAdapter
from yuxi.channels.adapters.wecom import WeComAdapter
from yuxi.channels.signing import ChannelSignatureError, WeChatCrypto

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def _aes_key_43() -> str:
    return base64.b64encode(os.urandom(32)).decode().rstrip("=")


def _feishu_encrypt(encrypt_key: str, plain: str) -> str:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = hashlib.sha256(encrypt_key.encode()).digest()
    iv = os.urandom(16)
    pad = 16 - (len(plain.encode()) % 16)
    padded = plain.encode() + bytes([pad]) * pad
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return base64.b64encode(iv + encryptor.update(padded) + encryptor.finalize()).decode()


class TestFeishuAdapter:
    async def test_url_verification_challenge(self) -> None:
        adapter = FeishuAdapter()
        body = json.dumps(
            {"encrypt": _feishu_encrypt("k1", json.dumps({"type": "url_verification", "challenge": "abc123"}))}
        )
        timestamp, nonce = "1609425990", "n1"
        signature = hashlib.sha256(f"{timestamp}{nonce}k1{body}".encode()).hexdigest()
        result = await adapter.parse_inbound(
            method="POST",
            query={},
            headers={
                "x-lark-signature": signature,
                "x-lark-request-timestamp": timestamp,
                "x-lark-request-nonce": nonce,
            },
            body=body.encode(),
            credentials={"app_id": "cli_x", "app_secret": "s", "encrypt_key": "k1"},
            platform_app_id="cli_x",
        )
        assert result.challenge is not None
        assert json.loads(result.challenge.body)["challenge"] == "abc123"

    async def test_bad_signature_rejected(self) -> None:
        adapter = FeishuAdapter()
        body = json.dumps({"encrypt": _feishu_encrypt("k1", "{}")})
        with pytest.raises(ChannelSignatureError):
            await adapter.parse_inbound(
                method="POST",
                query={},
                headers={"x-lark-signature": "bad", "x-lark-request-timestamp": "1", "x-lark-request-nonce": "n"},
                body=body.encode(),
                credentials={"encrypt_key": "k1"},
                platform_app_id="cli_x",
            )

    async def test_message_event_normalized(self) -> None:
        adapter = FeishuAdapter()
        event = {
            "schema": "2.0",
            "header": {"event_id": "ev1", "event_type": "im.message.receive_v1", "token": "vt"},
            "event": {
                "message": {
                    "message_id": "om_9",
                    "chat_id": "oc_1",
                    "chat_type": "p2p",
                    "message_type": "text",
                    "content": json.dumps({"text": "MSU-RAP 定位"}),
                },
                "sender": {"sender_id": {"open_id": "ou_1"}},
            },
        }
        body = json.dumps({"encrypt": _feishu_encrypt("k1", json.dumps(event))})
        timestamp, nonce = "1609425990", "n1"
        signature = hashlib.sha256(f"{timestamp}{nonce}k1{body}".encode()).hexdigest()
        result = await adapter.parse_inbound(
            method="POST",
            query={},
            headers={
                "x-lark-signature": signature,
                "x-lark-request-timestamp": timestamp,
                "x-lark-request-nonce": nonce,
            },
            body=body.encode(),
            credentials={"encrypt_key": "k1", "verification_token": "vt"},
            platform_app_id="cli_x",
        )
        assert len(result.envelopes) == 1
        envelope = result.envelopes[0]
        assert envelope.platform_message_id == "om_9"
        assert envelope.chat_id == "oc_1" and envelope.chat_type == "p2p"
        assert envelope.text == "MSU-RAP 定位" and envelope.user_id == "ou_1"

    async def test_group_mention_detection(self) -> None:
        adapter = FeishuAdapter()
        event = {
            "schema": "2.0",
            "header": {"event_id": "ev2", "event_type": "im.message.receive_v1"},
            "event": {
                "message": {
                    "message_id": "om_10",
                    "chat_id": "oc_g",
                    "chat_type": "group",
                    "message_type": "text",
                    "content": json.dumps({"text": "@_user_1 帮我查 RAP 位点"}),
                },
                "sender": {"sender_id": {"open_id": "ou_2"}},
                "mentions": [{"key": "@_user_1", "name": "bot"}],
            },
        }
        result = await adapter.parse_inbound(
            method="POST",
            query={},
            headers={},
            body=json.dumps(event).encode(),
            credentials={},
            platform_app_id="cli_x",
        )
        envelope = result.envelopes[0]
        assert envelope.mentioned_me is True
        assert envelope.text == "帮我查 RAP 位点"  # @ 占位段被剥离


class TestWeComAdapter:
    async def test_url_verification(self) -> None:
        token, key = "tk", _aes_key_43()
        crypto = WeChatCrypto(key, "corp1")
        echo_plain = crypto.decrypt(crypto.encrypt("echo-plain-text"))
        encrypted_echo = crypto.encrypt("echo-plain-text")
        signature = __import__("yuxi.channels.signing", fromlist=["wechat_signature"]).wechat_signature(
            token, "13945316929", "n", encrypted_echo
        )
        adapter = WeComAdapter()
        result = await adapter.parse_inbound(
            method="GET",
            query={"msg_signature": signature, "timestamp": "13945316929", "nonce": "n", "echostr": encrypted_echo},
            headers={},
            body=b"",
            credentials={"token": token, "encoding_aes_key": key},
            platform_app_id="corp1",
        )
        assert result.challenge is not None
        assert result.challenge.body == "echo-plain-text"
        assert echo_plain == "echo-plain-text"

    async def test_text_message_normalized(self) -> None:
        token, key = "tk", _aes_key_43()
        crypto = WeChatCrypto(key, "corp1")
        xml = (
            "<xml><ToUserName><![CDATA[corp1]]></ToUserName>"
            "<FromUserName><![CDATA[zhangsan]]></FromUserName>"
            "<MsgType><![CDATA[text]]></MsgType><Content><![CDATA[胚乳发育]]></Content>"
            "<MsgId>12345</MsgId><AgentID>1000002</AgentID></xml>"
        )
        encrypted = crypto.encrypt(xml)
        signature = __import__("yuxi.channels.signing", fromlist=["wechat_signature"]).wechat_signature(
            token, "13945316929", "n", encrypted
        )
        adapter = WeComAdapter()
        result = await adapter.parse_inbound(
            method="POST",
            query={"msg_signature": signature, "timestamp": "13945316929", "nonce": "n"},
            headers={},
            body=f"<xml><Encrypt><![CDATA[{encrypted}]]></Encrypt></xml>".encode(),
            credentials={"token": token, "encoding_aes_key": key},
            platform_app_id="corp1",
        )
        envelope = result.envelopes[0]
        assert envelope.platform_message_id == "12345"
        assert envelope.chat_id == "zhangsan" and envelope.text == "胚乳发育"
        assert result.ack_body == ""  # 空串回包化解 5s 被动回复时限


class TestTelegramAdapter:
    async def test_private_message_normalized(self) -> None:
        adapter = TelegramAdapter()
        update = {
            "update_id": 1001,
            "message": {
                "message_id": 5,
                "chat": {"id": 42, "type": "private"},
                "from": {"id": 7, "first_name": "Yu"},
                "text": "/reset",
            },
        }
        result = await adapter.parse_inbound(
            method="POST",
            query={},
            headers={},
            body=json.dumps(update).encode(),
            credentials={},
            platform_app_id="rice_bot",
        )
        envelope = result.envelopes[0]
        assert envelope.platform_message_id == "1001" and envelope.chat_id == "42"
        assert envelope.chat_type == "p2p" and envelope.text == "/reset"

    async def test_webhook_secret_enforced(self) -> None:
        adapter = TelegramAdapter()
        with pytest.raises(ChannelSignatureError):
            await adapter.parse_inbound(
                method="POST",
                query={},
                headers={"x-telegram-bot-api-secret-token": "wrong"},
                body=json.dumps({"update_id": 1}).encode(),
                credentials={"webhook_secret": "right"},
                platform_app_id="rice_bot",
            )
