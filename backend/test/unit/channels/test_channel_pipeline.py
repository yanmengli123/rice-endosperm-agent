"""渠道管线单测：入站幂等去重、thread 映射、凭据/配置归一、scopes 门禁。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from yuxi.channels.contracts import ChannelEnvelope, OutboundPayload
from yuxi.services.channel_service import (
    _insert_inbound_message,
    _normalize_config,
    _normalize_credentials,
    _resolve_thread,
)
from yuxi.storage.postgres.models_business import (
    ChannelApp,
    ChannelChat,
    ChannelMessage,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(ChannelApp.__table__.create)
        await conn.run_sync(ChannelEndUserTable().create)
        await conn.run_sync(ChannelChat.__table__.create)
        await conn.run_sync(ChannelMessage.__table__.create)
        await conn.run_sync(OutboxTable().create)
        await conn.run_sync(PairingTable().create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        yield db
    await engine.dispose()


def ChannelEndUserTable():
    from yuxi.storage.postgres.models_business import ChannelEndUser

    return ChannelEndUser.__table__


def OutboxTable():
    from yuxi.storage.postgres.models_business import ChannelOutboundOutbox

    return ChannelOutboundOutbox.__table__


def PairingTable():
    from yuxi.storage.postgres.models_business import ChannelPairing

    return ChannelPairing.__table__


def _make_app(app_id: int = 1) -> ChannelApp:
    return ChannelApp(
        id=app_id,
        tenant_id=1,
        channel_type="feishu",
        name="test-app",
        platform_app_id="cli_test",
        credentials_ciphertext="enc.v1:stub",
        path_token_hash="h" * 64,
        service_uid="u_service",
        config={"bound_agent_slug": "default-chatbot"},
    )


def _make_envelope(message_id: str = "om_1") -> ChannelEnvelope:
    return ChannelEnvelope(
        channel_type="feishu",
        platform_app_id="cli_test",
        platform_message_id=message_id,
        chat_id="oc_abc",
        chat_type="p2p",
        user_id="ou_user",
        text="水稻胚乳 markers",
    )


class TestInboundDedup:
    async def test_duplicate_platform_message_rejected(self, session) -> None:
        session.add(_make_app())
        await session.flush()

        first = await _insert_inbound_message(session, (await session.get(ChannelApp, 1)), _make_envelope("om_1"))
        assert first is not None and first.status == "received"

        second = await _insert_inbound_message(session, (await session.get(ChannelApp, 1)), _make_envelope("om_1"))
        assert second is None  # 平台重推：唯一索引兜住

        count = len((await session.execute(select(ChannelMessage))).scalars().all())
        assert count == 1

    async def test_different_message_ids_both_persisted(self, session) -> None:
        session.add(_make_app())
        await session.flush()
        app = await session.get(ChannelApp, 1)

        assert await _insert_inbound_message(session, app, _make_envelope("om_1")) is not None
        assert await _insert_inbound_message(session, app, _make_envelope("om_2")) is not None


class TestThreadMapping:
    async def test_same_owner_reuses_thread(self, session) -> None:
        session.add(_make_app())
        await session.flush()
        app = await session.get(ChannelApp, 1)

        first = await _resolve_thread(session, app, _make_envelope(), "u_alice")
        second = await _resolve_thread(session, app, _make_envelope(), "u_alice")
        assert first == second

        row = (await session.execute(select(ChannelChat))).scalar_one()
        assert row.owner_uid == "u_alice" and row.thread_id == first

    async def test_owner_change_gets_new_thread(self, session) -> None:
        session.add(_make_app())
        await session.flush()
        app = await session.get(ChannelApp, 1)

        alice_thread = await _resolve_thread(session, app, _make_envelope(), "u_alice")
        bob_thread = await _resolve_thread(session, app, _make_envelope(), "u_bob")
        assert alice_thread != bob_thread
        assert len((await session.execute(select(ChannelChat))).scalars().all()) == 2


class TestNormalization:
    async def test_credentials_missing_required_rejected(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            _normalize_credentials("feishu", {"app_id": "x"})
        assert exc_info.value.status_code == 422

    async def test_credentials_unknown_keys_dropped(self) -> None:
        normalized = _normalize_credentials("telegram", {"bot_token": "t", "evil": "x"})
        assert normalized == {"bot_token": "t"}

    async def test_config_defaults(self) -> None:
        config = _normalize_config({"bound_agent_slug": "bot"}, with_defaults=True)
        assert config["mention_only"] is True
        assert config["push_placeholder"] is True
        assert config["allowed_chats"] is None
        assert config["daily_limit"] is None


class TestOutboundPayload:
    async def test_payload_roundtrip_with_extra(self) -> None:
        payload = OutboundPayload(
            kind="markdown", chunks=["a", "b"], web_url="http://w", extra={"session_webhook": "https://x"}
        )
        restored = OutboundPayload.from_payload(payload.to_payload())
        assert restored == payload


class TestRequireScope:
    def _checker(self):
        from server.utils.auth_middleware import require_scope

        return require_scope("agent:runs")

    async def test_jwt_passes(self) -> None:
        request = SimpleNamespace(state=SimpleNamespace())  # 未设置 api_key_purpose
        await self._checker()(request)

    async def test_legacy_key_passes(self) -> None:
        request = SimpleNamespace(state=SimpleNamespace(api_key_purpose="desktop_legacy", api_key_scopes=None))
        await self._checker()(request)

    async def test_external_key_empty_scopes_passes_conversation(self) -> None:
        request = SimpleNamespace(state=SimpleNamespace(api_key_purpose="external_agent", api_key_scopes=None))
        await self._checker()(request)

    async def test_external_key_wrong_scope_rejected(self) -> None:
        request = SimpleNamespace(
            state=SimpleNamespace(api_key_purpose="external_agent", api_key_scopes=["other:thing"])
        )
        with pytest.raises(HTTPException) as exc_info:
            await self._checker()(request)
        assert exc_info.value.status_code == 403

    async def test_external_key_matching_scope_passes(self) -> None:
        request = SimpleNamespace(
            state=SimpleNamespace(api_key_purpose="external_agent", api_key_scopes=["agent:runs"])
        )
        await self._checker()(request)
