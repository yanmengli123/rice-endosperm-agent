"""Agent runtime streaming service.

This module is the LangGraph execution path used by the worker after an
``AgentRun`` has already been created. It restores input messages, builds the
agent runtime context, streams model/tool events, persists assistant output and
extracts UI-facing agent state.

Do not put run creation, request id idempotency, queueing or external
invocation response formatting here. Those responsibilities belong to
``agent_run_service`` and ``agent_invocation_service`` respectively. Keeping
this file focused on execution makes normal chat, resume runs and subagent runs
share the same runtime behavior once they reach the worker.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, Literal

from langchain.messages import AIMessage, AIMessageChunk, ToolMessage
from langgraph.types import Command
from yuxi import config as conf
from yuxi.agents.buildin import agent_manager
from yuxi.agents.context import build_agent_input_context, normalize_agent_context_config
from yuxi.agents.state import AgentStatePayload
from yuxi.knowledge.orchestration import prepare_knowledge_context
from yuxi.repositories.agent_repository import AgentRepository
from yuxi.repositories.agent_run_repository import AgentRunRepository
from yuxi.repositories.conversation_repository import ConversationRepository
from yuxi.repositories.subagent_thread_repository import SubagentThreadRepository
from yuxi.services.conversation_service import serialize_attachment
from yuxi.services.input_message_service import AgentRunInputMessage
from yuxi.services.knowledge_scope_service import resolve_effective_knowledge_scope
from yuxi.services.langfuse_service import (
    LangfuseRunContext,
    build_run_context,
    flush_langfuse,
    get_trace_info,
)
from yuxi.services.subagent_run_service import serialize_subagent_run_state
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import Agent, User
from yuxi.utils.guard import content_guard
from yuxi.utils.logging_config import logger
from yuxi.utils.question_utils import (
    normalize_questions as _normalize_interrupt_questions,
)
from yuxi.utils.reasoning_visibility import (
    ReasoningVisibilityBuffer,
    redact_reasoning_metadata,
    sanitize_visible_text,
)
from yuxi.utils.thread_utils import extract_thread_id as _metadata_thread_id


def _build_state_files(attachments: list[dict]) -> dict:
    """将附件列表转换为 StateBackend 格式的 files 字典

    StateBackend 期望的格式:
    {
        "/attachments/file.md": {
            "content": ["line1", "line2", ...],
            "created_at": "...",
            "modified_at": "...",
        }
    }
    """
    files = {}
    for attachment in attachments:
        if attachment.get("status") != "parsed":
            continue

        file_path = attachment.get("file_path")
        markdown = attachment.get("markdown")

        if not file_path or not markdown:
            continue

        now = datetime.now(UTC).isoformat()
        # 将 markdown 内容按行拆分
        content_lines = markdown.split("\n")
        files[file_path] = {
            "content": content_lines,
            "created_at": attachment.get("uploaded_at", now),
            "modified_at": attachment.get("uploaded_at", now),
        }

    return files


def _build_agent_context(agent, input_context: dict):
    context = agent.context_schema()
    context.update(input_context)
    return context


async def _get_langgraph_messages(agent_instance, config_dict, *, context):
    graph = await agent_instance.get_graph(context=context)
    state = await graph.aget_state(config_dict)

    if not state or not state.values:
        logger.warning("No state found in LangGraph")
        return None

    return state.values.get("messages", [])


def _build_langfuse_run_context(
    *,
    current_user,
    thread_id: str,
    agent_id: str,
    request_id: str,
    operation: str,
    backend_id: str | None = None,
    message_type: str | None = None,
    meta: dict | None = None,
) -> LangfuseRunContext:
    extra_metadata = None
    extra_tags = None
    invocation_meta = (meta or {}).get("agent_invocation_meta") if isinstance(meta, dict) else None
    evaluation = invocation_meta.get("evaluation") if isinstance(invocation_meta, dict) else None
    # 如果请求来自智能体评测，添加评测相关的 metadata 和 tags，方便在 Langfuse 中进行过滤和分析
    if (meta or {}).get("source") == "agent_evaluation" or (isinstance(evaluation, dict) and evaluation):
        extra_metadata = {
            "source": "agent_evaluation",
            "feature": "agent_evaluation",
        }
        extra_tags = ["agent_evaluation"]
        if isinstance(evaluation, dict):
            dataset_name = evaluation.get("dataset_name")
            experiment_name = evaluation.get("experiment_name")
            for key in ("dataset_name", "dataset_item_id", "experiment_name"):
                value = evaluation.get(key)
                if value:
                    extra_metadata[f"evaluation_{key}"] = str(value)
            if dataset_name:
                extra_tags.append(f"dataset:{dataset_name}")
            if experiment_name:
                extra_tags.append(f"experiment:{experiment_name}")

    return build_run_context(
        user_id=str(getattr(current_user, "uid", current_user.id)),
        thread_id=thread_id,
        agent_id=agent_id,
        request_id=request_id,
        operation=operation,
        backend_id=backend_id,
        message_type=message_type,
        username=getattr(current_user, "username", None),
        login_user_id=getattr(current_user, "uid", None),
        department_id=getattr(current_user, "department_id", None),
        extra_metadata=extra_metadata,
        extra_tags=extra_tags,
    )


def extract_agent_state(values: dict) -> AgentStatePayload:
    """从 LangGraph state 中提取 agent 状态"""
    if not isinstance(values, dict):
        return {"todos": [], "files": {}, "artifacts": [], "subagent_runs": [], "token_usage": None}

    # 直接获取，信任 state 的数据结构
    todos = values.get("todos")
    artifacts = values.get("artifacts")
    subagent_runs = values.get("subagent_runs")
    token_usage = values.get("token_usage")
    result: AgentStatePayload = {
        "todos": list(todos)[:20] if todos else [],
        "files": values.get("files") or {},
        "artifacts": list(artifacts) if artifacts else [],
        "subagent_runs": list(subagent_runs) if subagent_runs else [],
        "token_usage": dict(token_usage) if isinstance(token_usage, dict) else None,
    }

    return result


def _agent_state_signature(agent_state: AgentStatePayload | dict | None) -> str:
    if not agent_state:
        return ""
    try:
        return json.dumps(agent_state, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(agent_state)


def _metadata_namespace(metadata: dict | None) -> list[str]:
    if not isinstance(metadata, dict):
        return []
    namespace = metadata.get("namespace")
    if isinstance(namespace, list):
        return [str(item) for item in namespace]
    return []


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(child) for key, child in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(child) for child in value]
    if hasattr(value, "model_dump"):
        return _json_safe(value.model_dump())
    return str(value)


def _apply_model_override(input_context: dict, meta: dict | None) -> None:
    """对话级模型覆盖：meta.model_spec 优先于智能体配置的 model。值已在创建 run 时校验。"""
    model_spec = (meta or {}).get("model_spec")
    model_spec = model_spec.strip() if isinstance(model_spec, str) else model_spec
    if model_spec:
        input_context["model"] = model_spec


async def _activate_user_credential(*, db, uid: str, meta: dict | None):
    """P3 BYOK：按 run 冻结的凭据引用解密并激活任务级密钥覆盖。

    激活作用域是当前异步任务：graph 构建期间 load_chat_model 会读取该上下文，
    调用方必须在 finally 中 reset 返回的 ContextVar token。run 已冻结凭据时，
    解密失败、凭据撤销或供应商不匹配均失败关闭，禁止意外改用平台 Key。
    """
    ref = (meta or {}).get("user_credential") or {}
    credential_id = ref.get("credential_id")
    provider_id = ref.get("provider_id")
    if not credential_id or not provider_id:
        return None
    from yuxi.agents.models import set_user_credential_override
    from yuxi.services.user_credential_service import open_user_credential_runtime

    runtime = await open_user_credential_runtime(
        db,
        str(uid),
        int(credential_id),
        expected_provider_id=str(provider_id),
    )
    if not runtime or not runtime.get("api_key"):
        raise RuntimeError(f"本次运行冻结的用户模型凭据不可用: credential={credential_id}")
    return set_user_credential_override(
        str(provider_id),
        str(runtime["api_key"]),
        model_spec=runtime.get("model_spec"),
        model_id=runtime.get("model_id"),
        base_url=runtime.get("base_url"),
        protocol=runtime.get("protocol"),
    )


def _apply_subagent_runtime_context(input_context: dict, meta: dict | None) -> None:
    """把子智能体 run 的父线程和文件线程信息注入运行 context。"""
    meta = meta or {}
    # 仅对子智能体类型的 run 生效
    if meta.get("run_type") != "subagent":
        return
    # 这三个线程 ID 由 subagent_run_service 在创建 run 时写入 runtime，
    # 是子智能体区别于普通对话的唯一依据；缺失即上游契约被破坏，直接失败而非静默回退。
    for key in ("parent_thread_id", "file_thread_id", "skills_thread_id"):
        value = str(meta.get(key) or "").strip()
        if not value:
            raise ValueError(f"子智能体运行缺少必需的 {key}")
        input_context[key] = value
    # 标记为子智能体运行，供下游逻辑判断
    input_context["is_subagent_runtime"] = True


async def _ensure_knowledge_scope_snapshot(*, db, user: User, agent_slug: str, meta: dict) -> dict:
    snapshot = meta.get("knowledge_scope_snapshot")
    if not isinstance(snapshot, dict):
        snapshot = await resolve_effective_knowledge_scope(
            db=db,
            user=user,
            agent_slug=agent_slug,
        )
        meta["knowledge_scope_snapshot"] = snapshot
    return snapshot


def _apply_knowledge_scope_snapshot(input_context: dict, snapshot: dict | None) -> None:
    """把已解析快照绑定到 runtime；这里只能消费快照，不能再扩大范围。"""
    if not isinstance(snapshot, dict):
        return
    # BaseAgent reconstructs its context from input_context before building the
    # graph.  Persist the frozen snapshot in that transport object so tools,
    # prompts and middleware all observe the same scope in worker execution.
    input_context["_effective_knowledge_scope"] = snapshot
    input_context["knowledges"] = list(snapshot.get("effective_kb_ids") or [])
    if not snapshot.get("allow_web", False):
        input_context["tools"] = [
            name
            for name in (input_context.get("tools") or [])
            if str(name).lower() not in {"tavily_search", "web_search", "search_web"}
        ]
        input_context["subagents"] = [
            slug for slug in (input_context.get("subagents") or []) if str(slug).lower() != "web-search"
        ]


def _bind_knowledge_scope_to_context(context, snapshot: dict | None) -> None:
    if isinstance(snapshot, dict):
        setattr(context, "_effective_knowledge_scope", snapshot)


def _knowledge_contract_messages(
    contract: dict[str, Any], *, query: str, message_id: str
) -> tuple[AIMessage, ToolMessage]:
    retrieval_id = str(contract["retrieval_id"])
    assistant_message = AIMessage(
        id=message_id,
        content="",
        tool_calls=[
            {
                "id": retrieval_id,
                "name": "query_knowledge_scope",
                "args": {"query_text": query, "orchestrated_by": "backend"},
            }
        ],
        additional_kwargs={"knowledge_first": True},
    )
    tool_message = ToolMessage(
        id=f"msg_{uuid.uuid4().hex}",
        content=json.dumps(contract, ensure_ascii=False, separators=(",", ":")),
        tool_call_id=retrieval_id,
        name="query_knowledge_scope",
        additional_kwargs={"knowledge_first": True},
    )
    return assistant_message, tool_message


def _stream_message_key(metadata: dict | None, namespace: list[str], thread_id: str | None) -> tuple[str, str]:
    if not isinstance(metadata, dict):
        return thread_id or "", "/".join(namespace)
    return thread_id or "", str(metadata.get("run_id") or metadata.get("langgraph_node") or "/".join(namespace))


def _stream_message_id(
    message_ids: dict[tuple[str, str], str],
    key: tuple[str, str],
    preferred: str | None = None,
) -> str:
    if preferred:
        message_ids[key] = preferred
        return preferred
    return message_ids.setdefault(key, str(uuid.uuid4()))


def _message_chunk_yuxi_events(
    msg_dict: dict[str, Any],
    *,
    message_id: str,
    thread_id: str | None,
    namespace: list[str],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    route = {"thread_id": thread_id, "namespace": namespace}
    content = msg_dict.get("content")
    additional_kwargs = msg_dict.get("additional_kwargs") if isinstance(msg_dict.get("additional_kwargs"), dict) else {}
    reasoning_content = msg_dict.get("reasoning_content")
    additional_reasoning_content = additional_kwargs.get("reasoning_content")
    reasoning_state = additional_kwargs.get("reasoning_state")

    message_event: dict[str, Any] = {"type": "message_delta", "message_id": message_id, **route}
    if isinstance(content, str) and content:
        message_event["content"] = content
    has_reasoning = (isinstance(reasoning_content, str) and bool(reasoning_content)) or (
        isinstance(additional_reasoning_content, str) and bool(additional_reasoning_content)
    )
    if has_reasoning or reasoning_state == "thinking":
        # 原始 chain-of-thought 不属于产品输出协议；仅公开非敏感状态。
        message_event["reasoning_state"] = "thinking"
    if len(message_event) > 4:
        events.append(message_event)

    tool_call_chunks = msg_dict.get("tool_call_chunks")
    if isinstance(tool_call_chunks, list):
        for tool_call_chunk in tool_call_chunks:
            if not isinstance(tool_call_chunk, dict):
                continue
            args_delta = tool_call_chunk.get("args")
            if args_delta is None:
                args_delta = ""
            elif not isinstance(args_delta, str):
                args_delta = json.dumps(args_delta, ensure_ascii=False)
            if not tool_call_chunk.get("id") and not tool_call_chunk.get("name") and not args_delta:
                continue
            events.append(
                {
                    "type": "tool_call_delta",
                    "message_id": message_id,
                    "tool_call_id": tool_call_chunk.get("id"),
                    "name": tool_call_chunk.get("name") or None,
                    "args_delta": args_delta,
                    "index": tool_call_chunk.get("index") if tool_call_chunk.get("index") is not None else 0,
                    **route,
                }
            )
    return events


def _protocol_event_yuxi_event(
    event: dict[str, Any],
    *,
    message_id: str | None,
    thread_id: str | None,
    namespace: list[str],
) -> dict[str, Any] | None:
    event_name = event.get("event")
    if event_name in {"message-start", "content-block-start", "message-finish"} or not message_id:
        return None

    route = {"thread_id": thread_id, "namespace": namespace}
    if event_name == "content-block-delta":
        delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
        text = delta.get("text")
        if delta.get("type") == "text-delta" and isinstance(text, str) and text:
            return {"type": "message_delta", "message_id": message_id, "content": text, **route}
        return None

    if event_name == "content-block-finish":
        content = event.get("content") if isinstance(event.get("content"), dict) else {}
        if content.get("type") != "tool_call" or not content.get("id") and not content.get("name"):
            return None
        return {
            "type": "tool_call",
            "message_id": message_id,
            "tool_call_id": content.get("id"),
            "name": content.get("name"),
            "args": content.get("args") if content.get("args") is not None else {},
            "index": event.get("index") if event.get("index") is not None else 0,
            **route,
        }

    return None


def _context_compression_payload(payload: Any) -> dict | None:
    if isinstance(payload, dict) and payload.get("type") == "yuxi.context_compression":
        return payload
    return None


def _stream_event_response(event: dict[str, Any]) -> str:
    if event.get("type") != "message_delta":
        return ""
    return str(event.get("content") or "")


def _sanitize_stream_event(
    event: dict[str, Any],
    buffers: dict[str, ReasoningVisibilityBuffer],
) -> dict[str, Any]:
    """Redact structured and tagged reasoning before an event leaves the worker."""

    if event.get("type") != "message_delta":
        return event
    safe = dict(event)
    structured_reasoning = bool(safe.pop("reasoning_content", None) or safe.pop("additional_reasoning_content", None))
    message_id = str(safe.get("message_id") or "default")
    content = safe.get("content")
    tagged_reasoning = False
    if isinstance(content, str) and content:
        visible_delta, tagged_reasoning = buffers.setdefault(message_id, ReasoningVisibilityBuffer()).feed(content)
        if visible_delta:
            safe["content"] = visible_delta
        else:
            safe.pop("content", None)
    if structured_reasoning or tagged_reasoning or safe.get("reasoning_state") == "thinking":
        safe["reasoning_state"] = "thinking"
    return safe


def _message_payload_yuxi_events(
    msg: Any,
    *,
    metadata: dict[str, Any],
    namespace: list[str],
    thread_id: str | None,
    protocol_message_ids: dict[tuple[str, str], str],
) -> list[dict[str, Any]]:
    message_key = _stream_message_key(metadata, namespace, thread_id)
    if isinstance(msg, dict) and isinstance(msg.get("event"), str):
        preferred_message_id = str(msg["id"]) if msg.get("event") == "message-start" and msg.get("id") else None
        message_id = _stream_message_id(protocol_message_ids, message_key, preferred_message_id)
        stream_event = _protocol_event_yuxi_event(
            msg,
            message_id=message_id,
            thread_id=thread_id,
            namespace=namespace,
        )
        return [stream_event] if stream_event else []

    if isinstance(msg, AIMessageChunk) or hasattr(msg, "model_dump"):
        msg_dict = msg.model_dump()
    elif isinstance(msg, dict):
        msg_dict = dict(msg)
    else:
        msg_dict = {"content": str(msg)}

    message_id = str(msg_dict.get("id") or _stream_message_id(protocol_message_ids, message_key))
    return _message_chunk_yuxi_events(
        msg_dict,
        message_id=message_id,
        thread_id=thread_id,
        namespace=namespace,
    )


async def _stream_agent_events(agent, messages, *, input_context=None, **kwargs):
    async for mode, payload in agent.stream_messages_with_state(
        messages,
        input_context=input_context,
        **kwargs,
    ):
        yield mode, payload


async def _get_existing_message_ids(conv_repo: ConversationRepository, thread_id: str) -> set[str]:
    existing_messages = await conv_repo.get_messages_by_thread_id(thread_id)
    return {
        msg.extra_metadata["id"]
        for msg in existing_messages
        if msg.extra_metadata and "id" in msg.extra_metadata and isinstance(msg.extra_metadata["id"], str)
    }


async def _save_ai_message(
    conv_repo: ConversationRepository,
    thread_id: str,
    msg_dict: dict,
    trace_info: dict[str, Any] | None = None,
    run_id: str | None = None,
    request_id: str | None = None,
):
    content = msg_dict.get("content", "")
    tool_calls_data = msg_dict.get("tool_calls") or []
    if isinstance(content, list):
        if not tool_calls_data:
            tool_calls_data = [
                {"id": item.get("id"), "name": item.get("name"), "args": item.get("args") or {}}
                for item in content
                if isinstance(item, dict) and item.get("type") == "tool_call"
            ]
        content = "\n".join(
            item.get("text", "") for item in content if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    elif not isinstance(content, str):
        content = str(content)
    content = sanitize_visible_text(content)
    extra_metadata = redact_reasoning_metadata(msg_dict)
    if trace_info:
        extra_metadata.update(trace_info)

    ai_msg = await conv_repo.add_message_by_thread_id(
        thread_id=thread_id,
        role="assistant",
        content=content,
        message_type="text",
        extra_metadata=extra_metadata,
        run_id=run_id,
        request_id=request_id,
    )

    if ai_msg and tool_calls_data:
        for tc in tool_calls_data:
            await conv_repo.add_tool_call(
                message_id=ai_msg.id,
                tool_name=tc.get("name") or "unknown",
                tool_input=tc.get("args", {}),
                status="pending",
                langgraph_tool_call_id=tc.get("id"),
            )

    return ai_msg


async def _save_tool_message(conv_repo: ConversationRepository, msg_dict: dict) -> None:
    tool_call_id = msg_dict.get("tool_call_id")
    content = msg_dict.get("content", "")

    if not tool_call_id:
        return

    if isinstance(content, list):
        tool_output = json.dumps(content) if content else ""
    else:
        tool_output = str(content)

    await conv_repo.update_tool_call_output(
        langgraph_tool_call_id=tool_call_id,
        tool_output=tool_output,
        status="success",
    )


async def save_partial_message(
    conv_repo: ConversationRepository,
    thread_id: str,
    full_msg=None,
    error_message: str | None = None,
    error_type: str = "interrupted",
    trace_info: dict[str, Any] | None = None,
    run_id: str | None = None,
    request_id: str | None = None,
):
    try:
        extra_metadata = {
            "error_type": error_type,
            "is_error": True,
            "error_message": error_message or f"发生错误: {error_type}",
        }
        if full_msg:
            msg_dict = full_msg.model_dump() if hasattr(full_msg, "model_dump") else {}
            content = full_msg.content if hasattr(full_msg, "content") else str(full_msg)
            content = sanitize_visible_text(content if isinstance(content, str) else str(content))
            extra_metadata = redact_reasoning_metadata(msg_dict) | extra_metadata
        else:
            content = ""

        if trace_info:
            extra_metadata.update(trace_info)

        return await conv_repo.add_message_by_thread_id(
            thread_id=thread_id,
            role="assistant",
            content=content,
            message_type="text",
            extra_metadata=extra_metadata,
            run_id=run_id,
            request_id=request_id,
        )

    except Exception as e:
        logger.exception(f"Error saving message: {e}")
        return None


def _extract_total_tokens(state) -> int | None:
    """从 LangGraph 终态提取本次 run 的 token 总量（TokenUsageMiddleware 写入）。"""
    values = getattr(state, "values", None) or {}
    usage = values.get("token_usage")
    model_usage = usage.get("run_model_usage") if isinstance(usage, dict) else None
    if not isinstance(model_usage, dict) or not model_usage:
        model_usage = usage.get("model_usage") if isinstance(usage, dict) else None
    if isinstance(model_usage, dict) and model_usage:
        total_tokens = model_usage.get("total_tokens")
        if isinstance(total_tokens, (int, float)) and not isinstance(total_tokens, bool):
            return int(total_tokens)
        input_tokens = model_usage.get("input_tokens", model_usage.get("prompt_tokens", 0))
        output_tokens = model_usage.get("output_tokens", model_usage.get("completion_tokens", 0))
        if all(
            isinstance(value, (int, float)) and not isinstance(value, bool) for value in (input_tokens, output_tokens)
        ):
            return int(input_tokens) + int(output_tokens)
    return None


def _token_usage_delta(total_tokens: int | None, baseline_tokens: int | None) -> int | None:
    """Return tokens consumed after a checkpoint baseline.

    ``run_model_usage`` is persisted in LangGraph state and therefore remains
    cumulative across normal turns and resume loops.  AgentRun accounting is
    per run, so persisting the cumulative value would bill every previous turn
    again.  A missing baseline is kept distinguishable from a real zero to
    avoid silently overcharging when the checkpoint cannot be read.
    """
    if total_tokens is None or baseline_tokens is None:
        return None
    return max(int(total_tokens) - int(baseline_tokens), 0)


async def _checkpoint_total_tokens(agent, config_dict: dict, *, context) -> int | None:
    """Read cumulative token usage before a run starts."""
    try:
        graph = await agent.get_graph(context=context)
        state = await graph.aget_state(config_dict)
        return _extract_total_tokens(state) or 0
    except Exception:
        logger.warning("读取运行前 token checkpoint 基线失败；本次运行将跳过计量")
        return None


async def _persist_run_total_tokens(
    db,
    run_id: str | None,
    total_tokens: int | None,
    *,
    uid: str | None = None,
    model_spec: str | None = None,
    estimated: bool | None = None,
    credential_ref: dict | None = None,
    policy_version: int | None = None,
) -> None:
    if not run_id or total_tokens is None:
        return
    from sqlalchemy import select
    from yuxi.storage.postgres.models_business import UsageLedger

    repository = AgentRunRepository(db)
    run = await repository.get_run(str(run_id))
    if run is None:
        raise ValueError(f"无法为不存在的 AgentRun 写入用量: {run_id}")
    if uid is not None and str(run.uid) != str(uid):
        raise ValueError(f"AgentRun 用量归属不匹配: run_id={run_id}")

    # uid/tenant_id 只取 AgentRun 的服务端冻结值，调用方参数仅用于一致性校验。
    run.total_tokens = int(total_tokens)
    existing = (
        await db.execute(select(UsageLedger.id).where(UsageLedger.run_id == str(run_id)).limit(1))
    ).scalar_one_or_none()
    if existing is None:
        credential_ref = credential_ref or {}
        db.add(
            UsageLedger(
                run_id=str(run_id),
                uid=str(run.uid),
                tenant_id=run.tenant_id,
                model_spec=model_spec,
                total_tokens=int(total_tokens),
                estimated=bool(estimated),
                credential_source="user_byok" if credential_ref else "platform",
                credential_id=credential_ref.get("credential_id"),
                provider_id=credential_ref.get("provider_id"),
                policy_version=policy_version,
            )
        )
    await db.flush()


async def save_messages_from_langgraph_state(
    agent_instance,
    thread_id: str,
    conv_repo: ConversationRepository,
    config_dict: dict,
    context,
    trace_info: dict[str, Any] | None = None,
    run_id: str | None = None,
    request_id: str | None = None,
) -> None:
    messages = await _get_langgraph_messages(agent_instance, config_dict, context=context)
    if messages is None:
        return

    existing_ids = await _get_existing_message_ids(conv_repo, thread_id)

    last_ai_message = None
    for msg in messages:
        if hasattr(msg, "model_dump"):
            msg_dict = msg.model_dump()
        elif isinstance(msg, dict):
            msg_dict = dict(msg)
        else:
            continue

        msg_type = msg_dict.get("type", "unknown")
        if msg_type == "unknown":
            role = msg_dict.get("role")
            if role in {"assistant", "ai"}:
                msg_type = "ai"
            elif role in {"user", "human"}:
                msg_type = "human"
            elif role == "tool":
                msg_type = "tool"

        msg_id = getattr(msg, "id", None) or msg_dict.get("id")
        if not msg_id and msg_type == "ai":
            # 部分 provider 聚合路径会产生没有 id 的 AIMessage；不派生稳定 id 的
            # 话每次状态回存都会重复入库。按线程 + 内容生成确定性 id，下一轮
            # 回存时命中 existing_ids 被跳过。
            msg_id = str(uuid.uuid5(uuid.NAMESPACE_OID, f"{thread_id}|ai|{msg_dict.get('content')}"))
            msg_dict = {**msg_dict, "id": msg_id}
        if msg_type == "human" or msg_id in existing_ids:
            continue

        if msg_type == "ai":
            last_ai_message = await _save_ai_message(
                conv_repo,
                thread_id,
                msg_dict,
                trace_info=trace_info,
                run_id=run_id,
                request_id=request_id,
            )
        elif msg_type == "tool":
            await _save_tool_message(conv_repo, msg_dict)

    if run_id and last_ai_message:
        run_repo = AgentRunRepository(conv_repo.db)
        await run_repo.set_output_message(run_id, last_ai_message.id)
        await conv_repo.db.commit()


def _extract_interrupt_info(state) -> Any | None:
    """从 LangGraph state 中提取中断信息"""
    if hasattr(state, "tasks") and state.tasks:
        for task in state.tasks:
            if hasattr(task, "interrupts") and task.interrupts:
                return task.interrupts[0]

    interrupt_data = state.values.get("__interrupt__")
    if isinstance(interrupt_data, list) and interrupt_data:
        return interrupt_data[0]

    return None


def _coerce_interrupt_payload(info: Any) -> dict:
    """将 LangGraph interrupt 对象转换为 dict 结构。"""
    if isinstance(info, dict):
        return info

    payload = getattr(info, "value", None)
    if isinstance(payload, dict):
        return payload

    questions = getattr(info, "questions", None)
    source = getattr(info, "source", None)
    result: dict[str, Any] = {}
    if isinstance(questions, list):
        result["questions"] = questions
    if isinstance(source, str) and source.strip():
        result["source"] = source
    return result


def _build_ask_user_question_payload(info: Any, thread_id: str) -> dict[str, Any]:
    """将 interrupt 信息标准化为 ask_user_question_required 载荷。"""
    payload = _coerce_interrupt_payload(info)

    questions = _normalize_interrupt_questions(payload.get("questions"))

    if not questions:
        questions = [
            {
                "question_id": str(uuid.uuid4()),
                "question": "请选择一个选项",
                "options": [],
                "multi_select": False,
                "allow_other": True,
            }
        ]

    source = str(payload.get("source") or payload.get("tool_name") or "interrupt")

    return {
        "questions": questions,
        "source": source,
        "thread_id": thread_id,
    }


def _ensure_full_msg(full_msg: AIMessage | None, accumulated_content: list[str]) -> AIMessage | None:
    """如果 full_msg 为空且有累积内容，构建 AIMessage"""
    if not full_msg and accumulated_content:
        return AIMessage(content="".join(accumulated_content))
    return full_msg


def _extract_ai_message(messages: list[Any] | None) -> AIMessage | None:
    """从消息列表中提取最后一条 AIMessage。"""
    if not isinstance(messages, list):
        return None

    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            return msg

        msg_dict = msg.model_dump() if hasattr(msg, "model_dump") else {}
        if msg_dict.get("type") == "ai":
            content = msg_dict.get("content", "")
            return msg if hasattr(msg, "content") else AIMessage(content=content)

    return None


async def _resolve_agent_runtime(
    *,
    db,
    user: User,
    requested_agent_slug: str | None,
    thread_id: str | None,
    agent_kind: Literal["main", "subagent"] = "main",
) -> tuple[Agent, Any, dict]:
    """解析智能体运行时，返回 (Agent, backend, agent_config)"""
    agent_repo = AgentRepository(db)
    conv_repo = ConversationRepository(db)
    resolved_agent_slug = requested_agent_slug

    if thread_id:
        conversation = await conv_repo.get_conversation_by_thread_id(thread_id)
        if conversation:
            if conversation.uid != str(user.uid) or conversation.status == "deleted":
                raise ValueError("对话线程不存在")
            # Conversation.agent_id 是历史字段名，实际保存的是 Agent.slug。
            if requested_agent_slug and requested_agent_slug != conversation.agent_id:
                raise ValueError("已有线程已绑定智能体，不能切换")
            resolved_agent_slug = conversation.agent_id

    if not resolved_agent_slug:
        raise ValueError("缺少必需的 agent_slug 字段")

    agent_item = await agent_repo.get_visible_by_slug(slug=resolved_agent_slug, user=user, kind=agent_kind)
    if not agent_item:
        raise ValueError("智能体不存在或无权限访问")

    backend = agent_manager.get_agent(agent_item.backend_id)
    if not backend:
        raise ValueError(f"智能体后端 {agent_item.backend_id} 不存在")

    agent_config = await normalize_agent_context_config(
        (agent_item.config_json or {}).get("context", {}),
        db=db,
        user=user,
        context_schema=backend.context_schema,
    )
    return agent_item, backend, agent_config


async def check_and_handle_interrupts(
    agent,
    langgraph_config: dict,
    make_chunk,
    meta: dict,
    thread_id: str,
    context,
) -> AsyncIterator[bytes]:
    try:
        graph = await agent.get_graph(context=context)
        state = await graph.aget_state(langgraph_config)

        if not state or not state.values:
            return

        interrupt_info = _extract_interrupt_info(state)
        if interrupt_info:
            question_payload = _build_ask_user_question_payload(interrupt_info, thread_id)
            meta["interrupt"] = question_payload
            yield make_chunk(status="ask_user_question_required", meta=meta, **question_payload)

    except Exception as e:
        logger.exception(f"Error checking interrupts: {e}")


async def _ensure_thread_bound_agent(
    *,
    conv_repo: ConversationRepository,
    thread_id: str,
    uid: str,
    agent_item: Agent,
) -> None:
    conversation = await conv_repo.get_conversation_by_thread_id(thread_id)
    if not conversation:
        await conv_repo.create_conversation(
            uid=uid,
            agent_id=agent_item.slug,
            thread_id=thread_id,
            metadata={"backend_id": agent_item.backend_id},
        )
        return

    if conversation.agent_id != agent_item.slug:
        raise ValueError("已有线程已绑定智能体，不能切换")


def _normalize_attachment_file_ids(meta: dict | None) -> list[str]:
    file_ids = (meta or {}).get("attachment_file_ids") or []
    if not isinstance(file_ids, list):
        return []

    normalized = []
    seen = set()
    for file_id in file_ids:
        value = str(file_id).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


async def _bind_request_attachments(
    *,
    conv_repo: ConversationRepository,
    thread_id: str,
    request_id: str,
    attachment_file_ids: list[str],
) -> list[dict]:
    conversation = await conv_repo.get_conversation_by_thread_id(thread_id)
    if not conversation:
        return []

    if attachment_file_ids:
        attachments = await conv_repo.bind_attachments_to_request(conversation.id, request_id, attachment_file_ids)
    else:
        attachments = await conv_repo.get_attachments_by_request_id(conversation.id, request_id)

    return [serialize_attachment(attachment) for attachment in attachments]


async def stream_agent_chat(
    *,
    agent_slug: str,
    thread_id: str | None,
    meta: dict,
    input_message: AgentRunInputMessage,
    current_user,
    db,
    save_user_message: bool = True,
) -> AsyncIterator[bytes]:
    start_time = asyncio.get_event_loop().time()

    def make_chunk(content=None, **kwargs):
        chunk_thread_id = kwargs.pop("thread_id", None) or meta.get("thread_id") or thread_id
        return (
            json.dumps(
                {"request_id": meta.get("request_id"), "response": content, "thread_id": chunk_thread_id, **kwargs},
                ensure_ascii=False,
            ).encode("utf-8")
            + b"\n"
        )

    meta = dict(meta or {})
    if "request_id" not in meta or not meta.get("request_id"):
        logger.warning("请求缺少 request_id，已自动生成一个新的 request_id")
        meta["request_id"] = str(uuid.uuid4())

    uid = str(current_user.uid)
    if not thread_id:
        thread_id = str(uuid.uuid4())
        logger.warning(f"No thread_id provided, generated new thread_id: {thread_id}")

    query = input_message.content
    image_content = input_message.image_content
    human_message = input_message.require_langchain_message()
    message_type = input_message.message_type

    if conf.enable_content_guard and await content_guard.check(query):
        yield make_chunk(
            status="error", error_type="content_guard_blocked", error_message="输入内容包含敏感词", meta=meta
        )
        return

    from yuxi.agents.mcp.execution import McpExecutionContext, set_mcp_execution_context
    from yuxi.services.principal import resolve_tenant_id

    mcp_context_token = set_mcp_execution_context(
        McpExecutionContext(
            tenant_id=int(meta.get("tenant_id") or await resolve_tenant_id(db, uid)),
            uid=uid,
            run_id=meta.get("run_id"),
            agent_slug=agent_slug,
        )
    )

    try:
        agent_item, agent, agent_config = await _resolve_agent_runtime(
            db=db,
            user=current_user,
            requested_agent_slug=agent_slug,
            thread_id=thread_id,
            agent_kind="subagent" if meta.get("run_type") == "subagent" else "main",
        )
    except ValueError as e:
        # 该分支发生在主流式 try/finally 之前，必须在返回前显式清理。
        from yuxi.agents.mcp.execution import reset_mcp_execution_context

        reset_mcp_execution_context(mcp_context_token)
        yield make_chunk(status="error", error_type="invalid_agent", error_message=str(e), meta=meta)
        return

    meta.update(
        {
            "query": query,
            "agent_slug": agent_item.slug,
            "backend_id": agent_item.backend_id,
            "thread_id": thread_id,
            "uid": current_user.uid,
            "has_image": bool(image_content),
        }
    )
    knowledge_scope_snapshot = await _ensure_knowledge_scope_snapshot(
        db=db,
        user=current_user,
        agent_slug=agent_item.slug,
        meta=meta,
    )

    messages = [human_message]
    input_context = await build_agent_input_context(
        agent_config,
        thread_id=thread_id,
        uid=uid,
        run_id=meta.get("run_id"),
        request_id=meta.get("request_id"),
    )
    _apply_model_override(input_context, meta)
    _apply_subagent_runtime_context(input_context, meta)
    _apply_knowledge_scope_snapshot(input_context, knowledge_scope_snapshot)
    context = _build_agent_context(agent, input_context)
    _bind_knowledge_scope_to_context(context, knowledge_scope_snapshot)
    langfuse_run = _build_langfuse_run_context(
        current_user=current_user,
        thread_id=thread_id,
        agent_id=agent_item.slug,
        backend_id=agent_item.backend_id,
        request_id=meta["request_id"],
        operation="agent_chat_stream",
        message_type=message_type,
        meta=meta,
    )
    full_msg = None
    accumulated_content: list[str] = []
    trace_info: dict[str, Any] = {}
    last_agent_state_signature = ""
    credential_context_token = None

    try:
        credential_context_token = await _activate_user_credential(db=db, uid=uid, meta=meta)
        conv_repo = ConversationRepository(db)
        await _ensure_thread_bound_agent(
            conv_repo=conv_repo,
            thread_id=thread_id,
            uid=uid,
            agent_item=agent_item,
        )

        request_attachments = await _bind_request_attachments(
            conv_repo=conv_repo,
            thread_id=thread_id,
            request_id=meta["request_id"],
            attachment_file_ids=_normalize_attachment_file_ids(meta),
        )

        init_msg = {
            "role": "user",
            "content": query,
            "type": "human",
            "message_type": message_type,
            "extra_metadata": {
                "request_id": meta.get("request_id"),
                "attachments": request_attachments,
            },
        }
        if image_content:
            init_msg["image_content"] = image_content
        yield make_chunk(status="init", meta=meta, msg=init_msg)

        if save_user_message:
            try:
                await conv_repo.add_message_by_thread_id(
                    thread_id=thread_id,
                    role="user",
                    content=query,
                    message_type=message_type,
                    image_content=image_content,
                    extra_metadata={
                        "raw_message": human_message.model_dump(),
                        "request_id": meta.get("request_id"),
                        "attachments": request_attachments,
                    },
                )
            except Exception as e:
                logger.error(f"Error saving user message: {e}")

        retrieval_id = f"kr_{uuid.uuid4().hex}"
        knowledge_contract = await prepare_knowledge_context(
            db,
            question=query,
            scope_snapshot=knowledge_scope_snapshot,
            run_id=meta.get("run_id"),
            request_id=meta.get("request_id"),
            retrieval_id=retrieval_id,
        )
        input_context["_knowledge_contract"] = knowledge_contract
        setattr(context, "_knowledge_contract", knowledge_contract)
        if knowledge_contract.get("status") != "SKIPPED":
            synthetic_message_id = f"msg_{uuid.uuid4().hex}"
            assistant_retrieval_message, tool_retrieval_message = _knowledge_contract_messages(
                knowledge_contract,
                query=query,
                message_id=synthetic_message_id,
            )
            messages.extend([assistant_retrieval_message, tool_retrieval_message])
            yield make_chunk(
                status="loading",
                stream_event={
                    "type": "tool_call",
                    "message_id": synthetic_message_id,
                    "tool_call_id": retrieval_id,
                    "name": "query_knowledge_scope",
                    "args": {"query_text": query, "orchestrated_by": "backend"},
                    "index": 0,
                },
                meta=meta,
            )
            yield make_chunk(
                status="stream_event",
                event={
                    "method": "tools",
                    "namespace": [],
                    "data": {
                        "event": "tool-finished",
                        "tool_call_id": retrieval_id,
                        "tool_name": "query_knowledge_scope",
                        "output": tool_retrieval_message.model_dump(mode="json"),
                    },
                },
                meta=meta,
            )

        # 智能体流式执行期间不访问业务数据库，先结束预处理事务并归还连接池。
        await db.commit()

        # 先构建 langgraph_config
        langgraph_config = {"configurable": {"thread_id": thread_id, "uid": uid}}
        token_usage_baseline = await _checkpoint_total_tokens(agent, langgraph_config, context=context)

        # LangGraph 会自动从 checkpointer 恢复 state（包括 uploads）
        # 无需手动加载或传递

        protocol_message_ids: dict[tuple[str, str], str] = {}
        reasoning_visibility: dict[str, ReasoningVisibilityBuffer] = {}
        async for mode, payload in _stream_agent_events(
            agent,
            messages,
            input_context=input_context,
            callbacks=langfuse_run.callbacks,
            metadata=langfuse_run.metadata,
            tags=langfuse_run.tags,
        ):
            if mode == "values":
                agent_state = extract_agent_state(payload if isinstance(payload, dict) else {})
                signature = _agent_state_signature(agent_state)
                if signature and signature != last_agent_state_signature:
                    last_agent_state_signature = signature
                    yield make_chunk(status="agent_state", agent_state=agent_state, meta=meta)
                continue

            if mode == "custom":
                compression = _context_compression_payload(payload)
                if compression is not None:
                    yield make_chunk(status="context_compression", compression=compression, meta=meta)
                continue

            if mode == "stream_event":
                yield make_chunk(
                    status="stream_event",
                    event=payload,
                    namespace=payload.get("namespace") if isinstance(payload, dict) else [],
                    meta=meta,
                    thread_id=payload.get("thread_id") if isinstance(payload, dict) else None,
                )
                continue

            msg, metadata = payload
            namespace = _metadata_namespace(metadata)
            chunk_thread_id = _metadata_thread_id(metadata, thread_id if not namespace else None)
            if namespace and not chunk_thread_id:
                continue

            is_subagent_chunk = bool(chunk_thread_id and chunk_thread_id != thread_id)
            stream_events = _message_payload_yuxi_events(
                msg,
                metadata=metadata,
                namespace=namespace,
                thread_id=chunk_thread_id,
                protocol_message_ids=protocol_message_ids,
            )

            for stream_event in stream_events:
                stream_event = _sanitize_stream_event(stream_event, reasoning_visibility)
                content = _stream_event_response(stream_event)
                if not is_subagent_chunk and content:
                    trace_info = get_trace_info(langfuse_run)
                    accumulated_content.append(content)
                    content_for_check = "".join(accumulated_content[-10:])
                    if conf.enable_content_guard and await content_guard.check_with_keywords(content_for_check):
                        full_msg = AIMessage(content="".join(accumulated_content))
                        await save_partial_message(
                            conv_repo,
                            thread_id,
                            full_msg,
                            "content_guard_blocked",
                            trace_info=trace_info,
                            run_id=meta.get("run_id"),
                            request_id=meta.get("request_id"),
                        )
                        meta["time_cost"] = asyncio.get_event_loop().time() - start_time
                        yield make_chunk(status="interrupted", message="检测到敏感内容，已中断输出", meta=meta)
                        return

                yield make_chunk(
                    content=content,
                    stream_event=stream_event,
                    metadata=metadata,
                    status="loading",
                    thread_id=chunk_thread_id,
                )

        full_msg = _ensure_full_msg(full_msg, accumulated_content)
        trace_info = get_trace_info(langfuse_run)

        if conf.enable_content_guard and hasattr(full_msg, "content") and await content_guard.check(full_msg.content):
            await save_partial_message(
                conv_repo,
                thread_id,
                full_msg,
                "content_guard_blocked",
                trace_info=trace_info,
                run_id=meta.get("run_id"),
                request_id=meta.get("request_id"),
            )
            meta["time_cost"] = asyncio.get_event_loop().time() - start_time
            yield make_chunk(status="interrupted", message="检测到敏感内容，已中断输出", meta=meta)
            return

        interrupted = False
        async for chunk in check_and_handle_interrupts(agent, langgraph_config, make_chunk, meta, thread_id, context):
            interrupted = True
            yield chunk

        meta["time_cost"] = asyncio.get_event_loop().time() - start_time
        try:
            graph = await agent.get_graph(context=context)
            state = await graph.aget_state(langgraph_config)
            agent_state = extract_agent_state(getattr(state, "values", {})) if state else {}
        except Exception:
            agent_state = {}
            state = None
        run_total_tokens = _token_usage_delta(_extract_total_tokens(state), token_usage_baseline)

        final_signature = _agent_state_signature(agent_state)
        if final_signature and final_signature != last_agent_state_signature:
            last_agent_state_signature = final_signature
            yield make_chunk(status="agent_state", agent_state=agent_state, meta=meta)

        # 先存储数据库，再返回 finished，避免前端查询时数据未落库
        try:
            await save_messages_from_langgraph_state(
                agent_instance=agent,
                thread_id=thread_id,
                conv_repo=conv_repo,
                config_dict=langgraph_config,
                context=context,
                trace_info=trace_info,
                run_id=meta.get("run_id"),
                request_id=meta.get("request_id"),
            )
        except Exception as e:
            logger.exception(f"Error saving messages from LangGraph state: {e}")
            yield make_chunk(status="warning", message=f"消息保存失败: {e}", meta=meta)

        usage_state = getattr(state, "values", None) or {}
        usage_payload = usage_state.get("token_usage") if isinstance(usage_state, dict) else None
        await _persist_run_total_tokens(
            db,
            meta.get("run_id"),
            run_total_tokens,
            uid=meta.get("uid"),
            model_spec=meta.get("model_spec"),
            estimated=bool(usage_payload.get("estimate")) if isinstance(usage_payload, dict) else None,
            credential_ref=meta.get("user_credential"),
            policy_version=meta.get("policy_version"),
        )

        if interrupted:
            return

        yield make_chunk(status="finished", meta=meta)

    except (asyncio.CancelledError, ConnectionError) as e:
        logger.warning(f"Client disconnected, cancelling stream: {e}")

        async def save_cleanup():
            nonlocal full_msg
            full_msg = _ensure_full_msg(full_msg, accumulated_content)

            async with pg_manager.get_async_session_context() as new_db:
                new_conv_repo = ConversationRepository(new_db)
                await save_partial_message(
                    new_conv_repo,
                    thread_id,
                    full_msg=full_msg,
                    error_message="对话已中断" if not full_msg else None,
                    error_type="interrupted",
                    trace_info=trace_info,
                    run_id=meta.get("run_id"),
                    request_id=meta.get("request_id"),
                )

        cleanup_task = asyncio.create_task(save_cleanup())
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error(f"Error during cleanup save: {exc}")

        yield make_chunk(status="interrupted", message="对话已中断", meta=meta)

    except Exception as e:
        logger.exception(f"Error streaming messages: {e}")

        error_msg = f"Error streaming messages: {e}"
        error_type = "unexpected_error"

        full_msg = _ensure_full_msg(full_msg, accumulated_content)

        async with pg_manager.get_async_session_context() as new_db:
            new_conv_repo = ConversationRepository(new_db)
            await save_partial_message(
                new_conv_repo,
                thread_id,
                full_msg=full_msg,
                error_message=error_msg,
                error_type=error_type,
                trace_info=trace_info,
                run_id=meta.get("run_id"),
                request_id=meta.get("request_id"),
            )

        yield make_chunk(status="error", error_type=error_type, error_message=error_msg, meta=meta)
    finally:
        if credential_context_token is not None:
            from yuxi.agents.models import reset_user_credential_override

            reset_user_credential_override(credential_context_token)
        from yuxi.agents.mcp.execution import reset_mcp_execution_context

        reset_mcp_execution_context(mcp_context_token)
        flush_langfuse()


async def stream_agent_resume(
    *,
    thread_id: str,
    resume_input: Any,
    meta: dict,
    current_user,
    db,
) -> AsyncIterator[bytes]:
    start_time = asyncio.get_event_loop().time()

    def make_resume_chunk(content=None, **kwargs):
        chunk_thread_id = kwargs.pop("thread_id", None) or meta.get("thread_id") or thread_id
        return (
            json.dumps(
                {"request_id": meta.get("request_id"), "response": content, "thread_id": chunk_thread_id, **kwargs},
                ensure_ascii=False,
            ).encode("utf-8")
            + b"\n"
        )

    yield make_resume_chunk(status="init", meta=meta)

    resume_command = Command(resume=resume_input)

    uid = str(current_user.uid)
    from yuxi.agents.mcp.execution import McpExecutionContext, set_mcp_execution_context
    from yuxi.services.principal import resolve_tenant_id

    mcp_context_token = set_mcp_execution_context(
        McpExecutionContext(
            tenant_id=int(meta.get("tenant_id") or await resolve_tenant_id(db, uid)),
            uid=uid,
            run_id=meta.get("run_id"),
            agent_slug=meta.get("agent_slug"),
        )
    )
    try:
        agent_item, agent, agent_config = await _resolve_agent_runtime(
            db=db,
            user=current_user,
            requested_agent_slug=None,
            thread_id=thread_id,
        )
    except ValueError as e:
        # 该分支同样尚未进入下方主 try/finally。
        from yuxi.agents.mcp.execution import reset_mcp_execution_context

        reset_mcp_execution_context(mcp_context_token)
        yield make_resume_chunk(status="error", error_type="invalid_agent", error_message=str(e), meta=meta)
        return

    # 恢复流执行期间不访问业务数据库，先结束运行时解析事务并归还连接池。
    await db.commit()

    meta["agent_slug"] = agent_item.slug
    meta["backend_id"] = agent_item.backend_id
    knowledge_scope_snapshot = await _ensure_knowledge_scope_snapshot(
        db=db,
        user=current_user,
        agent_slug=agent_item.slug,
        meta=meta,
    )
    input_context = await build_agent_input_context(
        agent_config or {},
        thread_id=thread_id,
        uid=uid,
        run_id=meta.get("run_id"),
        request_id=meta.get("request_id"),
    )
    _apply_model_override(input_context, meta)
    _apply_knowledge_scope_snapshot(input_context, knowledge_scope_snapshot)
    context = _build_agent_context(agent, input_context)
    _bind_knowledge_scope_to_context(context, knowledge_scope_snapshot)
    langfuse_run = _build_langfuse_run_context(
        current_user=current_user,
        thread_id=thread_id,
        agent_id=agent_item.slug,
        backend_id=agent_item.backend_id,
        request_id=meta.get("request_id") or str(uuid.uuid4()),
        operation="agent_chat_resume",
        message_type="resume",
        meta=meta,
    )
    trace_info: dict[str, Any] = {}
    last_agent_state_signature = ""
    credential_context_token = None

    langgraph_config = {"configurable": {"thread_id": thread_id, "uid": uid}}
    token_usage_baseline = await _checkpoint_total_tokens(agent, langgraph_config, context=context)

    stream_source = agent.stream_resume_with_state(
        resume_command,
        input_context=input_context,
        callbacks=langfuse_run.callbacks,
        metadata=langfuse_run.metadata,
        tags=langfuse_run.tags,
    )

    protocol_message_ids: dict[tuple[str, str], str] = {}
    reasoning_visibility: dict[str, ReasoningVisibilityBuffer] = {}
    accumulated_content: list[str] = []
    conv_repo = ConversationRepository(db)

    try:
        credential_context_token = await _activate_user_credential(db=db, uid=uid, meta=meta)
        async for mode, payload in stream_source:
            if mode == "values":
                agent_state = extract_agent_state(payload if isinstance(payload, dict) else {})
                signature = _agent_state_signature(agent_state)
                if signature and signature != last_agent_state_signature:
                    last_agent_state_signature = signature
                    yield make_resume_chunk(status="agent_state", agent_state=agent_state, meta=meta)
                continue

            if mode == "stream_event":
                event_payload = payload if isinstance(payload, dict) else {}
                yield make_resume_chunk(
                    status="stream_event",
                    event=event_payload,
                    namespace=event_payload.get("namespace") or [],
                    meta=meta,
                    thread_id=event_payload.get("thread_id"),
                )
                continue

            if mode == "custom":
                compression = _context_compression_payload(payload)
                if compression is not None:
                    yield make_resume_chunk(status="context_compression", compression=compression, meta=meta)
                continue

            if mode != "messages":
                continue

            msg, metadata = payload
            metadata = dict(metadata or {})
            namespace = _metadata_namespace(metadata)
            chunk_thread_id = _metadata_thread_id(metadata, thread_id if not namespace else None)
            if namespace and not chunk_thread_id:
                continue

            if chunk_thread_id == thread_id:
                trace_info = get_trace_info(langfuse_run)

            stream_events = _message_payload_yuxi_events(
                msg,
                metadata=metadata,
                namespace=namespace,
                thread_id=chunk_thread_id,
                protocol_message_ids=protocol_message_ids,
            )

            for stream_event in stream_events:
                stream_event = _sanitize_stream_event(stream_event, reasoning_visibility)
                content = _stream_event_response(stream_event)
                if chunk_thread_id == thread_id and content:
                    # 与 chat 流一致的双道内容护栏（滚动检查），防止借 resume
                    # 路径绕过敏感内容检查。
                    accumulated_content.append(content)
                    if conf.enable_content_guard and await content_guard.check_with_keywords(
                        "".join(accumulated_content[-10:])
                    ):
                        await save_partial_message(
                            conv_repo,
                            thread_id,
                            AIMessage(content="".join(accumulated_content)),
                            "content_guard_blocked",
                            trace_info=trace_info,
                            run_id=meta.get("run_id"),
                            request_id=meta.get("request_id"),
                        )
                        meta["time_cost"] = asyncio.get_event_loop().time() - start_time
                        yield make_resume_chunk(status="interrupted", message="检测到敏感内容，已中断输出", meta=meta)
                        return

                yield make_resume_chunk(
                    content=content,
                    stream_event=stream_event,
                    metadata=metadata,
                    status="loading",
                    thread_id=chunk_thread_id,
                )

        full_resume_content = "".join(accumulated_content)
        if conf.enable_content_guard and full_resume_content and await content_guard.check(full_resume_content):
            await save_partial_message(
                conv_repo,
                thread_id,
                AIMessage(content=full_resume_content),
                "content_guard_blocked",
                trace_info=trace_info,
                run_id=meta.get("run_id"),
                request_id=meta.get("request_id"),
            )
            meta["time_cost"] = asyncio.get_event_loop().time() - start_time
            yield make_resume_chunk(status="interrupted", message="检测到敏感内容，已中断输出", meta=meta)
            return

        interrupted = False
        async for chunk in check_and_handle_interrupts(
            agent, langgraph_config, make_resume_chunk, meta, thread_id, context
        ):
            interrupted = True
            yield chunk

        meta["time_cost"] = asyncio.get_event_loop().time() - start_time

        try:
            graph = await agent.get_graph(context=context)
            state = await graph.aget_state(langgraph_config)
            agent_state = extract_agent_state(getattr(state, "values", {})) if state else {}
        except Exception:
            agent_state = {}
            state = None
        run_total_tokens = _token_usage_delta(_extract_total_tokens(state), token_usage_baseline)

        final_signature = _agent_state_signature(agent_state)
        if final_signature and final_signature != last_agent_state_signature:
            yield make_resume_chunk(status="agent_state", agent_state=agent_state, meta=meta)

        # 先存储数据库，再返回 finished，避免前端查询时数据未落库
        try:
            await save_messages_from_langgraph_state(
                agent_instance=agent,
                thread_id=thread_id,
                conv_repo=conv_repo,
                config_dict=langgraph_config,
                context=context,
                trace_info=trace_info,
                run_id=meta.get("run_id"),
                request_id=meta.get("request_id"),
            )
        except Exception as e:
            logger.exception(f"Error saving messages from LangGraph state: {e}")
            yield make_resume_chunk(status="warning", message=f"消息保存失败: {e}", meta=meta)

        usage_state = getattr(state, "values", None) or {}
        usage_payload = usage_state.get("token_usage") if isinstance(usage_state, dict) else None
        await _persist_run_total_tokens(
            db,
            meta.get("run_id"),
            run_total_tokens,
            uid=meta.get("uid"),
            model_spec=meta.get("model_spec"),
            estimated=bool(usage_payload.get("estimate")) if isinstance(usage_payload, dict) else None,
            credential_ref=meta.get("user_credential"),
            policy_version=meta.get("policy_version"),
        )

        if interrupted:
            return

        yield make_resume_chunk(status="finished", meta=meta)

    except (asyncio.CancelledError, ConnectionError) as e:
        logger.warning(f"Client disconnected during resume: {e}")

        async with pg_manager.get_async_session_context() as new_db:
            new_conv_repo = ConversationRepository(new_db)
            await save_partial_message(
                new_conv_repo,
                thread_id,
                error_message="对话恢复已中断",
                error_type="resume_interrupted",
                trace_info=trace_info,
                run_id=meta.get("run_id"),
                request_id=meta.get("request_id"),
            )

        yield make_resume_chunk(status="interrupted", message="对话恢复已中断", meta=meta)

    except Exception as e:
        logger.exception(f"Error during resume: {e}")

        async with pg_manager.get_async_session_context() as new_db:
            new_conv_repo = ConversationRepository(new_db)
            await save_partial_message(
                new_conv_repo,
                thread_id,
                error_message=f"Error during resume: {e}",
                error_type="resume_error",
                trace_info=trace_info,
                run_id=meta.get("run_id"),
                request_id=meta.get("request_id"),
            )

        yield make_resume_chunk(message=f"Error during resume: {e}", status="error")
    finally:
        if credential_context_token is not None:
            from yuxi.agents.models import reset_user_credential_override

            reset_user_credential_override(credential_context_token)
        from yuxi.agents.mcp.execution import reset_mcp_execution_context

        reset_mcp_execution_context(mcp_context_token)
        flush_langfuse()


def _serialize_state_messages(values: dict[str, Any]) -> list[dict[str, Any]]:
    messages = values.get("messages") if isinstance(values, dict) else None
    if not isinstance(messages, list):
        return []
    serialized = []
    for message in messages:
        if hasattr(message, "model_dump"):
            serialized.append(message.model_dump())
        elif isinstance(message, dict):
            serialized.append(dict(message))
        else:
            serialized.append({"type": "unknown", "content": str(message)})
    return serialized


async def _read_checkpoint_state(agent, *, uid: str, thread_id: str, context):
    graph = await agent.get_graph(context=context)
    langgraph_config = {"configurable": {"uid": uid, "thread_id": thread_id}}
    return await graph.aget_state(langgraph_config)


async def get_agent_state_view(
    *,
    thread_id: str,
    current_user: User,
    db,
    include_messages: bool = False,
) -> dict:
    from fastapi import HTTPException

    current_uid = str(current_user.uid)
    conv_repo = ConversationRepository(db)
    agent_repo = AgentRepository(db)
    run_repo = AgentRunRepository(db)
    conversation = await conv_repo.get_conversation_by_thread_id(thread_id)
    if conversation:
        if conversation.uid != str(current_uid) or conversation.status == "deleted":
            raise HTTPException(status_code=404, detail="对话线程不存在")

        agent_item = await agent_repo.get_by_slug(conversation.agent_id)
        if not agent_item:
            raise HTTPException(status_code=404, detail="智能体不存在")
        agent = agent_manager.get_agent(agent_item.backend_id)
        if not agent:
            raise HTTPException(status_code=404, detail="智能体后端不存在")
        agent_config = await normalize_agent_context_config(
            (agent_item.config_json or {}).get("context", {}),
            db=db,
            user=current_user,
            context_schema=agent.context_schema,
        )
        input_context = await build_agent_input_context(
            agent_config,
            thread_id=thread_id,
            uid=current_uid,
        )
        latest_run = await run_repo.get_latest_run_by_thread_for_user(thread_id, current_uid)
        if latest_run and isinstance(latest_run.input_payload, dict):
            model_spec = latest_run.input_payload.get("model_spec")
            if isinstance(model_spec, str) and model_spec.strip():
                input_context["model"] = model_spec.strip()
            knowledge_scope_snapshot = latest_run.input_payload.get("knowledge_scope_snapshot")
            _apply_knowledge_scope_snapshot(input_context, knowledge_scope_snapshot)
        context = _build_agent_context(agent, input_context)
        if latest_run and isinstance(latest_run.input_payload, dict):
            _bind_knowledge_scope_to_context(context, latest_run.input_payload.get("knowledge_scope_snapshot"))
        state = await _read_checkpoint_state(agent, uid=current_uid, thread_id=thread_id, context=context)
        values = getattr(state, "values", {}) if state else {}
        response = {"agent_state": extract_agent_state(values)}
        relation = await SubagentThreadRepository(db).get_by_child_conversation_for_user(
            conversation.id,
            str(current_uid),
        )
        if relation:
            parent_conversation = await conv_repo.get_conversation_by_id(relation.parent_conversation_id)
            if (
                not parent_conversation
                or parent_conversation.uid != str(current_uid)
                or parent_conversation.status == "deleted"
            ):
                raise HTTPException(status_code=404, detail="父对话线程不存在")
            response["parent_thread_id"] = parent_conversation.thread_id
            response["subagent_thread"] = relation.to_dict()
            latest_run = await run_repo.get_latest_subagent_run_by_thread_for_user(
                thread_id,
                str(current_uid),
            )
            if latest_run:
                try:
                    response["subagent_run"] = serialize_subagent_run_state(latest_run)
                except ValueError as exc:
                    logger.error(f"子智能体运行记录格式异常: thread_id={thread_id}, run_id={latest_run.id}, {exc}")
                    raise HTTPException(status_code=500, detail="子智能体运行记录格式异常") from exc
        if include_messages:
            response["messages"] = _serialize_state_messages(values)
        return response

    # 子智能体线程在创建时必然同时写入子对话与线程关系（见 SubagentRunService.start），
    # 由上面的 conversation 分支统一处理；走到这里说明该 thread 没有对应对话，即线程不存在。
    raise HTTPException(status_code=404, detail="对话线程不存在")
