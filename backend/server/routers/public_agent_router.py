"""HTTP adapter for durable v0.8 Sessions; AgentRun 1.x remains independent."""

from copy import deepcopy
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import func, select

from server.utils.auth_middleware import get_db
from server.utils.public_auth import authenticate_public_request
from yuxi.services import public_query_service as queries
from yuxi.services.public_api_schemas import SessionCreate, SessionEventCreate, SessionUpdate
from yuxi.services.public_control_service import submit_event
from yuxi.services.public_event_service import session_event_stream, validate_cursor
from yuxi.services.public_runtime_service import kick_public_session
from yuxi.services.public_session_service import (
    create_session,
    receipt_for_key,
    require_session,
    resolve_public_model,
    validate_key,
    visible_agent,
    visible_session_agent,
)
from yuxi.storage.postgres.models_business import PublicInput, User

public_agents = APIRouter(prefix="/v1/agents", tags=["Public Sessions"])


def agent_resource(agent):
    return {"id": agent.slug, "object": "agent", "name": agent.name, "description": agent.description}


@public_agents.get("")
@public_agents.get("/", include_in_schema=False)
async def list_agents(actor=Depends(authenticate_public_request), db=Depends(get_db)):
    from yuxi.repositories.agent_repository import AgentRepository

    user = await db.scalar(select(User).where(User.uid == actor.uid))
    agents = await AgentRepository(db).list_visible(user=user)
    return {"object": "list", "data": [agent_resource(agent) for agent in agents]}


def stream_response(request, actor, session_id, sequence=0, *, status_code=200):
    return StreamingResponse(
        session_event_stream(request, actor, session_id, after=sequence),
        status_code=status_code,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@public_agents.post("/sessions", status_code=201)
async def create(
    payload: SessionCreate,
    request: Request,
    background: BackgroundTasks,
    key: str = Header(alias="Idempotency-Key"),
    actor=Depends(authenticate_public_request),
    db=Depends(get_db),
):
    session, receipt, created = await create_session(db, actor, payload, key=key, report_created=True)
    # Start durable dispatch before returning a long-lived stream (response background tasks run after it closes).
    if created and receipt:
        if payload.stream:
            await kick_public_session(session.id)
        else:
            background.add_task(kick_public_session, session.id)
    if payload.stream:
        await db.close()
        return stream_response(request, actor, session.id, status_code=201)
    return await queries.session_resource(db, actor, session.id, receipt=receipt)


@public_agents.get("/sessions")
async def sessions(
    after: str | None = None,
    limit: int = Query(50, ge=1, le=100),
    order: Literal["asc", "desc"] = "desc",
    archived: bool = False,
    agent_id: str | None = None,
    is_pinned: bool | None = None,
    actor=Depends(authenticate_public_request),
    db=Depends(get_db),
):
    return await queries.list_sessions(
        db, actor, after=after, limit=limit, order=order, archived=archived, agent_id=agent_id, is_pinned=is_pinned
    )


@public_agents.get("/sessions/{session_id}")
async def retrieve(session_id: str, actor=Depends(authenticate_public_request), db=Depends(get_db)):
    return await queries.session_resource(db, actor, session_id)


@public_agents.post("/sessions/{session_id}")
async def update(
    session_id: str, payload: SessionUpdate, actor=Depends(authenticate_public_request), db=Depends(get_db)
):
    session = await require_session(db, actor, session_id, lock=True)
    if session.archived:
        raise HTTPException(409, "Session 已归档")
    snapshot = deepcopy(session.config_snapshot)
    if payload.agent and payload.agent.model:
        agent = await visible_session_agent(db, actor, session)
        snapshot["model_spec"] = await resolve_public_model(db, actor, agent, payload.agent.model, snapshot)
    for name in payload.yuxi.model_fields_set:
        value = getattr(payload.yuxi, name)
        if name == "tool_approval_mode" and value is not None:
            snapshot[name] = value
        elif name in {"title", "is_pinned"} and (value is not None or name == "title"):
            setattr(session, name, value)
    session.config_snapshot = snapshot
    await db.commit()
    return await queries.session_resource(db, actor, session_id)


@public_agents.post("/sessions/{session_id}/viewed")
async def viewed(session_id: str, actor=Depends(authenticate_public_request), db=Depends(get_db)):
    session = await require_session(db, actor, session_id, lock=True)
    session.read_event_seq = session.next_event_seq - 1
    await db.commit()
    return await queries.session_resource(db, actor, session_id)


@public_agents.post("/sessions/{session_id}/archive")
async def archive(session_id: str, actor=Depends(authenticate_public_request), db=Depends(get_db)):
    from yuxi.services.public_execution_service import latest_turn

    session = await require_session(db, actor, session_id, lock=True)
    turn = await latest_turn(db, session_id)
    pending = await db.scalar(
        select(func.count())
        .select_from(PublicInput)
        .where(PublicInput.session_id == session_id, PublicInput.status == "pending")
    )
    if pending or (turn and turn.status in {"in_progress", "requires_action", "cancelling"}):
        raise HTTPException(409, "请先完成或取消当前工作和待处理输入")
    session.archived = True
    await db.commit()
    return await queries.session_resource(db, actor, session_id)


@public_agents.get("/sessions/{session_id}/queue")
async def queue(session_id: str, actor=Depends(authenticate_public_request), db=Depends(get_db)):
    from yuxi.services.public_execution_service import latest_turn

    session = await require_session(db, actor, session_id)
    rows = list(
        await db.scalars(
            select(PublicInput)
            .where(PublicInput.session_id == session_id, PublicInput.status == "pending")
            .order_by(PublicInput.received_seq)
            .limit(100)
        )
    )
    turn = await latest_turn(db, session_id)
    return {
        "thread_id": session_id,
        "queue_paused": session.queue_paused,
        "tree_paused": session.tree_paused,
        "current_turn_id": turn.id if turn else None,
        "inputs": [await queries.input_resource(db, actor, session_id, row.id) for row in rows],
    }


@public_agents.get("/sessions/{session_id}/receipt")
async def receipt(
    session_id: str, idempotency_key: str, actor=Depends(authenticate_public_request), db=Depends(get_db)
):
    validate_key(idempotency_key)
    session = await require_session(db, actor, session_id)
    result = await receipt_for_key(db, session, idempotency_key)
    if result is None:
        raise HTTPException(404, "回执不存在")
    return result


@public_agents.post("/sessions/{session_id}/events", status_code=202)
async def events(
    session_id: str,
    payload: SessionEventCreate,
    background: BackgroundTasks,
    key: str = Header(alias="Idempotency-Key"),
    actor=Depends(authenticate_public_request),
    db=Depends(get_db),
):
    result, changed = await submit_event(db, actor, session_id, payload.events[0], key=key)
    await db.commit()
    if changed:
        background.add_task(kick_public_session, session_id)
    return result


@public_agents.get("/sessions/{session_id}/events")
async def stream(
    session_id: str,
    request: Request,
    after: str | None = None,
    last_event_id: str | None = Header(None, alias="Last-Event-ID"),
    actor=Depends(authenticate_public_request),
    db=Depends(get_db),
):
    if after is not None and last_event_id is not None and after != last_event_id:
        raise HTTPException(422, "重复的 SSE 游标不一致")
    sequence = await validate_cursor(db, actor, session_id, last_event_id or after)
    await db.close()
    return stream_response(request, actor, session_id, sequence)


@public_agents.get("/sessions/{session_id}/inputs/{input_id}")
async def input_detail(session_id: str, input_id: str, actor=Depends(authenticate_public_request), db=Depends(get_db)):
    return await queries.input_resource(db, actor, session_id, input_id)


@public_agents.get("/sessions/{session_id}/turns")
async def turns(
    session_id: str,
    after: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    order: Literal["asc", "desc"] = "desc",
    actor=Depends(authenticate_public_request),
    db=Depends(get_db),
):
    return await queries.list_turns(db, actor, session_id, after=after, limit=limit, order=order)


@public_agents.get("/sessions/{session_id}/turns/{turn_id}")
async def turn_detail(session_id: str, turn_id: str, actor=Depends(authenticate_public_request), db=Depends(get_db)):
    return await queries.turn_resource(db, actor, session_id, turn_id)


@public_agents.get("/sessions/{session_id}/items")
async def items(
    session_id: str,
    turn_id: str | None = None,
    after: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    order: Literal["asc", "desc"] = "desc",
    actor=Depends(authenticate_public_request),
    db=Depends(get_db),
):
    return await queries.list_items(db, actor, session_id, turn_id=turn_id, after=after, limit=limit, order=order)


@public_agents.get("/sessions/{session_id}/turns/{turn_id}/items")
async def turn_items(
    session_id: str,
    turn_id: str,
    after: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    order: Literal["asc", "desc"] = "desc",
    actor=Depends(authenticate_public_request),
    db=Depends(get_db),
):
    return await queries.list_items(db, actor, session_id, turn_id=turn_id, after=after, limit=limit, order=order)


@public_agents.get("/sessions/{session_id}/runs/{run_id}")
async def run_detail(session_id: str, run_id: str, actor=Depends(authenticate_public_request), db=Depends(get_db)):
    from yuxi.storage.postgres.models_business import AgentRun, PublicTurnRun

    await require_session(db, actor, session_id)
    run = await db.scalar(
        select(AgentRun)
        .join(PublicTurnRun, PublicTurnRun.run_id == AgentRun.id)
        .where(
            PublicTurnRun.session_id == session_id,
            AgentRun.id == run_id,
            AgentRun.uid == actor.uid,
            AgentRun.tenant_id == actor.tenant_id,
            AgentRun.conversation_thread_id == session_id,
        )
    )
    if run is None:
        raise HTTPException(404, "Run 不存在")
    return {
        "run_id": run.id,
        "thread_id": session_id,
        "status": run.status,
        "created_at": queries.unix_time(run.created_at),
        "completed_at": queries.unix_time(run.finished_at),
        "error": {"code": "run_failed", "message": "执行失败，请检查配置后重试。"} if run.status == "failed" else None,
    }


@public_agents.get("/sessions/{session_id}/cooperation")
async def cooperation(session_id: str, actor=Depends(authenticate_public_request), db=Depends(get_db)):
    from yuxi.storage.postgres.models_business import PublicSession

    session = await require_session(db, actor, session_id)
    root, seen = session, set()
    while root.parent_session_id:
        if root.id in seen or len(seen) >= 32:
            raise HTTPException(409, "协作会话层级异常")
        seen.add(root.id)
        root = await require_session(db, actor, root.parent_session_id)
    tree = select(PublicSession.id).where(PublicSession.id == root.id).cte("public_cooperation", recursive=True)
    tree = tree.union(
        select(PublicSession.id)
        .join(tree, PublicSession.parent_session_id == tree.c.id)
        .where(
            PublicSession.uid == actor.uid,
            PublicSession.tenant_id == actor.tenant_id,
            PublicSession.app_id == actor.app_id,
        )
    )
    rows = list(
        await db.scalars(
            select(PublicSession)
            .where(
                PublicSession.id.in_(select(tree.c.id)),
                PublicSession.uid == actor.uid,
                PublicSession.tenant_id == actor.tenant_id,
                PublicSession.app_id == actor.app_id,
            )
            .order_by(PublicSession.created_at, PublicSession.id)
            .limit(257)
        )
    )
    if len(rows) > 256:
        raise HTTPException(409, "协作成员超过可展示上限")
    return {
        "root_session_id": root.id,
        "sessions": [
            {
                "session_id": row.id,
                "parent_session_id": row.parent_session_id,
                "agent_id": row.agent_slug,
                "tree_paused": row.tree_paused,
            }
            for row in rows
        ],
    }


@public_agents.get("/sessions/{session_id}/state")
async def state(session_id: str, actor=Depends(authenticate_public_request), db=Depends(get_db)):
    from yuxi.services.public_execution_service import latest_turn
    from yuxi.storage.postgres.models_business import AgentRun
    from yuxi.agents.buildin import agent_manager
    from yuxi.services.chat_service import extract_agent_state

    session = await require_session(db, actor, session_id)
    turn = await latest_turn(db, session_id)
    snapshot = session.config_snapshot
    run = await db.get(AgentRun, turn.current_run_id) if turn else None
    if run and run.input_payload.get("agent_runtime") == "dsh":
        return {"agent_state": extract_agent_state({})}
    backend = agent_manager.get_agent(snapshot["backend_id"])
    context = backend.context_schema()
    context.update_from_dict(
        {
            **snapshot["agent_config"].get("context", {}),
            "uid": actor.uid,
            "thread_id": session_id,
            "public_session_id": session_id,
            "tool_approval_mode": snapshot["tool_approval_mode"],
        }
    )
    graph = await backend.get_graph(context=context, checkpoint_only=True)
    saved = await graph.aget_state({"configurable": {"uid": actor.uid, "thread_id": session_id}})
    value = extract_agent_state(saved.values if saved else {})
    # File contents stay behind authorized artifact reads; no raw checkpoint is returned.
    value["files"] = {path: {} for path in value["files"]}
    return {"agent_state": value}


@public_agents.get("/sessions/{session_id}/attachments")
async def attachments(session_id: str, actor=Depends(authenticate_public_request), db=Depends(get_db)):
    from yuxi.storage.postgres.models_business import PublicFile
    from yuxi.services.public_input_service import file_resource

    await require_session(db, actor, session_id)
    files = list(
        await db.scalars(
            select(PublicFile)
            .join(PublicInput, PublicInput.id == PublicFile.input_id)
            .where(
                PublicInput.session_id == session_id,
                PublicFile.uid == actor.uid,
                PublicFile.tenant_id == actor.tenant_id,
                PublicFile.app_id == actor.app_id,
            )
            .order_by(PublicFile.created_at, PublicFile.id)
            .limit(100)
        )
    )
    return {
        "attachments": [
            {
                **file_resource(file),
                "file_id": file.id,
                "file_name": file.filename,
                "download_url": f"/v1/agents/sessions/{session_id}/attachments/{file.id}/content",
            }
            for file in files
        ]
    }


@public_agents.get("/sessions/{session_id}/attachments/{file_id}/content")
async def attachment_content(
    session_id: str, file_id: str, actor=Depends(authenticate_public_request), db=Depends(get_db)
):
    from yuxi.services.public_input_service import require_file

    await require_session(db, actor, session_id)
    file = await require_file(db, actor, file_id)
    item = await db.get(PublicInput, file.input_id) if file.input_id else None
    if item is None or item.session_id != session_id:
        raise HTTPException(404, "附件不存在")
    return await file_content(file_id, actor, db)


@public_agents.get("/sessions/{session_id}/artifacts/{path:path}")
async def artifact(session_id: str, path: str, actor=Depends(authenticate_public_request), db=Depends(get_db)):
    from fastapi.responses import FileResponse
    from yuxi.services.thread_files_service import resolve_thread_artifact_view

    await require_session(db, actor, session_id)
    target = await resolve_thread_artifact_view(thread_id=session_id, current_uid=actor.uid, db=db, path=path)
    return FileResponse(
        target,
        filename=target.name,
        media_type="application/octet-stream",
        headers={"X-Content-Type-Options": "nosniff"},
    )


@public_agents.post("/files", status_code=201)
async def upload(file: UploadFile, actor=Depends(authenticate_public_request), db=Depends(get_db)):
    from yuxi.services.public_input_service import file_resource, upload_file

    result = await upload_file(db, actor, file)
    await db.commit()
    return file_resource(result)


@public_agents.get("/files/{file_id}")
async def file_detail(file_id: str, actor=Depends(authenticate_public_request), db=Depends(get_db)):
    from yuxi.services.public_input_service import file_resource, require_file

    file = await require_file(db, actor, file_id)
    if file.status != "draft" or file.input_id:
        raise HTTPException(409, "附件已提交，请通过 Session attachments 读取")
    return file_resource(file)


@public_agents.get("/files/{file_id}/content")
async def file_content(file_id: str, actor=Depends(authenticate_public_request), db=Depends(get_db)):
    import json
    from urllib.parse import quote

    from yuxi.services.public_input_service import require_file
    from yuxi.storage.minio import get_minio_client

    file = await require_file(db, actor, file_id)
    stored = json.loads(file.storage_key)
    data = await get_minio_client().adownload_file(stored["bucket_name"], stored["object_name"])
    return Response(
        data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(file.filename)}",
            "X-Content-Type-Options": "nosniff",
        },
    )


@public_agents.delete("/files/{file_id}")
async def delete_file(file_id: str, actor=Depends(authenticate_public_request), db=Depends(get_db)):
    from yuxi.services.public_input_service import delete_draft

    await delete_draft(db, actor, file_id)
    await db.commit()
    return {"id": file_id, "object": "file", "deleted": True}


@public_agents.get("/{agent_id}")
async def agent_detail(agent_id: str, actor=Depends(authenticate_public_request), db=Depends(get_db)):
    return agent_resource(await visible_agent(db, actor, agent_id))
