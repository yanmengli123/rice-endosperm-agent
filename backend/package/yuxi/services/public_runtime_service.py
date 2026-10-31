"""Durable public session recovery, checkpoint cancellation and run publication."""

import asyncio
from copy import deepcopy
from uuid import uuid4

from sqlalchemy import case, or_, select

from yuxi.agents.buildin import agent_manager
from yuxi.services.public_execution_service import dispatch_next_input, refresh_turn
from yuxi.services.public_identity_service import PublicActor
from yuxi.services.public_session_service import append_event, require_session
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import (
    AgentRun,
    PublicInput,
    PublicSession,
    PublicTurn,
    PublicTurnRun,
    Message,
)
from yuxi.storage.postgres.runtime_security import actor as database_actor
from yuxi.utils.datetime_utils import utc_now
from yuxi.utils.logging_config import logger


async def kick_public_session(session_id):
    from yuxi.services.agent_run_service import get_arq_pool

    try:
        queue = await get_arq_pool()
        await queue.enqueue_job("process_public_session", session_id, _job_id=f"public:{session_id}:{uuid4().hex}")
    except Exception as error:
        logger.warning(f"Public session dispatch deferred: {type(error).__name__}")


async def process_public_session(ctx, session_id):
    del ctx
    async with pg_manager.get_async_session_context() as db:
        subject = await db.get(PublicSession, session_id)
        if subject is None:
            return
        principal = PublicActor(subject.uid, subject.tenant_id, subject.app_id)
    with database_actor(principal.tenant_id, principal.uid):
        async with pg_manager.get_async_session_context() as db:
            session = await require_session(db, principal, session_id, lock=True)
            session.recovery_checked_at = utc_now()
            from yuxi.services.public_input_service import prepare_input_files
            from yuxi.storage.postgres.models_business import PublicFile

            preparing = list(
                await db.scalars(
                    select(PublicInput.id)
                    .where(
                        PublicInput.session_id == session_id,
                        PublicInput.status == "pending",
                        PublicInput.id.in_(select(PublicFile.input_id).where(PublicFile.status == "preparing")),
                    )
                    .order_by(PublicInput.received_seq)
                    .limit(100)
                )
            )
            for input_id in preparing:
                try:
                    async with db.begin_nested():
                        await prepare_input_files(db, input_id)
                except Exception as error:
                    logger.warning(f"Public attachment preparation deferred: {type(error).__name__}")
                    item = await db.get(PublicInput, input_id)
                    item.error = {"code": "attachment_preparation_failed", "message": "附件准备失败，等待重试。"}
            finished = list(
                await db.scalars(
                    select(AgentRun)
                    .join(PublicTurnRun, PublicTurnRun.run_id == AgentRun.id)
                    .join(PublicTurn, PublicTurn.id == PublicTurnRun.turn_id)
                    .where(
                        PublicTurnRun.session_id == session.id,
                        or_(
                            PublicTurnRun.published_complete.is_(False),
                            (PublicTurn.cancel_requested.is_(True) & (PublicTurn.current_run_id == AgentRun.id)),
                        ),
                        AgentRun.uid == principal.uid,
                        AgentRun.tenant_id == principal.tenant_id,
                        AgentRun.status.in_(["completed", "failed", "cancelled", "interrupted"]),
                    )
                    .order_by(AgentRun.created_at, AgentRun.id)
                    .limit(100)
                )
            )
            for run in finished:
                binding = await db.get(PublicTurnRun, run.id)
                turn = await db.get(PublicTurn, binding.turn_id)
                if run.output_message_id and binding.last_message_id:
                    message = await db.scalar(
                        select(Message).where(Message.id == run.output_message_id, Message.run_id == run.id)
                    )
                    if message and not (message.extra_metadata or {}).get("id"):
                        message.extra_metadata = {**(message.extra_metadata or {}), "id": binding.last_message_id}
                if turn.current_run_id == run.id:
                    if turn.cancel_requested:
                        await cleanup_cancelled_checkpoint(db, session, turn, run)
                    await refresh_turn(db, session, turn)
                await publish_finished_run(db, principal, session, turn, run)
            await db.commit()
        try:
            async with pg_manager.get_async_session_context() as db:
                run_id = await dispatch_next_input(db, principal, session_id)
                await db.commit()
                if run_id:
                    from yuxi.services.agent_run_service import dispatch_created_agent_run

                    await dispatch_created_agent_run(db=db, run_id=run_id)
        except Exception as error:
            # Acceptance remains valid; expose a bounded category without raw provider errors.
            logger.warning(f"Public input dispatch blocked: {type(error).__name__}")
            async with pg_manager.get_async_session_context() as db:
                session = await require_session(db, principal, session_id, lock=True)
                item = await db.scalar(
                    select(PublicInput)
                    .where(PublicInput.session_id == session_id, PublicInput.status == "pending")
                    .order_by(
                        case(
                            (PublicInput.id == session.priority_input_id, -1), (PublicInput.kind == "steer", 0), else_=1
                        ),
                        PublicInput.received_seq,
                    )
                    .limit(1)
                )
                if item:
                    item.error = {
                        "code": "execution_unavailable",
                        "message": "执行准备失败，恢复扫描将重试；可取消待办。",
                    }


async def cleanup_cancelled_checkpoint(db, session, turn, run):
    from yuxi.agents.checkpoint_cleanup import cancel_checkpoint
    from yuxi.repositories.agent_repository import AgentRepository

    if (run.input_payload or {}).get("agent_runtime") != "dsh":
        definition = await AgentRepository(db).get_by_slug(run.agent_slug)
        backend = agent_manager.get_agent(
            definition.backend_id if definition else session.config_snapshot["backend_id"]
        )
        if backend is None:
            raise RuntimeError("取消清理缺少原 Agent 后端")
        context = backend.context_schema()
        context.update_from_dict(
            {
                **deepcopy(run.input_payload.get("agent_context_snapshot") or {}),
                "uid": run.uid,
                "thread_id": session.id,
                "run_id": run.id,
                "public_session_id": session.id,
                "tool_approval_mode": run.input_payload.get("tool_approval_mode"),
            }
        )
        graph = await backend.get_graph(context=context, checkpoint_only=True)
        config = {"configurable": {"uid": run.uid, "thread_id": session.id}}
        async with asyncio.timeout(20):
            await cancel_checkpoint(graph, config)
        from yuxi.services.chat_service import save_messages_from_langgraph_state
        from yuxi.repositories.conversation_repository import ConversationRepository

        await save_messages_from_langgraph_state(
            backend,
            session.id,
            ConversationRepository(db),
            config,
            context,
            run_id=run.id,
            request_id=run.request_id,
            checkpoint_graph=graph,
        )
    turn.status, turn.waitpoint_json, turn.result_run_id = "cancelled", None, run.id
    turn.finished_at, turn.cancel_requested = utc_now(), False
    binding = await db.get(PublicTurnRun, run.id)
    if binding.published_complete:
        from yuxi.services.public_query_service import turn_resource

        await append_event(
            db,
            session,
            "agent.session.turn.cancelled",
            {
                "turn_id": turn.id,
                "turn": await turn_resource(
                    db, PublicActor(session.uid, session.tenant_id, session.app_id), session.id, turn.id
                ),
            },
        )


async def publish_finished_run(db, principal, session, turn, run):
    binding = await db.get(PublicTurnRun, run.id)
    if not binding or binding.published_complete:
        return
    from yuxi.services.public_query_service import list_items
    from yuxi.services.public_event_service import ensure_item_started

    cursor = None
    while True:
        page = await list_items(db, principal, session.id, turn_id=turn.id, order="asc", limit=100, after=cursor)
        for item in page["data"]:
            if item["yuxi"]["run_id"] != run.id or item.get("role") == "user":
                continue
            index = await ensure_item_started(db, session, turn.id, item["id"], item)
            await append_event(
                db,
                session,
                "agent.session.turn.item.done",
                {"turn_id": turn.id, "item": item, "item_id": item["id"], "output_index": index},
            )
            if item["type"] == "message":
                await append_event(
                    db,
                    session,
                    "agent.session.turn.content_part.done",
                    {
                        "turn_id": turn.id,
                        "item_id": item["id"],
                        "output_index": index,
                        "content_index": 0,
                        "part": item["content"][0],
                    },
                )
                await append_event(
                    db,
                    session,
                    "agent.session.turn.output_text.done",
                    {
                        "turn_id": turn.id,
                        "item_id": item["id"],
                        "output_index": index,
                        "content_index": 0,
                        "text": item["content"][0]["text"],
                    },
                )
        if not page["has_more"]:
            break
        cursor = page["last_id"]
    binding.published_complete = True
    await append_event(
        db, session, "yuxi.session.run.settled", {"turn_id": turn.id, "yuxi": {"run_id": run.id}, "status": run.status}
    )
    if turn.status in {"completed", "failed", "cancelled"} and turn.result_run_id == run.id:
        from yuxi.services.public_query_service import turn_resource

        await append_event(
            db,
            session,
            "agent.session.turn." + turn.status,
            {"turn_id": turn.id, "turn": await turn_resource(db, principal, session.id, turn.id)},
        )


async def recover_public_sessions(ctx=None, *, limit=100):
    async with pg_manager.get_async_session_context() as db:
        active = select(PublicTurn.session_id).where(PublicTurn.status.in_(["in_progress", "cancelling"]))
        pending = select(PublicInput.session_id).where(PublicInput.status == "pending")
        unpublished = (
            select(PublicTurnRun.session_id)
            .join(AgentRun, AgentRun.id == PublicTurnRun.run_id)
            .where(
                PublicTurnRun.published_complete.is_(False),
                AgentRun.status.in_(["completed", "failed", "cancelled", "interrupted"]),
            )
        )
        ids = list(
            await db.scalars(
                select(PublicSession.id)
                .where(
                    or_(PublicSession.id.in_(active), PublicSession.id.in_(pending), PublicSession.id.in_(unpublished))
                )
                .order_by(PublicSession.recovery_checked_at.asc().nulls_first(), PublicSession.id)
                .limit(limit)
            )
        )
    for session_id in ids:
        try:
            await process_public_session(ctx, session_id)
        except Exception as error:
            logger.warning(f"Public session recovery deferred: {type(error).__name__}")
            async with pg_manager.get_async_session_context() as db:
                failed = await db.get(PublicSession, session_id)
                if failed:
                    failed.recovery_checked_at = utc_now()
    return len(ids)
