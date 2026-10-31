"""Serialize public control commands with immutable receipts and queue ownership."""

from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import func, select

from yuxi.repositories.agent_run_repository import AgentRunRepository
from yuxi.services.public_execution_service import latest_turn, refresh_turn, resume_turn, run_yielded
from yuxi.services.public_session_service import (
    append_event,
    intent_hash,
    receipt_for_key,
    receive_message,
    require_session,
    validate_key,
)
from yuxi.storage.postgres.models_business import PublicInput, PublicReceipt, PublicSession, PublicTurn
from yuxi.utils.datetime_utils import utc_now


async def submit_event(db, actor, session_id, event, *, key):
    validate_key(key)
    fingerprint = intent_hash(event.model_dump(mode="json"))
    if event.type in {"yuxi.session.tree.stop", "yuxi.session.tree.continue"}:
        root = await require_session(db, actor, session_id)
        seen = set()
        while root.parent_session_id:
            if root.id in seen or len(seen) >= 32:
                raise HTTPException(409, "协作会话层级异常")
            seen.add(root.id)
            root = await require_session(db, actor, root.parent_session_id)
        await require_session(db, actor, root.id, lock=True)
    session = await require_session(db, actor, session_id, lock=True)
    previous = await receipt_for_key(db, session, key, fingerprint)
    if previous is not None:
        return previous, False
    current = await refresh_turn(db, session, await latest_turn(db, session.id))
    if event.type == "agent.session.input.message":
        return await receive_message(db, actor, session, event, key=key), True
    if event.type == "yuxi.session.input.resume":
        return await resume_turn(db, actor, session.id, event, key=key), True
    target, run_id = current, None
    if session.archived:
        raise HTTPException(409, "Session 已归档")
    if event.type in {"yuxi.session.input.cancel_input", "yuxi.session.input.promote"}:
        item = await db.scalar(
            select(PublicInput).where(PublicInput.id == event.input_id, PublicInput.session_id == session.id)
        )
        if not item:
            raise HTTPException(404, "Input 不存在")
        if item.status != "pending":
            raise HTTPException(409, "Input 已消费或取消")
        if current and await run_yielded(db, current.current_run_id) and current.status == "in_progress":
            raise HTTPException(409, "正在安全边界接管输入，请稍后查询")
        if event.type.endswith("cancel_input"):
            item.status, item.cancelled_at = "cancelled", utc_now()
            if session.priority_input_id == item.id:
                session.priority_input_id = None
        else:
            if current and current.status in {"requires_action", "cancelling"}:
                raise HTTPException(409, "等待或取消清理期间不能转换输入")
            if item.attachment_ids:
                from yuxi.storage.postgres.models_business import PublicFile

                ready = await db.scalar(
                    select(func.count())
                    .select_from(PublicFile)
                    .where(
                        PublicFile.input_id == item.id,
                        PublicFile.id.in_(item.attachment_ids),
                        PublicFile.status == "ready",
                    )
                )
                if ready != len(item.attachment_ids):
                    raise HTTPException(409, "附件准备完成后才能转换为引导输入")
            item.kind = "steer"
        target = None
    elif event.type == "agent.session.input.cancel":
        if event.yuxi.turn_id:
            target = await db.scalar(
                select(PublicTurn).where(PublicTurn.id == event.yuxi.turn_id, PublicTurn.session_id == session.id)
            )
            if target is None:
                raise HTTPException(404, "Turn 不存在")
            await refresh_turn(db, session, target)
        if target:
            run_id = target.current_run_id
            if event.yuxi.expected_run_id and event.yuxi.expected_run_id != run_id:
                raise HTTPException(409, "当前执行段已变化")
            if target.status not in {"completed", "failed", "cancelled"}:
                target.cancel_requested, target.status, target.waitpoint_json = True, "cancelling", None
                await AgentRunRepository(db).request_cancel(run_id)
                session.queue_paused = bool(
                    await db.scalar(
                        select(func.count())
                        .select_from(PublicInput)
                        .where(PublicInput.session_id == session.id, PublicInput.status == "pending")
                    )
                )
        elif event.yuxi.expected_run_id:
            raise HTTPException(409, "当前没有对应的执行段")
    elif event.type == "yuxi.session.input.continue":
        if current and current.status == "cancelling":
            raise HTTPException(409, "取消清理尚未完成")
        if session.tree_paused:
            raise HTTPException(409, "协作树已停止，请先继续协作树")
        session.queue_paused = False
        target = None
    elif event.type in {"yuxi.session.tree.stop", "yuxi.session.tree.continue"}:
        await control_tree(db, actor, session, event.type.endswith(".stop"))
        target = None
    else:
        raise HTTPException(422, "不支持的控制事件")
    session.last_active_at = utc_now()
    response = {
        "object": "yuxi.session.event.accepted",
        "event_id": uuid4().hex,
        "session_id": session.id,
        "input_id": None,
        "turn_id": target.id if target else None,
        "run_id": run_id,
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
    return response, True


async def control_tree(db, actor, session, stopping):
    # Lock the same root session before collecting its descendants.
    root = session
    seen = set()
    while root.parent_session_id:
        if root.id in seen or len(seen) >= 32:
            raise HTTPException(409, "协作会话层级异常")
        seen.add(root.id)
        root = await require_session(db, actor, root.parent_session_id, lock=True)
    descendants = select(PublicSession.id).where(PublicSession.id == root.id).cte("public_tree", recursive=True)
    descendants = descendants.union(
        select(PublicSession.id)
        .join(descendants, PublicSession.parent_session_id == descendants.c.id)
        .where(
            PublicSession.uid == actor.uid,
            PublicSession.tenant_id == actor.tenant_id,
            PublicSession.app_id == actor.app_id,
        )
    )
    sessions = list(
        await db.scalars(
            select(PublicSession)
            .where(
                PublicSession.id.in_(select(descendants.c.id)),
                PublicSession.uid == actor.uid,
                PublicSession.tenant_id == actor.tenant_id,
                PublicSession.app_id == actor.app_id,
            )
            .order_by(PublicSession.id)
            .with_for_update()
        )
    )
    for subject in sessions:
        turn = await refresh_turn(db, subject, await latest_turn(db, subject.id))
        if not stopping and turn and turn.status == "cancelling":
            raise HTTPException(409, "协作树取消清理尚未完成")
        subject.tree_paused = subject.queue_paused = stopping
        if stopping and turn and turn.status not in {"completed", "failed", "cancelled"}:
            turn.cancel_requested, turn.status, turn.waitpoint_json = True, "cancelling", None
            await AgentRunRepository(db).request_cancel(turn.current_run_id)
    if stopping:
        from yuxi.services.collaboration_service import stop_group

        first = await latest_turn(db, root.id)
        if first:
            await stop_group(db, uid=actor.uid, run_id=first.current_run_id, commit=False)
