"""AgentRun lifecycle service.

This module owns the durable ``AgentRun`` contract: validating the run scope,
persisting the input message, creating the run row, enqueueing worker execution,
streaming run events, loading final results and requesting cancellation.

Keep source-specific orchestration outside this file. Normal chat, external
invocation and subagent tools may all create AgentRun records, but each caller
should translate its own request shape into this module's public run APIs first.
The worker then executes every run through the same queue and ``chat_service``
runtime path, so this module must not depend on agent-call, evaluation or
subagent presentation details.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from yuxi.agents.buildin import agent_manager
from yuxi.agents.models import resolve_chat_model_spec
from yuxi.models.providers.cache import model_cache
from yuxi.repositories.agent_repository import AgentRepository
from yuxi.repositories.agent_run_repository import TERMINAL_RUN_STATUSES, AgentRunRepository
from yuxi.repositories.conversation_repository import ConversationRepository
from yuxi.repositories.knowledge_retrieval_repository import (
    KnowledgeRetrievalRepository,
    serialize_retrieval_run,
)
from yuxi.services.input_message_service import (
    AgentRunInputMessage,
    build_resume_input_message,
)
from yuxi.services.knowledge_scope_service import resolve_effective_knowledge_scope
from yuxi.services.run_queue_service import (
    append_run_stream_event,
    build_run_event_envelope,
    get_arq_pool,
    get_last_run_stream_seq,
    list_recent_run_stream_events,
    list_run_stream_events,
    normalize_after_seq,
    publish_cancel_signal,
)
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import AgentRun, Message, User, UserModelPreference, UserQuota
from yuxi.utils.datetime_utils import utc_now_naive
from yuxi.utils.hash_utils import hash_id
from yuxi.utils.logging_config import logger

SSE_HEARTBEAT_SECONDS = int(os.getenv("RUN_SSE_HEARTBEAT_SECONDS", "15"))  # SSE 连接空闲多久发送心跳
SSE_MAX_CONNECTION_MINUTES = int(os.getenv("RUN_SSE_MAX_CONNECTION_MINUTES", "30"))  # SSE 连接最大持续时间
SSE_POLL_INTERVAL_SECONDS = float(os.getenv("RUN_SSE_POLL_INTERVAL_SECONDS", "1.0"))  # SSE 轮询间隔
RUN_PROGRESS_RECENT_EVENT_SCAN_LIMIT = 100
RUN_PROGRESS_MESSAGE_LIMIT = 3
RUN_PROGRESS_CONTENT_MAX_CHARS = 800
AGENT_RUN_PROTOCOL_VERSION = "1.1"


def _public_knowledge_scope(snapshot: object) -> dict[str, Any]:
    """Return the immutable, non-secret knowledge scope used by one run."""
    if not isinstance(snapshot, dict):
        snapshot = {}
    members = []
    for raw_member in snapshot.get("members") or []:
        if not isinstance(raw_member, dict):
            continue
        members.append(
            {
                "kb_id": raw_member.get("kb_id"),
                "kb_name": raw_member.get("kb_name"),
                "kb_type": raw_member.get("kb_type"),
                "priority": raw_member.get("priority"),
                "document_enabled": bool(raw_member.get("document_enabled", False)),
                "graph_enabled": bool(raw_member.get("graph_enabled", False)),
                "structured_enabled": bool(raw_member.get("structured_enabled", False)),
                "included_via": raw_member.get("included_via"),
            }
        )
    return {
        "scope_id": snapshot.get("scope_id"),
        "scope_version": snapshot.get("scope_version"),
        "scope_mode": snapshot.get("scope_mode"),
        "knowledge_strategy": snapshot.get("knowledge_strategy"),
        "retrieval_mode": snapshot.get("retrieval_mode"),
        "allow_web": bool(snapshot.get("allow_web", False)),
        "kb_count": len(members),
        "members": members,
    }


def _public_retrieval_summary(record: object) -> dict[str, Any]:
    serialized = serialize_retrieval_run(record)
    return {
        key: serialized.get(key)
        for key in (
            "retrieval_id",
            "status",
            "intent",
            "query_mode",
            "planner_version",
            "entity_resolver_version",
            "retrieval_orchestrator_version",
            "claim_validator_version",
            "contract_schema_version",
            "source_status",
            "returned_relation_count",
            "returned_claim_count",
            "returned_evidence_count",
            "warnings",
            "error_code",
            "finished_at",
        )
    }


def _build_server_run_context(run: object, retrieval_records: list[object] | None = None) -> dict[str, Any]:
    input_payload = getattr(run, "input_payload", None)
    if not isinstance(input_payload, dict):
        input_payload = {}
    return {
        "protocol_version": AGENT_RUN_PROTOCOL_VERSION,
        "model_spec": input_payload.get("model_spec"),
        "knowledge_scope": _public_knowledge_scope(input_payload.get("knowledge_scope_snapshot")),
        "knowledge_retrievals": [_public_retrieval_summary(record) for record in (retrieval_records or [])],
    }


async def _load_server_run_context(run: object, db: AsyncSession) -> dict[str, Any]:
    if not isinstance(getattr(run, "input_payload", None), dict):
        return _build_server_run_context(run)
    records: list[object] = []
    try:
        records = await KnowledgeRetrievalRepository(db).list_for_run(str(getattr(run, "id", "")))
        return _build_server_run_context(run, records)
    except Exception as error:
        # Run completion must remain available even if optional audit metadata cannot be loaded.
        logger.warning(f"Failed to load knowledge retrieval context for run {getattr(run, 'id', '')}: {error}")
        return _build_server_run_context(run)


def _resolve_agent_run_request_id(
    *,
    meta: dict,
    run_type: Literal["chat", "resume"],
    resume: object | None,
    created_by_run_id: str | None,
) -> str:
    raw_request_id = meta.get("request_id")
    if raw_request_id:
        return str(raw_request_id)
    if run_type == "resume":
        resume_key = json.dumps(resume, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hash_id("resume:", f"{created_by_run_id}:{resume_key}", length=64)
    return str(uuid.uuid4())


class AgentRunWaitTimeout(Exception):
    """等待结束但 run 尚未进入终态。"""

    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        status = str(result.get("status") or "unknown")
        run_id = str(result.get("agent_run_id") or result.get("run_id") or "")
        super().__init__(f"agent run {run_id} is still {status} after waiting")


def resolve_agent_run_model_spec(
    model_spec: str | None,
    agent_item,
    agent_backend,
    user_model_spec: str | None = None,
) -> str:
    """解析本次 run 实际使用的模型：显式覆盖优先，否则配置模型，最后系统默认模型。"""
    normalized = model_spec.strip() if isinstance(model_spec, str) else None
    if normalized:
        info = model_cache.get_model_info(normalized)
        if not info or info.model_type != "chat":
            raise HTTPException(status_code=422, detail=f"未找到可用聊天模型: '{normalized}'")
        return normalized

    if user_model_spec:
        user_info = model_cache.get_model_info(user_model_spec)
        if user_info and user_info.model_type == "chat":
            return user_model_spec
        # 用户偏好指向的模型可能已被管理员下线；此时回落智能体/系统默认并留痕
        logger.warning(f"用户级模型偏好已失效，回退默认解析: {user_model_spec}")

    context = agent_backend.context_schema()
    config_json = getattr(agent_item, "config_json", None) or {}
    config_context = config_json.get("context") if isinstance(config_json, dict) else {}
    if isinstance(config_context, dict):
        context.update_from_dict(config_context)

    return resolve_chat_model_spec(getattr(context, "model", None))


def _build_run_response(run) -> dict:
    return {
        "run_id": run.id,
        "thread_id": run.conversation_thread_id,
        "status": run.status,
        "request_id": run.request_id,
        "stream_url": f"/api/agent/runs/{run.id}/events",
        "run_context": _build_server_run_context(run),
    }


def _format_sse(data: dict, event: str, event_id: str | None = None) -> str:
    lines = [f"event: {event}", f"data: {json.dumps(data, ensure_ascii=False)}"]
    if event_id:
        lines.append(f"id: {event_id}")
    lines.append("")
    return "\n".join(lines) + "\n"


def _format_heartbeat() -> str:
    return ": heartbeat\n\n"


def _compact_message_dict(message: dict) -> dict:
    compact = {
        key: message[key] for key in ("id", "role", "content", "type", "message_type") if message.get(key) is not None
    }
    extra_metadata = message.get("extra_metadata")
    if isinstance(extra_metadata, dict) and extra_metadata.get("attachments"):
        compact["extra_metadata"] = {"attachments": extra_metadata["attachments"]}
    return compact


def _compact_semantic_stream_event(stream_event: dict) -> dict:
    event_type = stream_event.get("type")
    if event_type == "message_delta":
        return {
            key: stream_event[key]
            for key in ("type", "message_id", "content", "reasoning_content", "additional_reasoning_content")
            if stream_event.get(key)
        }

    if event_type in {"tool_call", "tool_call_delta"}:
        compact = {
            key: stream_event[key]
            for key in ("type", "message_id", "tool_call_id", "name", "args", "args_delta")
            if stream_event.get(key) is not None and stream_event.get(key) != ""
        }
        if stream_event.get("index"):
            compact["index"] = stream_event["index"]
        return compact

    return {key: value for key, value in stream_event.items() if key not in {"thread_id", "namespace"}}


def _compact_tool_stream_event(event: dict) -> dict:
    compact = {key: event[key] for key in ("method",) if event.get(key)}
    data = event.get("data")
    if isinstance(data, dict):
        compact_data = {
            key: data[key]
            for key in ("event", "tool_call_id", "tool_name", "output", "error")
            if data.get(key) is not None and data.get(key) != ""
        }
        if compact_data:
            compact["data"] = compact_data
    return compact


def _compact_stream_chunk(chunk: dict) -> dict:
    compact = {
        key: chunk[key]
        for key in (
            "status",
            "run_id",
            "message",
            "error_type",
            "error_message",
            "retryable",
            "job_try",
            "questions",
            "interrupt_info",
            "source",
            "agent_state",
            "compression",
        )
        if chunk.get(key) is not None and chunk.get(key) != ""
    }
    if isinstance(chunk.get("msg"), dict):
        compact["msg"] = _compact_message_dict(chunk["msg"])
    if isinstance(chunk.get("stream_event"), dict):
        compact["stream_event"] = _compact_semantic_stream_event(chunk["stream_event"])
    if isinstance(chunk.get("event"), dict):
        compact["event"] = _compact_tool_stream_event(chunk["event"])
    return compact


def _request_id_from_chunk(chunk: object) -> str | None:
    if not isinstance(chunk, dict):
        return None
    request_id = chunk.get("request_id")
    if isinstance(request_id, str) and request_id:
        return request_id
    msg = chunk.get("msg")
    extra_metadata = msg.get("extra_metadata") if isinstance(msg, dict) else None
    if isinstance(extra_metadata, dict):
        request_id = extra_metadata.get("request_id")
        if isinstance(request_id, str) and request_id:
            return request_id
    return None


def _request_id_from_payload(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    request_id = payload.get("request_id")
    if isinstance(request_id, str) and request_id:
        return request_id
    request_id = _request_id_from_chunk(payload.get("chunk"))
    if request_id:
        return request_id
    items = payload.get("items")
    if isinstance(items, list):
        for item in items:
            request_id = _request_id_from_chunk(item)
            if request_id:
                return request_id
    return None


def _compact_run_event_payload(event_type: str, payload: dict | None) -> dict:
    if not isinstance(payload, dict):
        return {}

    if event_type == "messages":
        compact: dict = {}
        if isinstance(payload.get("items"), list):
            compact["items"] = [
                _compact_stream_chunk(item) if isinstance(item, dict) else item for item in payload["items"]
            ]
        if isinstance(payload.get("chunk"), dict):
            compact["chunk"] = _compact_stream_chunk(payload["chunk"])
        return compact

    compact = {key: value for key, value in payload.items() if key not in {"chunk", "request_id"}}
    if isinstance(payload.get("chunk"), dict):
        compact["chunk"] = _compact_stream_chunk(payload["chunk"])
    return compact


def _is_empty_agent_state(agent_state: object) -> bool:
    if not isinstance(agent_state, dict):
        return False
    return all(not value for value in agent_state.values())


def _compact_run_event_envelope(envelope: dict) -> dict | None:
    event_type = str(envelope.get("event") or "")
    payload = envelope.get("payload")
    if event_type == "metadata":
        return None
    if event_type == "custom" and isinstance(payload, dict) and payload.get("name") == "yuxi.agent_state":
        state = payload.get("agent_state")
        chunk = payload.get("chunk") if isinstance(payload.get("chunk"), dict) else {}
        if _is_empty_agent_state(state) or _is_empty_agent_state(chunk.get("agent_state")):
            return None

    compact = {key: envelope[key] for key in ("run_id", "thread_id") if key in envelope}
    request_id = _request_id_from_payload(payload)
    if request_id:
        compact["request_id"] = request_id
    compact["payload"] = _compact_run_event_payload(event_type, payload)
    return compact


def _progress_message_from_chunk(chunk: dict, *, seq: str) -> dict | None:
    """把单个消息 chunk 转成 status 可展示的一条进度。"""
    stream_event = chunk.get("stream_event")
    if not isinstance(stream_event, dict):
        return None
    stream_type = stream_event.get("type")
    message_id = str(stream_event.get("message_id") or "").strip()

    content = ""
    kind = ""
    if stream_type == "message_delta":
        content = (
            stream_event.get("content")
            or stream_event.get("reasoning_content")
            or stream_event.get("additional_reasoning_content")
            or ""
        )
        kind = "assistant_message" if stream_event.get("content") else "assistant_reasoning"
    elif stream_type in {"tool_call", "tool_call_delta"}:
        tool_name = str(stream_event.get("name") or stream_event.get("tool_call_id") or "工具").strip()
        content = f"调用工具 {tool_name}" if stream_type == "tool_call" else f"正在准备工具 {tool_name}"
        kind = stream_type
    else:
        return None

    content = str(content).strip()
    if not content:
        return None
    if len(content) > RUN_PROGRESS_CONTENT_MAX_CHARS:
        content = "..." + content[-RUN_PROGRESS_CONTENT_MAX_CHARS:]

    base = {"seq": seq}
    if message_id:
        base["message_id"] = message_id
    tool_call_id = str(stream_event.get("tool_call_id") or "").strip()
    if tool_call_id:
        base["tool_call_id"] = tool_call_id
    return {**base, "kind": kind, "content": content}


async def get_agent_run_progress(run_id: str, *, message_limit: int = RUN_PROGRESS_MESSAGE_LIMIT) -> dict:
    """读取适合 status 轮询返回的轻量运行进度快照。"""
    try:
        events = await list_recent_run_stream_events(run_id, limit=RUN_PROGRESS_RECENT_EVENT_SCAN_LIMIT)
    except Exception as e:
        logger.warning(f"Failed to read run progress events for run {run_id}: {e}")
        return {"last_seq": "0-0", "messages": []}

    last_seq = str(events[0]["seq"]) if events else "0-0"
    limit = max(1, int(message_limit or RUN_PROGRESS_MESSAGE_LIMIT))
    messages = []

    for event in events:
        envelope = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if event.get("event_type") != "messages" and envelope.get("event") != "messages":
            continue
        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            continue

        chunks = []
        if isinstance(payload.get("chunk"), dict):
            chunks.append(payload["chunk"])
        if isinstance(payload.get("items"), list):
            chunks.extend(item for item in payload["items"] if isinstance(item, dict))

        for chunk in reversed(chunks):
            message = _progress_message_from_chunk(chunk, seq=str(event.get("seq") or ""))
            if message:
                messages.append(message)
            if len(messages) >= limit:
                return {"last_seq": last_seq, "messages": list(reversed(messages))}

    return {"last_seq": last_seq, "messages": list(reversed(messages))}


async def _get_user_model_pref(*, db: AsyncSession, uid: str) -> str | None:
    result = await db.execute(select(UserModelPreference).filter(UserModelPreference.uid == uid))
    pref = result.scalar_one_or_none()
    return pref.chat_model_spec if pref else None


async def _enforce_user_quota(*, db: AsyncSession, uid: str) -> None:
    """运行创建前的配额预检；未配置配额的用户不受限。"""
    if not uid:
        return
    quota = (
        await db.execute(select(UserQuota).filter(UserQuota.uid == uid).with_for_update())
    ).scalar_one_or_none()
    if quota is None:
        return
    now = utc_now_naive()
    if quota.daily_run_limit is not None:
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        used_runs = (
            await db.execute(
                select(func.count(AgentRun.id)).filter(
                    AgentRun.uid == uid,
                    AgentRun.created_at >= day_start,
                    AgentRun.status.notin_(["failed", "cancelled"]),
                )
            )
        ).scalar() or 0
        if int(used_runs) >= int(quota.daily_run_limit):
            raise HTTPException(
                status_code=429,
                detail=f"今日运行次数已达配额（{quota.daily_run_limit} 次），请联系管理员调整",
            )
    if quota.monthly_token_limit is not None:
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        used_tokens = (
            await db.execute(
                select(func.coalesce(func.sum(AgentRun.total_tokens), 0)).filter(
                    AgentRun.uid == uid,
                    AgentRun.created_at >= month_start,
                )
            )
        ).scalar() or 0
        if int(used_tokens) >= int(quota.monthly_token_limit):
            raise HTTPException(
                status_code=429,
                detail=f"本月 token 用量已达配额（{quota.monthly_token_limit}），请联系管理员调整",
            )


async def create_agent_run_view(
    *,
    input_message: AgentRunInputMessage | None,
    agent_slug: str,
    thread_id: str,
    meta: dict,
    current_uid: str,
    db: AsyncSession,
    model_spec: str | None = None,
    resume: object | None = None,
    created_by_run_id: str | None = None,
) -> dict:
    """创建 chat/resume run 的 HTTP 入口，输入正文由 Message 承载，run 只登记运行元数据。"""
    meta = meta or {}
    if input_message is None and resume is None:
        raise HTTPException(status_code=422, detail="input_message 或 resume 不能为空")

    run_type = "resume" if resume is not None else "chat"
    run_created_by_id = created_by_run_id if run_type == "resume" else None
    request_id = _resolve_agent_run_request_id(
        meta=meta,
        run_type=run_type,
        resume=resume,
        created_by_run_id=run_created_by_id,
    )

    scope = await prepare_agent_run_creation_scope(
        agent_slug=agent_slug,
        conversation_thread_id=thread_id,
        current_uid=current_uid,
        db=db,
        request_id=request_id,
        run_type=run_type,
        agent_kind="main",
        created_by_run_id=run_created_by_id,
    )
    if scope.existing_run:
        return _build_run_response(scope.existing_run)

    # 幂等命中不重复消耗配额。锁定用户配额行直到本次 run 创建事务提交，
    # 避免同一用户的并发请求同时通过“先计数、后创建”的检查窗口。
    await _enforce_user_quota(db=db, uid=current_uid)

    if run_type == "resume":
        resolved_model_spec = scope.parent_run.input_payload["model_spec"]
    else:
        user_model_spec = await _get_user_model_pref(db=db, uid=current_uid)
        resolved_model_spec = resolve_agent_run_model_spec(
            model_spec,
            scope.agent_item,
            scope.agent_backend,
            user_model_spec=user_model_spec,
        )

    parent_payload = scope.parent_run.input_payload if run_type == "resume" else None
    knowledge_scope_snapshot = (
        parent_payload.get("knowledge_scope_snapshot") if isinstance(parent_payload, dict) else None
    )
    if not isinstance(knowledge_scope_snapshot, dict):
        knowledge_scope_snapshot = await resolve_effective_knowledge_scope(
            db=db,
            user=scope.current_user,
            agent_slug=agent_slug,
        )

    run_input_message = _prepare_run_input_message(
        run_type=run_type,
        input_message=input_message,
        resume=resume,
        request_id=request_id,
        model_spec=resolved_model_spec,
        meta=meta,
    )

    persisted_input_message = await create_agent_run_input_message(
        db=db,
        conversation_id=scope.conversation.id,
        request_id=request_id,
        input_message=run_input_message,
    )
    input_payload = {
        "model_spec": resolved_model_spec,
        "knowledge_scope_snapshot": knowledge_scope_snapshot,
    }

    run, created = await persist_agent_run_record(
        agent_slug=agent_slug,
        conversation_thread_id=thread_id,
        current_uid=current_uid,
        db=db,
        request_id=request_id,
        conversation_id=scope.conversation.id,
        run_type=run_type,
        input_payload=input_payload,
        persisted_input_message=persisted_input_message,
        created_by_run_id=run_created_by_id,
    )
    if created:
        await db.commit()
        try:
            await enqueue_agent_run(run.id)
        except Exception:
            # 入队失败必须立刻把 run 置为失败：幂等命中会让同 request_id 的重试
            # 永远不再入队，线程随即被唯一活跃索引锁死（只能靠对账任务回收）。
            logger.exception(f"Failed to enqueue agent run {run.id}")
            await mark_run_enqueue_failed(run.id)
            raise

    return _build_run_response(run)


@dataclass(frozen=True)
class AgentRunCreationScope:
    """run 创建前置校验后的数据库作用域，避免和 Agent runtime context 混淆。"""

    conversation: Any
    agent_item: Any
    agent_backend: Any
    current_user: Any
    existing_run: Any | None
    parent_run: Any | None = None


def _prepare_run_input_message(
    *,
    run_type: Literal["chat", "resume"],
    input_message: AgentRunInputMessage | None,
    resume: object | None,
    request_id: str,
    model_spec: str,
    meta: dict,
) -> AgentRunInputMessage:
    metadata: dict[str, Any] = {"request_id": request_id}
    if attachment_file_ids := (meta.get("attachment_file_ids") or []):
        metadata["attachment_file_ids"] = attachment_file_ids
    if source := meta.get("source"):
        metadata["source"] = source
    if isinstance(meta.get("agent_invocation_meta"), dict):
        metadata["agent_invocation_meta"] = meta["agent_invocation_meta"]
    if run_type == "chat":
        if input_message is None:
            raise HTTPException(status_code=422, detail="input_message 不能为空")
        if raw_message := input_message.raw_message():
            metadata["raw_message"] = raw_message
        return input_message.with_metadata(metadata)

    metadata["resume"] = resume
    metadata["source"] = "ask_user_question_resume"
    return build_resume_input_message(resume).with_metadata(metadata)


def _same_run_request_scope(
    run,
    *,
    uid: str,
    agent_slug: str,
    conversation_thread_id: str,
    run_type: str,
    created_by_run_id: str | None = None,
    subagent_thread_relation_id: int | None = None,
) -> bool:
    """判断幂等命中的 run 是否确实属于同一次语义创建请求。"""
    return (
        run.uid == str(uid)
        and run.agent_slug == agent_slug
        and run.conversation_thread_id == conversation_thread_id
        and run.run_type == run_type
        and run.created_by_run_id == created_by_run_id
        and getattr(run, "subagent_thread_relation_id", None) == subagent_thread_relation_id
    )


def _run_busy_exception(*, active_run, agent_slug: str, conversation_thread_id: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": "run_busy",
            "message": "该智能体线程正在运行，请等待、查询或取消当前运行后再继续",
            "active_run_id": active_run.id,
            "active_run_status": active_run.status,
            "agent_slug": agent_slug,
            "thread_id": conversation_thread_id,
        },
    )


async def create_agent_run_input_message(
    *,
    db: AsyncSession,
    conversation_id: int,
    request_id: str,
    input_message: AgentRunInputMessage,
) -> Message:
    """先落库输入消息；run 创建后再回填 run_id，避免 Message 外键先指向不存在的 run。"""
    message = Message(
        conversation_id=conversation_id,
        role="user",
        content=input_message.content,
        message_type=input_message.message_type,
        image_content=input_message.image_content,
        request_id=request_id,
        delivery_status="complete",
        extra_metadata=input_message.extra_metadata,
    )
    db.add(message)
    await db.flush()
    return message


async def persist_agent_run_record(
    *,
    agent_slug: str,
    conversation_thread_id: str,
    current_uid: str,
    db: AsyncSession,
    request_id: str,
    conversation_id: int,
    run_type: str,
    input_payload: dict,
    persisted_input_message: Message,
    created_by_run_id: str | None = None,
    subagent_thread_relation_id: int | None = None,
) -> tuple[Any, bool]:
    """登记一条 AgentRun 并绑定已创建的输入消息，返回是否为本次新建。"""
    run_id = str(uuid.uuid4())
    try:
        async with db.begin_nested():
            run = await AgentRunRepository(db).create_run(
                run_id=run_id,
                conversation_thread_id=conversation_thread_id,
                agent_slug=agent_slug,
                uid=str(current_uid),
                request_id=request_id,
                input_payload=input_payload,
                conversation_id=conversation_id,
                created_by_run_id=created_by_run_id,
                subagent_thread_relation_id=subagent_thread_relation_id,
                run_type=run_type,
                input_message_id=persisted_input_message.id,
            )
            persisted_input_message.run_id = run_id
            await db.flush()
    except IntegrityError:
        run_repo = AgentRunRepository(db)
        existing = await run_repo.get_run_by_request_id(request_id, str(current_uid))
        if existing and _same_run_request_scope(
            existing,
            uid=str(current_uid),
            agent_slug=agent_slug,
            conversation_thread_id=conversation_thread_id,
            run_type=run_type,
            created_by_run_id=created_by_run_id,
            subagent_thread_relation_id=subagent_thread_relation_id,
        ):
            await db.delete(persisted_input_message)
            await db.flush()
            return existing, False
        active_run = await run_repo.get_active_run_by_thread_for_user(
            agent_slug=agent_slug,
            conversation_thread_id=conversation_thread_id,
            uid=str(current_uid),
        )
        if active_run:
            raise _run_busy_exception(
                active_run=active_run,
                agent_slug=agent_slug,
                conversation_thread_id=conversation_thread_id,
            )
        raise HTTPException(status_code=409, detail="request_id 冲突")

    return run, True


async def prepare_agent_run_creation_scope(
    *,
    agent_slug: str,
    conversation_thread_id: str,
    current_uid: str,
    db: AsyncSession,
    request_id: str,
    run_type: Literal["chat", "resume", "subagent"],
    agent_kind: Literal["main", "subagent"],
    created_by_run_id: str | None = None,
    subagent_thread_relation_id: int | None = None,
) -> AgentRunCreationScope:
    """校验 run 创建作用域，加载对话、智能体、后端和幂等状态，并拒绝同线程并发写入。"""
    if not conversation_thread_id:
        raise HTTPException(status_code=422, detail="conversation_thread_id 不能为空")

    conversation = await ConversationRepository(db).get_conversation_by_thread_id(conversation_thread_id)
    if not conversation or conversation.uid != str(current_uid) or conversation.status == "deleted":
        raise HTTPException(status_code=404, detail="对话线程不存在")
    # Conversation.agent_id 是历史字段名，实际保存的是 Agent.slug。
    if conversation.agent_id != agent_slug:
        raise HTTPException(status_code=409, detail="已有线程已绑定智能体，不能切换")

    user_result = await db.execute(select(User).where(User.uid == str(current_uid)))
    current_user = user_result.scalar_one_or_none()
    if not current_user:
        raise HTTPException(status_code=404, detail="用户不存在")

    agent_repo = AgentRepository(db)
    agent_item = await agent_repo.get_visible_by_slug(slug=agent_slug, user=current_user, kind=agent_kind)
    if not agent_item:
        raise HTTPException(status_code=404, detail="智能体不存在")

    agent_backend = agent_manager.get_agent(agent_item.backend_id)
    if not agent_backend:
        raise HTTPException(status_code=404, detail=f"智能体后端 {agent_item.backend_id} 不存在")

    run_repo = AgentRunRepository(db)
    existing = await run_repo.get_run_by_request_id(request_id, str(current_uid))
    if existing and not _same_run_request_scope(
        existing,
        uid=str(current_uid),
        agent_slug=agent_slug,
        conversation_thread_id=conversation_thread_id,
        run_type=run_type,
        created_by_run_id=created_by_run_id,
        subagent_thread_relation_id=subagent_thread_relation_id,
    ):
        raise HTTPException(status_code=409, detail="request_id 冲突")
    parent_run = None
    if run_type == "resume":
        if not created_by_run_id:
            raise HTTPException(status_code=422, detail="created_by_run_id 不能为空")
        if not existing:
            parent_run = await run_repo.get_run_for_user(created_by_run_id, str(current_uid))
            if not parent_run or parent_run.conversation_thread_id != conversation_thread_id:
                raise HTTPException(status_code=404, detail="被恢复的运行任务不存在")
            if parent_run.status != "interrupted":
                raise HTTPException(status_code=409, detail="只有 interrupted run 可以恢复")
            parent_payload = parent_run.input_payload
            if not isinstance(parent_payload, dict) or not parent_payload.get("model_spec"):
                raise HTTPException(status_code=409, detail="被恢复的运行任务缺少模型快照")
    if not existing:
        active_run = await run_repo.get_active_run_by_thread_for_user(
            agent_slug=agent_slug,
            conversation_thread_id=conversation_thread_id,
            uid=str(current_uid),
        )
        if active_run:
            raise _run_busy_exception(
                active_run=active_run,
                agent_slug=agent_slug,
                conversation_thread_id=conversation_thread_id,
            )
    return AgentRunCreationScope(
        conversation=conversation,
        agent_item=agent_item,
        agent_backend=agent_backend,
        current_user=current_user,
        existing_run=existing,
        parent_run=parent_run,
    )


async def enqueue_agent_run(run_id: str) -> None:
    """把已持久化的 run 投递到后台 worker 队列。"""
    queue = await get_arq_pool()
    await queue.enqueue_job("process_agent_run", run_id, _job_id=f"run:{run_id}")


# 孤儿 run 对账阈值：pending 超过入队正常耗时、cancel_requested 超过取消生效
# 正常耗时即判定失联；running 需超过 job_timeout（3600s）留出余量。
STALE_PENDING_CUTOFF_SECONDS = 600
STALE_RUNNING_CUTOFF_SECONDS = 5400
STALE_CANCEL_CUTOFF_SECONDS = 600


async def mark_run_enqueue_failed(run_id: str) -> None:
    async with pg_manager.get_async_session_context() as db:
        await AgentRunRepository(db).set_terminal_status(
            run_id,
            status="failed",
            error_type="enqueue_failed",
            error_message="任务入队失败，请重新发送",
        )


async def reconcile_stale_agent_runs(ctx: Any = None) -> int:
    """把卡在非终态的孤儿 run 收敛为终态，释放被唯一活跃索引锁住的线程。

    由 worker 启动与定时任务调用，覆盖三类窗口：commit 后入队前进程崩溃
    （永久 pending）、worker 崩溃/OOM（无人收尾的 running）、取消信号丢失
    （停滞的 cancel_requested）。
    """
    del ctx
    now = utc_now_naive()
    reconciled: list[tuple[str, str | None, str, dict]] = []
    async with pg_manager.get_async_session_context() as db:
        repo = AgentRunRepository(db)
        stale_runs = await repo.list_stale_non_terminal_runs(
            now=now,
            pending_cutoff_seconds=STALE_PENDING_CUTOFF_SECONDS,
            running_cutoff_seconds=STALE_RUNNING_CUTOFF_SECONDS,
            cancel_cutoff_seconds=STALE_CANCEL_CUTOFF_SECONDS,
        )
        for run in stale_runs:
            if run.status == "cancel_requested":
                status = "cancelled"
                chunk = {"status": "interrupted", "message": "取消请求已生效"}
            elif run.status == "pending":
                status = "failed"
                chunk = {
                    "status": "error",
                    "error_type": "stale_run_reconciled",
                    "error_message": "任务长时间未入队执行，已被系统回收，请重新发送",
                    "retryable": False,
                }
            else:
                status = "failed"
                chunk = {
                    "status": "error",
                    "error_type": "stale_run_reconciled",
                    "error_message": "执行中断（worker 失联超时），请重新发送",
                    "retryable": False,
                }
            updated = await repo.set_terminal_status(
                run.id,
                status=status,
                error_type="stale_run_reconciled",
                error_message=chunk.get("error_message") or chunk.get("message"),
            )
            # set_terminal_status 对已终态 run 是 no-op；只有真正写入才补发 end 事件
            if updated is not None and updated.status == status:
                reconciled.append((run.id, run.conversation_thread_id, status, chunk))
        # 会话上下文退出时统一 commit

    for run_id, thread_id, status, chunk in reconciled:
        logger.warning(f"Reconciled stale agent run {run_id} -> {status}")
        try:
            await append_run_stream_event(
                run_id,
                "end",
                {"status": status, "chunk": chunk},
                thread_id=thread_id,
            )
        except Exception:
            logger.exception(f"Failed to append reconcile end event for run {run_id}")
    if reconciled:
        logger.warning(f"Reconciled {len(reconciled)} stale agent runs")
    return len(reconciled)


async def get_agent_run_view(*, run_id: str, current_uid: str, db: AsyncSession) -> dict:
    repo = AgentRunRepository(db)
    run = await repo.get_run_for_user(run_id, str(current_uid))
    if not run:
        raise HTTPException(status_code=404, detail="运行任务不存在")
    return {"run": run.to_dict()}


def _select_output_message(messages: list[Message], *, output_message_id: int | None) -> Message | None:
    """优先选用运行记录的输出消息，否则回退到最后一条 assistant 消息。"""
    if output_message_id:
        for message in messages:
            if message.id == output_message_id and message.role == "assistant":
                return message

    for message in reversed(messages):
        if message.role == "assistant":
            return message
    return None


async def get_agent_run_result(*, run_id: str, current_uid: str, db: AsyncSession) -> dict:
    """加载某个 run 的最终结果（状态/输出/Langfuse trace/错误），供 chat/eval/cron 等统一复用。"""
    run = await AgentRunRepository(db).get_run_for_user(run_id, str(current_uid))
    if not run:
        return {
            "status": "failed",
            "agent_run_id": run_id,
            "output": "",
            "error": {"type": "run_not_found", "message": "运行任务不存在"},
        }

    messages: list[Message] = []
    if run.conversation_id:
        result = await db.execute(
            select(Message)
            .where(Message.conversation_id == run.conversation_id)
            .order_by(Message.created_at.asc(), Message.id.asc())
        )
        messages = list(result.scalars().unique().all())

    output_message = _select_output_message(messages, output_message_id=run.output_message_id)
    output_metadata = (
        output_message.extra_metadata if output_message and isinstance(output_message.extra_metadata, dict) else {}
    )

    payload: dict[str, Any] = {
        "status": run.status,
        "output": output_message.content if output_message else "",
        "agent_slug": run.agent_slug,
        "thread_id": run.conversation_thread_id,
        "conversation_id": run.conversation_id,
        "agent_run_id": run.id,
        "request_id": run.request_id,
        "final_message_id": output_message.id if output_message else None,
        "langfuse_trace_id": output_metadata.get("langfuse_trace_id"),
        "run_context": await _load_server_run_context(run, db),
    }
    if run.error_type or run.error_message:
        payload["error"] = {"type": run.error_type, "message": run.error_message}
    return payload


async def load_agent_run_result(*, run_id: str, current_uid: str) -> dict:
    """自开独立会话读取 run 结果，用于流结束/后台调用等请求会话已不可用的场景。"""
    async with pg_manager.get_async_session_context() as db:
        return await get_agent_run_result(run_id=run_id, current_uid=current_uid, db=db)


async def await_agent_run_result(*, run_id: str, current_uid: str) -> dict:
    """阻塞至 run 终结并返回最终结果，供 cron 等 in-process 调用。

    复用有限事件流 ``stream_agent_run_events``：它在 run 终结或超时后自然结束，
    因此排空即等待，无需额外轮询。等待上限继承事件流内部的 ``SSE_MAX_CONNECTION_MINUTES``。
    如果等待结束后 run 仍非终态，抛出 ``AgentRunWaitTimeout``，避免调用方把非终态误当最终结果。
    """
    async for _ in stream_agent_run_events(run_id=run_id, after_seq="0-0", current_uid=current_uid, verbose=False):
        pass
    result = await load_agent_run_result(run_id=run_id, current_uid=current_uid)
    if str(result.get("status") or "") not in TERMINAL_RUN_STATUSES:
        raise AgentRunWaitTimeout(result)
    return result


async def request_cancel_agent_run(
    *,
    run_id: str,
    current_uid: str,
    db: AsyncSession,
    cascade_children: bool = False,
):
    """请求取消一个 run，并可同时向仍活跃的子 run 发布取消信号。"""
    repo = AgentRunRepository(db)
    run = await repo.get_run_for_user(run_id, str(current_uid))
    if not run:
        raise HTTPException(status_code=404, detail="运行任务不存在")

    # FOR UPDATE 写锁在同一会话上必须串行；取消信号之间互不依赖，统一并发发布。
    cancelled_ids = []
    if cascade_children:
        child_runs = await repo.list_active_child_runs_for_user(run_id, str(current_uid))
        for child_run in child_runs:
            await repo.request_cancel(child_run.id)
            cancelled_ids.append(child_run.id)

    run = await repo.request_cancel(run_id)
    cancelled_ids.append(run_id)
    await db.commit()
    await asyncio.gather(*(publish_cancel_signal(cid) for cid in cancelled_ids))
    return run


async def cancel_agent_run_view(*, run_id: str, current_uid: str, db: AsyncSession) -> dict:
    """HTTP 取消入口：取消父 run 时默认级联取消活跃子 run。"""
    run = await request_cancel_agent_run(run_id=run_id, current_uid=current_uid, db=db, cascade_children=True)
    return {"run": run.to_dict() if run else None}


async def stream_agent_run_events(
    *,
    run_id: str,
    after_seq: str,
    current_uid: str,
    verbose: bool = True,
) -> AsyncIterator[str]:
    """按 SSE 格式读取 run 事件流；终结事件缺失时根据数据库状态补发 end。"""
    started_at = utc_now_naive()
    last_heartbeat_ts = started_at

    last_seq = normalize_after_seq(after_seq)

    try:
        while True:
            try:
                async with pg_manager.get_async_session_context() as db:
                    repo = AgentRunRepository(db)
                    run = await repo.get_run_for_user(run_id, str(current_uid))
                    if not run:
                        yield _format_sse({"run_id": run_id, "message": "运行任务不存在"}, event="error")
                        return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Run SSE DB error for run {run_id}: {e}")
                yield _format_sse(
                    {
                        "run_id": run_id,
                        "message": "运行事件流暂时不可用，请重连",
                        "reason": "db_error",
                    },
                    event="error",
                )
                return

            try:
                events = await list_run_stream_events(run_id, after_seq=last_seq, limit=200)
            except Exception as e:
                logger.warning(f"Run SSE redis error for run {run_id}: {e}")
                yield _format_sse(
                    {
                        "run_id": run_id,
                        "message": "运行事件流暂时不可用，请重连",
                        "reason": "redis_error",
                    },
                    event="error",
                )
                return

            emitted_terminal = False
            for event in events:
                seq = str(event.get("seq") or "0-0")
                last_seq = seq
                event_type = event.get("event_type") or "message"
                envelope = event.get("payload") or {}
                if not verbose and isinstance(envelope, dict):
                    envelope = _compact_run_event_envelope(envelope)
                    if envelope is None:
                        continue
                yield _format_sse(envelope, event=event_type, event_id=seq)
                if event_type == "end":
                    emitted_terminal = True

            if emitted_terminal:
                return

            if run.status in TERMINAL_RUN_STATUSES and not events:
                terminal_seq = last_seq
                if terminal_seq in {"", "0-0"}:
                    terminal_seq = await get_last_run_stream_seq(run_id)
                if terminal_seq in {"", "0-0"}:
                    terminal_seq = None
                terminal_envelope = build_run_event_envelope(
                    run_id=run_id,
                    thread_id=run.conversation_thread_id,
                    event_type="end",
                    payload={"status": run.status, "request_id": run.request_id},
                    created_at=utc_now_naive().isoformat(),
                )
                if not verbose:
                    terminal_envelope = _compact_run_event_envelope(terminal_envelope)
                yield _format_sse(
                    terminal_envelope,
                    event="end",
                    event_id=terminal_seq,
                )
                return

            now = utc_now_naive()
            elapsed_seconds = (now - started_at).total_seconds()
            heartbeat_elapsed = (now - last_heartbeat_ts).total_seconds()
            if heartbeat_elapsed >= SSE_HEARTBEAT_SECONDS:
                yield _format_heartbeat()
                last_heartbeat_ts = now

            if elapsed_seconds >= SSE_MAX_CONNECTION_MINUTES * 60:
                return

            await asyncio.sleep(SSE_POLL_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        return


async def get_active_run_by_thread(*, thread_id: str, current_uid: str, db: AsyncSession) -> dict:
    """读取线程当前仍需前端关注的最近一个 chat/resume run。"""
    from yuxi.storage.postgres.models_business import AgentRun

    # 线程内的 run 是串行的，最近一条 run 即代表线程当前状态。
    # 已被回复的 interrupted run 会被更晚创建的 resume run 取代，因此不会再被当作待处理中断返回。
    result = await db.execute(
        select(AgentRun)
        .where(
            AgentRun.conversation_thread_id == thread_id,
            AgentRun.uid == str(current_uid),
            AgentRun.run_type.in_(["chat", "resume"]),
        )
        .order_by(AgentRun.created_at.desc())
        .limit(1)
    )
    run = result.scalar_one_or_none()
    if run and run.status in ("pending", "running", "cancel_requested", "interrupted"):
        return {"run": run.to_dict()}
    return {"run": None}
