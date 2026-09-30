"""P0 加固单测：重放窗口、企微事件幂等键、PII 摘要口径、链接门控、分片续传、@ 判定。"""

from __future__ import annotations

import base64
import os
import time
from types import SimpleNamespace

import pytest
from yuxi.channels.adapters.base import ChannelPushError
from yuxi.channels.adapters.feishu import FeishuAdapter
from yuxi.channels.adapters.wecom import WeComAdapter
from yuxi.channels.contracts import OutboundPayload
from yuxi.channels.signing import WeChatCrypto
from yuxi.services.channel_service import (
    _build_content_digest,
    _check_replay_window,
    _nonce_first_seen,
    _should_include_web_url,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def _aes_key_43() -> str:
    return base64.b64encode(os.urandom(32)).decode().rstrip("=")


class TestReplayWindow:
    async def test_fresh_wecom_callback_passes(self) -> None:
        signal = _check_replay_window("wecom", {"timestamp": str(int(time.time())), "nonce": "n1"}, {})
        assert signal.stale is False and signal.nonce == "n1"

    async def test_stale_callback_rejected(self) -> None:
        signal = _check_replay_window("wecom", {"timestamp": str(int(time.time()) - 400), "nonce": "n1"}, {})
        assert signal.stale is True

    async def test_dingtalk_millisecond_timestamp_normalized(self) -> None:
        # 钉钉 sign 为确定性 HMAC 不作 nonce（F1）：仅时间窗，毫秒归一秒
        signal = _check_replay_window("dingtalk", {}, {"timestamp": str(int(time.time() * 1000)), "sign": "s"})
        assert signal.stale is False and signal.nonce is None

    async def test_missing_timestamp_fail_open(self) -> None:
        signal = _check_replay_window("wecom", {}, {})
        assert signal.stale is False and signal.timestamp is None

    async def test_telegram_skips_window(self) -> None:
        signal = _check_replay_window("telegram", {}, {})
        assert signal == (None, None, False)

    async def test_nonce_first_seen_then_duplicate(self, monkeypatch) -> None:
        class _FakeRedis:
            def __init__(self) -> None:
                self.store: dict[str, str] = {}

            async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False):
                if nx and key in self.store:
                    return None
                self.store[key] = value
                return True

        fake = _FakeRedis()

        async def _fake_client():
            return fake

        monkeypatch.setattr("yuxi.storage.redis.manager.get_async_redis_client", _fake_client)
        assert await _nonce_first_seen(1, "n1") is True
        assert await _nonce_first_seen(1, "n1") is False  # 重放/重推被吞
        assert await _nonce_first_seen(1, "n2") is True

    async def test_nonce_cache_unavailable_fail_open(self, monkeypatch) -> None:
        async def _broken_client():
            raise RuntimeError("redis down")

        monkeypatch.setattr("yuxi.storage.redis.manager.get_async_redis_client", _broken_client)
        assert await _nonce_first_seen(1, "n1") is True


class TestWeComEventIdempotency:
    async def _parse_event(self) -> str | None:
        token, key = "tk", _aes_key_43()
        crypto = WeChatCrypto(key, "corp1")
        xml = (
            "<xml><ToUserName><![CDATA[corp1]]></ToUserName>"
            "<FromUserName><![CDATA[zhangsan]]></FromUserName>"
            "<MsgType><![CDATA[event]]></MsgType><Event><![CDATA[subscribe]]></Event>"
            "<EventKey><![CDATA[qrscene_abc]]></EventKey><CreateTime>1712345678</CreateTime></xml>"
        )
        encrypted = crypto.encrypt(xml)
        from yuxi.channels.signing import wechat_signature

        signature = wechat_signature(token, "13945316929", "n", encrypted)
        adapter = WeComAdapter()
        result = await adapter.parse_inbound(
            method="POST",
            query={"msg_signature": signature, "timestamp": "13945316929", "nonce": "n"},
            headers={},
            body=f"<xml><Encrypt><![CDATA[{encrypted}]]></Encrypt></xml>".encode(),
            credentials={"token": token, "encoding_aes_key": key},
            platform_app_id="corp1",
        )
        assert result.envelopes
        return result.envelopes[0].platform_message_id

    async def test_event_has_synthetic_idempotency_key(self) -> None:
        first = await self._parse_event()
        second = await self._parse_event()
        assert first is not None and first.startswith("evt:subscribe:1712345678:zhangsan")
        assert first == second  # 平台重推 3 次：同键，消息表唯一索引兜住


class TestDigestAndLinkPolicy:
    async def test_inbound_digest_defaults_to_hash(self) -> None:
        app = SimpleNamespace(config={})
        digest = _build_content_digest(app, "用户隐私消息内容")
        assert digest is not None and digest.startswith("sha256:") and len(digest) == len("sha256:") + 16

    async def test_inbound_digest_preview_opt_in(self) -> None:
        app = SimpleNamespace(config={"store_text_preview": True})
        assert _build_content_digest(app, "前 80 字预览") == "前 80 字预览"

    async def test_empty_text_digest_none(self) -> None:
        assert _build_content_digest(SimpleNamespace(config={}), "  ") is None

    async def test_web_url_only_for_bound_user(self) -> None:
        app = SimpleNamespace(service_uid="u_service")
        assert _should_include_web_url("u_alice", app) is True
        assert _should_include_web_url("u_service", app) is False  # 服务账号会话不推裸链接
        assert _should_include_web_url("", app) is False


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """按预置结果序列出栈的假 httpx 客户端。"""

    def __init__(self, results: list[dict]) -> None:
        self.results = list(results)
        self.sent: list[dict] = []

    async def post(self, url: str, **kwargs) -> _FakeResponse:
        self.sent.append(kwargs)
        return _FakeResponse(self.results.pop(0))


class TestChunkResume:
    async def test_start_chunk_resumes_and_reports_progress(self, monkeypatch) -> None:
        adapter = WeComAdapter()
        # 3 片，第 1 片此前已送达（delivered_chunks=1）：本次从第 2 片续传，
        # 第 3 片失败 → chunks_sent=1（本次只发了第 2 片）
        client = _FakeClient([{"errcode": 0}, {"errcode": 1, "errmsg": "boom"}])
        monkeypatch.setattr("yuxi.channels.adapters.wecom.get_http_client", lambda: client)

        async def _cached_token(*args, **kwargs):
            return "tok"

        monkeypatch.setattr("yuxi.channels.adapters.wecom.token_cache.get", _cached_token)
        payload = OutboundPayload(kind="markdown", chunks=["c1", "c2", "c3"])
        with pytest.raises(ChannelPushError) as exc_info:
            await adapter.push(
                credentials={"corp_id": "c", "corp_secret": "s", "agent_id": "1"},
                platform_app_id="corp1",
                chat_id="zhangsan",
                user_id=None,
                payload=payload,
                start_chunk=1,
            )
        assert exc_info.value.chunks_sent == 1
        assert len(client.sent) == 2  # 只发了 c2、c3，c1 未重复推送

    async def test_full_resume_completes(self, monkeypatch) -> None:
        adapter = WeComAdapter()
        client = _FakeClient([{"errcode": 0}, {"errcode": 0}])
        monkeypatch.setattr("yuxi.channels.adapters.wecom.get_http_client", lambda: client)

        async def _cached_token(*args, **kwargs):
            return "tok"

        monkeypatch.setattr("yuxi.channels.adapters.wecom.token_cache.get", _cached_token)
        payload = OutboundPayload(kind="markdown", chunks=["c1", "c2", "c3"])
        await adapter.push(
            credentials={"corp_id": "c", "corp_secret": "s", "agent_id": "1"},
            platform_app_id="corp1",
            chat_id="zhangsan",
            user_id=None,
            payload=payload,
            start_chunk=1,
        )
        sent_contents = [call["json"]["markdown"]["content"] for call in client.sent]
        assert sent_contents == ["c2", "c3"]


class TestFeishuMentionByBotOpenId:
    async def _parse_group_event(self, mentions: list[dict], monkeypatch) -> bool:
        async def _bot_open_id(self, credentials, platform_app_id):
            return "ou_bot"

        monkeypatch.setattr(FeishuAdapter, "_get_bot_open_id", _bot_open_id)
        event = {
            "schema": "2.0",
            "header": {"event_id": "ev9", "event_type": "im.message.receive_v1"},
            "event": {
                "message": {
                    "message_id": "om_11",
                    "chat_id": "oc_g",
                    "chat_type": "group",
                    "message_type": "text",
                    "content": '{"text":"@_user_1 查一下"}',
                },
                "sender": {"sender_id": {"open_id": "ou_2"}},
                "mentions": mentions,
            },
        }
        result = await FeishuAdapter().parse_inbound(
            method="POST",
            query={},
            headers={},
            body=__import__("json").dumps(event).encode(),
            credentials={},
            platform_app_id="cli_x",
        )
        return result.envelopes[0].mentioned_me

    async def test_mention_bot_detected_by_open_id(self, monkeypatch) -> None:
        mentions = [{"key": "@_user_1", "id": {"open_id": "ou_bot"}, "name": "bot"}]
        assert await self._parse_group_event(mentions, monkeypatch) is True

    async def test_mention_others_not_detected(self, monkeypatch) -> None:
        mentions = [{"key": "@_user_1", "id": {"open_id": "ou_human"}, "name": "同事"}]
        assert await self._parse_group_event(mentions, monkeypatch) is False
