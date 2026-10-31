import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.services import public_query_service as service
from yuxi.services.input_message_service import build_chat_input_message_from_openai_content
from yuxi.services.public_identity_service import PublicActor
from yuxi.storage.postgres.models_business import (
    AgentRun,
    Base,
    Conversation,
    Message,
    PublicInput,
    PublicSession,
    PublicSessionEvent,
    PublicTurn,
    PublicTurnRun,
    Tenant,
    User,
    ToolCall,
)
from yuxi.utils.datetime_utils import utc_now


@pytest.fixture
async def history():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        db.add(Tenant(id=1, name="tenant"))
        for number, uid in enumerate(["alice", "bob"], 1):
            db.add(User(uid=uid, username=uid, password_hash="unused"))
            db.add(
                Conversation(id=number, thread_id=uid, tenant_id=1, uid=uid, agent_id="assistant", public_session=True)
            )
            db.add(
                PublicSession(
                    id=uid,
                    uid=uid,
                    tenant_id=1,
                    agent_slug="assistant",
                    title=uid,
                    creation_key=uid,
                    creation_hash=uid,
                    config_snapshot={"model_spec": "test:model", "tool_approval_mode": "always_trust"},
                )
            )
        await db.flush()
        for run_id, uid, conversation, turn_id, sequence in [
            ("old", "alice", 1, "turn-1", 1),
            ("chat", "alice", 1, "turn-2", 2),
            ("resume", "alice", 1, "turn-2", 2),
            ("bob", "bob", 2, "bob-turn", 1),
        ]:
            db.add(
                AgentRun(
                    id=run_id,
                    uid=uid,
                    tenant_id=1,
                    request_id=run_id,
                    conversation_thread_id=uid,
                    conversation_id=conversation,
                    agent_slug="assistant",
                    status="completed",
                    input_payload={},
                )
            )
            if run_id != "resume":
                db.add(
                    PublicTurn(
                        id=turn_id,
                        session_id=uid,
                        sequence=sequence,
                        status="completed",
                        current_run_id=run_id,
                        result_run_id=run_id,
                        finished_at=utc_now(),
                    )
                )
            db.add(PublicTurnRun(run_id=run_id, session_id=uid, turn_id=turn_id))
        await db.flush()
        previous = await db.get(AgentRun, "old")
        previous.output_message_id = 1
        resumed = await db.get(AgentRun, "resume")
        resumed.output_message_id = 14
        turn = await db.get(PublicTurn, "turn-2")
        turn.current_run_id = turn.result_run_id = "resume"
        raw = build_chat_input_message_from_openai_content(
            [
                {"type": "text", "text": "question"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,YQ=="}},
            ]
        )
        db.add_all(
            [
                Message(id=1, conversation_id=1, role="assistant", content="previous answer", run_id="old"),
                Message(
                    id=10,
                    conversation_id=1,
                    role="user",
                    content="question",
                    run_id="chat",
                    extra_metadata={"raw_message": raw.raw_message(), "public_input_id": "input-1"},
                ),
                Message(id=11, conversation_id=1, role="assistant", content="", run_id="chat"),
                Message(id=12, conversation_id=1, role="assistant", content="progress", run_id="chat"),
                Message(id=13, conversation_id=1, role="system", content="INTERNAL PROMPT", run_id="chat"),
                Message(
                    id=14,
                    conversation_id=1,
                    role="assistant",
                    content="<think>PRIVATE REASONING</think>final answer",
                    run_id="resume",
                ),
                Message(id=15, conversation_id=1, role="assistant", content="UNREGISTERED AUDIT"),
                Message(id=16, conversation_id=2, role="assistant", content="WRONG CONVERSATION", run_id="resume"),
                Message(id=21, conversation_id=2, role="assistant", content="BOB PRIVATE", run_id="bob"),
            ]
        )
        db.add(
            ToolCall(
                id=77,
                message_id=11,
                langgraph_tool_call_id="call-1",
                tool_name="read_file",
                tool_input={"path": "report.txt"},
                tool_output="file contents",
                status="success",
            )
        )
        db.add(
            PublicInput(
                id="input-1",
                session_id="alice",
                received_seq=1,
                kind="follow_up",
                status="consumed",
                run_id="chat",
                turn_id="turn-2",
                consumed_at=utc_now(),
                attachment_ids=[],
                config_snapshot={},
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "question"},
                            {"type": "input_image", "image_url": "data:image/png;base64,YQ=="},
                        ],
                    }
                ],
            )
        )
        await db.commit()
        yield db, PublicActor(uid="alice", tenant_id=1)
    await engine.dispose()


async def test_items_paginate_registered_messages_and_real_tools_without_internal_history(history):
    db, actor = history
    seen, after = [], None
    while True:
        page = await service.list_items(db, actor, "alice", order="asc", limit=2, after=after)
        seen.extend(page["data"])
        after = page["last_id"]
        if not page["has_more"]:
            break
    assert [item["id"] for item in seen] == ["msg:1", "msg:10", "fc:77", "fo:77", "msg:12", "msg:14"]
    assert seen[1]["content"][1] == {"type": "input_image", "image_url": "data:image/png;base64,YQ=="}
    assert seen[2]["name"] == "read_file" and seen[3]["call_id"] == seen[2]["call_id"]
    assert seen[-1]["content"] == [{"type": "output_text", "text": "final answer"}]
    assert seen[-1]["phase"] == "final_answer" and seen[-2]["phase"] == "commentary"
    assert "PRIVATE" not in str(seen) and "INTERNAL" not in str(seen) and "AUDIT" not in str(seen)
    reverse = await service.list_items(db, actor, "alice", order="desc", limit=100)
    assert [item["id"] for item in reverse["data"]] == [item["id"] for item in reversed(seen)]
    assert await db.scalar(select(func.count()).select_from(PublicSessionEvent)) == 0


@pytest.mark.parametrize("turn_id,after", [(None, "msg:21"), ("turn-1", "fo:77"), (None, "msg:999")])
async def test_item_cursor_cannot_escape_its_session_or_turn(history, turn_id, after):
    db, actor = history
    with pytest.raises(HTTPException) as invalid:
        await service.list_items(db, actor, "alice", turn_id=turn_id, after=after)
    assert invalid.value.status_code == 400


async def test_turn_output_uses_exact_result_run_and_never_previous_answers(history):
    db, actor = history
    resource = await service.turn_resource(db, actor, "alice", "turn-2")
    assert resource["object"] == "agent.session.turn" and resource["status"] == "completed"
    assert resource["yuxi"]["result_run_id"] == "resume"
    assert resource["yuxi"]["output"][0]["id"] == "msg:14"
    assert {run["id"] for run in resource["yuxi"]["runs"]} == {"chat", "resume"}
    (await db.get(AgentRun, "resume")).output_message_id = None
    await db.commit()
    assert (await service.turn_resource(db, actor, "alice", "turn-2"))["yuxi"]["output"] == []
    with pytest.raises(HTTPException) as hidden:
        await service.turn_resource(db, actor, "alice", "bob-turn")
    assert hidden.value.status_code == 404


async def test_session_and_input_queries_keep_scope_and_do_not_dispatch(history):
    db, actor = history
    resource = await service.session_resource(db, actor, "alice")
    assert resource["id"] == "alice" and resource["status"] == "completed"
    assert resource["yuxi"]["current_turn"]["id"] == "turn-2" and resource["yuxi"]["receipt"] is None
    item = await service.input_resource(db, actor, "alice", "input-1")
    assert item["items"][0]["id"] == "msg:10" and item["attachment_status"] == "ready"
    assert item["messages"][0]["content"] == item["items"][0]["content"]
    for other in [
        PublicActor(uid="bob", tenant_id=1),
        PublicActor(uid="alice", tenant_id=2),
        PublicActor(uid="alice", tenant_id=1, app_id="forged"),
    ]:
        with pytest.raises(HTTPException) as denied:
            await service.session_resource(db, other, "alice")
        assert denied.value.status_code == 404
    assert await db.scalar(select(func.count()).select_from(AgentRun)) == 4


async def test_session_and_turn_lists_page_with_scoped_cursors_and_current_state(history):
    db, actor = history
    sessions = await service.list_sessions(db, actor, limit=1)
    assert [row["id"] for row in sessions["data"]] == ["alice"]
    assert sessions["data"][0]["yuxi"]["current_turn"]["id"] == "turn-2"
    assert (await service.list_sessions(db, actor, after="alice"))["data"] == []
    with pytest.raises(HTTPException) as session_cursor:
        await service.list_sessions(db, actor, after="bob")
    assert session_cursor.value.status_code == 400
    turns = await service.list_turns(db, actor, "alice", order="asc", limit=1)
    assert turns["last_id"] == "turn-1" and turns["has_more"]
    assert (await service.list_turns(db, actor, "alice", order="asc", after=turns["last_id"]))["last_id"] == "turn-2"
    with pytest.raises(HTTPException) as turn_cursor:
        await service.list_turns(db, actor, "alice", after="bob-turn")
    assert turn_cursor.value.status_code == 400
    (await db.get(PublicSession, "alice")).archived = True
    await db.commit()
    assert (await service.list_sessions(db, actor))["data"] == []
    assert (await service.session_resource(db, actor, "alice"))["yuxi"]["archived"] is True


async def test_pending_and_cancelled_inputs_never_appear_as_formal_items(history):
    db, actor = history
    pending = PublicInput(
        id="pending",
        session_id="alice",
        received_seq=2,
        kind="follow_up",
        messages=[{"role": "user", "content": [{"type": "input_text", "text": "UNCONSUMED"}]}],
        config_snapshot={},
        attachment_ids=[],
    )
    db.add(pending)
    await db.commit()
    assert (await service.input_resource(db, actor, "alice", pending.id))["items"] == []
    assert (await service.session_resource(db, actor, "alice"))["yuxi"]["queued_input_count"] == 1
    assert "UNCONSUMED" not in str(await service.list_items(db, actor, "alice", limit=100))
    pending.status, pending.cancelled_at = "cancelled", utc_now()
    await db.commit()
    assert (await service.input_resource(db, actor, "alice", pending.id))["items"] == []
    assert (await service.session_resource(db, actor, "alice"))["yuxi"]["queued_input_count"] == 0


async def test_buffered_unvalidated_assistant_text_is_never_published_by_history(history):
    db, actor = history
    run = await db.get(AgentRun, "chat")
    run.input_payload = {"public_guarded_output": True}
    await db.commit()
    assert "progress" not in str(await service.list_items(db, actor, "alice", limit=100))
    run.status = "interrupted"
    run.output_message_id = 12
    turn = await db.get(PublicTurn, "turn-2")
    turn.result_run_id = "chat"
    await db.commit()
    assert (await service.turn_resource(db, actor, "alice", "turn-2"))["yuxi"]["output"] == []
