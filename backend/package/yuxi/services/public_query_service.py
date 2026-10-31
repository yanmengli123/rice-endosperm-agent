"""Bounded public projections of registered Session work, without execution side effects."""

from datetime import UTC

from fastapi import HTTPException
from sqlalchemy import String, and_, case, cast, func, literal, or_, select, tuple_, union_all

from yuxi.services.public_execution_service import latest_turn
from yuxi.services.public_session_service import require_session
from yuxi.storage.postgres.models_business import (
    AgentRun,
    Message,
    PublicFile,
    PublicInput,
    PublicSession,
    PublicTurn,
    PublicTurnRun,
    ToolCall,
    UsageLedger,
)
from yuxi.utils.reasoning_visibility import sanitize_visible_text


def unix_time(value):
    if value is None:
        return None
    return int((value.replace(tzinfo=UTC) if value.tzinfo is None else value).timestamp())


def page_response(data, *, has_more=False):
    return {
        "object": "list",
        "data": data,
        "first_id": data[0]["id"] if data else None,
        "last_id": data[-1]["id"] if data else None,
        "has_more": has_more,
    }


def public_status(status):
    return "in_progress" if status == "cancelling" else status


async def session_resource(db, actor, session_id, *, receipt=None):
    session = await require_session(db, actor, session_id)
    turn = await latest_turn(db, session.id)
    count = await db.scalar(
        select(func.count())
        .select_from(PublicInput)
        .where(
            PublicInput.session_id == session.id,
            PublicInput.status == "pending",
        )
    )
    return session_projection(session, turn, count, receipt=receipt)


def session_projection(session, turn, queued, *, receipt=None):
    return {
        "id": session.id,
        "object": "agent.session",
        "agent": {"id": session.agent_slug, "model": session.config_snapshot["model_spec"]},
        "created_at": unix_time(session.created_at),
        "last_active_at": unix_time(session.last_active_at),
        "status": public_status(turn.status) if turn else "idle",
        "yuxi": {
            "project_id": session.config_snapshot.get("project_id") or session.id,
            "title": session.title,
            "archived": session.archived,
            "is_pinned": session.is_pinned,
            "tool_approval_mode": session.config_snapshot["tool_approval_mode"],
            "queue_paused": session.queue_paused,
            "tree_paused": session.tree_paused,
            "parent_session_id": session.parent_session_id,
            "has_unread_events": session.next_event_seq - 1 > session.read_event_seq,
            "unread": session.next_event_seq - 1 > session.read_event_seq,
            "queued_input_count": queued,
            "receipt": receipt,
            "current_turn": {
                "id": turn.id,
                "status": public_status(turn.status),
                "current_run_id": turn.current_run_id,
                "result_run_id": turn.result_run_id,
                "waitpoint": turn.waitpoint_json,
            }
            if turn
            else None,
        },
    }


async def list_sessions(
    db, actor, *, after=None, limit=50, order="desc", archived=False, agent_id=None, is_pinned=None
):
    if not 1 <= limit <= 100 or order not in {"asc", "desc"}:
        raise HTTPException(422, "分页参数无效")
    scoped = select(PublicSession).where(
        PublicSession.uid == actor.uid,
        PublicSession.tenant_id == actor.tenant_id,
        PublicSession.app_id == actor.app_id,
        PublicSession.archived.is_(archived),
    )
    if agent_id is not None:
        scoped = scoped.where(PublicSession.agent_slug == agent_id)
    if is_pinned is not None:
        scoped = scoped.where(PublicSession.is_pinned.is_(is_pinned))
    stmt = scoped
    if after:
        cursor = await db.scalar(scoped.where(PublicSession.id == after))
        if cursor is None:
            raise HTTPException(400, "Session 游标不属于当前作用域")
        columns, values = tuple_(PublicSession.created_at, PublicSession.id), (cursor.created_at, cursor.id)
        stmt = stmt.where(columns > values if order == "asc" else columns < values)
    columns = (PublicSession.created_at, PublicSession.id)
    rows = list(
        await db.scalars(
            stmt.order_by(*(col.asc() if order == "asc" else col.desc() for col in columns)).limit(limit + 1)
        )
    )
    selected = rows[:limit]
    ids = [row.id for row in selected]
    ranked = (
        select(
            PublicTurn.id,
            func.row_number()
            .over(
                partition_by=PublicTurn.session_id,
                order_by=PublicTurn.sequence.desc(),
            )
            .label("rank"),
        )
        .where(PublicTurn.session_id.in_(ids))
        .subquery()
    )
    turns = {
        row.session_id: row
        for row in await db.scalars(
            select(PublicTurn).join(ranked, ranked.c.id == PublicTurn.id).where(ranked.c.rank == 1)
        )
    }
    counts = dict(
        (
            await db.execute(
                select(PublicInput.session_id, func.count())
                .where(
                    PublicInput.session_id.in_(ids),
                    PublicInput.status == "pending",
                )
                .group_by(PublicInput.session_id)
            )
        ).all()
    )
    return page_response(
        [session_projection(row, turns.get(row.id), counts.get(row.id, 0)) for row in selected],
        has_more=len(rows) > limit,
    )


async def list_turns(db, actor, session_id, *, after=None, limit=20, order="desc"):
    if not 1 <= limit <= 100 or order not in {"asc", "desc"}:
        raise HTTPException(422, "分页参数无效")
    session = await require_session(db, actor, session_id)
    stmt = select(PublicTurn).where(PublicTurn.session_id == session.id)
    if after:
        cursor = await db.scalar(stmt.where(PublicTurn.id == after))
        if cursor is None:
            raise HTTPException(400, "Turn 游标不属于当前作用域")
        stmt = stmt.where(
            PublicTurn.sequence > cursor.sequence if order == "asc" else PublicTurn.sequence < cursor.sequence
        )
    rows = list(
        await db.scalars(
            stmt.order_by(PublicTurn.sequence.asc() if order == "asc" else PublicTurn.sequence.desc()).limit(limit + 1)
        )
    )
    return page_response([turn_projection(turn, session) for turn in rows[:limit]], has_more=len(rows) > limit)


def turn_projection(turn, session):
    return {
        "id": turn.id,
        "object": "agent.session.turn",
        "session_id": session.id,
        "agent_id": session.agent_slug,
        "subagent_id": None,
        "status": public_status(turn.status),
        "created_at": unix_time(turn.created_at),
        "started_at": unix_time(turn.created_at),
        "completed_at": unix_time(turn.finished_at),
        "usage": None,
        "error": {"code": "run_failed", "message": "本轮执行失败，请检查配置后重试。"}
        if turn.status == "failed"
        else None,
        "yuxi": {
            "current_run_id": turn.current_run_id,
            "result_run_id": turn.result_run_id,
            "waitpoint": turn.waitpoint_json,
            "cancellation_pending": turn.status == "cancelling",
        },
    }


async def require_turn(db, session, turn_id):
    turn = await db.scalar(select(PublicTurn).where(PublicTurn.session_id == session.id, PublicTurn.id == turn_id))
    if turn is None:
        raise HTTPException(404, "Turn 不存在")
    return turn


def message_item(message, *, turn, run, input_content=None):
    assistant = message.role == "assistant"
    text = sanitize_visible_text(message.content) if assistant else None
    metadata = message.extra_metadata or {}
    if (
        text
        and metadata.get("presentation_mode") == "MCP_VALUE_ONLY"
        and (metadata.get("source_output_guard") or {}).get("status") != "SKIPPED"
    ):
        from yuxi.knowledge.rendering.source_answer_renderer import render_source_answer

        text = render_source_answer(text)
    content = [{"type": "output_text", "text": text}] if assistant else input_content
    return {
        "id": public_message_id(message),
        "type": "message",
        "role": message.role,
        "status": "completed" if message.delivery_status == "complete" else "incomplete",
        "turn_id": turn.id,
        "content": content,
        "phase": (
            "final_answer" if turn.result_run_id == run.id and run.output_message_id == message.id else "commentary"
        )
        if assistant
        else None,
        "yuxi": {"run_id": run.id, "message_id": message.id, "created_at": unix_time(message.created_at)},
    }


def public_message_id(message):
    raw = (message.extra_metadata or {}).get("id")
    if message.role == "assistant" and isinstance(raw, str) and 0 < len(raw) <= 256:
        return f"msg:{message.run_id}:{raw}"
    return f"msg:{message.id}"


def user_content(message):
    from yuxi.services.input_message_service import restore_chat_input_message

    raw = restore_chat_input_message(
        content=message.content,
        image_content=message.image_content,
        metadata=message.extra_metadata or {},
    ).require_langchain_message()
    parts = [{"type": "text", "text": raw.content}] if isinstance(raw.content, str) else raw.content
    return [
        {"type": "input_text", "text": part["text"]}
        if part["type"] == "text"
        else {"type": "input_image", "image_url": part["image_url"]["url"]}
        for part in parts
    ]


async def turn_resource(db, actor, session_id, turn_id):
    session = await require_session(db, actor, session_id)
    turn = await require_turn(db, session, turn_id)
    runs = list(
        await db.scalars(
            select(AgentRun)
            .join(PublicTurnRun, PublicTurnRun.run_id == AgentRun.id)
            .where(
                PublicTurnRun.session_id == session.id,
                PublicTurnRun.turn_id == turn.id,
                AgentRun.uid == actor.uid,
                AgentRun.tenant_id == actor.tenant_id,
                AgentRun.conversation_thread_id == session.id,
            )
            .order_by(AgentRun.created_at, AgentRun.id)
        )
    )
    by_id = {run.id: run for run in runs}
    result = by_id.get(turn.result_run_id)
    output = []
    if result and result.status == "completed" and result.output_message_id:
        message = await db.scalar(
            select(Message).where(
                Message.id == result.output_message_id,
                Message.run_id == result.id,
                Message.conversation_id == result.conversation_id,
                Message.role == "assistant",
            )
        )
        if message:
            output.append(message_item(message, turn=turn, run=result))
    totals = (
        await db.execute(
            select(
                func.coalesce(func.sum(UsageLedger.input_tokens), 0),
                func.coalesce(func.sum(UsageLedger.output_tokens), 0),
                func.coalesce(func.sum(UsageLedger.total_tokens), 0),
            ).where(
                UsageLedger.run_id.in_(by_id), UsageLedger.uid == actor.uid, UsageLedger.tenant_id == actor.tenant_id
            )
        )
    ).one()
    response = turn_projection(turn, session)
    response["yuxi"].update(
        {
            "output": output,
            "runs": [
                {
                    "id": run.id,
                    "status": run.status,
                    "created_at": unix_time(run.created_at),
                    "completed_at": unix_time(run.finished_at),
                }
                for run in runs
            ],
            "usage": dict(zip(("input_tokens", "output_tokens", "total_tokens"), map(int, totals), strict=True)),
        }
    )
    return response


async def input_resource(db, actor, session_id, input_id):
    session = await require_session(db, actor, session_id)
    item = await db.scalar(select(PublicInput).where(PublicInput.id == input_id, PublicInput.session_id == session.id))
    if item is None:
        raise HTTPException(404, "Input 不存在")
    files = (
        list(
            await db.scalars(
                select(PublicFile).where(
                    PublicFile.input_id == item.id,
                    PublicFile.uid == actor.uid,
                    PublicFile.tenant_id == actor.tenant_id,
                    PublicFile.app_id == actor.app_id,
                )
            )
        )
        if item.attachment_ids
        else []
    )
    by_file_id = {file.id: file for file in files}
    files = [by_file_id[file_id] for file_id in item.attachment_ids if file_id in by_file_id]
    items = []
    if item.status == "consumed":
        turn = await require_turn(db, session, item.turn_id)
        run = await db.scalar(
            select(AgentRun)
            .join(PublicTurnRun, PublicTurnRun.run_id == AgentRun.id)
            .where(
                AgentRun.id == item.run_id,
                AgentRun.uid == actor.uid,
                AgentRun.tenant_id == actor.tenant_id,
                AgentRun.conversation_thread_id == session.id,
                PublicTurnRun.session_id == session.id,
                PublicTurnRun.turn_id == turn.id,
            )
        )
        if run is None:
            raise RuntimeError("Input 的执行关联丢失")
        messages = list(
            await db.scalars(
                select(Message)
                .where(
                    Message.run_id == run.id,
                    Message.conversation_id == run.conversation_id,
                    Message.role == "user",
                    Message.extra_metadata["public_input_id"].as_string() == item.id,
                )
                .order_by(Message.id)
                .limit(20)
            )
        )
        items = [message_item(message, turn=turn, run=run, input_content=user_content(message)) for message in messages]
    return {
        "input_id": item.id,
        "thread_id": session.id,
        "kind": item.kind,
        "status": item.status,
        "turn_id": item.turn_id,
        "run_id": item.run_id,
        "received_seq": item.received_seq,
        "messages": item.messages,
        "content": "\n".join(
            part["text"] for message in item.messages for part in message["content"] if part["type"] == "input_text"
        ),
        "items": items,
        "attachment_file_ids": item.attachment_ids,
        "received_at": item.created_at,
        "consumed_at": item.consumed_at,
        "cancelled_at": item.cancelled_at,
        "attachment_status": "ready"
        if len(files) == len(item.attachment_ids) and all(file.status == "ready" for file in files)
        else "preparing",
        "attachment_error": "附件准备失败，等待重试。" if any(file.error for file in files) else None,
        "attachments": [{"file_id": file.id, "file_name": file.filename, "status": file.status} for file in files],
        "error": item.error,
    }


async def list_items(db, actor, session_id, *, turn_id=None, after=None, limit=20, order="desc"):
    if not 1 <= limit <= 100 or order not in {"asc", "desc"}:
        raise HTTPException(422, "分页参数无效")
    session = await require_session(db, actor, session_id)
    if turn_id:
        await require_turn(db, session, turn_id)
    scope = (
        select(PublicTurnRun.run_id)
        .join(AgentRun, AgentRun.id == PublicTurnRun.run_id)
        .where(
            PublicTurnRun.session_id == session.id,
            AgentRun.uid == actor.uid,
            AgentRun.tenant_id == actor.tenant_id,
            AgentRun.conversation_thread_id == session.id,
        )
    )
    if turn_id:
        scope = scope.where(PublicTurnRun.turn_id == turn_id)
    base = select(Message.id.label("message_id"), Message.run_id.label("run_id")).join(
        AgentRun, (AgentRun.id == Message.run_id) & (AgentRun.conversation_id == Message.conversation_id)
    )
    eligible = [Message.run_id.in_(scope)]
    raw_id = Message.extra_metadata["id"].as_string()
    message_id = case(
        (
            and_(Message.role == "assistant", raw_id.is_not(None), func.length(raw_id) > 0, func.length(raw_id) <= 256),
            literal("msg:") + Message.run_id + literal(":") + raw_id,
        ),
        else_=literal("msg:") + cast(Message.id, String),
    )
    messages = base.add_columns(literal(0).label("tool_id"), literal(0).label("kind"), message_id.label("id")).where(
        *eligible,
        Message.role.in_(["user", "assistant"]),
        or_(
            Message.role == "user",
            and_(
                AgentRun.status == "completed",
                or_(
                    func.coalesce(AgentRun.input_payload["public_guarded_output"].as_boolean(), False).is_(False),
                    Message.id == AgentRun.output_message_id,
                ),
            ),
        ),
        (Message.role == "user") | (Message.content != ""),
        func.coalesce(Message.extra_metadata["source"].as_string(), "") != "ask_user_question_resume",
    )
    tools = base.join(ToolCall, ToolCall.message_id == Message.id).where(*eligible)
    candidates = union_all(
        messages,
        tools.add_columns(
            ToolCall.id.label("tool_id"),
            literal(1).label("kind"),
            (literal("fc:") + cast(ToolCall.id, String)).label("id"),
        ),
        tools.add_columns(
            ToolCall.id.label("tool_id"),
            literal(2).label("kind"),
            (literal("fo:") + cast(ToolCall.id, String)).label("id"),
        ),
    ).subquery()
    stmt = select(candidates)
    columns = (candidates.c.message_id, candidates.c.tool_id, candidates.c.kind)
    if after:
        cursor = (await db.execute(select(candidates).where(candidates.c.id == after))).first()
        if cursor is None:
            raise HTTPException(400, "Items 游标不属于当前作用域")
        value = (cursor.message_id, cursor.tool_id, cursor.kind)
        stmt = stmt.where(tuple_(*columns) > value if order == "asc" else tuple_(*columns) < value)
    stmt = stmt.order_by(*(column.asc() if order == "asc" else column.desc() for column in columns)).limit(limit + 1)
    selected = list((await db.execute(stmt)).all())
    rows = selected[:limit]
    message_rows = {
        row.id: row
        for row in await db.scalars(
            select(Message).where(
                Message.id.in_({item.message_id for item in rows}),
                Message.run_id.in_(scope),
            )
        )
    }
    tool_rows = {
        row.id: row
        for row in await db.scalars(
            select(ToolCall).where(
                ToolCall.id.in_({item.tool_id for item in rows if item.tool_id}),
                ToolCall.message_id.in_(message_rows),
            )
        )
    }
    runs = {
        row.id: row for row in await db.scalars(select(AgentRun).where(AgentRun.id.in_({item.run_id for item in rows})))
    }
    turns = {
        row.id: row
        for row in await db.scalars(
            select(PublicTurn).where(
                PublicTurn.session_id == session.id,
                PublicTurn.id.in_(
                    select(PublicTurnRun.turn_id).where(
                        PublicTurnRun.run_id.in_(runs),
                        PublicTurnRun.session_id == session.id,
                    )
                ),
            )
        )
    }
    mapping = {
        row.run_id: row.turn_id
        for row in await db.scalars(
            select(PublicTurnRun).where(
                PublicTurnRun.run_id.in_(runs),
                PublicTurnRun.session_id == session.id,
            )
        )
    }
    data = []
    for row in rows:
        message, run, turn = message_rows[row.message_id], runs[row.run_id], turns[mapping[row.run_id]]
        if row.kind == 0:
            content = user_content(message) if message.role == "user" else None
            data.append(message_item(message, turn=turn, run=run, input_content=content))
        else:
            call = tool_rows[row.tool_id]
            item = {
                "id": row.id,
                "type": "function_call" if row.kind == 1 else "function_call_output",
                "call_id": call.langgraph_tool_call_id or f"tool:{call.id}",
                "turn_id": turn.id,
                "status": "in_progress" if call.status == "pending" else "completed",
                "yuxi": {"run_id": run.id, "message_id": message.id, "tool_status": call.status},
            }
            item.update(
                {"name": call.tool_name, "arguments": call.tool_input or {}}
                if row.kind == 1
                else {"output": sanitize_visible_text(call.tool_output) if call.tool_output else None}
            )
            data.append(item)
    response = page_response(data, has_more=len(selected) > limit)
    response["yuxi"] = {"runs": [{"id": run.id, "status": run.status} for run in runs.values()]}
    return response
