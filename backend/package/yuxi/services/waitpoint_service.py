"""Persist human input before terminal publication and consume it atomically."""

import hashlib
import json

from fastapi import HTTPException
from sqlalchemy import select

from yuxi.storage.postgres.models_business import AgentRun, AgentRunWaitpoint
from yuxi.utils.datetime_utils import utc_now


async def persist_waitpoint(db, *, run_id, chunk, approval_calls=None):
    questions = chunk.get("questions")
    if not isinstance(questions, list) or not questions:
        return
    if len(questions) > 20 or any(
        not isinstance(q, dict)
        or not isinstance(q.get("question_id"), str)
        or not q["question_id"].strip()
        or not isinstance(q.get("question"), str)
        for q in questions
    ):
        raise ValueError("人工等待点必须包含有效的 question_id 和问题正文")
    if len({q["question_id"] for q in questions}) != len(questions):
        raise ValueError("人工等待点的 question_id 不能重复")
    payload = {
        "questions": questions,
        "source": str(chunk.get("source") or "interrupt"),
        "status": str(chunk.get("status") or "ask_user_question_required"),
    }
    run = await db.scalar(select(AgentRun).where(AgentRun.id == run_id).with_for_update())
    if run is None:
        raise ValueError("等待点所属运行不存在")
    existing = await db.get(AgentRunWaitpoint, run_id)
    details = (
        approval_calls
        if approval_calls is not None
        else (existing.payload_json.get("approval_calls") if existing else None)
    )
    if details is not None:
        if payload["source"] != "tool_approval" or {item["call_id"] for item in details} != {
            q["question_id"] for q in questions
        }:
            raise ValueError("工具审批的调用与问题不匹配")
        payload["approval_calls"] = details
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
    if len(raw.encode("utf-8")) > 65536:
        raise ValueError("人工等待点内容超过上限")
    revision = hashlib.sha256(raw.encode()).hexdigest()
    if existing:
        if existing.revision != revision:
            raise ValueError("同一运行的人工等待点内容不能改写")
        return
    db.add(
        AgentRunWaitpoint(run_id=run_id, tenant_id=run.tenant_id, uid=run.uid, revision=revision, payload_json=payload)
    )
    await db.flush()


async def get_waitpoint(db, *, uid, run_id):
    from yuxi.services.collaboration_service import _run, assert_resume_allowed

    await assert_resume_allowed(db, uid=uid, run_id=run_id)
    run = await _run(db, uid, run_id)
    if run.status != "interrupted":
        raise HTTPException(409, "成员当前没有待处理的人工输入")
    item = await db.get(AgentRunWaitpoint, run_id)
    if item is None or item.uid != uid or item.tenant_id != run.tenant_id:
        raise HTTPException(409, "旧等待点未保存完整问题，无法在协作面板恢复")
    return {
        "run_id": run_id,
        "revision": item.revision,
        "thread_id": run.conversation_thread_id,
        "agent_slug": run.agent_slug,
        **item.payload_json,
    }


async def claim_waitpoint(db, *, parent_run_id, resume_run_id):
    item = await db.get(AgentRunWaitpoint, parent_run_id)
    if item is not None:
        if item.resolved_by_run_id and item.resolved_by_run_id != resume_run_id:
            raise HTTPException(409, "此等待点已处理")
        item.resolved_by_run_id, item.resolved_at = resume_run_id, utc_now()
        await db.flush()


async def resume_waitpoint(db, *, uid, run_id, revision, answer, request_id):
    from yuxi.repositories.agent_run_repository import AgentRunRepository
    from yuxi.services.agent_run_service import create_agent_run_view, _build_run_response
    from yuxi.services.collaboration_service import _run, is_worker_member, root_run
    from yuxi.storage.postgres.models_business import Message

    try:
        serialized = json.dumps(answer, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (ValueError, TypeError):
        raise HTTPException(422, "回答须为有效 JSON") from None
    if len(serialized.encode("utf-8")) > 65536:
        raise HTTPException(422, "回答超过 64 KiB 上限")
    key = hashlib.sha256(f"waitpoint:{uid}:{run_id}:{request_id}".encode()).hexdigest()
    await root_run(db, uid=uid, run_id=run_id, lock=True)
    previous = await AgentRunRepository(db).get_run_by_request_id(key, uid)
    if previous:
        message = await db.get(Message, previous.input_message_id)
        if previous.created_by_run_id != run_id or message is None or json.loads(message.content) != answer:
            raise HTTPException(409, "request_id 与已有回答冲突")
        return _build_run_response(previous)
    point = await get_waitpoint(db, uid=uid, run_id=run_id)
    if point["revision"] != revision:
        raise HTTPException(409, "等待点内容已变更，请重新加载")
    identifiers = {question["question_id"] for question in point["questions"]}
    if answer != "reject" and (not isinstance(answer, dict) or set(answer) != identifiers):
        raise HTTPException(422, "回答必须覆盖当前等待点的全部 question_id")
    run = await _run(db, uid, run_id)
    agent_slug, thread_id = run.agent_slug, run.conversation_thread_id
    kind = "subagent" if is_worker_member(run) else "main"
    # Release the read lock: creation takes quota locks before the root lock.
    # Its insertion guard checks stop and consumption state again atomically.
    await db.commit()
    result = await create_agent_run_view(
        input_message=None,
        agent_slug=agent_slug,
        thread_id=thread_id,
        meta={"request_id": key},
        current_uid=uid,
        db=db,
        resume=answer,
        created_by_run_id=run_id,
        agent_kind=kind,
    )
    accepted = await AgentRunRepository(db).get_run_by_request_id(key, uid)
    message = await db.get(Message, accepted.input_message_id) if accepted else None
    if message is None or json.loads(message.content) != answer:
        raise HTTPException(409, "request_id 与已有回答冲突")
    return result
