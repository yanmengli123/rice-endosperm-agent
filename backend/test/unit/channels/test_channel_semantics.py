"""编排层语义回归（P0 二轮）：回调分流/重放放行/游标前进条件/锁续期/token 续发。

这批测试锁的是「承诺的不变量」本身：平台重推必须能再次进入管线、ingest 失败
游标不得前进、锁 60s 后仍归同实例、token 刷新不重发已成功片——都在真实编排
路径上断言，而非纯函数。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from yuxi.channels.adapters.base import ChannelPushError
from yuxi.channels.adapters.wecom import WeComAdapter
from yuxi.channels.contracts import OutboundPayload
from yuxi.services import channel_service
from yuxi.storage.postgres.models_business import (
    ChannelApp,
    ChannelChat,
    ChannelEndUser,
    ChannelMessage,
    ChannelOutboundOutbox,
    ChannelPairing,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

PATH_TOKEN = "test-token-1234"


class _FakeNonceRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def get(self, key: str):
        return self.store.get(key)

    async def expire(self, key: str, ttl: int):
        return key in self.store


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(ChannelApp.__table__.create)
        await conn.run_sync(ChannelEndUser.__table__.create)
        await conn.run_sync(ChannelChat.__table__.create)
        await conn.run_sync(ChannelMessage.__table__.create)
        await conn.run_sync(ChannelOutboundOutbox.__table__.create)
        await conn.run_sync(ChannelPairing.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        db.add(
            ChannelApp(
                id=1,
                tenant_id=1,
                channel_type="feishu",
                name="t",
                platform_app_id="cli_t",
                credentials_ciphertext="",
                path_token_hash=hashlib.sha256(PATH_TOKEN.encode()).hexdigest(),
                service_uid="u_service",
                config={"bound_agent_slug": "bot"},
                is_enabled=True,
            )
        )
        await db.commit()
        yield db
    await engine.dispose()


def _feishu_event(message_id: str = "om_t1", text: str = "hello") -> bytes:
    event = {
        "schema": "2.0",
        "header": {"event_id": "ev_t1", "event_type": "im.message.receive_v1"},
        "event": {
            "message": {
                "message_id": message_id,
                "chat_id": "oc_t",
                "chat_type": "p2p",
                "message_type": "text",
                "content": json.dumps({"text": text}),
            },
            "sender": {"sender_id": {"open_id": "ou_t"}},
        },
    }
    return json.dumps(event).encode()


def _fresh_headers(nonce: str = "n1", offset_seconds: int = 0) -> dict[str, str]:
    return {
        "x-lark-request-timestamp": str(int(time.time()) + offset_seconds),
        "x-lark-request-nonce": nonce,
    }


@pytest_asyncio.fixture
async def nonce_redis(monkeypatch):
    fake = _FakeNonceRedis()

    async def _client():
        return fake

    monkeypatch.setattr("yuxi.storage.redis.manager.get_async_redis_client", _client)
    return fake


class TestCallbackOrchestration:
    """F1/F2/S6：回调编排层的分流与重放语义。"""

    async def test_ingest_failure_503_then_retry_reaches_pipeline(self, session, monkeypatch, nonce_redis) -> None:
        """首次 ingest 异常 → 503；同 nonce 重推必须仍能进入管线（F1 核心）。"""
        monkeypatch.setattr(channel_service, "_load_credentials", lambda app: {})
        calls = {"count": 0}

        async def _flaky_ingest(db, app, envelope):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("db hiccup before insert")
            return None

        monkeypatch.setattr(channel_service, "ingest_channel_envelope", _flaky_ingest)

        first = await channel_service.handle_channel_callback(
            db=session,
            channel_type="feishu",
            path_token=PATH_TOKEN,
            method="POST",
            query={},
            headers=_fresh_headers("n1"),
            body=_feishu_event(),
        )
        assert first[0] == 503  # F2：内部异常换平台重试，不是 200 吞掉

        second = await channel_service.handle_channel_callback(
            db=session,
            channel_type="feishu",
            path_token=PATH_TOKEN,
            method="POST",
            query={},
            headers=_fresh_headers("n1"),
            body=_feishu_event(),
        )
        assert second[0] == 200
        assert calls["count"] == 2  # nonce 已见 → 放行进管线（未短路）

    async def test_stale_first_seen_403_seen_passes(self, session, monkeypatch, nonce_redis) -> None:
        """陈旧回调：首见 403；同 nonce 再来（已见）放行。"""
        monkeypatch.setattr(channel_service, "_load_credentials", lambda app: {})
        ingested = {"count": 0}

        async def _ingest(db, app, envelope):
            ingested["count"] += 1

        monkeypatch.setattr(channel_service, "ingest_channel_envelope", _ingest)

        stale_headers = _fresh_headers("n2", offset_seconds=-400)
        first = await channel_service.handle_channel_callback(
            db=session,
            channel_type="feishu",
            path_token=PATH_TOKEN,
            method="POST",
            query={},
            headers=stale_headers,
            body=_feishu_event(),
        )
        assert first[0] == 403 and ingested["count"] == 0

        second = await channel_service.handle_channel_callback(
            db=session,
            channel_type="feishu",
            path_token=PATH_TOKEN,
            method="POST",
            query={},
            headers=stale_headers,
            body=_feishu_event(),
        )
        assert second[0] == 200 and ingested["count"] == 1

    async def test_signature_failure_does_not_consume_nonce(self, session, monkeypatch, nonce_redis) -> None:
        """S6：验签失败不写 nonce——只有合法请求才写状态。"""
        monkeypatch.setattr(channel_service, "_load_credentials", lambda app: {})
        status, _, _ = await channel_service.handle_channel_callback(
            db=session,
            channel_type="feishu",
            path_token=PATH_TOKEN,
            method="POST",
            query={},
            headers=_fresh_headers("n3"),
            body=b"not-json",
        )
        assert status == 403
        assert not [k for k in nonce_redis.store if "nonce" in k]

        # 同 nonce 的合法请求仍按首见处理，正常进管线
        ingested = {"count": 0}

        async def _ingest(db, app, envelope):
            ingested["count"] += 1

        monkeypatch.setattr(channel_service, "ingest_channel_envelope", _ingest)
        status, _, _ = await channel_service.handle_channel_callback(
            db=session,
            channel_type="feishu",
            path_token=PATH_TOKEN,
            method="POST",
            query={},
            headers=_fresh_headers("n3"),
            body=_feishu_event(),
        )
        assert status == 200 and ingested["count"] == 1

    async def test_credentials_internal_error_503(self, session, monkeypatch) -> None:
        """F2：凭据加载失败 → 5xx（换平台重试），不是 200 静默丢失。"""

        def _broken(app):
            raise RuntimeError("decrypt failed")

        monkeypatch.setattr(channel_service, "_load_credentials", _broken)
        status, _, _ = await channel_service.handle_channel_callback(
            db=session,
            channel_type="feishu",
            path_token=PATH_TOKEN,
            method="POST",
            query={},
            headers=_fresh_headers(),
            body=_feishu_event(),
        )
        assert status == 503


class TestBatchCursorAdvance:
    """F3：零 hard-failure 才前进游标。"""

    async def test_ingest_envelopes_counts_hard_failures(self, session, monkeypatch) -> None:
        class _Ctx:
            def __init__(self, db):
                self.db = db

            async def __aenter__(self):
                return self.db

            async def __aexit__(self, *args):
                return False

        monkeypatch.setattr(channel_service.pg_manager, "get_async_session_context", lambda: _Ctx(session))
        outcomes = iter([RuntimeError("boom"), None])

        async def _ingest(db, app, envelope):
            outcome = next(outcomes)
            if outcome is not None:
                raise outcome

        monkeypatch.setattr(channel_service, "ingest_channel_envelope", _ingest)
        envelopes = [SimpleNamespace(), SimpleNamespace()]
        assert await channel_service.ingest_channel_envelopes({"id": 1}, envelopes) == 1

    async def test_runner_advances_cursor_only_on_zero_failures(self, monkeypatch) -> None:
        from yuxi.channels import runtime

        persist_calls: list[int] = []

        class _FakeTGAdapter:
            def __init__(self):
                self.fetch_count = 0

            async def resolve_bot_username(self, credentials, platform_app_id):
                return None

            async def fetch_updates(self, **kwargs):
                self.fetch_count += 1
                if self.fetch_count > 1:
                    raise asyncio.CancelledError  # 结束 runner
                return [{"update_id": 5}]

            def parse_update(self, update, *, platform_app_id, bot_username=None):
                return SimpleNamespace(envelopes=[SimpleNamespace()])

        async def _failing_ingest(app_row, envelopes):
            return 2  # 本批有 hard-failure

        async def _ok_ingest(app_row, envelopes):
            return 0

        async def _persist(app_id, offset):
            persist_calls.append(offset)

        async def _lock(app_id):
            return True

        async def _creds(app_id):
            return {}

        async def _cursor(app_id):
            return 0

        for ingest_impl, expected in ((_failing_ingest, []), (_ok_ingest, [6])):
            persist_calls.clear()
            monkeypatch.setattr(runtime, "TelegramAdapter", _FakeTGAdapter)
            # runner 在函数体内 from channel_service import，patch 源模块
            monkeypatch.setattr(channel_service, "ingest_channel_envelopes", ingest_impl)
            monkeypatch.setattr(runtime, "_persist_cursor", _persist)
            monkeypatch.setattr(runtime, "_acquire_or_renew_lock", _lock)
            monkeypatch.setattr(channel_service, "load_channel_credentials", _creds)
            monkeypatch.setattr(runtime, "_read_cursor", _cursor)
            # N2：失败批会退避（10s 起，指数升级）——本测试断言游标语义，时间归零
            monkeypatch.setattr(runtime, "FAILED_BATCH_BACKOFF_SECONDS", 0)
            monkeypatch.setattr(runtime, "FAILED_BATCH_BACKOFF_CAP_SECONDS", 0)
            task = asyncio.create_task(runtime._run_telegram_long_poll({"id": 1, "platform_app_id": "bot"}))
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=2)
            assert persist_calls == expected


class TestLockRenewal:
    async def test_renewal_refreshes_ttl(self, nonce_redis, monkeypatch) -> None:
        """F4：同值续期必须 EXPIRE 刷新 TTL，否则 60s 后双副本互抢。"""
        from yuxi.channels import runtime

        expire_calls: list[tuple[str, int]] = []
        original_expire = nonce_redis.expire

        async def _expire(key, ttl):
            expire_calls.append((key, ttl))
            return await original_expire(key, ttl)

        monkeypatch.setattr(nonce_redis, "expire", _expire)

        assert await runtime._acquire_or_renew_lock(7) is True  # 抢锁
        expire_calls.clear()
        assert await runtime._acquire_or_renew_lock(7) is True  # 续期（同实例）
        assert expire_calls and expire_calls[0][1] == runtime.LOCK_TTL_SECONDS


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeTokenClient:
    """带 gettoken GET 的假客户端：post 按预置序列出栈。"""

    def __init__(self, post_results: list[dict]) -> None:
        self.post_results = list(post_results)
        self.sent: list[str] = []
        self.token_fetches = 0

    async def get(self, url: str, **kwargs) -> _FakeResponse:
        self.token_fetches += 1
        return _FakeResponse({"errcode": 0, "access_token": f"tok{self.token_fetches}", "expires_in": 7200})

    async def post(self, url: str, **kwargs) -> _FakeResponse:
        self.sent.append(kwargs["json"]["markdown"]["content"])
        return _FakeResponse(self.post_results.pop(0))


class TestTokenRefreshResume:
    async def test_refresh_resumes_from_position_without_resend(self, monkeypatch) -> None:
        """F5：第 1 片遇 token 失效 → 重取后从该片重试，成功片不重发。"""
        adapter = WeComAdapter()
        # c1 发送 → 42001 token 失效；重取后 c1/c2/c3 全部成功
        client = _FakeTokenClient(
            [{"errcode": 42001, "errmsg": "expired"}, {"errcode": 0}, {"errcode": 0}, {"errcode": 0}]
        )
        monkeypatch.setattr("yuxi.channels.adapters.wecom.get_http_client", lambda: client)

        async def _no_cache(*args, **kwargs):
            return None

        async def _noop(*args, **kwargs):
            return None

        monkeypatch.setattr("yuxi.channels.adapters.wecom.token_cache.get", _no_cache)
        monkeypatch.setattr("yuxi.channels.adapters.wecom.token_cache.set", _noop)
        monkeypatch.setattr("yuxi.channels.adapters.wecom.token_cache.clear", _noop)

        payload = OutboundPayload(kind="markdown", chunks=["c1", "c2", "c3"])
        await adapter.push(
            credentials={"corp_id": "c", "corp_secret": "s", "agent_id": "1"},
            platform_app_id="corp1",
            chat_id="zhangsan",
            user_id=None,
            payload=payload,
        )
        # 失败尝试 1 次 c1 + 成功 c1/c2/c3 各一次：无「成功重发」
        assert client.sent == ["c1", "c1", "c2", "c3"]
        assert client.token_fetches == 2

    async def test_refresh_then_later_failure_reports_position(self, monkeypatch) -> None:
        """F5+续传：刷新后发到 c3 失败 → chunks_sent 报告本次真实成功数。"""
        adapter = WeComAdapter()
        client = _FakeTokenClient(
            [{"errcode": 42001}, {"errcode": 0}, {"errcode": 0}, {"errcode": 1, "errmsg": "boom"}]
        )
        monkeypatch.setattr("yuxi.channels.adapters.wecom.get_http_client", lambda: client)

        async def _no_cache(*args, **kwargs):
            return None

        async def _noop(*args, **kwargs):
            return None

        monkeypatch.setattr("yuxi.channels.adapters.wecom.token_cache.get", _no_cache)
        monkeypatch.setattr("yuxi.channels.adapters.wecom.token_cache.set", _noop)
        monkeypatch.setattr("yuxi.channels.adapters.wecom.token_cache.clear", _noop)

        payload = OutboundPayload(kind="markdown", chunks=["c1", "c2", "c3"])
        with pytest.raises(ChannelPushError) as exc_info:
            await adapter.push(
                credentials={"corp_id": "c", "corp_secret": "s", "agent_id": "1"},
                platform_app_id="corp1",
                chat_id="zhangsan",
                user_id=None,
                payload=payload,
            )
        assert exc_info.value.chunks_sent == 2  # c1/c2 已送达（c3 失败不计入）


class TestPolicyOrderAndQuota:
    """S3/S4：非文本与指令在策略门之后；限额只计 dispatched。"""

    async def test_media_on_disabled_app_no_reply(self, session, monkeypatch) -> None:
        app = await session.get(ChannelApp, 1)
        app.is_enabled = False
        await session.commit()
        replies: list[str] = []

        async def _reply(*args, **kwargs):
            replies.append("called")

        monkeypatch.setattr(channel_service, "_push_reply", _reply)
        from yuxi.channels.contracts import ChannelEnvelope

        envelope = ChannelEnvelope(
            channel_type="feishu",
            platform_app_id="cli_t",
            platform_message_id="om_m1",
            chat_id="oc_t",
            user_id="ou_t",
            text="",
            raw={"unsupported_media": True},
        )
        await channel_service.ingest_channel_envelope(session, app, envelope)
        message = (await session.execute(select(ChannelMessage))).scalar_one()
        assert message.status == "ignored" and message.status_detail == "channel_disabled"
        assert replies == []  # 停用渠道不出站任何回复

    async def test_media_on_enabled_app_replies(self, session, monkeypatch) -> None:
        app = await session.get(ChannelApp, 1)
        replies: list[str] = []

        async def _reply(db, app, message, text, **kwargs):
            replies.append(text)

        monkeypatch.setattr(channel_service, "_push_reply", _reply)
        from yuxi.channels.contracts import ChannelEnvelope

        envelope = ChannelEnvelope(
            channel_type="feishu",
            platform_app_id="cli_t",
            platform_message_id="om_m2",
            chat_id="oc_t",
            user_id="ou_t",
            text="",
            raw={"unsupported_media": True},
        )
        await channel_service.ingest_channel_envelope(session, app, envelope)
        message = (
            await session.execute(select(ChannelMessage).where(ChannelMessage.platform_message_id == "om_m2"))
        ).scalar_one()
        assert message.status == "replied" and message.status_detail == "unsupported_media"
        assert replies and "仅支持文本" in replies[0]

    async def test_daily_limit_counts_only_dispatched(self, session, monkeypatch) -> None:
        """S4：replied（/help、非文本提示）不占额度，只有 dispatched 占。"""
        app = await session.get(ChannelApp, 1)
        app.config = {**app.config, "daily_limit": 1}
        await session.commit()

        # 三条 replied（指令/提示类）——不占额度
        for index in range(3):
            session.add(
                ChannelMessage(
                    tenant_id=1,
                    channel_app_id=1,
                    direction="in",
                    platform_message_id=f"om_r{index}",
                    platform_chat_id="oc_t",
                    status="replied",
                )
            )
        await session.commit()

        from yuxi.channels.contracts import ChannelEnvelope

        envelope = ChannelEnvelope(
            channel_type="feishu",
            platform_app_id="cli_t",
            platform_message_id="om_q1",
            chat_id="oc_t",
            user_id="ou_t",
            text="还有额度吗",
            raw={},
        )
        message = await channel_service._insert_inbound_message(session, app, envelope)
        gate = await channel_service._evaluate_policy(session, app, envelope, message)
        assert gate == (None, None)  # replied 不占额 → 放行

        # 一条 dispatched → 占满额度
        session.add(
            ChannelMessage(
                tenant_id=1,
                channel_app_id=1,
                direction="in",
                platform_message_id="om_d1",
                platform_chat_id="oc_t",
                status="dispatched",
            )
        )
        await session.commit()
        envelope2 = ChannelEnvelope(
            channel_type="feishu",
            platform_app_id="cli_t",
            platform_message_id="om_q2",
            chat_id="oc_t",
            user_id="ou_t",
            text="再来一条",
            raw={},
        )
        message2 = await channel_service._insert_inbound_message(session, app, envelope2)

        async def _reply(*args, **kwargs):
            return None

        monkeypatch.setattr(channel_service, "_push_reply", _reply)
        gate2 = await channel_service._evaluate_policy(session, app, envelope2, message2)
        assert gate2 == ("rejected", "daily_limit")


class TestConfigWhitelist:
    """S2：config 闭集与部分更新语义。"""

    async def test_unknown_config_key_rejected(self) -> None:
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            channel_service._normalize_config({"evil_key": True})
        assert exc_info.value.status_code == 422

    async def test_store_text_preview_wired(self) -> None:
        config = channel_service._normalize_config({"store_text_preview": True}, with_defaults=True)
        assert config["store_text_preview"] is True
        assert channel_service._normalize_config({}, with_defaults=True)["store_text_preview"] is False

    async def test_partial_update_preserves_existing(self) -> None:
        existing = {**channel_service.CONFIG_DEFAULTS, "bound_agent_slug": "bot", "transport_cursor": 42}
        merged = dict(existing)
        merged.update(channel_service._normalize_config({"daily_limit": 5}))
        assert merged["bound_agent_slug"] == "bot"  # 未提供的不被刷掉
        assert merged["daily_limit"] == 5
        assert merged["transport_cursor"] == 42  # 内部键不受 API 影响
