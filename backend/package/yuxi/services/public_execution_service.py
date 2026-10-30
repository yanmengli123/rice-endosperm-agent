"""Consume accepted inputs and bind their durable Turn/Run in one transaction."""

from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import case, select

from yuxi.services.agent_run_service import create_agent_run_view
from yuxi.services.input_message_service import build_chat_input_message_from_openai_content
from yuxi.services.principal import resolve_tenant_id
from yuxi.services.public_input_service import prepare_input_files
from yuxi.services.public_session_service import (
    append_event,
    intent_hash,
    receipt_for_key,
    require_session,
    validate_key,
    visible_agent,
)
from yuxi.storage.postgres.models_business import (
    AgentRun,
    AgentRunWaitpoint,
    Message,
    PublicInput,
    PublicReceipt,
    PublicTurn,
    PublicTurnRun,
)
from yuxi.utils.datetime_utils import utc_now


async def latest_turn(db, session_id):
    return await db.scalar(
        select(PublicTurn)
        .where(PublicTurn.session_id == session_id)
        .order_by(PublicTurn.sequence.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )


async def refresh_turn(db, session, turn):
    """Read only committed execution state; a model chunk is not a Turn result."""
    if turn is None or turn.status in {"completed", "failed", "cancelled"}:
        return turn
    run = await db.scalar(
        select(AgentRun)
        .where(
            AgentRun.id == turn.current_run_id,
            AgentRun.uid == session.uid,
            AgentRun.tenant_id == session.tenant_id,
            AgentRun.conversation_thread_id == session.id,
        )
        .execution_options(populate_existing=True)
    )
    if run is None:
        raise RuntimeError("Session Turn 的执行关联丢失")
    previous = (turn.status, turn.waitpoint_json)
    if run.status in {"pending", "running", "cancel_requested"}:
        turn.status = "cancelling" if run.status == "cancel_requested" else "in_progress"
    elif run.status == "interrupted":
        point = await db.get(AgentRunWaitpoint, run.id)
        if point and not point.resolved_by_run_id:
            turn.status = "requires_action"
            approval = point.payload_json.get("source") == "tool_approval"
            turn.waitpoint_json = {
                "id": f"{run.id}:{point.revision}",
                "kind": "approval" if approval else "answer",
                "run_id": run.id,
                **(
                    {"calls": point.payload_json["approval_calls"]}
                    if approval
                    else {"questions": point.payload_json["questions"]}
                ),
            }
        else:
            turn.status = "failed"
    elif run.status in {"completed", "failed", "cancelled"}:
        turn.status = run.status
    else:
        raise RuntimeError("Session Run 的状态无效")
    if turn.status in {"completed", "failed", "cancelled"}:
        turn.finished_at, turn.waitpoint_json = utc_now(), None
        turn.result_run_id = run.id
    if previous != (turn.status, turn.waitpoint_json):
        session.last_active_at = utc_now()
        await append_event(
            db,
            session,
            "yuxi.session.turn.state",
            {
                "session_id": session.id,
                "turn_id": turn.id,
                "status": turn.status,
                "current_run_id": run.id,
                "result_run_id": turn.result_run_id,
                "waitpoint": turn.waitpoint_json,
            },
        )
    return turn


def input_messages(items):
    """Keep each user message and each content part in its original order."""
    messages, parts = [], []
    for item in items:
        for message in item.messages:
            content = [
                {"type": "text", "text": part["text"]}
                if part["type"] == "input_text"
                else {"type": "image_url", "image_url": {"url": part["image_url"]}}
                for part in message["content"]
            ]
            messages.append(build_chat_input_message_from_openai_content(content))
            parts.extend(content)
    return build_chat_input_message_from_openai_content(parts), messages


async def dispatch_next_input(db, actor, session_id):
    """Caller commits, then dispatches the returned run; outbox covers process loss."""
    session = await require_session(db, actor, session_id, lock=True)
    current = await refresh_turn(db, session, await latest_turn(db, session.id))
    if (
        session.archived
        or session.queue_paused
        or (
            current
            and current.status
            in {
                "in_progress",
                "requires_action",
                "cancelling",
            }
        )
    ):
        return None
    if await resolve_tenant_id(db, actor.uid) != session.tenant_id:
        raise HTTPException(403, "Session 的租户归属已变化")
    agent = await visible_agent(db, actor, session.agent_slug)
    queue = select(PublicInput).where(
        PublicInput.session_id == session.id,
        PublicInput.status == "pending",
    )
    first = await db.scalar(
        queue.order_by(
            case((PublicInput.kind == "steer", 0), else_=1),
            PublicInput.received_seq,
        ).limit(1)
    )
    if first is None:
        return None
    pending = (
        list(await db.scalars(queue.where(PublicInput.kind == "steer").order_by(PublicInput.received_seq).limit(20)))
        if first.kind == "steer"
        else [first]
    )
    selected = []
    message_count = 0
    for item in pending:
        if message_count + len(item.messages) > 20:
            break
        if item.attachment_ids:
            await prepare_input_files(db, item.id)
        selected.append(item)
        message_count += len(item.messages)
    snapshot = first.config_snapshot
    if snapshot.get("backend_id", agent.backend_id) != agent.backend_id:
        raise HTTPException(409, "Agent 后端已变更，请取消旧输入后重新提交")
    turn = PublicTurn(id=uuid4().hex, session_id=session.id, sequence=session.next_turn_seq)
    session.next_turn_seq += 1
    db.add(turn)
    await db.flush()
    combined, messages = input_messages(selected)
    result = await create_agent_run_view(
        input_message=combined,
        ordered_input_messages=messages,
        agent_slug=session.agent_slug,
        thread_id=session.id,
        meta={"request_id": first.id, "source": "public_api", "attachment_file_ids": first.attachment_ids},
        current_uid=actor.uid,
        db=db,
        public_session_id=session.id,
        execution_snapshot=snapshot,
        commit_and_dispatch=False,
    )
    run = await db.get(AgentRun, result["run_id"])
    run.input_payload = {
        **run.input_payload,
        "public_context": {
            "session_id": session.id,
            "turn_id": turn.id,
            "input_ids": [item.id for item in selected],
        },
    }
    turn.current_run_id = run.id
    db.add(PublicTurnRun(run_id=run.id, session_id=session.id, turn_id=turn.id))
    stored_messages = list(await db.scalars(select(Message).where(Message.run_id == run.id).order_by(Message.id)))
    offset = 0
    for item in selected:
        item.status, item.turn_id, item.run_id, item.consumed_at = "consumed", turn.id, run.id, utc_now()
        for index, message in enumerate(stored_messages[offset : offset + len(item.messages)]):
            metadata = {**(message.extra_metadata or {}), "public_input_id": item.id, "public_message_index": index}
            metadata.pop("attachment_file_ids", None)
            if index == len(item.messages) - 1 and item.attachment_ids:
                metadata["attachment_file_ids"] = item.attachment_ids
            message.extra_metadata = metadata
        offset += len(item.messages)
    session.last_active_at = utc_now()
    await append_event(
        db,
        session,
        "agent.session.turn.created",
        {
            "session_id": session.id,
            "turn_id": turn.id,
            "yuxi": {"run_id": run.id, "input_ids": [item.id for item in selected]},
        },
    )
    await db.flush()
    return run.id


async def resume_turn(db, actor, session_id, event, *, key):
    """Bind a complete human decision to exactly one pending checkpoint and receipt."""
    import json

    validate_key(key)
    fingerprint = intent_hash(event.model_dump(mode="json"))
    session = await require_session(db, actor, session_id, lock=True)
    previous = await receipt_for_key(db, session, key, fingerprint)
    if previous is not None:
        return previous
    turn = await latest_turn(db, session.id)
    await refresh_turn(db, session, turn)
    if session.archived or not turn or turn.id != event.turn_id or turn.status != "requires_action":
        raise HTTPException(409, "Turn 当前没有待处理的人工输入")
    point = turn.waitpoint_json
    if point["id"] != event.waitpoint_id:
        raise HTTPException(409, "等待点已变更，请重新加载")
    if event.response.type != point["kind"]:
        raise HTTPException(422, "回答类型与等待点不匹配")
    if event.response.type == "approval":
        values = event.response.decisions
        identifiers = [item.call_id for item in values]
        expected = {call["call_id"] for call in point["calls"]}
        answer = {item.call_id: item.decision for item in values}
    else:
        values = event.response.answers
        identifiers = [item.question_id for item in values]
        expected = {question["question_id"] for question in point["questions"]}
        answer = {
            item.question_id: item.answer.model_dump(mode="json") if hasattr(item.answer, "model_dump") else item.answer
            for item in values
        }
    if len(set(identifiers)) != len(identifiers) or set(identifiers) != expected:
        raise HTTPException(422, "回答必须逐项覆盖当前等待点，不能重复或遗漏")
    if len(json.dumps(answer, ensure_ascii=False).encode()) > 65536:
        raise HTTPException(422, "回答超过 64 KiB 上限")
    await visible_agent(db, actor, session.agent_slug)
    parent_id = turn.current_run_id
    result = await create_agent_run_view(
        input_message=None,
        agent_slug=session.agent_slug,
        thread_id=session.id,
        meta={"request_id": intent_hash(["public-resume", session.id, key])},
        current_uid=actor.uid,
        db=db,
        resume=answer,
        created_by_run_id=parent_id,
        public_session_id=session.id,
        commit_and_dispatch=False,
    )
    run = await db.get(AgentRun, result["run_id"])
    run.input_payload = {
        **run.input_payload,
        "public_context": {
            "session_id": session.id,
            "turn_id": turn.id,
            "input_ids": [],
            "resumed_run_id": parent_id,
        },
    }
    turn.current_run_id, turn.status, turn.waitpoint_json = run.id, "in_progress", None
    db.add(PublicTurnRun(run_id=run.id, session_id=session.id, turn_id=turn.id))
    session.last_active_at = utc_now()
    response = {
        "object": "yuxi.session.event.accepted",
        "event_id": uuid4().hex,
        "session_id": session.id,
        "input_id": None,
        "turn_id": turn.id,
        "run_id": run.id,
        "status": "accepted",
    }
    db.add(
        PublicReceipt(
            id=response["event_id"],
            session_id=session.id,
            idempotency_key=key,
            intent_hash=fingerprint,
            event_type=event.type,
            response_json=response,
        )
    )
    await append_event(db, session, "yuxi.session.input.accepted", response)
    await db.flush()
    return response
