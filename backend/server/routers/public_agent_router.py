"""HTTP adapter for durable v0.8 Sessions; AgentRun 1.x remains independent."""

from copy import deepcopy
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import case, func, select

from server.utils.auth_middleware import get_db
from server.utils.public_auth import authenticate_public_request
from yuxi.services import public_query_service as queries
from yuxi.services.public_api_schemas import SessionCreate, SessionEventCreate, SessionUpdate
from pydantic import BaseModel, ConfigDict, Field
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


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, max_length=255)


@public_agents.get("/projects")
async def projects(actor=Depends(authenticate_public_request), db=Depends(get_db)):
    from yuxi.services.public_project_service import project_resource
    from yuxi.storage.postgres.models_business import PublicProject

    rows = await db.scalars(
        select(PublicProject)
        .where(
            PublicProject.uid == actor.uid,
            PublicProject.tenant_id == actor.tenant_id,
            PublicProject.app_id == actor.app_id,
            PublicProject.archived.is_(False),
        )
        .order_by(PublicProject.created_at.desc())
        .limit(100)
    )
    return {"object": "list", "data": [project_resource(row) for row in rows]}


@public_agents.post("/projects", status_code=201)
async def project_create(payload: ProjectCreate, actor=Depends(authenticate_public_request), db=Depends(get_db)):
    from yuxi.services.public_project_service import create_project, project_resource

    result = await create_project(db, actor, name=payload.name)
    await db.commit()
    return project_resource(result)


@public_agents.post("/projects/{project_id}/archive")
async def project_archive(project_id: str, actor=Depends(authenticate_public_request), db=Depends(get_db)):
    from yuxi.services.public_project_service import require_project, project_resource

    project = await require_project(db, actor, project_id)
    project.archived = True
    await db.commit()
    return project_resource(project)


@public_agents.get("/sessions/{session_id}/files")
async def project_files(
    session_id: str, path: str | None = None, actor=Depends(authenticate_public_request), db=Depends(get_db)
):
    from yuxi.agents.backends.sandbox import (
        ensure_thread_dirs,
        resolve_virtual_path,
        sandbox_outputs_dir,
        sandbox_uploads_dir,
        sandbox_workspace_dir,
        virtual_path_for_thread_file,
    )
    from yuxi.services.thread_files_service import _list_directory_entries

    session = await require_session(db, actor, session_id)
    file_thread = session.config_snapshot.get("project_id") or session.id
    ensure_thread_dirs(file_thread, actor.uid)
    roots = [
        sandbox_outputs_dir(file_thread).resolve(),
        sandbox_uploads_dir(file_thread).resolve(),
        sandbox_workspace_dir(file_thread, actor.uid).resolve(),
    ]
    if path:
        try:
            target = resolve_virtual_path(file_thread, path, uid=actor.uid)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        if not any(target.is_relative_to(root) for root in roots):
            raise HTTPException(403, "目录不可访问")
        if not target.is_dir():
            raise HTTPException(404, "目录不存在")
        entries = _list_directory_entries(file_thread, actor.uid, target)
    else:
        entries = [
            {"name": root.name, "path": virtual_path_for_thread_file(file_thread, root, uid=actor.uid), "is_dir": True}
            for root in roots
        ]
    for entry in entries:
        if not entry["is_dir"]:
            entry["artifact_url"] = f"/api/v1/agents/sessions/{session_id}/artifacts/{entry['path'].lstrip('/')}"
    return {"files": entries, "project_id": file_thread}


@public_agents.post("/sessions/{session_id}/files", status_code=201)
async def upload_project_file(
    session_id: str,
    file: UploadFile,
    relative_path: str = Form(..., min_length=1, max_length=1024),
    actor=Depends(authenticate_public_request),
    db=Depends(get_db),
):
    from pathlib import PurePosixPath
    from yuxi.agents.backends.sandbox import ensure_thread_dirs, resolve_virtual_path
    from yuxi.utils.paths import VIRTUAL_PATH_UPLOADS
    from yuxi.utils.upload_utils import read_upload_with_limit

    session = await require_session(db, actor, session_id, lock=True)
    if session.archived:
        raise HTTPException(409, "会话已归档")
    parts = PurePosixPath(relative_path).parts
    if (
        not parts
        or relative_path.startswith("/")
        or "\\" in relative_path
        or any(part in {".", ".."} or ":" in part or any(ord(c) < 32 for c in part) for part in parts)
    ):
        raise HTTPException(422, "文件夹相对路径无效")
    file_thread = session.config_snapshot.get("project_id") or session.id
    ensure_thread_dirs(file_thread, actor.uid)
    virtual = f"{VIRTUAL_PATH_UPLOADS}/project-files/{'/'.join(parts)}"
    try:
        target = resolve_virtual_path(file_thread, virtual, uid=actor.uid)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    data = await read_upload_with_limit(file, max_size_bytes=5 * 1024 * 1024, too_large_message="文件超过 5 MiB")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("xb") as output:
            output.write(data)
    except FileExistsError:
        raise HTTPException(409, "项目中已存在同名文件，请更名后上传") from None
    return {
        "name": target.name,
        "path": virtual,
        "is_dir": False,
        "size": len(data),
        "artifact_url": f"/api/v1/agents/sessions/{session_id}/artifacts/{virtual.lstrip('/')}",
    }


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


@public_agents.get("/sessions/search")
async def search(
    q: str = Query(min_length=1, max_length=200),
    agent_id: str | None = None,
    limit: int = Query(20, ge=1, le=50),
    offset: int = Query(0, ge=0),
    actor=Depends(authenticate_public_request),
    db=Depends(get_db),
):
    from yuxi.services.public_capability_service import search_sessions

    return await search_sessions(db, actor, q, agent_id=agent_id, limit=limit, offset=offset)


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
            .order_by(
                case((PublicInput.id == session.priority_input_id, 0), (PublicInput.kind == "steer", 1), else_=2),
                PublicInput.received_seq,
            )
            .limit(100)
        )
    )
    turn = await latest_turn(db, session_id)
    return {
        "thread_id": session_id,
        "queue_paused": session.queue_paused,
        "tree_paused": session.tree_paused,
        "status": "paused"
        if session.queue_paused or session.tree_paused
        else ("running" if turn and turn.status in {"in_progress", "requires_action", "cancelling"} else "ready"),
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
async def state(
    session_id: str, include_relations: bool = True, actor=Depends(authenticate_public_request), db=Depends(get_db)
):
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
    result = {"agent_state": value}
    if include_relations:
        result["cooperation"] = await cooperation(session_id, actor, db)
    return result


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
                PublicFile.status != "deleted",
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


@public_agents.delete("/sessions/{session_id}/attachments/{file_id}")
async def delete_attachment(
    session_id: str, file_id: str, actor=Depends(authenticate_public_request), db=Depends(get_db)
):
    from yuxi.services.public_capability_service import delete_attachment as delete

    result = await delete(db, actor, session_id, file_id)
    await db.commit()
    return result


class SaveArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=4096)
    destination_path: str | None = Field(default=None, min_length=1, max_length=4096)


@public_agents.post("/sessions/{session_id}/artifacts/save")
async def save_artifact(
    session_id: str, payload: SaveArtifact, actor=Depends(authenticate_public_request), db=Depends(get_db)
):
    from yuxi.services.public_capability_service import save_artifact as save

    result = await save(db, actor, session_id, payload.path, payload.destination_path)
    await db.commit()
    return result


@public_agents.get("/sessions/{session_id}/audits")
async def audits(session_id: str, request: Request, actor=Depends(authenticate_public_request), db=Depends(get_db)):
    from yuxi.services.public_capability_service import thread_audits

    user = await db.scalar(select(User).where(User.uid == actor.uid))
    if (
        actor.app_id
        or user.role != "superadmin"
        or request.headers.get("authorization", "").removeprefix("Bearer ").startswith("yxkey_")
    ):
        raise HTTPException(403, "仅超级管理员登录会话可读取模型与工具审计")
    return await thread_audits(db, actor, session_id)


@public_agents.post("/sessions/{session_id}/compress")
async def compress(session_id: str, actor=Depends(authenticate_public_request), db=Depends(get_db)):
    from yuxi.services.public_compression_service import compress_session

    return await compress_session(db, actor, session_id)


@public_agents.get("/sessions/{session_id}/artifacts/{path:path}")
async def artifact(
    session_id: str,
    path: str,
    download: bool = False,
    preview: bool = False,
    actor=Depends(authenticate_public_request),
    db=Depends(get_db),
):
    import mimetypes
    from fastapi.responses import FileResponse
    from yuxi.services.thread_files_service import resolve_thread_artifact_view

    session = await require_session(db, actor, session_id)
    target = await resolve_thread_artifact_view(
        thread_id=session_id,
        current_uid=actor.uid,
        db=db,
        path=path,
        file_thread_id=session.config_snapshot.get("project_id") or session.id,
    )
    mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    # Active documents never execute in the product origin, including SVG/HTML previews.
    inline = preview and not download
    if inline and mime not in {"image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf"}:
        mime = "text/plain"
    return FileResponse(
        target,
        filename=target.name,
        media_type=mime,
        content_disposition_type="inline" if inline else "attachment",
        headers={"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox; default-src 'none'"},
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
