import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.services import collaboration_service as service
from yuxi.storage.postgres.models_business import (
    Agent,
    AgentRun,
    Base,
    CollaborationMessage,
    CollaborationStop,
    Tenant,
    TenantMembership,
    User,
)


@pytest.fixture
async def collaboration_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda conn: Base.metadata.create_all(
                conn,
                tables=[
                    Tenant.__table__,
                    User.__table__,
                    TenantMembership.__table__,
                    Agent.__table__,
                    AgentRun.__table__,
                    CollaborationMessage.__table__,
                    CollaborationStop.__table__,
                ],
            )
        )
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        db.add(Tenant(id=1, name="research"))
        db.add(User(uid="alice", username="alice", password_hash="unused", role="user"))
        db.add(TenantMembership(tenant_id=1, uid="alice", status="active"))
        for name, parent, kind, thread in [
            ("root", None, "chat", "main"),
            ("child", "root", "subagent", "child-thread"),
            ("sibling", "root", "subagent", "sibling-thread"),
            ("foreign", None, "chat", "another-project"),
        ]:
            db.add(
                AgentRun(
                    id=name,
                    tenant_id=1,
                    uid="alice",
                    request_id=name,
                    created_by_run_id=parent,
                    agent_slug="research",
                    run_type=kind,
                    conversation_thread_id=thread,
                    status="running",
                    input_payload={},
                )
            )
        await db.commit()
        yield db
    await engine.dispose()


async def _send(db, **overrides):
    return await service.send_message(
        db,
        **dict(
            dict(
                uid="alice",
                sender_run_id="root",
                target_run_id="child",
                body="Please verify the original figure",
                request_id="message-1",
            ),
            **overrides,
        ),
    )


async def test_guidance_is_idempotent_and_cannot_cross_project_or_user(collaboration_db):
    db = collaboration_db
    first = await _send(db)
    assert (await _send(db))["id"] == first["id"]
    assert len((await db.scalars(select(CollaborationMessage))).all()) == 1
    with pytest.raises(HTTPException) as conflict:
        await _send(db, body="Different instruction")
    assert conflict.value.status_code == 409
    with pytest.raises(HTTPException) as other:
        await _send(db, target_run_id="foreign", request_id="new-request")
    assert other.value.status_code == 404
    with pytest.raises(HTTPException) as other_user:
        await _send(db, uid="bob")
    assert other_user.value.status_code == 403


async def test_siblings_share_messages_and_tree_keeps_parent_links(collaboration_db):
    db = collaboration_db
    sent = await _send(db, sender_run_id="child", target_run_id="sibling")
    result = await service.tree(db, uid="alice", run_id="sibling")
    assert result["root_run_id"] == "root"
    assert {row["run_id"] for row in result["members"]} == {"root", "child", "sibling"}
    assert next(row for row in result["members"] if row["run_id"] == "sibling")["parent_run_id"] == "root"
    assert result["messages"][0]["id"] == sent["id"]


async def test_delivery_replays_after_crash_and_checkpoint_prevents_duplicates(collaboration_db):
    db = collaboration_db
    sent = await _send(db)
    await db.commit()
    first, ids = await service.pending_guidance(db, uid="alice", run_id="child", delivered_ids=set())
    await db.commit()
    # Delivery acknowledgement alone is not a checkpoint: retry must replay.
    replay, _ = await service.pending_guidance(db, uid="alice", run_id="child", delivered_ids=set())
    assert replay[0].id == first[0].id == "collaboration:" + sent["id"]
    assert first[0].content.endswith("Please verify the original figure")
    assert await service.pending_guidance(db, uid="alice", run_id="child", delivered_ids=set(ids)) == ([], [])
    assert (await service.tree(db, uid="alice", run_id="root"))["messages"][0]["delivered_at"]
    assert await service.pending_guidance(db, uid="alice", run_id="direct-evaluation", delivered_ids=set()) == ([], [])


async def test_stopped_root_rejects_late_messages_and_task_dispatch(collaboration_db):
    db = collaboration_db
    root = await db.get(AgentRun, "root")
    root.status = "cancel_requested"
    await db.commit()
    with pytest.raises(HTTPException) as stopped:
        await _send(db)
    assert stopped.value.status_code == 409
    with pytest.raises(HTTPException) as dispatch:
        await service.assert_spawn_allowed(db, uid="alice", creator_run_id="root")
    assert dispatch.value.status_code == 409


async def test_stopping_a_member_keeps_siblings_and_group_stop_covers_all(collaboration_db, monkeypatch):
    from yuxi.services import agent_run_service

    signals = []

    async def publish(run_id):
        assert not db.in_transaction()
        signals.append(run_id)

    monkeypatch.setattr(agent_run_service, "publish_cancel_signal", publish)
    db = collaboration_db
    result = await service.stop_group(db, uid="alice", run_id="child", whole_group=False)
    assert result["cancel_requested_run_ids"] == ["child"]
    assert (await db.get(AgentRun, "root")).status == "running"
    assert (await db.get(AgentRun, "sibling")).status == "running"
    await service.stop_group(db, uid="alice", run_id="root")
    assert set(signals) == {"root", "child", "sibling"}
    assert (await db.get(AgentRun, "foreign")).status == "running"


async def test_group_stop_is_not_limited_by_the_tree_display_budget(collaboration_db, monkeypatch):
    from yuxi.services import agent_run_service

    async def publish(_run_id):
        pass

    monkeypatch.setattr(agent_run_service, "publish_cancel_signal", publish)
    monkeypatch.setattr(service, "MAX_MEMBERS", 2)
    db = collaboration_db
    with pytest.raises(HTTPException) as oversized:
        await service.tree(db, uid="alice", run_id="root")
    assert oversized.value.status_code == 409
    stopped = await service.stop_group(db, uid="alice", run_id="root")
    assert set(stopped["cancel_requested_run_ids"]) == {"root", "child", "sibling"}


async def test_stop_invalidates_waitpoints_without_rewriting_terminal_history(collaboration_db, monkeypatch):
    from yuxi.services import agent_run_service
    import asyncio

    async def publish(_run_id):
        pass

    monkeypatch.setattr(agent_run_service, "publish_cancel_signal", publish)
    db = collaboration_db
    root = await db.get(AgentRun, "root")
    root.status = "interrupted"
    await db.commit()
    await service.stop_group(db, uid="alice", run_id="root")
    assert root.status == "interrupted"
    with pytest.raises(HTTPException) as stopped:
        await service.assert_resume_allowed(db, uid="alice", run_id="root")
    assert stopped.value.status_code == 409
    tree = await service.tree(db, uid="alice", run_id="root")
    member = next(item for item in tree["members"] if item["run_id"] == "root")
    assert member["status"] == "interrupted" and member["effective_status"] == "cancelled"
    assert member["waiting_for_input"] is False
    with pytest.raises(asyncio.CancelledError):
        await service.pending_guidance(db, uid="alice", run_id="root", delivered_ids=set())
