"""Durable public reception and immutable receipts, independent of run dispatch."""

import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select

from yuxi.agents.buildin import agent_manager
from yuxi.repositories.agent_repository import AgentRepository
from yuxi.repositories.conversation_repository import ConversationRepository
from yuxi.services.agent_run_service import resolve_agent_run_model_spec
from yuxi.services.knowledge_scope_service import resolve_effective_knowledge_scope
from yuxi.services.public_identity_service import PublicActor, app_principal
from yuxi.storage.postgres.models_business import (
    PublicInput,
    PublicReceipt,
    PublicSession,
    PublicSessionEvent,
    PublicTurn,
    User,
)
from yuxi.utils.datetime_utils import utc_now


def intent_hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def validate_key(key):
    if not key or len(key) > 128 or key != key.strip() or any(ord(char) < 32 for char in key):
        raise HTTPException(422, "Idempotency-Key 须为 1–128 个无首尾空白或控制字符的字符")


async def require_session(db, actor: PublicActor, session_id, *, lock=False):
    stmt = select(PublicSession).where(
        PublicSession.id == session_id,
        PublicSession.uid == actor.uid,
        PublicSession.tenant_id == actor.tenant_id,
        PublicSession.app_id == actor.app_id,
    )
    if lock:
        stmt = stmt.with_for_update()
    item = await db.scalar(stmt.execution_options(populate_existing=True))
    if item is None:
        raise HTTPException(404, "Session 不存在")
    return item


async def append_event(db, session, event_type, payload):
    sequence = session.next_event_seq
    session.next_event_seq += 1
    payload = {
        **payload,
        "type": event_type,
        "session_id": session.id,
        "event_id": payload.get("event_id") or uuid4().hex,
    }
    event = PublicSessionEvent(
        session_id=session.id, sequence=sequence, type=event_type, payload_json=jsonable_encoder(payload)
    )
    db.add(event)
    await db.flush()
    return event


async def visible_agent(db, actor, agent_slug, *, kind="main"):
    user = await db.scalar(select(User).where(User.uid == actor.uid))
    if not user or user.is_deleted or user.is_disabled:
        raise HTTPException(403, "当前用户已不可用")
    if actor.app_id:
        app, _owner = await app_principal(db, uid=actor.uid)
        if app.id != actor.app_id or (kind == "main" and agent_slug not in app.agent_slugs):
            raise HTTPException(404, "Agent 不可用")
    agent = await AgentRepository(db).get_visible_by_slug(slug=agent_slug, user=user, kind=kind)
    if not agent:
        raise HTTPException(404, "Agent 不可用")
    return agent


async def visible_session_agent(db, actor, session):
    root, seen = session, set()
    while root.parent_session_id:
        if root.id in seen or len(seen) >= 32:
            raise HTTPException(409, "协作会话层级异常")
        seen.add(root.id)
        root = await require_session(db, actor, root.parent_session_id)
    if root.id != session.id:
        await visible_agent(db, actor, root.agent_slug)
    return await visible_agent(db, actor, session.agent_slug, kind="subagent" if session.parent_session_id else "main")


async def resolve_public_model(db, actor, agent, requested, snapshot=None):
    from yuxi.services.user_credential_service import list_active_custom_model_specs

    backend = agent_manager.get_agent(agent.backend_id)
    if not backend:
        raise HTTPException(409, "Agent 后端不可用")
    definition = (
        SimpleNamespace(model_policy=snapshot["model_policy"], config_json=snapshot["agent_config"])
        if snapshot is not None
        else agent
    )
    return resolve_agent_run_model_spec(
        requested,
        definition,
        backend,
        allowed_custom_model_specs=await list_active_custom_model_specs(db, actor.uid),
    )


async def create_session(db, actor, payload, *, key, report_created=False):
    validate_key(key)
    raw = payload.model_dump(mode="json")
    fingerprint = intent_hash(raw)
    # A user lock also covers the first Session creation, before its row exists.
    await db.scalar(select(User).where(User.uid == actor.uid).with_for_update())
    previous = await db.scalar(
        select(PublicSession).where(PublicSession.uid == actor.uid, PublicSession.creation_key == key)
    )
    if previous:
        if previous.creation_hash != fingerprint or previous.app_id != actor.app_id:
            raise HTTPException(409, "Idempotency-Key 与已有创建意图冲突")
        receipt = await db.scalar(
            select(PublicReceipt).where(PublicReceipt.session_id == previous.id, PublicReceipt.idempotency_key == key)
        )
        result = (previous, receipt.response_json if receipt else None)
        return (*result, False) if report_created else result
    agent = await visible_agent(db, actor, payload.agent_id)
    model = await resolve_public_model(db, actor, agent, payload.agent.model if payload.agent else None)
    user = await db.scalar(select(User).where(User.uid == actor.uid))
    knowledge_scope = await resolve_effective_knowledge_scope(db=db, user=user, agent_slug=agent.slug)
    session_id = str(uuid4())
    from yuxi.services.public_project_service import create_project, require_project

    project = (await require_project(db, actor, payload.project_id, active=True) if payload.project_id
               else await create_project(db, actor, name=payload.title))
    conversation = await ConversationRepository(db).add_conversation(
        uid=actor.uid,
        agent_id=agent.slug,
        thread_id=session_id,
        title=payload.title,
        metadata={"source": "public_api", "public_app_id": actor.app_id},
    )
    conversation.public_session = True
    session = PublicSession(
        id=session_id,
        uid=actor.uid,
        tenant_id=actor.tenant_id,
        app_id=actor.app_id,
        agent_slug=agent.slug,
        creation_key=key,
        creation_hash=fingerprint,
        title=payload.title,
        config_snapshot={
            "project_id": project.id,
            "backend_id": agent.backend_id,
            "agent_config": deepcopy(agent.config_json or {}),
            "model_spec": model,
            "tool_approval_mode": payload.tool_approval_mode or "always_trust",
            "knowledge_scope_snapshot": knowledge_scope,
            "model_policy": agent.model_policy,
        },
    )
    db.add(session)
    await db.flush()
    created_event = await append_event(db, session, "agent.session.created", {"session_id": session_id})
    receipt = None
    if payload.input is not None:
        from yuxi.services.public_api_schemas import InputMessage, MessageEvent, MessageOptions

        messages = (
            [InputMessage(role="user", content=[{"type": "input_text", "text": payload.input}])]
            if isinstance(payload.input, str)
            else payload.input
        )
        event = MessageEvent(
            type="agent.session.input.message",
            input=messages,
            yuxi=MessageOptions(mode="follow_up", attachment_file_ids=payload.yuxi.attachment_file_ids),
        )
        receipt = await receive_message(db, actor, session, event, key=key, fingerprint=fingerprint)
    from yuxi.services.public_query_service import session_resource

    created_event.payload_json = {
        **created_event.payload_json,
        "session": await session_resource(db, actor, session.id, receipt=receipt),
    }
    await db.commit()
    return (session, receipt, True) if report_created else (session, receipt)


async def receipt_for_key(db, session, key, fingerprint=None):
    previous = await db.scalar(
        select(PublicReceipt).where(PublicReceipt.session_id == session.id, PublicReceipt.idempotency_key == key)
    )
    if previous is not None and fingerprint is not None and previous.intent_hash != fingerprint:
        raise HTTPException(409, "Idempotency-Key 与已有提交意图冲突")
    return previous.response_json if previous else None


async def receive_message(db, actor, session, event, *, key, fingerprint=None):
    """Caller owns the session lock and commits the input, files and receipt together."""
    from yuxi.services.public_input_service import normalize_messages, bind_files

    validate_key(key)
    fingerprint = fingerprint or intent_hash(event.model_dump(mode="json"))
    previous = await receipt_for_key(db, session, key, fingerprint)
    if previous is not None:
        return previous
    if session.archived:
        raise HTTPException(409, "Session 已归档")
    current = await db.scalar(
        select(PublicTurn).where(PublicTurn.session_id == session.id).order_by(PublicTurn.sequence.desc()).limit(1)
    )
    if current and current.status == "requires_action":
        raise HTTPException(409, "当前 Turn 等待回答或审批，请先处理等待点")
    if current and current.status == "cancelling":
        raise HTTPException(409, "正在清理取消的 Turn，请稍后提交")
    await visible_session_agent(db, actor, session)
    count = await db.scalar(
        select(func.count())
        .select_from(PublicInput)
        .where(PublicInput.session_id == session.id, PublicInput.status == "pending")
    )
    if count >= 100:
        raise HTTPException(429, "本 Session 待处理输入已达 100 条")
    messages = normalize_messages(event.input)
    mode = event.yuxi.mode or ("steer" if current and current.status == "in_progress" else "follow_up")
    if mode == "steer" and (event.yuxi.model or event.yuxi.tool_approval_mode or event.yuxi.attachment_file_ids):
        raise HTTPException(422, "steer 不支持覆盖模型、审批模式或携带附件")
    snapshot = deepcopy(session.config_snapshot)
    if event.yuxi.model:
        agent = await visible_session_agent(db, actor, session)
        snapshot["model_spec"] = await resolve_public_model(db, actor, agent, event.yuxi.model, snapshot)
    if event.yuxi.tool_approval_mode:
        snapshot["tool_approval_mode"] = event.yuxi.tool_approval_mode
    item = PublicInput(
        id=uuid4().hex,
        session_id=session.id,
        received_seq=session.next_input_seq,
        kind=mode,
        messages=messages,
        config_snapshot=snapshot,
        attachment_ids=event.yuxi.attachment_file_ids,
    )
    session.next_input_seq += 1
    session.last_active_at = utc_now()
    db.add(item)
    await db.flush()
    await bind_files(db, actor, item)
    if session.queue_paused or session.tree_paused:
        session.queue_paused = session.tree_paused = False
        session.priority_input_id = item.id
    response = {
        "object": "yuxi.session.event.accepted",
        "event_id": uuid4().hex,
        "session_id": session.id,
        "input_id": item.id,
        "turn_id": None,
        "run_id": None,
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
    return response
