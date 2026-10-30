from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.services import public_session_service as service
from yuxi.services.public_api_schemas import SessionCreate, SessionEventCreate
from yuxi.services.public_input_service import normalize_messages
from yuxi.services.public_identity_service import PublicActor
from yuxi.storage.postgres.models_business import (
    Agent,
    Base,
    Department,
    PublicFile,
    PublicInput,
    PublicReceipt,
    PublicSessionEvent,
    Tenant,
    TenantMembership,
    User,
)
from yuxi.utils.datetime_utils import utc_now


@pytest.fixture
async def reception(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        db.add(Tenant(id=1, name="tenant"))
        db.add(Department(id=1, tenant_id=1, name="department"))
        db.add(User(uid="alice", username="alice", role="user", department_id=1, password_hash="unused"))
        db.add(TenantMembership(uid="alice", tenant_id=1, role="member", status="active"))
        db.add(
            Agent(
                slug="assistant",
                name="Assistant",
                backend_id="chatbot",
                tenant_id=1,
                created_by="alice",
                config_json={"context": {"system_prompt": "original"}},
            )
        )
        await db.commit()
        monkeypatch.setattr(service.agent_manager, "get_agent", lambda _: SimpleNamespace())
        monkeypatch.setattr(service, "resolve_agent_run_model_spec", lambda model, *_, **__: model or "test:model")
        from unittest.mock import AsyncMock

        monkeypatch.setattr(
            service, "resolve_effective_knowledge_scope", AsyncMock(return_value={"effective_kb_ids": []})
        )
        yield db, PublicActor(uid="alice", tenant_id=1)
    await engine.dispose()


def _message(text, **options):
    return SessionEventCreate.model_validate(
        {
            "events": [
                {
                    "type": "agent.session.input.message",
                    "input": [{"role": "user", "content": [{"type": "input_text", "text": text}]}],
                    "yuxi": options,
                }
            ]
        }
    ).events[0]


async def test_create_replay_and_ordered_inputs_preserve_receipts(reception):
    db, actor = reception
    payload = SessionCreate(agent_id="assistant", input="first")
    session, receipt = await service.create_session(db, actor, payload, key="create")
    replay, replay_receipt = await service.create_session(db, actor, payload, key="create")
    assert replay.id == session.id and replay_receipt == receipt
    assert receipt["turn_id"] is None and receipt["run_id"] is None
    second = await service.receive_message(db, actor, session, _message("second", mode="follow_up"), key="second")
    await db.commit()
    assert await service.receipt_for_key(db, session, "second") == second
    assert (
        await service.receive_message(db, actor, session, _message("second", mode="follow_up"), key="second") == second
    )
    with pytest.raises(HTTPException) as conflict:
        await service.receive_message(db, actor, session, _message("changed", mode="follow_up"), key="second")
    assert conflict.value.status_code == 409
    inputs = list(await db.scalars(select(PublicInput).order_by(PublicInput.received_seq)))
    assert [item.received_seq for item in inputs] == [1, 2]
    assert [item.messages[0]["content"][0]["text"] for item in inputs] == ["first", "second"]
    assert all(item.status == "pending" and item.run_id is None for item in inputs)
    assert await db.scalar(select(func.count()).select_from(PublicReceipt)) == 2
    events = list(await db.scalars(select(PublicSessionEvent).order_by(PublicSessionEvent.sequence)))
    assert [item.sequence for item in events] == [1, 2, 3]
    with pytest.raises(HTTPException) as changed_creation:
        await service.create_session(db, actor, SessionCreate(agent_id="assistant", input="changed"), key="create")
    assert changed_creation.value.status_code == 409


async def test_session_scope_and_file_claim_are_checked_before_acceptance(reception):
    db, actor = reception
    session, _ = await service.create_session(db, actor, SessionCreate(agent_id="assistant"), key="create")
    file = PublicFile(
        id="f" * 32,
        uid=actor.uid,
        tenant_id=1,
        filename="report.txt",
        mime_type="text/plain",
        size_bytes=1,
        sha256="a" * 64,
        storage_key="server-only",
        expires_at=utc_now() + timedelta(hours=1),
    )
    db.add(file)
    await db.commit()
    with pytest.raises(HTTPException) as denied:
        await service.require_session(db, PublicActor(uid="alice", tenant_id=1, app_id="forged"), session.id)
    assert denied.value.status_code == 404
    receipt = await service.receive_message(
        db, actor, session, _message("read", mode="follow_up", attachment_file_ids=[file.id]), key="file"
    )
    await db.commit()
    assert file.input_id == receipt["input_id"] and file.status == "preparing"
    with pytest.raises(HTTPException) as reused:
        await service.receive_message(
            db, actor, session, _message("again", mode="follow_up", attachment_file_ids=[file.id]), key="other"
        )
    assert reused.value.status_code == 409
    await db.rollback()


@pytest.mark.parametrize(
    "url", ["https://untrusted/image.png", "data:image/png;base64,?", "data:text/html;base64,eA=="]
)
def test_public_images_cannot_fetch_external_urls_or_bypass_base64_validation(url):
    with pytest.raises(HTTPException):
        normalize_messages([{"role": "user", "content": [{"type": "input_image", "image_url": url}]}])


@pytest.mark.parametrize(
    "body",
    [
        {"agent_id": "assistant", "tenant_id": 1},
        {"agent_id": "assistant", "stream": True},
        {"agent_id": "assistant", "agent": {"system_prompt": "override"}},
    ],
)
def test_unknown_scope_and_agent_override_fields_are_rejected(body):
    with pytest.raises(ValidationError):
        SessionCreate.model_validate(body)
