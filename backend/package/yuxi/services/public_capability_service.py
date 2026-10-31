"""Scoped public history search, debugging and workdir operations."""

import asyncio
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import and_, or_, select

from yuxi.services.public_query_service import session_resource
from yuxi.services.public_session_service import append_event, require_session
from yuxi.storage.postgres.models_business import (
    AgentRun,
    Message,
    PublicInput,
    PublicSession,
    PublicTurn,
    PublicTurnRun,
    ToolCall,
    Conversation,
)
from yuxi.utils.datetime_utils import format_utc_datetime
from yuxi.utils.reasoning_visibility import sanitize_visible_text


async def search_sessions(db, actor, query, *, agent_id=None, limit=20, offset=0):
    query = query.strip()
    if not query:
        return {"items": [], "has_more": False, "limit": limit, "offset": offset}
    from yuxi.knowledge.evidence.verbatim import escape_like

    pattern = "%" + escape_like(query) + "%"
    visible_messages = (
        select(Message.id)
        .join(AgentRun, AgentRun.id == Message.run_id)
        .join(PublicTurnRun, PublicTurnRun.run_id == AgentRun.id)
        .where(
            AgentRun.uid == actor.uid,
            AgentRun.tenant_id == actor.tenant_id,
            AgentRun.conversation_id == Message.conversation_id,
            Message.role.in_(["user", "assistant"]),
            Message.content.ilike(pattern, escape="/"),
            or_(Message.role == "user", and_(AgentRun.status == "completed", Message.id == AgentRun.output_message_id)),
        )
    )
    statement = select(PublicSession).where(
        PublicSession.uid == actor.uid,
        PublicSession.tenant_id == actor.tenant_id,
        PublicSession.app_id == actor.app_id,
        PublicSession.archived.is_(False),
        or_(
            PublicSession.title.ilike(pattern, escape="/"),
            visible_messages.where(PublicTurnRun.session_id == PublicSession.id).exists(),
        ),
    )
    if agent_id:
        statement = statement.where(PublicSession.agent_slug == agent_id)
    rows = list(
        await db.scalars(
            statement.order_by(PublicSession.last_active_at.desc(), PublicSession.id).offset(offset).limit(limit + 1)
        )
    )
    items = []
    for session in rows[:limit]:
        # Only public message text contributes a snippet; internal audit roles never match.
        matches = list(
            await db.scalars(
                select(Message)
                .where(Message.id.in_(visible_messages.where(PublicTurnRun.session_id == session.id)))
                .order_by(Message.id.desc())
                .limit(3)
            )
        )
        snippets = []
        for message in matches:
            content = sanitize_visible_text(message.content) if message.role == "assistant" else message.content
            position = content.casefold().find(query.casefold())
            if position < 0:
                continue
            snippets.append(
                {
                    "message_id": message.id,
                    "content": content[max(0, position - 80) : position + 240],
                    "created_at": format_utc_datetime(message.created_at),
                }
            )
        if not snippets and query.casefold() not in (session.title or "").casefold():
            continue
        items.append(
            {
                "session": await session_resource(db, actor, session.id),
                "matched_count": len(snippets),
                "message_id": snippets[0]["message_id"] if snippets else None,
                "latest_match_at": snippets[0]["created_at"] if snippets else None,
                "snippets": snippets,
            }
        )
    return {"items": items, "has_more": len(rows) > limit, "limit": limit, "offset": offset}


async def require_idle(db, session):
    active = await db.scalar(
        select(PublicTurn.id)
        .where(
            PublicTurn.session_id == session.id, PublicTurn.status.in_(["in_progress", "requires_action", "cancelling"])
        )
        .limit(1)
    )
    pending = await db.scalar(
        select(PublicInput.id).where(PublicInput.session_id == session.id, PublicInput.status == "pending").limit(1)
    )
    if active or pending:
        raise HTTPException(409, "会话仍有执行、等待或排队输入")


async def public_checkpoint(session):
    """Reconstruct the frozen checkpoint graph without initializing tools or a model."""
    from yuxi.agents.buildin import agent_manager

    snapshot = session.config_snapshot
    if snapshot["backend_id"] == "DshAgent":
        raise HTTPException(409, "DSH 的上下文由独立运行进程管理")
    backend = agent_manager.get_agent(snapshot["backend_id"])
    if backend is None:
        raise HTTPException(409, "会话原后端不可用")
    context = backend.context_schema()
    context.update_from_dict(
        {
            **snapshot["agent_config"].get("context", {}),
            "uid": session.uid,
            "thread_id": session.id,
            "file_thread_id": session.config_snapshot.get("project_id") or session.id,
            "public_session_id": session.id,
            "tool_approval_mode": snapshot["tool_approval_mode"],
        }
    )
    return await backend.get_graph(context=context, checkpoint_only=True), context


async def delete_attachment(db, actor, session_id, file_id):
    from langgraph.graph import END
    from yuxi.agents.backends.sandbox import sandbox_uploads_dir
    from yuxi.services.conversation_service import _build_state_uploads
    from yuxi.services.public_input_service import require_file, remove_draft_data
    from yuxi.services.mention_search_service import invalidate_mention_cache

    session = await require_session(db, actor, session_id, lock=True)
    file = await require_file(db, actor, file_id, lock=True)
    item = await db.get(PublicInput, file.input_id) if file.input_id else None
    if item is None or item.session_id != session.id:
        raise HTTPException(404, "附件不存在")
    await require_idle(db, session)
    conversation = await db.scalar(
        select(Conversation)
        .where(Conversation.thread_id == session.id, Conversation.uid == actor.uid)
        .with_for_update()
    )
    records = list((conversation.extra_metadata or {}).get("attachments") or [])
    target = next((record for record in records if record.get("file_id") == file.id), None)
    remaining = [record for record in records if record.get("file_id") != file.id]
    if session.config_snapshot["backend_id"] != "DshAgent":
        graph, _context = await public_checkpoint(session)
        config = {"configurable": {"thread_id": session.id, "uid": actor.uid}}
        saved = await graph.aget_state(config)
        if saved.next or saved.interrupts:
            raise HTTPException(409, "checkpoint 仍有未完成任务")
        if saved.values:
            await graph.aupdate_state(config, {"uploads": _build_state_uploads(remaining)})
            if (await graph.aget_state(config)).next:
                await graph.aupdate_state(config, None, as_node=END)
    paths = set()
    root = sandbox_uploads_dir(session.config_snapshot.get("project_id") or session.id).resolve()
    for key in ("storage_path", "original_storage_path", "markdown_storage_path"):
        value = (target or {}).get(key)
        if value:
            path = Path(value).resolve()
            if not path.is_relative_to(root):
                raise HTTPException(409, "附件存储路径不属于当前会话")
            paths.add(path)
    await remove_draft_data(file)
    for path in paths:
        path.unlink(missing_ok=True)
    # Preserve immutable Input/file references for history, but revoke all content reads.
    file.status = "deleted"
    conversation.extra_metadata = {**(conversation.extra_metadata or {}), "attachments": remaining}
    await invalidate_mention_cache(session.id)
    await append_event(db, session, "yuxi.session.attachment.deleted", {"file_id": file.id})
    return {"message": "附件已删除", "file_id": file.id, "deleted": True}


async def thread_audits(db, actor, session_id):
    session = await require_session(db, actor, session_id)
    messages = list(
        await db.scalars(
            select(Message)
            .join(AgentRun, AgentRun.id == Message.run_id)
            .join(PublicTurnRun, PublicTurnRun.run_id == AgentRun.id)
            .where(
                PublicTurnRun.session_id == session.id,
                Message.conversation_id == AgentRun.conversation_id,
                AgentRun.uid == actor.uid,
                AgentRun.tenant_id == actor.tenant_id,
            )
            .order_by(Message.id)
            .limit(501)
        )
    )
    runs = list(
        await db.scalars(
            select(AgentRun)
            .join(PublicTurnRun, PublicTurnRun.run_id == AgentRun.id)
            .where(
                PublicTurnRun.session_id == session.id, AgentRun.uid == actor.uid, AgentRun.tenant_id == actor.tenant_id
            )
            .order_by(AgentRun.created_at, AgentRun.id)
            .limit(501)
        )
    )
    tools = list(
        await db.scalars(
            select(ToolCall)
            .where(ToolCall.message_id.in_([item.id for item in messages[:500]]))
            .order_by(ToolCall.id)
            .limit(500)
        )
    )
    by_message = {}
    for tool in tools:
        by_message.setdefault(tool.message_id, []).append(tool.to_dict())
    turns = list(
        await db.scalars(
            select(PublicTurn).where(PublicTurn.session_id == session.id).order_by(PublicTurn.sequence).limit(500)
        )
    )
    return {
        "audits": [
            {
                "message_id": message.id,
                "run_id": message.run_id,
                "type": {"user": "human", "assistant": "ai"}.get(message.role, message.role),
                "content": message.content,
                "created_at": format_utc_datetime(message.created_at),
                "tool_calls": by_message.get(message.id, []),
            }
            for message in messages[:500]
        ],
        "runs": [
            {
                "run_id": run.id,
                "status": run.status,
                "timing": {
                    "created_at": format_utc_datetime(run.created_at),
                    "started_at": format_utc_datetime(run.started_at),
                    "finished_at": format_utc_datetime(run.finished_at),
                },
            }
            for run in runs[:500]
        ],
        "turns": [
            {
                "turn_id": turn.id,
                "status": turn.status,
                "created_at": format_utc_datetime(turn.created_at),
                "finished_at": format_utc_datetime(turn.finished_at),
            }
            for turn in turns
        ],
        "truncated": len(messages) > 500,
        "runs_truncated": len(runs) > 500,
    }


async def save_artifact(db, actor, session_id, path, destination_path=None):
    from yuxi.agents.backends.sandbox import sandbox_workspace_dir, resolve_virtual_path, virtual_path_for_thread_file
    from yuxi.services.thread_files_service import resolve_thread_artifact_view, _next_available_artifact_path
    from yuxi.services.mention_search_service import invalidate_workspace_mention_cache

    session = await require_session(db, actor, session_id, lock=True)
    source = await resolve_thread_artifact_view(
        thread_id=session_id,
        current_uid=actor.uid,
        db=db,
        path=path,
        file_thread_id=session.config_snapshot.get("project_id") or session.id,
    )
    workspace = sandbox_workspace_dir(session_id, actor.uid).resolve()
    if destination_path:
        try:
            target = resolve_virtual_path(session_id, destination_path, uid=actor.uid).resolve()
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        if not target.is_relative_to(workspace) or target == workspace:
            raise HTTPException(403, "目标必须位于当前用户的工作区")
        target.parent.mkdir(parents=True, exist_ok=True)
    else:
        target_dir = workspace / "saved_artifacts"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = _next_available_artifact_path(target_dir, source.name)

    # Exclusive creation prevents overwriting existing user files, including concurrent copies.
    def copy():
        import shutil

        try:
            with source.open("rb") as original, target.open("xb") as saved:
                shutil.copyfileobj(original, saved)
        except FileExistsError:
            raise HTTPException(409, "目标文件已存在") from None

    await asyncio.to_thread(copy)
    await invalidate_workspace_mention_cache(actor.uid)
    virtual = virtual_path_for_thread_file(session_id, Path(target), uid=actor.uid)
    return {
        "name": target.name,
        "source_path": path,
        "saved_path": virtual,
        "saved_artifact_url": f"/api/v1/agents/sessions/{session_id}/artifacts/{virtual.lstrip('/')}",
    }
