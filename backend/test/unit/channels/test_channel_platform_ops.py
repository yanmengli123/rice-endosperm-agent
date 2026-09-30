"""平台能力与运营面回归（三轮）。

覆盖：出站 Markdown 呈现矩阵、Telegram 群聊 @ 判定与指令归一化、钉钉加密回调、
来源 IP 白名单（log/enforce）、额度回告每窗口一次、身份指令绕过策略门、
绑定爆破熔断、日桶指标计数、保留期清理（PENDING 保护）、以及 F5 的**原始缺陷形态**
（分片已成功、后续片遇 token 过期 → 不得重发已成功片）。
"""

from __future__ import annotations

import hashlib
import itertools
import json
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from yuxi.channels.adapters.dingtalk import DingTalkAdapter
from yuxi.channels.adapters.telegram import TelegramAdapter
from yuxi.channels.contracts import ChannelEnvelope, OutboundPayload
from yuxi.channels.render import markdown_mode, render_outbound
from yuxi.channels.signing import ChannelSignatureError, WeChatCrypto, wechat_signature
from yuxi.services import channel_service
from yuxi.storage.postgres.models_business import (
    ChannelApp,
    ChannelChat,
    ChannelEndUser,
    ChannelMessage,
    ChannelOutboundOutbox,
    ChannelPairing,
)
from yuxi.utils.datetime_utils import utc_now

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

PATH_TOKEN = "platform-ops-token"


class _FakeRedis:
    """最小 Redis 假体：供指标/绑定熔断/nonce 用（get/set/incr/expire/delete/scan_iter）。"""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttl: dict[str, int] = {}

    async def set(self, key, value, ex=None, nx=False, xx=False):
        if nx and key in self.store:
            return None
        if xx and key not in self.store:
            return None
        self.store[key] = str(value)
        if ex:
            self.ttl[key] = int(ex)
        return True

    async def get(self, key):
        return self.store.get(key)

    async def incr(self, key, amount=1):
        value = int(self.store.get(key) or 0) + int(amount)
        self.store[key] = str(value)
        return value

    async def expire(self, key, ttl):
        self.ttl[key] = int(ttl)
        return key in self.store

    async def delete(self, key):
        self.store.pop(key, None)
        self.ttl.pop(key, None)
        return 1

    async def scan_iter(self, match="*", count=200):
        import fnmatch

        for key in list(self.store):
            if fnmatch.fnmatch(key, match):
                yield key


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for table in (
            ChannelApp.__table__,
            ChannelEndUser.__table__,
            ChannelChat.__table__,
            ChannelMessage.__table__,
            ChannelOutboundOutbox.__table__,
            ChannelPairing.__table__,
        ):
            await conn.run_sync(table.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(
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
        await session.commit()
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def fake_redis(monkeypatch):
    fake = _FakeRedis()

    async def _client():
        return fake

    monkeypatch.setattr("yuxi.storage.redis.manager.get_async_redis_client", _client)
    return fake


def _envelope(text: str = "hello", *, chat_id: str = "oc_t", chat_type: str = "p2p"):
    return ChannelEnvelope(
        channel_type="feishu",
        platform_app_id="cli_t",
        platform_message_id=f"om_{time_token()}",
        chat_id=chat_id,
        chat_type=chat_type,
        user_id="ou_t",
        text=text,
    )


_SEQ = itertools.count(1)


def time_token() -> str:
    """platform_message_id 上有唯一约束：连发也要唯一。"""

    import time

    return f"{time.time_ns()}-{next(_SEQ)}"


async def _gate(session, app, envelope, message):
    return await channel_service._evaluate_policy(session, app, envelope, message)


class TestRenderMatrix:
    """出站 Markdown 呈现闭集：native / card / plain（修「飞书/Telegram 裸露符号」）。"""

    async def test_platform_modes(self) -> None:
        assert markdown_mode("wecom") == "native"
        assert markdown_mode("dingtalk") == "native"
        assert markdown_mode("feishu") == "card"
        assert markdown_mode("wechat_oa") == "plain"
        assert markdown_mode("telegram") == "plain"
        assert markdown_mode("unknown_platform") == "plain"

    async def test_plain_platforms_strip_markdown(self) -> None:
        for channel in ("telegram", "wechat_oa"):
            _, chunks = render_outbound("**答案** 与 `code`", channel)
            assert "**" not in "".join(chunks), channel

    async def test_native_and_card_keep_markdown(self) -> None:
        for channel in ("wecom", "dingtalk", "feishu"):
            _, chunks = render_outbound("**答案**", channel)
            assert "**" in "".join(chunks), channel


class TestTelegramGroupSemantics:
    """群内指令归一化 + 按 bot username 的权威 @ 判定。"""

    async def test_normalize_command_suffix(self) -> None:
        assert TelegramAdapter.normalize_group_text("/help@mybot", "mybot") == "/help"
        assert TelegramAdapter.normalize_group_text("@mybot /help", "mybot") == "/help"
        assert TelegramAdapter.normalize_group_text("@other /help", "mybot") == "@other /help"
        assert TelegramAdapter.normalize_group_text("/help@mybot", None) == "/help@mybot"
        assert TelegramAdapter.normalize_group_text("/bind ABCD", "mybot") == "/bind ABCD"

    async def test_mention_detection_is_bot_specific(self) -> None:
        assert TelegramAdapter.detect_mention({"entities": [{"type": "bot_command"}]}, "mybot") is True
        at_bot = {"entities": [{"type": "mention"}], "text": "@mybot 你好"}
        assert TelegramAdapter.detect_mention(at_bot, "mybot") is True
        at_other = {"entities": [{"type": "mention"}], "text": "@someone else"}
        assert TelegramAdapter.detect_mention(at_other, "mybot") is False
        # 冷缓存（拿不到 username）fail-open 到旧语义：宁多答勿失答
        assert TelegramAdapter.detect_mention(at_other, None) is True
        reply = {"reply_to_message": {"from": {"username": "mybot"}}}
        assert TelegramAdapter.detect_mention(reply, "mybot") is True

    async def test_parse_update_group_command(self) -> None:
        update = {
            "update_id": 7,
            "message": {
                "message_id": 77,
                "chat": {"id": -100, "type": "supergroup"},
                "from": {"id": 5, "first_name": "U"},
                "text": "/help@mybot",
                "entities": [{"type": "bot_command", "offset": 0, "length": 10}],
            },
        }
        result = TelegramAdapter().parse_update(update, platform_app_id="123", bot_username="mybot")
        envelope = result.envelopes[0]
        assert envelope.text == "/help"  # 归一化后指令解析器可识别
        assert envelope.chat_type == "group"
        assert envelope.mentioned_me is True  # bot_command → 通过 mention_only 门


def _aes_key_43() -> str:
    """固定的 43 位 EncodingAESKey（同一测试内加解密必须共用同一密钥）。"""

    import base64

    return base64.b64encode(bytes(range(32))).decode().rstrip("=")


class TestDingTalkEncryptedCallback:
    """钉钉加密回调（opt-in）：签名 + AES 明文布局与 challenge 加密封包。"""

    TOKEN = "tok123"
    CORP = "corp1"

    def _credentials(self) -> dict[str, str]:
        return {"encoding_aes_key": _aes_key_43(), "token": self.TOKEN, "corp_id": self.CORP}

    def _body(self, event: dict) -> tuple[bytes, dict[str, str]]:
        crypto = WeChatCrypto(self._credentials()["encoding_aes_key"], self.CORP)
        encrypt = crypto.encrypt(json.dumps(event, ensure_ascii=False))
        import time as _time

        timestamp = str(int(_time.time() * 1000))
        nonce = "nonce1"
        query = {
            "signature": wechat_signature(self.TOKEN, timestamp, nonce, encrypt),
            "timestamp": timestamp,
            "nonce": nonce,
        }
        return json.dumps({"encrypt": encrypt}).encode(), query

    async def test_encrypted_message_parsed(self) -> None:
        event = {
            "msgtype": "text",
            "text": {"content": "你好钉钉"},
            "conversationType": "1",
            "senderStaffId": "u1",
            "senderNick": "U",
            "msgId": "m9",
            "sessionWebhook": "https://oapi.dingtalk.com/robot/send",
        }
        body, query = self._body(event)
        result = await DingTalkAdapter().parse_inbound(
            method="POST",
            query=query,
            headers={},
            body=body,
            credentials=self._credentials(),
            platform_app_id="ding1",
        )
        envelope = result.envelopes[0]
        assert envelope.text == "你好钉钉"
        assert envelope.raw["session_webhook"].startswith("https://")

    async def test_bad_signature_rejected(self) -> None:
        body, query = self._body({"msgtype": "text", "text": {"content": "x"}})
        with pytest.raises(ChannelSignatureError):
            await DingTalkAdapter().parse_inbound(
                method="POST",
                query={**query, "signature": "deadbeef"},
                headers={},
                body=body,
                credentials=self._credentials(),
                platform_app_id="ding1",
            )

    async def test_encrypted_challenge_returns_signed_envelope(self) -> None:
        body, query = self._body({"challenge": "abc", "token": self.TOKEN, "type": "url_verification"})
        result = await DingTalkAdapter().parse_inbound(
            method="POST",
            query=query,
            headers={},
            body=body,
            credentials=self._credentials(),
            platform_app_id="ding1",
        )
        assert result.challenge is None
        ack = json.loads(result.ack_body)
        assert ack["msg_signature"] == wechat_signature(self.TOKEN, ack["timeStamp"], ack["nonce"], ack["encrypt"])
        crypto = WeChatCrypto(self._credentials()["encoding_aes_key"], self.CORP)
        assert json.loads(crypto.decrypt(ack["encrypt"]))["challenge"] == "abc"

    async def test_plaintext_path_unchanged(self) -> None:
        event = {"msgtype": "text", "text": {"content": "明文"}, "senderStaffId": "u2", "msgId": "m1"}
        result = await DingTalkAdapter().parse_inbound(
            method="POST", query={}, headers={}, body=json.dumps(event).encode(), credentials={}, platform_app_id="d"
        )
        assert result.envelopes[0].text == "明文"


class TestIpAllowlist:
    """来源 IP 白名单（F2）：opt-in + log 先观察 + enforce 拦截（fail-closed）。"""

    async def test_client_ip_takes_rightmost_forwarded_for(self) -> None:
        assert channel_service._client_ip({"x-forwarded-for": "1.2.3.4, 10.0.0.9"}) == "10.0.0.9"
        assert channel_service._client_ip({"x-real-ip": "9.9.9.9"}) == "9.9.9.9"
        assert channel_service._client_ip({}) == ""

    async def test_disabled_by_default(self, db) -> None:
        app = await db.get(ChannelApp, 1)
        assert channel_service._check_ip_allowlist(app, "8.8.8.8") == "allowed"

    async def test_enforce_mode(self, db) -> None:
        app = await db.get(ChannelApp, 1)
        app.config = {**app.config, "ip_allowlist": ["10.0.0.0/8"], "ip_allowlist_mode": "enforce"}
        await db.commit()
        assert channel_service._check_ip_allowlist(app, "10.1.2.3") == "allowed"
        assert channel_service._check_ip_allowlist(app, "8.8.8.8") == "blocked"
        assert channel_service._check_ip_allowlist(app, "") == "blocked"  # enforce 下 fail-closed
        assert channel_service._check_ip_allowlist(app, "not-an-ip") == "blocked"

    async def test_log_mode_observes_only(self, db) -> None:
        app = await db.get(ChannelApp, 1)
        app.config = {**app.config, "ip_allowlist": ["10.0.0.0/8"], "ip_allowlist_mode": "log"}
        await db.commit()
        assert channel_service._check_ip_allowlist(app, "8.8.8.8") == "allowed"

    async def test_config_validation(self) -> None:
        from fastapi import HTTPException

        assert channel_service._normalize_config({"ip_allowlist": ["10.0.0.0/8"]})["ip_allowlist"] == ["10.0.0.0/8"]
        assert channel_service._normalize_config({"ip_allowlist_mode": "enforce"})["ip_allowlist_mode"] == "enforce"
        with pytest.raises(HTTPException) as exc:
            channel_service._normalize_config({"ip_allowlist_mode": "yes"})
        assert exc.value.status_code == 422
        with pytest.raises(HTTPException):
            channel_service._normalize_config({"ip_allowlist": "10.0.0.0/8"})  # 必须是列表


import itertools  # noqa: E402

_ID_SEQ = itertools.count(7000)


def next_id() -> int:
    """SQLite 下 BIGINT 主键不自动递增：显式给 id（见 AGENTS.md 测试方言约定）。"""

    return next(_ID_SEQ)


async def _inbound_message(session, envelope, *, status: str = "received") -> ChannelMessage:
    """按 router 的落库口径造入站行（SQLite 下 BIGINT 主键需显式 id）。"""

    message = ChannelMessage(
        id=next_id(),
        tenant_id=1,
        channel_app_id=1,
        direction="in",
        platform_message_id=envelope.platform_message_id,
        platform_chat_id=envelope.chat_id,
        platform_user_id=envelope.user_id,
        chat_type=envelope.chat_type,
        payload=envelope.to_payload(),
        status=status,
    )
    session.add(message)
    await session.commit()
    return message


class TestDailyLimitNoticeOnce:
    """G2：额度回告每窗口一次，不逐条骚扰（旧版被拦一条发一条）。"""

    async def test_notice_only_once(self, db, monkeypatch) -> None:
        app = await db.get(ChannelApp, 1)
        app.config = {**app.config, "daily_limit": 1}
        db.add(app)
        # 窗口内已有一条真实消耗：只有 status=dispatched 的入站行才占额度
        await _inbound_message(db, _envelope("先前提问"), status="dispatched")

        pushes: list[str] = []

        async def _fake_reply(session, app_row, message, content, **kwargs):
            pushes.append(content)
            return "om_fake"

        monkeypatch.setattr(channel_service, "_push_reply", _fake_reply)

        first = _envelope("第一问")
        first_message = await _inbound_message(db, first)
        assert await _gate(db, app, first, first_message) == ("rejected", "daily_limit")
        first_message.status_detail = "daily_limit"  # router 落库的就是拒绝原因
        await db.commit()

        second = _envelope("第二问")
        second_message = await _inbound_message(db, second)
        assert await _gate(db, app, second, second_message) == ("rejected", "daily_limit")
        assert len(pushes) == 1  # 第二条不再回告


class TestIdentityCommandBypass:
    """/bind /help /unbind /status 不得被 chat/user/mention 门静默吞掉。"""

    async def test_commands_bypass_strategy_gates(self, db) -> None:
        app = await db.get(ChannelApp, 1)
        app.config = {**app.config, "allowed_chats": ["oc_named"], "mention_only": True}
        db.add(app)
        await db.commit()

        # 未授权群里的身份指令：必须能走到指令解析（返回 None = 交回 handle_inbound）
        command = _envelope("/help", chat_id="oc_other", chat_type="group")
        command.mentioned_me = False
        assert await _gate(db, app, command, None) == (None, None)

        # 同群的普通消息仍被拦（策略门本身没被削弱）
        plain = _envelope("你好", chat_id="oc_other", chat_type="group")
        plain.mentioned_me = False
        plain_message = await _inbound_message(db, plain)
        assert await _gate(db, app, plain, plain_message) == ("rejected", "chat_not_allowed")

        # 授权群内未 @ 的普通消息：静默忽略
        allowed = _envelope("你好", chat_id="oc_named", chat_type="group")
        allowed.mentioned_me = False
        allowed_message = await _inbound_message(db, allowed)
        assert await _gate(db, app, allowed, allowed_message) == ("ignored", "group_not_mentioned")


class TestBindThrottle:
    """绑定码爆破熔断（C2）：同应用同用户窗口内失败达阈值即锁，Redis 挂了不误伤。"""

    async def test_lockout_after_limit_then_clear(self, db, fake_redis) -> None:
        app = await db.get(ChannelApp, 1)
        assert await channel_service._bind_attempt_allowed(app, "ou_v") is True
        for _ in range(channel_service.BIND_FAIL_LIMIT):
            await channel_service._record_bind_failure(app, "ou_v")
        assert await channel_service._bind_attempt_allowed(app, "ou_v") is False
        assert fake_redis.ttl[channel_service._bind_fail_key(app, "ou_v")] == channel_service.BIND_FAIL_WINDOW_SECONDS
        await channel_service._clear_bind_failures(app, "ou_v")
        assert await channel_service._bind_attempt_allowed(app, "ou_v") is True

    async def test_scope_is_app_plus_user(self, db, fake_redis) -> None:
        app = await db.get(ChannelApp, 1)
        await channel_service._record_bind_failure(app, "ou_a")
        assert await channel_service._bind_attempt_allowed(app, "ou_b") is True

    async def test_fail_open_when_redis_down(self, db, monkeypatch) -> None:
        app = await db.get(ChannelApp, 1)

        async def _broken():
            raise RuntimeError("redis down")

        monkeypatch.setattr("yuxi.storage.redis.manager.get_async_redis_client", _broken)
        assert await channel_service._bind_attempt_allowed(app, "ou_v") is True
        await channel_service._record_bind_failure(app, "ou_v")  # 计数失败也不得抛到用户面前


class TestChannelMetrics:
    """运营指标：Redis 日桶计数 → 聚合读数 → admin 端点告警口径。"""

    async def test_counters_and_rollup(self, fake_redis) -> None:
        from yuxi.channels import telemetry

        await telemetry.incr_metric(telemetry.Metrics.INBOUND_RECEIVED, app_id=1)
        await telemetry.incr_metric(telemetry.Metrics.INBOUND_RECEIVED, app_id=1)
        await telemetry.incr_metric(telemetry.Metrics.INBOUND_IP_BLOCKED, app_id=1)
        await telemetry.incr_metric(telemetry.Metrics.OUTBOUND_DEAD, app_id=2, delta=2)

        snapshot = await telemetry.read_metrics(days=1)
        day = telemetry.metric_day()
        by_day = snapshot["by_day"][day]
        assert by_day[telemetry.Metrics.INBOUND_RECEIVED] == 2
        assert by_day[telemetry.Metrics.INBOUND_IP_BLOCKED] == 1
        assert by_day[telemetry.Metrics.OUTBOUND_DEAD] == 2
        assert snapshot["by_app"]["1"][telemetry.Metrics.INBOUND_RECEIVED] == 2
        assert telemetry.Metrics.OUTBOUND_DEAD not in snapshot["by_app"]["1"]
        key = telemetry.metric_key(telemetry.Metrics.INBOUND_RECEIVED, 1)
        assert fake_redis.ttl[key] == telemetry.KEY_TTL_SECONDS  # 桶不无限增长

    async def test_observability_failures_never_break_delivery(self, monkeypatch) -> None:
        from yuxi.channels import telemetry

        async def _broken():
            raise RuntimeError("redis down")

        monkeypatch.setattr("yuxi.storage.redis.manager.get_async_redis_client", _broken)
        await telemetry.incr_metric(telemetry.Metrics.INBOUND_RECEIVED, app_id=1)  # 写失败不抛
        snapshot = await telemetry.read_metrics(days=1)
        assert snapshot["by_day"] == {}
        assert snapshot["errors"]  # 读失败要说清原因，不能静默出空看板

    async def test_metrics_endpoint_publishes_alert_calibre(self, monkeypatch) -> None:
        from server.routers.channel_router import channel_metrics

        async def _broken():
            raise RuntimeError("redis down")

        monkeypatch.setattr("yuxi.storage.redis.manager.get_async_redis_client", _broken)
        payload = await channel_metrics(current_user=None, days=1)
        assert payload["slo"]["dead_letter_rate"] == 0.001
        assert any("outbound_dead" in alert for alert in payload["slo"]["alerts"])


class TestRetentionPurge:
    """保留期清理：历史流水不再无限堆积；未投递完的 PENDING 分片绝不删。"""

    async def test_pending_protected_purge(self, db, monkeypatch, fake_redis) -> None:
        from sqlalchemy import select

        old = utc_now() - timedelta(days=120)
        purgeable = next_id()
        protected = next_id()
        db.add(
            ChannelMessage(
                id=purgeable,
                tenant_id=1,
                channel_app_id=1,
                direction="in",
                platform_message_id="m_purge_me",
                platform_chat_id="oc_t",
                platform_user_id="ou_t",
                status="processed",
                created_at=old,
            )
        )
        db.add(
            ChannelMessage(
                id=protected,
                tenant_id=1,
                channel_app_id=1,
                direction="in",
                platform_message_id="m_keep_me",
                platform_chat_id="oc_t",
                platform_user_id="ou_t",
                status="failed",
                created_at=old,
            )
        )
        db.add(
            ChannelOutboundOutbox(
                id=next_id(),
                tenant_id=1,
                channel_app_id=1,
                channel_message_id=protected,
                payload={"chunks": ["待重投"]},
                status="PENDING",
                created_at=old,
            )
        )
        db.add(
            ChannelOutboundOutbox(
                id=next_id(),
                tenant_id=1,
                channel_app_id=1,
                channel_message_id=purgeable,
                payload={"chunks": ["早已发完"]},
                status="PUSHED",
                created_at=old,
                pushed_at=old,
            )
        )
        db.add(
            ChannelPairing(
                id=next_id(),
                tenant_id=1,
                channel_app_id=1,
                code_hash="a" * 64,
                created_by="u_creator",
                status="consumed",
                expires_at=old,
                consumed_at=old,
            )
        )
        await db.commit()

        class _Ctx:
            async def __aenter__(self):
                return db

            async def __aexit__(self, *args):
                return False

        monkeypatch.setattr(channel_service.pg_manager, "get_async_session_context", lambda: _Ctx())
        summary = await channel_service.purge_channel_history(None)
        assert summary == {"messages": 1, "outbox": 1, "pairings": 1}

        remaining_messages = {row.platform_message_id for row in (await db.execute(select(ChannelMessage))).scalars()}
        assert remaining_messages == {"m_keep_me"}
        remaining_outbox = [row.status for row in (await db.execute(select(ChannelOutboundOutbox))).scalars()]
        assert remaining_outbox == ["PENDING"]  # 待发分片不会被保留期带走


class TestWecomTokenRefreshDoesNotResend:
    """F5 原始形态：c1 已送达、c2 遇 token 过期 → 刷新后从 c2 续发，绝不重发 c1。"""

    @staticmethod
    def _client(responses: list[dict]) -> tuple[object, list[str]]:
        sent: list[str] = []

        class _Resp:
            def __init__(self, payload):
                self._payload = payload

            def json(self):
                return dict(self._payload)

        class _Client:
            async def post(self, url, params=None, json=None):
                sent.append(json["markdown"]["content"])
                return _Resp(responses.pop(0))

        return _Client(), sent

    async def test_partial_progress_survives_token_refresh(self, monkeypatch) -> None:
        from yuxi.channels.adapters.base import token_cache
        from yuxi.channels.adapters.wecom import WeComAdapter

        client, sent = self._client(
            [{"errcode": 0}, {"errcode": 42001, "errmsg": "token expired"}, {"errcode": 0}, {"errcode": 0}]
        )
        monkeypatch.setattr("yuxi.channels.adapters.wecom.get_http_client", lambda: client)

        cleared: list[str] = []

        async def _clear(channel_type, platform_app_id):
            cleared.append(f"{channel_type}:{platform_app_id}")

        monkeypatch.setattr(token_cache, "clear", _clear)

        token_calls: list[bool] = []

        async def _token(self, credentials, platform_app_id, *, force=False):
            token_calls.append(bool(force))
            return "fresh" if force else "stale"

        monkeypatch.setattr(WeComAdapter, "_get_access_token", _token)

        adapter = WeComAdapter()
        adapter.outbound_rate_per_second = 1000  # 分片节流不是本测试的关注点
        await adapter.push(
            credentials={"corp_id": "ci", "corp_secret": "cs"},
            platform_app_id="corp1",
            chat_id="wm_t",
            user_id="wm_u",
            payload=OutboundPayload(kind="markdown", chunks=["c1", "c2", "c3"]),
        )

        assert sent == ["c1", "c2", "c2", "c3"]  # c1 只发过一次，续发从 c2 起
        assert token_calls == [False, True]  # 中途确实重取过 token
        assert cleared == ["wecom:corp1"]

    async def test_hard_failure_reports_progress(self, monkeypatch) -> None:
        from yuxi.channels.adapters.base import ChannelPushError
        from yuxi.channels.adapters.wecom import WeComAdapter

        client, sent = self._client([{"errcode": 0}, {"errcode": 45009, "errmsg": "api freq out"}])
        monkeypatch.setattr("yuxi.channels.adapters.wecom.get_http_client", lambda: client)

        async def _token(self, credentials, platform_app_id, *, force=False):
            return "stale"

        monkeypatch.setattr(WeComAdapter, "_get_access_token", _token)

        adapter = WeComAdapter()
        adapter.outbound_rate_per_second = 1000
        with pytest.raises(ChannelPushError) as failure:
            await adapter.push(
                credentials={"corp_id": "ci", "corp_secret": "cs"},
                platform_app_id="corp1",
                chat_id="wm_t",
                user_id="wm_u",
                payload=OutboundPayload(kind="markdown", chunks=["c1", "c2", "c3"]),
            )
        assert failure.value.chunks_sent == 1  # relay 据此从 c2 续投，不重发 c1
        assert sent == ["c1", "c2"]
