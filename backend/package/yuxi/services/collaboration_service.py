"""Durable run-tree messages, consumed only at model execution boundaries."""

from uuid import uuid4
import asyncio

from fastapi import HTTPException
from langchain_core.messages import HumanMessage
from sqlalchemy import func, select

from yuxi.services.principal import resolve_tenant_id
from yuxi.storage.postgres.models_business import AgentRun, AgentRunWaitpoint, CollaborationMessage, CollaborationStop
from yuxi.utils.datetime_utils import format_utc_datetime, utc_now

ACTIVE = {"pending", "running", "interrupted"}
MAX_MEMBERS = 256
MAX_MESSAGES = 100


def freeze_collaboration_limits(agent):
    from yuxi.services.subagent_run_service import resolve_max_concurrent_subagent_runs

    config = getattr(agent, "config_json", None) or {}
    context = config.get("context") if isinstance(config, dict) else {}
    context = context if isinstance(context, dict) else {}
    try:
        depth = max(1, min(4, int(context.get("max_collaboration_depth", 1))))
    except (TypeError, ValueError, OverflowError):
        depth = 1
    return {"version": 1, "max_depth": depth, "max_concurrent": resolve_max_concurrent_subagent_runs(agent)}


async def _limits(db, root):
    from yuxi.repositories.agent_repository import AgentRepository
    from types import SimpleNamespace

    frozen = (root.input_payload or {}).get("collaboration_limits")
    if isinstance(frozen, dict):
        # Validate even server-owned snapshots, including restored legacy data.
        return freeze_collaboration_limits(
            SimpleNamespace(
                config_json={
                    "context": {
                        "max_collaboration_depth": frozen.get("max_depth", 1),
                        "max_concurrent_subagent_runs": frozen.get("max_concurrent"),
                    }
                }
            )
        )
    return freeze_collaboration_limits(await AgentRepository(db).get_by_slug(root.agent_slug))


async def _depth(db, uid, run):
    depth, seen = 0, set()
    while run.created_by_run_id:
        if run.id in seen or len(seen) >= 32:
            raise HTTPException(409, "协作层级异常")
        seen.add(run.id)
        if run.run_type == "subagent":
            depth += 1
        run = await _run(db, uid, run.created_by_run_id)
    return depth


async def may_delegate(db, *, uid, run_id):
    root = await root_run(db, uid=uid, run_id=run_id)
    current = await _run(db, uid, run_id)
    return await _depth(db, uid, current) < (await _limits(db, root))["max_depth"]


async def _run(db, uid, run_id, *, lock=False):
    stmt = select(AgentRun).where(AgentRun.id == run_id, AgentRun.uid == uid)
    if lock:
        stmt = stmt.with_for_update()
    item = await db.scalar(stmt.execution_options(populate_existing=True))
    if item is None:
        raise HTTPException(404, "运行任务不存在")
    return item


async def root_run(db, *, uid, run_id, lock=False):
    try:
        tenant_id = await resolve_tenant_id(db, uid)
    except ValueError:
        raise HTTPException(403, "当前身份无法访问协作") from None
    run = await _run(db, uid, run_id)
    seen = set()
    while run.created_by_run_id:
        if run.id in seen or len(seen) >= 32:
            raise HTTPException(409, "协作层级异常")
        seen.add(run.id)
        run = await _run(db, uid, run.created_by_run_id)
    if lock:
        run = await _run(db, uid, run.id, lock=True)
    if run.tenant_id != tenant_id:
        raise HTTPException(404, "协作任务不存在")
    return run


async def members(db, root):
    result, frontier = [root], [root.id]
    seen = {root.id}
    for _level in range(32):
        children = (
            await db.scalars(
                select(AgentRun)
                .where(
                    AgentRun.created_by_run_id.in_(frontier),
                    AgentRun.uid == root.uid,
                    AgentRun.tenant_id == root.tenant_id,
                )
                .order_by(AgentRun.created_at, AgentRun.id)
            )
        ).all()
        children = [child for child in children if child.id not in seen]
        if not children:
            return result
        result.extend(children)
        if len(result) > MAX_MEMBERS:
            raise HTTPException(409, "协作成员超过可展示上限")
        frontier = [child.id for child in children]
        seen.update(frontier)
    raise HTTPException(409, "协作层级超过可展示上限")


def _message(item):
    return {
        "id": item.id,
        "sender_run_id": item.sender_run_id,
        "target_run_id": item.target_run_id,
        "sender_kind": item.sender_kind,
        "body": item.body,
        "created_at": format_utc_datetime(item.created_at),
        "delivered_at": format_utc_datetime(item.delivered_at) if item.delivered_at else None,
    }


async def _stopped_scopes(db, root):
    return set(
        (
            await db.scalars(
                select(CollaborationStop.scope_run_id).where(
                    CollaborationStop.root_run_id == root.id,
                    CollaborationStop.tenant_id == root.tenant_id,
                    CollaborationStop.uid == root.uid,
                )
            )
        ).all()
    )


def _is_stopped(run_id, group, scopes):
    seen = set()
    while run_id and run_id not in seen:
        if run_id in scopes:
            return True
        seen.add(run_id)
        run = group.get(run_id)
        run_id = run.created_by_run_id if run else None
    return False


async def tree(db, *, uid, run_id):
    root = await root_run(db, uid=uid, run_id=run_id)
    rows = await members(db, root)
    group = {row.id: row for row in rows}
    scopes = await _stopped_scopes(db, root)
    waits = {
        item.run_id: item
        for item in (
            await db.scalars(
                select(AgentRunWaitpoint).where(
                    AgentRunWaitpoint.run_id.in_(group),
                    AgentRunWaitpoint.uid == uid,
                    AgentRunWaitpoint.tenant_id == root.tenant_id,
                )
            )
        ).all()
    }

    def latest(row):
        seen = set()
        while row.id not in seen:
            seen.add(row.id)
            target = waits.get(row.id)
            if not target or not target.resolved_by_run_id or target.resolved_by_run_id not in group:
                break
            row = group[target.resolved_by_run_id]
        return row

    messages = (
        await db.scalars(
            select(CollaborationMessage)
            .where(
                CollaborationMessage.root_run_id == root.id,
                CollaborationMessage.uid == uid,
                CollaborationMessage.tenant_id == root.tenant_id,
            )
            .order_by(CollaborationMessage.created_at, CollaborationMessage.id)
        )
    ).all()
    return {
        "root_run_id": root.id,
        "members": [
            {
                "run_id": row.id,
                "parent_run_id": row.created_by_run_id,
                "thread_id": row.conversation_thread_id,
                "agent_slug": row.agent_slug,
                "status": row.status,
                "run_type": row.run_type,
                "is_subagent_member": is_worker_member(row),
                "current_run_id": latest(row).id,
                "stopped": _is_stopped(row.id, group, scopes),
                "effective_status": (
                    "cancelled" if row.status == "interrupted" and _is_stopped(row.id, group, scopes) else row.status
                ),
                "waiting_for_input": row.status == "interrupted"
                and latest(row).id == row.id
                and not _is_stopped(row.id, group, scopes),
            }
            for row in rows
        ],
        "messages": [_message(item) for item in messages],
    }


async def send_message(db, *, uid, sender_run_id, target_run_id, body, request_id, sender_kind="agent"):
    body, request_id = body.strip(), request_id.strip()
    if not body or len(body) > 8192 or not request_id or len(request_id) > 64:
        raise HTTPException(422, "消息须为 1–8192 字符，request_id 须为 1–64 字符")
    root = await root_run(db, uid=uid, run_id=sender_run_id, lock=True)
    existing = await db.scalar(
        select(CollaborationMessage).where(
            CollaborationMessage.root_run_id == root.id,
            CollaborationMessage.sender_run_id == sender_run_id,
            CollaborationMessage.request_id == request_id,
        )
    )
    if existing:
        if existing.target_run_id != target_run_id or existing.body != body or existing.sender_kind != sender_kind:
            raise HTTPException(409, "同一消息 request_id 不能用于不同内容")
        return _message(existing)
    source = await _run(db, uid, sender_run_id)
    group = {item.id: item for item in await members(db, root)}
    if source.id not in group:
        raise HTTPException(404, "发送方不属于当前协作")
    target = group.get(target_run_id)
    if target is None:
        raise HTTPException(404, "接收方不属于当前协作")
    scopes = await _stopped_scopes(db, root)
    if _is_stopped(source.id, group, scopes) or _is_stopped(target.id, group, scopes):
        raise HTTPException(409, "协作已停止；旧等待点不能继续")
    if root.status not in ACTIVE or source.status not in ACTIVE or target.status not in ACTIVE:
        raise HTTPException(409, "协作或成员已停止；请继续会话后再发送")
    count = await db.scalar(
        select(func.count()).select_from(CollaborationMessage).where(CollaborationMessage.root_run_id == root.id)
    )
    if count >= MAX_MESSAGES:
        raise HTTPException(409, "本组协作消息已达到 100 条上限")
    item = CollaborationMessage(
        id=str(uuid4()),
        tenant_id=root.tenant_id,
        uid=uid,
        root_run_id=root.id,
        sender_run_id=source.id,
        target_run_id=target.id,
        target_thread_id=target.conversation_thread_id,
        request_id=request_id,
        body=body,
        sender_kind=sender_kind,
    )
    db.add(item)
    await db.flush()
    return _message(item)


async def pending_guidance(db, *, uid, run_id, delivered_ids):
    # Direct graph invocations (e.g. legacy evaluation) may carry an execution
    # ID without a persisted AgentRun. They have no collaboration mailbox.
    await resolve_tenant_id(db, uid)
    exists = await db.scalar(select(AgentRun.id).where(AgentRun.id == run_id, AgentRun.uid == uid))
    if exists is None:
        return [], []
    root = await root_run(db, uid=uid, run_id=run_id)
    run = await _run(db, uid, run_id)
    scopes = await _stopped_scopes(db, root)
    current = run
    for _depth in range(32):
        if current.id in scopes:
            raise asyncio.CancelledError("协作已停止")
        if not current.created_by_run_id:
            break
        current = await _run(db, uid, current.created_by_run_id)
    if run.status not in ACTIVE or root.status not in ACTIVE:
        return [], []
    rows = (
        await db.scalars(
            select(CollaborationMessage)
            .where(
                CollaborationMessage.root_run_id == root.id,
                CollaborationMessage.uid == uid,
                CollaborationMessage.target_thread_id == run.conversation_thread_id,
                CollaborationMessage.tenant_id == root.tenant_id,
            )
            .order_by(CollaborationMessage.created_at, CollaborationMessage.id)
            .limit(MAX_MESSAGES)
        )
    ).all()
    unseen = [row for row in rows if row.id not in delivered_ids]
    # The checkpointed ID set is authoritative for delivery. A process crash after
    # this flush but before a graph checkpoint safely replays the same message IDs.
    for row in unseen:
        if row.delivered_at is None:
            row.delivered_at = utc_now()
    await db.flush()
    messages = [
        HumanMessage(
            id="collaboration:" + row.id,
            content=(
                f"[协作消息；来自{'用户' if row.sender_kind == 'user' else '成员 ' + row.sender_run_id}]\n{row.body}"
            ),
            additional_kwargs={"collaboration_message_id": row.id},
        )
        for row in unseen
    ]
    return messages, [row.id for row in unseen]


async def assert_spawn_allowed(db, *, uid, creator_run_id, child_thread_id=None):
    from yuxi.services.subagent_run_service import SubagentRunConcurrencyLimit

    root = await root_run(db, uid=uid, run_id=creator_run_id, lock=True)
    creator = await _run(db, uid, creator_run_id, lock=True)
    if root.status not in ACTIVE or creator.status not in {"pending", "running"}:
        raise HTTPException(409, "父运行已停止，不能继续派发子智能体")
    # Recheck the group budget under the root lock. The earlier UI preflight
    # cannot serialize two concurrent inserts into different child threads.
    limits = await _limits(db, root)
    if await _depth(db, uid, creator) >= limits["max_depth"]:
        raise HTTPException(409, f"已达到协作层级上限（{limits['max_depth']} 层）")
    limit = limits["max_concurrent"]
    group = await members(db, root)
    if _is_stopped(creator.id, {item.id: item for item in group}, await _stopped_scopes(db, root)):
        raise HTTPException(409, "协作已停止，不能继续派发")
    if len(group) >= MAX_MEMBERS:
        raise HTTPException(409, "本组协作已达到成员记录上限，请在新会话继续")
    active = [
        item
        for item in group
        if is_worker_member(item)
        and item.status in {"pending", "running", "cancel_requested"}
        and item.conversation_thread_id != child_thread_id
    ]
    if len(active) >= limit:
        raise SubagentRunConcurrencyLimit(limit=limit, active_run_ids=tuple(item.id for item in active))


def is_worker_member(run):
    runtime = (run.input_payload or {}).get("runtime") or {}
    return run.run_type == "subagent" or (run.run_type == "resume" and runtime.get("kind") == "subagent")


async def assert_resume_allowed(db, *, uid, run_id):
    root = await root_run(db, uid=uid, run_id=run_id, lock=True)
    scopes = await _stopped_scopes(db, root)
    current = await _run(db, uid, run_id)
    target = current
    for _depth in range(32):
        if current.id in scopes:
            raise HTTPException(409, "协作已停止，旧等待点不能恢复；请发送新任务")
        if not current.created_by_run_id:
            break
        current = await _run(db, uid, current.created_by_run_id)
    else:
        raise HTTPException(409, "协作层级异常")
    if target.status != "interrupted":
        raise HTTPException(409, "运行当前不在等待人工输入")
    consumed = await db.scalar(
        select(AgentRun.id)
        .where(
            AgentRun.created_by_run_id == target.id,
            AgentRun.run_type == "resume",
            AgentRun.uid == uid,
        )
        .limit(1)
    )
    point = await db.get(AgentRunWaitpoint, target.id)
    if consumed or (point and point.resolved_by_run_id):
        raise HTTPException(409, "此等待点已处理，请刷新成员状态")
    if is_worker_member(target):
        group = await members(db, root)
        limit = (await _limits(db, root))["max_concurrent"]
        active = [
            item
            for item in group
            if is_worker_member(item)
            and item.status in {"pending", "running", "cancel_requested"}
            and item.conversation_thread_id != target.conversation_thread_id
        ]
        if len(active) >= limit:
            raise HTTPException(409, f"已达协作并发上限（{limit}），请等待其他成员结束后重试")
        if len(group) >= MAX_MEMBERS:
            raise HTTPException(409, "本组协作已达到成员记录上限，请在新会话继续")
    if target.created_at is not None:
        newer = await db.scalar(
            select(AgentRun.id)
            .where(
                AgentRun.conversation_thread_id == target.conversation_thread_id,
                AgentRun.uid == uid,
                AgentRun.created_at > target.created_at,
                AgentRun.run_type.in_(["chat", "resume", "subagent"]),
            )
            .limit(1)
        )
        if newer:
            raise HTTPException(409, "此会话已推进到新任务，不能重放旧等待点")


async def stop_group(db, *, uid, run_id, whole_group=True):
    from yuxi.repositories.agent_run_repository import AgentRunRepository
    from yuxi.services.agent_run_service import publish_cancel_signal

    root = await root_run(db, uid=uid, run_id=run_id, lock=True)
    selected = root if whole_group else await _run(db, uid, run_id)
    existing_stop = await db.get(CollaborationStop, (root.id, selected.id))
    if existing_stop is None:
        db.add(CollaborationStop(root_run_id=root.id, scope_run_id=selected.id, tenant_id=root.tenant_id, uid=uid))
        await db.flush()
    # Cancellation must also work when a large legacy tree exceeds the UI
    # display budget. UNION deduplicates corrupt cycles without losing children
    # behind already completed intermediate members.
    descendants = select(AgentRun.id).where(AgentRun.id == selected.id).cte("collaboration_descendants", recursive=True)
    descendants = descendants.union(
        select(AgentRun.id)
        .join(descendants, AgentRun.created_by_run_id == descendants.c.id)
        .where(AgentRun.uid == root.uid, AgentRun.tenant_id == root.tenant_id)
    )
    rows = (
        await db.scalars(
            select(AgentRun)
            .join(descendants, AgentRun.id == descendants.c.id)
            .where(AgentRun.uid == uid, AgentRun.tenant_id == root.tenant_id)
            .order_by(AgentRun.created_at, AgentRun.id)
        )
    ).all()
    repository = AgentRunRepository(db)
    stopped, requested = [], []
    # Same root lock as send_message and task creation: no late dispatch can
    # pass the parent-status gate after cancellation has become durable.
    for row in rows:
        if row.status in ACTIVE or row.status == "cancel_requested":
            updated = await repository.request_cancel(row.id)
            stopped.append(row.id)
            if updated.status == "cancel_requested":
                requested.append(row.id)
    await db.commit()
    await asyncio.gather(*(publish_cancel_signal(item) for item in requested))
    return {"root_run_id": root.id, "stopped_run_ids": stopped, "cancel_requested_run_ids": requested}
