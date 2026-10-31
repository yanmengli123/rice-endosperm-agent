"""Public SSE facts contain visible output and canonical registered identities."""

import asyncio
import json
import re
import time
from contextvars import ContextVar

from anyio import CancelScope
from fastapi import HTTPException
from sqlalchemy import func, select

from yuxi.services.public_session_service import append_event, require_session
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import PublicSessionEvent, PublicTurnRun

public_run = ContextVar("public_run", default=None)


async def ensure_item_started(db, session, turn_id, item_id, item=None):
    previous = await db.scalar(
        select(PublicSessionEvent)
        .where(
            PublicSessionEvent.session_id == session.id,
            PublicSessionEvent.type == "agent.session.turn.item.added",
            PublicSessionEvent.payload_json["item_id"].as_string() == item_id,
        )
        .limit(1)
    )
    if previous:
        return previous.payload_json["output_index"]
    index = await db.scalar(
        select(func.count())
        .select_from(PublicSessionEvent)
        .where(
            PublicSessionEvent.session_id == session.id,
            PublicSessionEvent.type == "agent.session.turn.item.added",
            PublicSessionEvent.payload_json["turn_id"].as_string() == turn_id,
        )
    )
    await append_event(
        db,
        session,
        "agent.session.turn.item.added",
        {
            "turn_id": turn_id,
            "item_id": item_id,
            "output_index": index,
            "item": item
            or {
                "id": item_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "status": "in_progress",
                "phase": "commentary",
            },
        },
    )
    if item is None or item.get("type") == "message":
        await append_event(
            db,
            session,
            "agent.session.turn.content_part.added",
            {
                "turn_id": turn_id,
                "item_id": item_id,
                "output_index": index,
                "content_index": 0,
                "part": {"type": "output_text", "text": ""},
            },
        )
    return index


async def project_run_event(run, event_type, payload, *, thread_id=None):
    if thread_id and thread_id != run.conversation_thread_id:
        return
    from yuxi.services.public_identity_service import PublicActor

    context = run.input_payload.get("public_context") or {}
    async with pg_manager.get_async_session_context() as db:
        session = await require_session(
            db, PublicActor(run.uid, run.tenant_id, context.get("app_id")), context["session_id"], lock=True
        )
        binding = await db.get(PublicTurnRun, run.id)
        if not binding or binding.session_id != session.id or binding.turn_id != context["turn_id"]:
            raise RuntimeError("公开事件的执行归属不一致")
        if binding.published_complete:
            return
        if event_type == "metadata":
            await append_event(
                db,
                session,
                "agent.session.turn.in_progress",
                {"turn_id": binding.turn_id, "yuxi": {"run_id": run.id}, "status": "in_progress"},
            )
        chunks = payload.get("items", []) if event_type == "messages" else [payload.get("chunk") or {}]
        for chunk in chunks:
            stream = chunk.get("stream_event") or {}
            if stream.get("type") != "message_delta" or not stream.get("content"):
                continue
            raw_id = stream.get("message_id")
            if not isinstance(raw_id, str) or not 0 < len(raw_id) <= 256:
                continue
            binding.last_message_id = raw_id
            item_id = f"msg:{run.id}:{raw_id}"
            index = await ensure_item_started(
                db,
                session,
                binding.turn_id,
                item_id,
                {
                    "id": item_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "status": "in_progress",
                    "turn_id": binding.turn_id,
                    "phase": "commentary",
                    "yuxi": {"run_id": run.id},
                },
            )
            await append_event(
                db,
                session,
                "agent.session.turn.output_text.delta",
                {
                    "turn_id": binding.turn_id,
                    "item_id": item_id,
                    "output_index": index,
                    "content_index": 0,
                    "delta": stream["content"],
                    "yuxi": {"run_id": run.id},
                },
            )


async def validate_cursor(db, principal, session_id, cursor):
    session = await require_session(db, principal, session_id)
    if cursor is None:
        return 0
    match = re.fullmatch(r"v2:([^:]{1,64}):(0|[1-9][0-9]{0,18})", cursor)
    if not match or match[1] != session.id:
        raise HTTPException(422, "SSE 游标无效或不属于当前 Session")
    sequence = int(match[2])
    if sequence >= session.next_event_seq:
        raise HTTPException(422, "SSE 游标超过已提交事件位置")
    return sequence


def sse_frame(event_type, payload, cursor=None):
    body = f"event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n"
    return (f"id: {cursor}\n" if cursor else "") + body + "\n"


async def session_event_stream(request, principal, session_id, *, after=0, max_seconds=1800, poll_seconds=1):
    from server.utils.public_auth import authenticate_public_request

    started, heartbeat, sequence = time.monotonic(), time.monotonic(), after
    while time.monotonic() - started < max_seconds:
        if await request.is_disconnected():
            return
        try:
            # Starlette cancels the response task on disconnect. Finish the bounded DB
            # operation and release its transaction before honoring that cancellation.
            with CancelScope(shield=True):
                async with asyncio.timeout(5), pg_manager.get_async_session_context() as db:
                    authenticated = await authenticate_public_request(request, db)
                    if (authenticated.uid, authenticated.tenant_id, authenticated.app_id) != (
                        principal.uid,
                        principal.tenant_id,
                        principal.app_id,
                    ):
                        raise HTTPException(403, "订阅身份已变化")
                    await require_session(db, authenticated, session_id)
                    events = list(
                        await db.scalars(
                            select(PublicSessionEvent)
                            .where(PublicSessionEvent.session_id == session_id, PublicSessionEvent.sequence > sequence)
                            .order_by(PublicSessionEvent.sequence)
                            .limit(100)
                        )
                    )
        except (HTTPException, TimeoutError) as error:
            yield sse_frame(
                "yuxi.session.error",
                {
                    "type": "yuxi.session.error",
                    "code": getattr(error, "status_code", 503),
                    "message": "订阅暂不可用，请重新验证凭据后连接",
                },
            )
            return
        for event in events:
            sequence = event.sequence
            yield sse_frame(event.type, event.payload_json, f"v2:{session_id}:{sequence}")
        if time.monotonic() - heartbeat >= 15:
            heartbeat = time.monotonic()
            yield f": heartbeat\nid: v2:{session_id}:{sequence}\n\n"
        if not events:
            await asyncio.sleep(poll_seconds)
