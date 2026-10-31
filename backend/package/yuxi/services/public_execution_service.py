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
    visible_session_agent,
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
    binding = await db.get(PublicTurnRun, run.id)
    if turn.cancel_requested:
        turn.status, turn.waitpoint_json = "cancelling", None
    elif binding and binding.yielded and run.status == "interrupted":
        turn.status, turn.waitpoint_json = "in_progress", None
    elif run.status in {"pending", "running", "cancel_requested"}:
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
        if turn.status == "requires_action":
            from yuxi.services.public_query_service import turn_resource
            from yuxi.services.public_identity_service import PublicActor

            kind = (
                "yuxi.session.turn.waiting" if turn.status == "requires_action" else "agent.session.turn." + turn.status
            )
            await append_event(
                db,
                session,
                kind,
                {
                    "turn_id": turn.id,
                    "turn": await turn_resource(
                        db, PublicActor(session.uid, session.tenant_id, session.app_id), session.id, turn.id
                    ),
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
    continuation = None
    if current and current.status == "in_progress":
        previous_run = await db.get(AgentRun, current.current_run_id)
        if previous_run.status == "interrupted" and await run_yielded(db, previous_run.id):
            continuation = previous_run
    if (
        session.archived
        or session.queue_paused
        or session.tree_paused
        or (current and current.status in {"in_progress", "requires_action", "cancelling"} and continuation is None)
    ):
        return None
    if await resolve_tenant_id(db, actor.uid) != session.tenant_id:
        raise HTTPException(403, "Session 的租户归属已变化")
    agent = await visible_session_agent(db, actor, session)
    queue = select(PublicInput).where(
        PublicInput.session_id == session.id,
        PublicInput.status == "pending",
    )
    if continuation is not None:
        queue = queue.where(PublicInput.kind == "steer")
    if session.priority_input_id:
        queue = queue.where(PublicInput.id == session.priority_input_id)
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
        if first.kind == "steer" and not session.priority_input_id
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
    if continuation is not None:
        payload = continuation.input_payload
        snapshot = {
            **snapshot,
            "agent_config": {"context": payload["agent_context_snapshot"]},
            "model_spec": payload["model_spec"],
            "tool_approval_mode": payload["tool_approval_mode"],
            "knowledge_scope_snapshot": payload["knowledge_scope_snapshot"],
        }
    if snapshot.get("backend_id", agent.backend_id) != agent.backend_id:
        raise HTTPException(409, "Agent 后端已变更，请取消旧输入后重新提交")
    turn = (
        current
        if continuation is not None
        else PublicTurn(id=uuid4().hex, session_id=session.id, sequence=session.next_turn_seq)
    )
    if continuation is None:
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
        public_continuation_of=continuation.id if continuation else None,
        agent_kind="subagent" if session.parent_session_id else "main",
    )
    run = await db.get(AgentRun, result["run_id"])
    run.input_payload = {
        **run.input_payload,
        "public_context": {
            "session_id": session.id,
            "app_id": session.app_id,
            "turn_id": turn.id,
            "input_ids": [item.id for item in selected],
        },
    }
    if session.parent_session_id:
        run.input_payload = {**run.input_payload, "runtime": snapshot["runtime"]}
    turn.current_run_id = run.id
    db.add(PublicTurnRun(run_id=run.id, session_id=session.id, turn_id=turn.id))
    stored_messages = list(await db.scalars(select(Message).where(Message.run_id == run.id).order_by(Message.id)))
    offset = 0
    for item in selected:
        item.error = None
        item.status, item.turn_id, item.run_id, item.consumed_at = "consumed", turn.id, run.id, utc_now()
        for index, message in enumerate(stored_messages[offset : offset + len(item.messages)]):
            metadata = {**(message.extra_metadata or {}), "public_input_id": item.id, "public_message_index": index}
            metadata.pop("attachment_file_ids", None)
            if index == len(item.messages) - 1 and item.attachment_ids:
                metadata["attachment_file_ids"] = item.attachment_ids
            message.extra_metadata = metadata
        offset += len(item.messages)
    session.last_active_at = utc_now()
    session.priority_input_id = None
    await append_event(
        db,
        session,
        "yuxi.session.run.queued" if continuation else "agent.session.turn.created",
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
    await visible_session_agent(db, actor, session)
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
        agent_kind="subagent" if session.parent_session_id else "main",
    )
    run = await db.get(AgentRun, result["run_id"])
    run.input_payload = {
        **run.input_payload,
        "public_context": {
            "session_id": session.id,
            "turn_id": turn.id,
            "input_ids": [],
            "resumed_run_id": parent_id,
            "app_id": session.app_id,
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


async def run_yielded(db, run_id):
    item = await db.get(PublicTurnRun, run_id)
    return bool(item and item.yielded)


async def mark_steer_boundary(db, *, run_id, uid):
    from yuxi.services.public_identity_service import PublicActor
    from yuxi.storage.postgres.models_business import PublicSession

    binding = await db.get(PublicTurnRun, run_id)
    if binding is None:
        return False
    subject = await db.get(PublicSession, binding.session_id)
    if subject is None or subject.uid != uid:
        return False
    session = await require_session(db, PublicActor(uid, subject.tenant_id, subject.app_id), subject.id, lock=True)
    turn = await db.get(PublicTurn, binding.turn_id)
    if (
        session.queue_paused
        or session.tree_paused
        or turn.cancel_requested
        or turn.current_run_id != run_id
        or turn.status != "in_progress"
    ):
        return False
    pending = await db.scalar(
        select(PublicInput.id)
        .where(
            PublicInput.session_id == session.id,
            PublicInput.kind == "steer",
            PublicInput.status == "pending",
        )
        .limit(1)
    )
    if pending is None:
        return False
    binding.yielded = True
    await db.flush()
    return True


async def assert_continuation_allowed(db, *, uid, run_id):
    from yuxi.services.collaboration_service import root_run, members, _stopped_scopes, _is_stopped

    root = await root_run(db, uid=uid, run_id=run_id, lock=True)
    group = {item.id: item for item in await members(db, root)}
    if _is_stopped(run_id, group, await _stopped_scopes(db, root)):
        raise HTTPException(409, "协作已停止，不能接续旧运行")
    binding = await db.get(PublicTurnRun, run_id)
    previous = await db.get(AgentRun, run_id)
    turn = await db.get(PublicTurn, binding.turn_id) if binding else None
    if (
        not binding
        or not binding.yielded
        or previous.status != "interrupted"
        or not turn
        or turn.cancel_requested
        or turn.current_run_id != run_id
    ):
        raise HTTPException(409, "引导接续状态已变化")


async def lock_public_parent(db, creator):
    from types import SimpleNamespace
    from sqlalchemy import func
    from yuxi.services.collaboration_service import freeze_collaboration_limits
    from yuxi.services.public_identity_service import PublicActor
    from yuxi.storage.postgres.models_business import PublicSession

    context = creator.input_payload["public_context"]
    actor = PublicActor(creator.uid, creator.tenant_id, context.get("app_id"))
    parent = await require_session(db, actor, context["session_id"])
    root, seen = parent, set()
    while root.parent_session_id:
        if root.id in seen or len(seen) >= 32:
            raise HTTPException(409, "协作会话层级异常")
        seen.add(root.id)
        root = await require_session(db, actor, root.parent_session_id)
    await require_session(db, actor, root.id, lock=True)
    parent = await require_session(db, actor, parent.id, lock=True)
    if root.tree_paused or parent.tree_paused:
        raise HTTPException(409, "协作树已停止，不能创建子任务")
    limits = freeze_collaboration_limits(SimpleNamespace(config_json=root.config_snapshot["agent_config"]))
    if len(seen) >= limits["max_depth"]:
        raise HTTPException(409, "已达到协作层级上限")
    tree = select(PublicSession.id).where(PublicSession.id == root.id).cte("public_spawn_tree", recursive=True)
    tree = tree.union(
        select(PublicSession.id)
        .join(tree, PublicSession.parent_session_id == tree.c.id)
        .where(
            PublicSession.uid == actor.uid,
            PublicSession.tenant_id == actor.tenant_id,
            PublicSession.app_id == actor.app_id,
        )
    )
    active = await db.scalar(
        select(func.count())
        .select_from(PublicTurn)
        .join(AgentRun, AgentRun.id == PublicTurn.current_run_id)
        .where(
            PublicTurn.session_id.in_(select(tree.c.id)),
            PublicTurn.session_id != root.id,
            AgentRun.status.in_(["pending", "running", "cancel_requested"]),
        )
    )
    if active >= limits["max_concurrent"]:
        raise HTTPException(409, "已达到协作树并发子任务上限")
    await visible_session_agent(db, actor, parent)
    return parent


async def register_public_child(db, creator, run, definition):
    """Register the child's own Session/Turn before its dispatch outbox is committed."""
    from copy import deepcopy
    from yuxi.services.public_identity_service import PublicActor
    from yuxi.storage.postgres.models_business import Conversation, PublicSession

    context = creator.input_payload["public_context"]
    actor = PublicActor(run.uid, run.tenant_id, context.get("app_id"))
    parent = await require_session(db, actor, context["session_id"])
    session = await db.get(PublicSession, run.conversation_thread_id)
    snapshot = {
        "backend_id": definition.backend_id,
        "agent_config": deepcopy(definition.config_json or {}),
        "model_policy": definition.model_policy,
        "model_spec": run.input_payload["model_spec"],
        "knowledge_scope_snapshot": deepcopy(run.input_payload["knowledge_scope_snapshot"]),
        "tool_approval_mode": creator.input_payload.get("tool_approval_mode", "always_trust"),
        "runtime": deepcopy(run.input_payload["runtime"]),
    }
    if session is None:
        conversation = await db.get(Conversation, run.conversation_id)
        conversation.public_session = True
        session = PublicSession(
            id=run.conversation_thread_id,
            uid=run.uid,
            tenant_id=run.tenant_id,
            app_id=parent.app_id,
            parent_session_id=parent.id,
            agent_slug=run.agent_slug,
            creation_key="child:" + run.conversation_thread_id,
            creation_hash=intent_hash([parent.id, run.agent_slug]),
            title=definition.name,
            config_snapshot=snapshot,
        )
        db.add(session)
        await db.flush()
        await append_event(db, session, "agent.session.created", {"session_id": session.id})
    elif (session.uid, session.tenant_id, session.app_id, session.parent_session_id) != (
        parent.uid,
        parent.tenant_id,
        parent.app_id,
        parent.id,
    ):
        raise HTTPException(409, "子会话归属不一致")
    previous = await refresh_turn(db, session, await latest_turn(db, session.id))
    if previous and previous.status in {"in_progress", "requires_action", "cancelling"}:
        raise HTTPException(409, "子会话仍有在途工作")
    turn = PublicTurn(id=uuid4().hex, session_id=session.id, sequence=session.next_turn_seq, current_run_id=run.id)
    session.next_turn_seq += 1
    db.add(turn)
    await db.flush()
    db.add(PublicTurnRun(run_id=run.id, session_id=session.id, turn_id=turn.id))
    run.input_payload = {
        **run.input_payload,
        "agent_context_snapshot": snapshot["agent_config"].get("context", {}),
        "collaboration_limits": creator.input_payload["collaboration_limits"],
        "tool_approval_mode": snapshot["tool_approval_mode"],
        "public_context": {"session_id": session.id, "app_id": session.app_id, "turn_id": turn.id, "input_ids": []},
    }
    await append_event(
        db,
        parent,
        "yuxi.session.child.created",
        {"child_session_id": session.id, "turn_id": turn.id, "yuxi": {"run_id": run.id}},
    )
    await append_event(db, session, "agent.session.turn.created", {"turn_id": turn.id, "yuxi": {"run_id": run.id}})
    await db.flush()
