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
import base64
import json
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, Literal

from langchain.messages import AIMessage, AIMessageChunk, ToolMessage
from langgraph.types import Command
from sqlalchemy import select
from yuxi import config as conf
from yuxi.agents.buildin import agent_manager
from yuxi.agents.context import build_agent_input_context, normalize_agent_context_config
from yuxi.agents.mcp.capability_registry import profile_for_server_tool
from yuxi.agents.state import AgentStatePayload
from yuxi.knowledge.orchestration import prepare_knowledge_context
from yuxi.knowledge.planning.turn_execution_plan import (
    AuthorityDecision,
    AuthorityOutcome,
    Capability,
    EvidenceLevel,
    RunSourceManifest,
    SourceClass,
    SourceUseRecord,
    TaskIntent,
    TurnExecutionPlan,
    plan_turn,
)
from yuxi.knowledge.rendering.answer_draft import render_answer_draft
from yuxi.knowledge.rendering.citation_channel import apply_citation_channel, render_locator_chip
from yuxi.knowledge.rendering.profile_projection import project_data_plane
from yuxi.knowledge.rendering.provider_status_view import render_provider_status_answer
from yuxi.knowledge.rendering.source_output_guard import (
    fact_catalog_summary,
    guard_answer_for_evidence_level,
    guard_glossary_answer,
    render_degraded_fact_sheet,
)
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
from yuxi.services.run_stream_errors import (
    MODEL_CONNECTION_ERROR,
    MODEL_CONNECTION_ERROR_MESSAGE,
    is_model_connection_error,
)
from yuxi.services.subagent_run_service import serialize_subagent_run_state
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import Agent, AgentRun, MCPCallAudit, User
from yuxi.utils.guard import content_guard
from yuxi.utils.logging_config import logger
from yuxi.utils.markdown_tables import normalize_markdown_tables
from yuxi.utils.question_utils import (
    normalize_questions as _normalize_interrupt_questions,
)
from yuxi.utils.reasoning_visibility import (
    ReasoningVisibilityBuffer,
    redact_reasoning_metadata,
    sanitize_visible_text,
)
from yuxi.utils.thread_utils import extract_thread_id as _metadata_thread_id

_RUN_TIMEOUT_ERROR_MESSAGE = "服务端长时间未收到检索或模型输出，已安全结束本次任务，请重试。"


def _stream_abort_details(
    exc: asyncio.CancelledError | ConnectionError,
    *,
    resume: bool = False,
) -> tuple[str, str, str]:
    """Map cancellation provenance to persisted/UI semantics.

    The AgentRun worker cancels the pending ``__anext__`` task to enforce its
    own timeout and user-cancel budgets. That cancellation is not a client
    disconnect, so preserving the reason prevents a failed run from being
    rendered later as the misleading generic ``对话已中断`` message.
    """

    reason = str(exc.args[0]) if isinstance(exc, asyncio.CancelledError) and exc.args else ""
    if reason in {"run_idle_timeout", "run_total_timeout"}:
        return "error", reason, _RUN_TIMEOUT_ERROR_MESSAGE
    if reason == "cancel_requested":
        return "interrupted", "cancelled", "对话已取消"
    if resume:
        return "interrupted", "resume_interrupted", "对话恢复已中断"
    return "interrupted", "interrupted", "对话已中断"


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


def _model_query_message(message: Any, model_query: str) -> Any:
    """送给模型的文本剥离控制 token（mention.v2）；历史落库仍用原始问题。"""
    from langchain_core.messages import HumanMessage

    additional_kwargs = dict(getattr(message, "additional_kwargs", None) or {})
    content = getattr(message, "content", None)
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append({**part, "text": model_query})
            else:
                parts.append(part)
        return HumanMessage(content=parts, additional_kwargs=additional_kwargs)
    return HumanMessage(content=model_query, additional_kwargs=additional_kwargs)


def _frozen_mention_resolution(meta: dict | None) -> dict[str, Any]:
    value = (meta or {}).get("mention_resolution")
    return value if isinstance(value, dict) else {}


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


def _decode_image_bytes(image_content: str | None) -> bytes | None:
    """解码图片附件（裸 base64）供视觉观察通道使用；非法编码返回 None。"""
    if not image_content:
        return None
    try:
        return base64.b64decode(image_content, validate=False)
    except (ValueError, TypeError):
        return None


def _deterministic_locator_answer(contract: dict[str, Any]) -> str | None:
    """Render QUOTE_LOCATOR without giving an LLM any page-number authority."""
    from yuxi.knowledge.contracts.locator_binding import authoritative_locator_projection

    plan = contract.get("retrieval_plan") or {}
    if plan.get("answer_mode") != "DETERMINISTIC_LOCATOR":
        return None
    locator = contract.get("locator_resolution") or {}
    status = str(locator.get("status") or "")
    policy = contract.get("answer_policy") or {}
    binding = locator.get("binding") or {}
    authoritative_locator = authoritative_locator_projection(binding)
    locator_authorized = bool(
        policy.get("page_claim_allowed") is True
        and policy.get("document_citations_allowed") is True
        and authoritative_locator is not None
    )
    if locator_authorized:
        figure_label = str(locator.get("container_label") or "").strip()
        prefix = f"已定位 {figure_label}：" if figure_label else "已可靠定位到原文："
        return f"{prefix}{render_locator_chip(authoritative_locator)}"
    if status == "MULTIPLE_MATCHES":
        documents = _candidate_documents(locator, policy)
        if len(documents) > 1:
            listing = "\n".join(f"- {item.get('filename') or item.get('file_id')}" for item in documents)
            return (
                "该编号在当前知识范围内命中多篇文献，无法唯一确定是哪一篇：\n"
                f"{listing}\n"
                "请输入 @ 选择「文献」指定其中一篇，或粘贴题注原文后重试。"
            )
        return "该原句在当前知识范围内存在多个物理位置，当前无法可靠定位唯一原文页码。"
    # P0 降级：定位失败但视觉观察可用——描述看到了什么 + 可行动建议（永不给页码）
    visual_hint = _visual_description(locator)
    caption_candidates = _caption_search_hint(locator)
    if visual_hint or caption_candidates:
        lines = []
        if visual_hint:
            lines.append(visual_hint)
        if caption_candidates:
            lines.append(caption_candidates)
        lines.append("未能从库内图表唯一匹配该截图。建议：")
        lines.append("① 截取包含图表编号（如 Figure 2）或题注的更完整区域")
        lines.append("② 直接粘贴题注文字查询")
        lines.append("③ 输入 @ 选择文献后用 Figure N 查询")
        return "\n".join(lines)
    return "当前无法可靠定位原文页码。"


def _caption_search_hint(locator: dict[str, Any]) -> str:
    """P1：题注搜索候选 → "可能来自"提示（无页码，仅编号+文件名）。"""
    candidates = locator.get("caption_search_candidates")
    if not isinstance(candidates, list) or not candidates:
        return ""
    top = candidates[0]
    label = str(top.get("figure_label") or "").strip()
    filename = str(top.get("filename") or "").strip()
    hits = int(top.get("signal_hits") or 0)
    if not label or hits < 1:
        return ""
    hint = f"该截图可能来自 {label}（{filename}）" if filename else f"该截图可能来自 {label}"
    if len(candidates) > 1:
        others = [str(c.get("figure_label") or "").strip() for c in candidates[1:4] if c.get("figure_label")]
        if others:
            hint += f"；其他可能：{', '.join(others)}"
    return hint + "。"


def _visual_description(locator: dict[str, Any]) -> str:
    """从 locator_resolution.observation_summary 生成确定性视觉描述——只引用观察契约字段。"""
    summary = locator.get("observation_summary")
    if not isinstance(summary, dict):
        return ""
    parts: list[str] = []
    label = str(summary.get("figure_label") or "").strip()
    if label:
        parts.append(f"截图包含图表编号「{label}」")
    entities = [str(item) for item in (summary.get("visible_entities") or []) if str(item).strip()][:4]
    if entities:
        parts.append(f"可见基因/蛋白名：{', '.join(entities)}")
    text_fragments = [str(item) for item in (summary.get("visible_text") or []) if str(item).strip()][:3]
    if text_fragments:
        parts.append(f"图中文字：{', '.join(text_fragments)}")
    structure = [str(item) for item in (summary.get("visual_structure_active") or []) if str(item).strip()]
    structure_cn = {
        "bar_chart": "柱状图",
        "line_chart": "折线图",
        "microscopy": "显微照片",
        "tissue_images": "组织切片",
        "gel": "凝胶电泳",
        "phylogenetic_tree": "系统进化树",
    }
    described = [structure_cn.get(item, item) for item in structure if item in structure_cn]
    if described:
        parts.append(f"图表类型：{'、'.join(described)}")
    panels = summary.get("panel_labels") or []
    if panels and len(panels) > 1:
        parts.append(f"包含 {len(panels)} 个子图 ({', '.join(str(p) for p in panels[:6])})")
    return "；".join(parts) + "。" if parts else ""


def _candidate_documents(locator: dict[str, Any], policy: dict[str, Any] | None) -> list[dict[str, str]]:
    """MULTIPLE_MATCHES 下可发布的候选文献清单——只含文档身份（file_id/kb_id/filename），
    不带页码、不带图片；策略位 candidate_documents_allowed 未授权即为空。"""
    if not (policy or {}).get("candidate_documents_allowed"):
        return []
    documents = locator.get("candidate_documents")
    if not isinstance(documents, list):
        return []
    published: list[dict[str, str]] = []
    for item in documents:
        if not isinstance(item, dict) or not item.get("file_id"):
            continue
        published.append(
            {
                "file_id": str(item.get("file_id") or ""),
                "kb_id": str(item.get("kb_id") or ""),
                "filename": str(item.get("filename") or ""),
            }
        )
        if len(published) >= 10:
            break
    return published


_CITATION_READY_KEYS = ("status", "evidence_id", "file_id", "filename", "zone", "page", "anchor_id")


def _citation_ready_payload(locator: dict[str, Any]) -> dict[str, Any]:
    """citation_ready 载荷（v2）：七键 + kb_id/revision_id（前端取图三段式的硬前置）。

    figures 只在 ``figure_card_enabled`` 开启且后端投影已附着时携带——**字段缺席 ⟺ 未发布**
    （不发明空数组）。图卡数据只来自 ``locator_resolution["figure_projection"]``（编排器在
    contract_hash 之前写入的确定性投影），这里不现查表、不做任何二次裁决。
    """
    citation = {key: locator.get(key) for key in _CITATION_READY_KEYS}
    citation["kb_id"] = locator.get("kb_id")
    citation["revision_id"] = locator.get("parse_revision_id")
    payload: dict[str, Any] = {"citation": citation}
    if getattr(conf, "figure_card_enabled", False):
        projection = locator.get("figure_projection")
        figures = projection.get("figures") if isinstance(projection, dict) else None
        if figures:
            payload["figures"] = list(figures)
    return payload


def _guard_knowledge_answer(text: str, contract: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    from yuxi.knowledge.contracts.locator_binding import authoritative_locator_projection

    locator_resolution = contract.get("locator_resolution") or {}
    binding = locator_resolution.get("binding") or {}
    # locator block → binding → 权威芯片；缺失绑定渲染为失败关闭文案（无 Binding 就没有页码）
    locator_bindings = {str(binding["binding_id"]): binding} if binding.get("binding_id") else None
    rendered, draft_validation = render_answer_draft(text, locator_bindings=locator_bindings)
    guarded, citation_validation = apply_citation_channel(
        rendered,
        contract.get("citations") or [],
        locator=contract.get("locator_resolution"),
        partition_intent=(contract.get("locator_intent") or {}).get("partition_intent"),
        authority_policy=contract.get("answer_policy"),
    )
    citation_validation["answer_draft"] = draft_validation
    # I4 逐 Claim 证据授权执行：机制/题注句在权限拒绝时逐句过 resolve_binding
    # 验证——有 VERIFIED 证据绑定的保留，无据（或引用池为空无法验证）的删除。
    if isinstance(contract.get("answer_policy"), dict):
        from yuxi.knowledge.rendering.explanation_claims import enforce_explanation_grounding

        answer_policy = contract["answer_policy"]
        evidence_authorized = bool(
            answer_policy.get("page_claim_allowed") is True
            and answer_policy.get("document_citations_allowed") is True
            and authoritative_locator_projection(binding) is not None
        )
        guarded, mechanism_removed = enforce_explanation_grounding(
            guarded,
            policy=answer_policy,
            citations=(contract.get("citations") or []) if evidence_authorized else [],
            locator=locator_resolution,
        )
        citation_validation["mechanism_claims_removed"] = mechanism_removed
    # P4 解释绑定：复合意图流按 Claim 分类验证（CAPTION_FACT/TEXT_SUPPORTED_
    # INTERPRETATION 必须绑定对应载体；UNSUPPORTED 明示，不静默输出）
    if (contract.get("locator_intent") or {}).get("compound") and locator_resolution.get("status") == "VERIFIED":
        from yuxi.knowledge.rendering.explanation_claims import classify_explanation_claims

        observation = contract.get("figure_image_observation")
        citation_validation["explanation_claims"] = classify_explanation_claims(
            guarded,
            citations=contract.get("citations") or [],
            locator=locator_resolution,
            observation=observation if isinstance(observation, dict) and "figure_label" in observation else None,
        )
    return guarded, citation_validation


def _initial_source_manifest(plan: TurnExecutionPlan) -> RunSourceManifest:
    canonical_requested = Capability.CANONICAL_LOOKUP in plan.required_capabilities
    return RunSourceManifest(
        plan_id=plan.plan_id,
        source_policy=plan.source.policy,
        document_evidence_requested=plan.evidence.required,
        mcp_requested=plan.requires_mcp,
        status=(
            "PLANNED" if plan.requires_document_retrieval or plan.requires_mcp or canonical_requested else "COMPLETED"
        ),
    )


def _plan_failure_answer(plan: TurnExecutionPlan) -> str:
    if plan.error_code == "PLAN_UNSATISFIABLE":
        return (
            "本轮指定的 MCP 来源不具备经过服务端信任登记的 PDF 原文页码定位能力，"
            "因此执行计划不可满足；系统没有改用知识库或猜测页码。"
        )
    if plan.error_code == "MCP_SERVER_NOT_CONFIGURED":
        return (
            f"本轮点名的 MCP 服务器「{plan.required_server_missing}」未绑定到当前智能体，"
            "系统未改用其他等价能力服务器代答；请在扩展页安装并启用该服务器后重试。"
        )
    if getattr(plan, "required_server", None):
        return (
            f"本轮点名的 MCP 服务器「{plan.required_server}」没有成功完成的工具调用，"
            "系统未静默改用其他等价能力服务器代答。"
        )
    if plan.source.policy.value == "BIBLIOGRAPHY_ONLY":
        return "本轮所需的受信文献检索来源当前不可用；系统没有改用模型记忆或本地知识库补写文献。"
    if plan.task.primary_intent.value == "GLOSSARY_LOOKUP":
        return "本轮禁止使用术语词典权威源，系统没有改用文献、网络或模型记忆补写术语定义。"
    if plan.requires_mcp:
        return "本轮指定的 MCP 来源当前不可用；系统没有静默改用知识库或网络来源。"
    return "本轮要求的文献来源当前不可用，无法在不扩大来源范围的前提下完成回答。"


def _apply_skill_plan_dispatch(context, plan: TurnExecutionPlan, manifest: RunSourceManifest) -> None:
    """Skill 注入服从 plan（P0-A）：本轮 plan 明确禁用 STRUCTURED_DATABASE 时，
    MCP 依赖型 Skill 不进提示词/可读闭包——其 SOURCE-ONLY 契约不得约束
    词典/文献/文档轮的输出格式。分派结果写 manifest amendment 供审计。"""
    if SourceClass.STRUCTURED_DATABASE not in set(plan.source.forbidden_sources):
        return
    dependency_map: dict = getattr(context, "_runtime_skill_dependency_map", None) or {}
    mcp_backed = {str(slug) for slug, node in dependency_map.items() if isinstance(node, dict) and node.get("mcps")}
    dropped: list[str] = []
    for attr in ("_prompt_skills", "_readable_skills"):
        current = list(getattr(context, attr, None) or [])
        kept = [slug for slug in current if str(slug) not in mcp_backed]
        dropped.extend(slug for slug in current if str(slug) in mcp_backed)
        setattr(context, attr, kept)
    if dropped:
        manifest.amendments.append(
            {
                "type": "SKILL_PLAN_DISPATCH",
                "dropped_skills": list(dict.fromkeys(str(slug) for slug in dropped)),
                "reason_code": "STRUCTURED_DATABASE_FORBIDDEN",
            }
        )


async def _persist_turn_runtime(
    db,
    run_id: str | None,
    *,
    plan: TurnExecutionPlan,
    manifest: RunSourceManifest,
) -> None:
    if not run_id:
        return
    run = await AgentRunRepository(db).get_run(str(run_id))
    if run is None:
        return
    run.input_payload = {
        **dict(run.input_payload or {}),
        "turn_execution_plan": plan.public_dict(),
        "run_source_manifest": manifest.model_dump(mode="json"),
    }
    await db.flush()


def _pinned_mention_items(mention_resolution: dict | None, mention_type: str) -> list[tuple[str, str]]:
    """从冻结结论提取已解析的执行者类提及：[(resource_id, strength)]。"""
    if not isinstance(mention_resolution, dict):
        return []
    items: list[dict] = []
    for item in mention_resolution.get("mentions") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("type")) != mention_type or str(item.get("status")) != "RESOLVED":
            continue
        resource_id = str(item.get("resource_id") or "").strip()
        if resource_id:
            items.append((resource_id, str(item.get("strength") or "REQUIRED").upper()))
    return items


#: MCP 负状态：科学上"合法无结果"或失败，不算成功调用，不参与 adoption/匹配。
_MCP_ADOPTION_NEGATIVE_STATUSES = frozenset(
    {"NOT_FOUND", "NO_EVIDENCE", "AMBIGUOUS", "CONFLICT", "UNAVAILABLE", "ERROR"}
)


def _record_mention_fulfillment(
    manifest: RunSourceManifest,
    *,
    mention_type: str,
    resource_id: str,
    strength: str,
    fulfilled: bool,
    reason_code: str | None,
) -> None:
    """后验回写（缺口 B 语义）：FULFILLED 记账；未兑现按强度分级——

    REQUIRED → amendment + manifest.status 降级 DEGRADED（run 级可观测）；
    PREFERRED → 仅审计记 MENTION_PREFERRED_NOT_USED，不打扰用户（防"狼来了"）。
    """
    manifest.amendments.append(
        {
            "type": "MENTION_FULFILLED" if fulfilled else "MENTION_UNFULFILLED",
            "mention_type": mention_type,
            "resource_id": resource_id,
            "strength": strength,
            "reason_code": reason_code,
        }
    )
    if not fulfilled and strength == "REQUIRED" and manifest.status == "COMPLETED":
        manifest.status = "DEGRADED"


def _append_mcp_source_uses(
    manifest: RunSourceManifest,
    *,
    audits: list[Any],
    matched_ids: set[int],
) -> None:
    """Project append-only MCP audit rows into the run-level source ledger.

    F1（审计为权威）：受信注册表、answer-eligible、非 discovery 的**成功**调用
    一律 adopted——计划未要求 MCP 的轮次（golden 实测 run 09452049：序列零标记
    放行）同样要挂账本核验；``matched_ids`` 只再服务于 requires_mcp 与 manifest
    状态。负状态（NOT_FOUND 等）不算成功调用，不参与 adoption。
    """
    existing = {item.source_use_id for item in manifest.source_uses}
    for audit in audits:
        source_use_id = f"mcp:{audit.id}"
        if source_use_id in existing:
            continue
        profile = profile_for_server_tool(str(audit.server_slug or ""), str(audit.capability_name or ""))
        bibliography = bool(profile and profile.source_class == "BIBLIOGRAPHY")
        discovery = bool(profile and profile.source_class == "DISCOVERY")
        provenance = audit.provenance or {}
        provider_status = str(provenance.get("provider_status") or "").upper()
        if not provider_status:
            # Legacy MCP adapters expose structured payloads as text, so host-level
            # ``provider_status`` can be absent even though the append-only fact
            # manifest contains the authoritative top-level /status field.
            for fact in (provenance.get("fact_manifest") or {}).get("facts") or []:
                if str(fact.get("path") or "") == "/status" and fact.get("string_value"):
                    provider_status = str(fact["string_value"]).upper()
                    break
        execution_status = str(audit.status).upper()
        succeeded = str(audit.status).lower() == "success" and provider_status not in _MCP_ADOPTION_NEGATIVE_STATUSES
        adopted = (
            succeeded
            and not discovery
            and (int(audit.id) in matched_ids or (profile is not None and profile.answer_eligible))
        )
        manifest.source_uses.append(
            SourceUseRecord(
                source_use_id=source_use_id,
                source_class=(
                    SourceClass.DISCOVERY
                    if discovery
                    else SourceClass.BIBLIOGRAPHY
                    if bibliography
                    else SourceClass.STRUCTURED_DATABASE
                ),
                evidence_level=(
                    EvidenceLevel.NONE
                    if discovery
                    else EvidenceLevel.BIBLIOGRAPHIC
                    if bibliography
                    else EvidenceLevel.DATA_PROVENANCE
                ),
                provider_id=str(audit.server_slug),
                operation=str(audit.capability_name),
                status=execution_status,
                execution_status=execution_status,
                provider_status=provider_status or None,
                request_digest=audit.arguments_digest,
                result_digest=audit.result_digest,
                evidence_ids=[source_use_id] if adopted else [],
                provenance={"mcp_call_audit_id": int(audit.id), **dict(audit.provenance or {})},
                adopted=adopted,
            )
        )
        existing.add(source_use_id)


def _append_knowledge_source_uses(
    manifest: RunSourceManifest,
    *,
    plan: TurnExecutionPlan,
    contract: dict[str, Any],
) -> None:
    """Record the actual evidence channels used by the frozen knowledge Contract."""
    grouped: dict[tuple[SourceClass, str], list[str]] = {}
    for row in contract.get("evidence") or []:
        if not isinstance(row, dict):
            continue
        origin = str(row.get("source_type") or row.get("origin") or "DOCUMENT").upper()
        if origin in {"CSV_ROW", "CANONICAL_RECORD"}:
            source_class = SourceClass.CANONICAL_RECORD
            plane = "CANONICAL_DATA"
        elif origin in {"GRAPH", "CANONICAL_CLAIM", "STRUCTURED"}:
            source_class = SourceClass.KNOWLEDGE_GRAPH
            plane = "GRAPH_EVIDENCE"
        else:
            source_class = SourceClass.LOCAL_DOCUMENT
            plane = "DOCUMENT_EVIDENCE"
        provider_id = str(row.get("kb_id") or "knowledge-scope")
        evidence_id = str(row.get("evidence_id") or "").strip()
        grouped.setdefault((source_class, provider_id), [])
        if evidence_id:
            grouped[(source_class, provider_id)].append(evidence_id)
        manifest.used_planes = list(dict.fromkeys([*manifest.used_planes, plane]))

    existing = {item.source_use_id for item in manifest.source_uses}
    for (source_class, provider_id), evidence_ids in grouped.items():
        source_use_id = f"knowledge:{contract.get('retrieval_id')}:{source_class}:{provider_id}"
        if source_use_id in existing:
            continue
        manifest.source_uses.append(
            SourceUseRecord(
                source_use_id=source_use_id,
                source_class=source_class,
                evidence_level=(
                    EvidenceLevel.DATA_PROVENANCE
                    if source_class == SourceClass.CANONICAL_RECORD
                    else plan.evidence.level
                ),
                provider_id=provider_id,
                operation="query_knowledge_scope",
                status=str(contract.get("status") or "UNKNOWN"),
                evidence_ids=list(dict.fromkeys(evidence_ids)),
                provenance={"retrieval_id": contract.get("retrieval_id")},
                adopted=True,
            )
        )
        existing.add(source_use_id)

    authority_decision = contract.get("authority_decision")
    authority_decision = authority_decision if isinstance(authority_decision, dict) else {}
    if authority_decision.get("authority_kind") == "GLOSSARY" and not grouped:
        source_use_id = f"knowledge:{contract.get('retrieval_id')}:glossary-scope"
        if source_use_id not in existing:
            manifest.source_uses.append(
                SourceUseRecord(
                    source_use_id=source_use_id,
                    source_class=SourceClass.CANONICAL_RECORD,
                    evidence_level=EvidenceLevel.DATA_PROVENANCE,
                    provider_id="glossary-scope",
                    operation="exact_canonical_lookup",
                    status=str(authority_decision.get("outcome") or "UNAVAILABLE"),
                    evidence_ids=[],
                    provenance={
                        "retrieval_id": contract.get("retrieval_id"),
                        "revision_ids": authority_decision.get("revision_ids") or [],
                        "lookup_terms": authority_decision.get("lookup_terms") or [],
                    },
                    adopted=True,
                )
            )

    evidence_ids = [
        str(row.get("evidence_id"))
        for row in contract.get("evidence") or []
        if isinstance(row, dict) and row.get("evidence_id")
    ]
    raw_outcome = str(authority_decision.get("outcome") or "").upper()
    try:
        outcome = AuthorityOutcome(raw_outcome) if raw_outcome else None
    except ValueError:
        outcome = None
    if outcome is None:
        outcome = AuthorityOutcome.HIT if evidence_ids else AuthorityOutcome.MISS
        if str(contract.get("status") or "").upper() in {"FAILED", "UNAVAILABLE"}:
            outcome = AuthorityOutcome.UNAVAILABLE
    claim_id = "claim:literature" if plan.task.primary_intent.value == "HYBRID_VERIFICATION" else "claim:primary"
    manifest.authority_outcomes = [
        *[item for item in manifest.authority_outcomes if item.claim_id != claim_id],
        AuthorityDecision(
            claim_id=claim_id,
            outcome=outcome,
            evidence_level=plan.evidence.level,
            evidence_ids=list(dict.fromkeys(evidence_ids)),
            reason_code=(str(authority_decision.get("reason_code") or contract.get("error_code") or "") or None),
        ),
    ]


# 事实核验修复：草稿超长直接放弃修复走降级渲染，避免修复调用本身成为成本放大器。
_FACT_REPAIR_MAX_ATTEMPTS = 2
_FACT_REPAIR_DRAFT_LIMIT = 60000
# 修复轮时间预算：request/stream_chunk 超时只封"静默"（多久没有 chunk），不封
# "总时长"（慢而不断流的修复流两者都不触发）。run 看门狗在 idle 180s / total 300s
# 处收尸（run_worker.RUN_STREAM_*_TIMEOUT_SECONDS），修复轮全程对事件流静默，
# 因此每轮外加固墙钟兜底，且 SDK 层零重试防预算被隐藏重试翻倍。预算关系由
# test_source_fact_repair.py 的不变量测试锁死：
# _FACT_REPAIR_MAX_ATTEMPTS * _FACT_REPAIR_ROUND_WALL_BUDGET_SECONDS < idle < total。
_FACT_REPAIR_REQUEST_TIMEOUT_SECONDS = 35.0
_FACT_REPAIR_STREAM_CHUNK_TIMEOUT_SECONDS = 30.0
_FACT_REPAIR_ROUND_WALL_BUDGET_SECONDS = 40.0
#: run 级总时限参考值（权威在 run_worker.RUN_STREAM_TOTAL_TIMEOUT_SECONDS=300；
#: 此处仅用于把修复预算钉进 run 剩余时限——修复抢不过看门狗就没有意义）。
_RUN_STREAM_TOTAL_REFERENCE_SECONDS = 300.0


def _effective_repair_wall_budget(run_deadline_remaining: float | None) -> float:
    """每轮修复墙钟 = min(标称墙钟, run 剩余时限 / 轮数)，下限 5s。"""
    if run_deadline_remaining is None:
        return _FACT_REPAIR_ROUND_WALL_BUDGET_SECONDS
    return max(
        5.0,
        min(_FACT_REPAIR_ROUND_WALL_BUDGET_SECONDS, run_deadline_remaining / _FACT_REPAIR_MAX_ATTEMPTS),
    )


_FACT_REPAIR_PROMPT = """你是 MCP 事实核验修复器。下面这份回答草稿未通过服务器端事实级核验，\
你必须修复它使其通过核验。你只能做三类操作，绝不能引入新事实：
1. 为缺少 [MCP-F:audit:fact] 标记的行补上真实标记（只能使用【可用事实清单】里的标记，\
字符串型事实与数值型事实的标记同样有效）；
2. 删除无法引用任何事实支撑的行或字段（包括无来源的数字、来历不明的叙述）；
3. 把写错的数字改成【可用事实清单】中明确给出的数值。
规则：
- 保持首行「数据模式：SOURCE-ONLY」原样不变；
- 除标题（# 开头）、表头行、分隔线以外的**每一行**（包括概述句、结论句、注释句）\
都必须在同一行末尾附带至少一个真实标记；没有事实可引的句子必须整行删除，不许保留；
- 行内独立数字必须存在于该行所引事实的数值中；坐标差等派生数字若清单中没有对应计算结果，\
删除该句而不是自己计算；
- 不要新增任何清单之外的结论、解释或数字；不要输出任何解释性前言或代码围栏，\
直接输出修复后的完整回答。"""


async def _repair_source_fact_grounding(
    draft_text: str,
    validation: dict[str, Any],
    source_uses: list[Any],
    *,
    repair_model_spec: str | None = None,
    wall_budget: float | None = None,
) -> str | None:
    """Bounded repair round: add markers / drop unsupported lines, never new facts.

    修复模型只拿到草稿、核验失败明细与事实清单（含数值），没有任何工具通道；
    修复结果仍要走同一终态门禁，因此这一步不可能放宽任何核验语义。
    ``wall_budget`` 由调用方按 run 剩余时限收窄（修复抢不过看门狗就没有意义）。
    """
    draft = str(draft_text or "")
    if not draft.strip() or len(draft) > _FACT_REPAIR_DRAFT_LIMIT:
        return None
    catalog = fact_catalog_summary(source_uses)
    if not catalog:
        return None
    grounding = validation.get("fact_grounding") or {}
    lines = draft.splitlines()
    ungrounded: list[str] = []
    for number in grounding.get("ungrounded_lines") or []:
        if isinstance(number, int) and 0 < number <= len(lines):
            ungrounded.append(lines[number - 1])
    unsupported = [
        {"line": item.get("line"), "value": item.get("value")}
        for item in grounding.get("unsupported_numbers") or []
        if isinstance(item, dict)
    ]
    if not ungrounded and not unsupported and not (grounding.get("invalid_markers") or []):
        return None
    payload = {
        "核验失败明细": {
            "缺少标记的行": ungrounded[:20],
            "无支撑数字": unsupported[:20],
            "无效标记": grounding.get("invalid_markers") or [],
        },
        "可用事实清单": catalog[:120],
    }
    try:
        from langchain.messages import HumanMessage, SystemMessage
        from yuxi.agents.models import load_chat_model

        repair_kwargs = {
            "temperature": 0,
            "request_timeout": _FACT_REPAIR_REQUEST_TIMEOUT_SECONDS,
            "stream_chunk_timeout": _FACT_REPAIR_STREAM_CHUNK_TIMEOUT_SECONDS,
            "max_retries": 0,
        }
        model = None
        if isinstance(repair_model_spec, str) and repair_model_spec.strip():
            try:
                model = load_chat_model(repair_model_spec.strip(), **repair_kwargs)
            except Exception:
                model = None
        if model is None:
            model = load_chat_model(None, **repair_kwargs)
        messages = [
            SystemMessage(content=_FACT_REPAIR_PROMPT),
            HumanMessage(
                content=f"【回答草稿】\n{draft}\n\n【核验输入(JSON)】\n{json.dumps(payload, ensure_ascii=False)}"
            ),
        ]
        # 走流式聚合：部分网关/供应商只稳定放行 SSE 请求，非流式 invoke 会连接失败。
        collected: list[str] = []

        async def _collect_stream() -> None:
            async for chunk in model.astream(messages):
                piece = getattr(chunk, "content", "")
                if isinstance(piece, str):
                    collected.append(piece)

        # 墙钟兜底：chunk 持续到达但整体拖长（慢流）时，request/stream_chunk 超时
        # 都不会触发，只有这里能保证修复轮有界、不给 run 看门狗留静默窗口。
        try:
            await asyncio.wait_for(
                _collect_stream(),
                timeout=wall_budget or _FACT_REPAIR_ROUND_WALL_BUDGET_SECONDS,
            )
        except TimeoutError:
            logger.warning(
                "MCP fact grounding repair round exceeded "
                f"{_FACT_REPAIR_ROUND_WALL_BUDGET_SECONDS:.0f}s wall budget; round abandoned"
            )
            return None
    except Exception as error:  # 模型不可用时修复通道直接失效，回落降级渲染
        logger.warning(f"MCP fact grounding repair skipped: {type(error).__name__}: {error}")
        return None
    repaired = "".join(collected).strip()
    if not repaired.startswith("数据模式"):
        return None
    return repaired


async def _finalize_guarded_source_text(
    draft: str,
    *,
    evidence_level,
    source_uses: list[Any],
    repair_model_spec: str | None = None,
    source_policy: str | None = None,
    requires_mcp: bool = False,
    repair_wall_budget: float | None = None,
) -> tuple[str, dict[str, Any]]:
    """终态门禁 + 数据面投影兜底 + 有界修复 + 确定性降级（v4 分层）。

    拒绝后优先数据面投影（确定性、值视图、构造性 marker）；投影可用时直接
    发布并**跳过修复轮**——修好了也只是叙述+表，修不好白等 2×墙钟（实测
    模型慢时 ~80s）。投影不可用才走修复轮，且每轮预算纳入 run 剩余时限：
    修复抢不过看门狗就没有意义。修复失败绝不发布未核验内容。
    """
    finalize_started_at = time.monotonic()
    repair_elapsed_ms = 0
    guarded, validation = guard_answer_for_evidence_level(
        draft,
        evidence_level=evidence_level,
        source_uses=source_uses,
        source_policy=source_policy,
        requires_mcp=requires_mcp,
    )
    attempts: list[dict[str, Any]] = []

    def _passed(report: dict[str, Any]) -> bool:
        grounding = report.get("fact_grounding") or {}
        return bool(grounding.get("required")) is False or bool(grounding.get("passed"))

    if not _passed(validation):
        projection = project_data_plane(source_uses)
        if projection is not None:
            guarded = (
                "数据模式：SOURCE-ONLY\n\n"
                "（模型叙述未通过逐行核验，已改为直接呈现服务端核验的数据摘要）\n\n" + projection.blocks
            )
            validation = {
                **validation,
                "status": "DEGRADED",
                "degraded_render": True,
                "degraded_via_projection": True,
                "data_plane_projection": {
                    "kind": projection.kind,
                    "audit_id": projection.audit_id,
                    "fact_count": len(projection.used_fact_ids),
                },
            }
            logger.info(
                "Source fact grounding rejected the draft; data-plane projection "
                f"published (kind={projection.kind}, repair rounds skipped)"
            )
        else:
            per_round_budget = _effective_repair_wall_budget(repair_wall_budget)
            for attempt in range(1, _FACT_REPAIR_MAX_ATTEMPTS + 1):
                round_started_at = time.monotonic()
                repaired = await _repair_source_fact_grounding(
                    draft,
                    validation,
                    source_uses,
                    repair_model_spec=repair_model_spec,
                    wall_budget=per_round_budget,
                )
                round_elapsed_ms = int((time.monotonic() - round_started_at) * 1000)
                repair_elapsed_ms += round_elapsed_ms
                attempts.append({"attempt": attempt, "repaired": bool(repaired), "elapsed_ms": round_elapsed_ms})
                if not repaired:
                    # 瞬时失败（连接/超时）也给第二轮一次机会；草稿超限等永久性
                    # 原因会连续返回 None，同样快速放行到降级渲染。
                    continue
                guarded, validation = guard_answer_for_evidence_level(
                    repaired,
                    evidence_level=evidence_level,
                    source_uses=source_uses,
                    source_policy=source_policy,
                    requires_mcp=requires_mcp,
                )
                logger.info(
                    "Source fact grounding repair round finished; "
                    f"passed={_passed(validation)} "
                    f"grounding={json.dumps(validation.get('fact_grounding') or {}, ensure_ascii=False)}"
                )
                if _passed(validation):
                    break
            if not _passed(validation):
                degraded = render_degraded_fact_sheet(source_uses)
                if degraded is not None:
                    guarded = degraded
                    validation = {**validation, "status": "DEGRADED", "degraded_render": True}
                logger.info(
                    "Source fact grounding rejected the model draft; "
                    f"attempts={json.dumps(attempts, ensure_ascii=False)} "
                    f"grounding={json.dumps(validation.get('fact_grounding') or {}, ensure_ascii=False)} "
                    f"draft_head={draft[:4000]!r}"
                )
    if attempts:
        validation = {**validation, "fact_repair_attempts": attempts}
    try:
        from yuxi.trace import emit_trace

        grounding = validation.get("fact_grounding") or {}
        emit_trace(
            category="ANSWER",
            operation="source_guard",
            event_type="answer.source_guard.completed",
            attributes={
                "guard_status": str(validation.get("status") or ""),
                "evidence_level": str(getattr(evidence_level, "value", evidence_level)),
                "fact_required": bool(grounding.get("required")),
                "fact_passed": bool(grounding.get("passed")),
                "marker_count": int(grounding.get("marker_count") or 0),
                "ungrounded_line_count": len(grounding.get("ungrounded_lines") or []),
                "unsupported_number_count": len(grounding.get("unsupported_numbers") or []),
                "invalid_marker_count": len(grounding.get("invalid_markers") or []),
                "degraded_render": bool(validation.get("degraded_render")),
                "repair_attempt_count": len(attempts),
                "elapsed_ms": int((time.monotonic() - finalize_started_at) * 1000),
                "repair_elapsed_ms": repair_elapsed_ms,
            },
            visibility="USER",
        )
    except Exception:  # 轨迹事件绝不影响发布路径
        pass
    return guarded, validation


async def _finalize_mcp_manifest(
    db,
    *,
    run_id: str | None,
    plan: TurnExecutionPlan,
    manifest: RunSourceManifest,
    mention_resolution: dict | None = None,
) -> bool:
    """Return True only when a successful, capability-matched MCP call exists.

    mention.v2 逐服务器后验与 ``requires_mcp`` 解耦：AUTO 策略下用户 @ 的
    MCP 服务器同样要核对"实际成功调用过"，等价能力的服务器不能替代指定项。
    """
    pinned_mcps = _pinned_mention_items(mention_resolution, "mcp")
    if not run_id:
        return True
    audits = list(
        (await db.execute(select(MCPCallAudit).where(MCPCallAudit.run_id == str(run_id)).order_by(MCPCallAudit.id)))
        .scalars()
        .all()
    )
    required = set(plan.required_capabilities)
    matched = []
    for audit in audits:
        if str(audit.status).lower() != "success":
            continue
        if str((audit.provenance or {}).get("provider_status") or "").upper() in _MCP_ADOPTION_NEGATIVE_STATUSES:
            continue
        profile = profile_for_server_tool(str(audit.server_slug or ""), str(audit.capability_name or ""))
        if (Capability.GENERIC_MCP in required and (profile is None or profile.answer_eligible)) or (
            profile and profile.answer_eligible and not required.isdisjoint(profile.capabilities)
        ):
            matched.append(audit)
    manifest.mcp_call_count = len(audits)
    manifest.successful_mcp_call_count = len(matched)
    manifest.mcp_servers = list(dict.fromkeys(str(audit.server_slug) for audit in matched))
    _append_mcp_source_uses(
        manifest,
        audits=audits,
        matched_ids={int(audit.id) for audit in matched},
    )
    if matched:
        matched_planes = [
            "BIBLIOGRAPHY"
            if (profile := profile_for_server_tool(str(audit.server_slug or ""), str(audit.capability_name or "")))
            and profile.source_class == "BIBLIOGRAPHY"
            else "MCP_DATA"
            for audit in matched
        ]
        manifest.used_planes = list(dict.fromkeys([*manifest.used_planes, *matched_planes]))
    if plan.requires_mcp:
        decision_claim_id = (
            "claim:database" if plan.task.primary_intent.value == "HYBRID_VERIFICATION" else "claim:primary"
        )
        decision_level = (
            EvidenceLevel.BIBLIOGRAPHIC
            if plan.evidence.level == EvidenceLevel.BIBLIOGRAPHIC
            else EvidenceLevel.DATA_PROVENANCE
        )
        # 点名服务器后验（P1-A）：required_server 存在时，capability 命中还必须
        # 来自该服务器的成功调用；等价能力服务器（如 ricekb 之于 bio-mcp）
        # 不得静默顶替点名项。
        required_server = str(getattr(plan, "required_server", None) or "")
        server_satisfied = not required_server or any(str(audit.server_slug) == required_server for audit in matched)
        decision_satisfied = bool(matched) and server_satisfied
        manifest.authority_outcomes = [
            *[item for item in manifest.authority_outcomes if item.claim_id != decision_claim_id],
            AuthorityDecision(
                claim_id=decision_claim_id,
                outcome=AuthorityOutcome.HIT if decision_satisfied else AuthorityOutcome.UNAVAILABLE,
                evidence_level=decision_level,
                evidence_ids=[f"mcp:{audit.id}" for audit in matched],
                reason_code=(
                    None
                    if decision_satisfied
                    else "MCP_SERVER_NOT_INVOKED"
                    if matched
                    else "MCP_CAPABILITY_NOT_FULFILLED"
                ),
            ),
        ]
    else:
        server_satisfied = True
    if pinned_mcps:
        successful_servers = {str(audit.server_slug) for audit in audits if str(audit.status).lower() == "success"}
        for slug, strength in pinned_mcps:
            _record_mention_fulfillment(
                manifest,
                mention_type="mcp",
                resource_id=slug,
                strength=strength,
                fulfilled=slug in successful_servers,
                reason_code=None if slug in successful_servers else "MENTION_MCP_NOT_INVOKED",
            )
    if matched and server_satisfied:
        return True
    if plan.requires_mcp:
        manifest.status = "SOURCE_UNAVAILABLE"
        manifest.error_code = "MCP_SERVER_NOT_INVOKED" if matched and not server_satisfied else "SOURCE_UNAVAILABLE"
        return False
    return True


async def _finalize_mention_subagents(
    db,
    *,
    run_id: str | None,
    manifest: RunSourceManifest,
    mention_resolution: dict | None,
) -> None:
    """子智能体后验：@subagent 指定项必须在本轮产生对应子运行（created_by_run_id 血缘）。"""
    pinned = _pinned_mention_items(mention_resolution, "subagent")
    if not run_id:
        return
    child_runs = list(
        (await db.execute(select(AgentRun).where(AgentRun.created_by_run_id == str(run_id)))).scalars().all()
    )
    child_slugs = {str(child.agent_slug) for child in child_runs}
    existing_source_uses = {item.source_use_id for item in manifest.source_uses}
    for child in child_runs:
        payload = child.input_payload if isinstance(child.input_payload, dict) else {}
        child_manifest = payload.get("run_source_manifest")
        if not isinstance(child_manifest, dict):
            continue
        for raw_source_use in child_manifest.get("source_uses") or []:
            if not isinstance(raw_source_use, dict):
                continue
            child_source_use_id = str(raw_source_use.get("source_use_id") or "").strip()
            source_use_id = f"subagent:{child.id}:{child_source_use_id}"
            if not child_source_use_id or source_use_id in existing_source_uses:
                continue
            try:
                source_use = SourceUseRecord.model_validate(
                    {
                        **raw_source_use,
                        "source_use_id": source_use_id,
                        "provenance": {
                            **dict(raw_source_use.get("provenance") or {}),
                            "child_run_id": str(child.id),
                            "child_agent_slug": str(child.agent_slug),
                        },
                        "adopted": bool(raw_source_use.get("adopted")) and str(child.status).lower() == "completed",
                    }
                )
            except ValueError:
                continue
            manifest.source_uses.append(source_use)
            existing_source_uses.add(source_use_id)
        for raw_outcome in child_manifest.get("authority_outcomes") or []:
            if not isinstance(raw_outcome, dict):
                continue
            try:
                manifest.authority_outcomes.append(
                    AuthorityDecision.model_validate(
                        {
                            **raw_outcome,
                            "claim_id": f"subagent:{child.id}:{raw_outcome.get('claim_id') or 'claim'}",
                        }
                    )
                )
            except ValueError:
                continue
    for slug, strength in pinned:
        _record_mention_fulfillment(
            manifest,
            mention_type="subagent",
            resource_id=slug,
            strength=strength,
            fulfilled=slug in child_slugs,
            reason_code=None if slug in child_slugs else "MENTION_SUBAGENT_NOT_DELEGATED",
        )


def _settle_source_manifest_status(
    plan: TurnExecutionPlan, manifest: RunSourceManifest, mcp_source_valid: bool
) -> bool:
    """``requires_mcp`` 成功路径收敛 manifest 状态。

    返回 True 表示来源门禁失败、调用方应替换为计划失败答复。注意（P0-2）：
    mention 后验写入的 ``DEGRADED`` 不能被能力级 ``COMPLETED`` 覆写——
    「等价能力的其他服务器成功过」不等于「用户指定的服务器被调用过」。
    """
    answer_mode = str(getattr(getattr(plan, "answer", None), "mode", ""))
    if plan.requires_mcp and not mcp_source_valid:
        # 值答案失败轮保留 SOURCE_UNAVAILABLE，让后续五态视图区分
        # UNAVAILABLE / NOT_FOUND；其他回答模式仍走既有计划失败文案。
        return answer_mode != "MCP_VALUE_ONLY"
    if plan.requires_mcp and manifest.status != "DEGRADED":
        manifest.status = "COMPLETED"
    return False


def _finalize_mention_skills(
    manifest: RunSourceManifest,
    *,
    mention_resolution: dict | None,
    readable_skills: list[str] | None,
) -> None:
    """技能兑现回写：预激活是确定性后端行为——slug 仍在可读闭包内即 FULFILLED。

    readable 集不可得时按 Run 创建时的鉴权结论记 FULFILLED（创建时已验证
    available ∩ Agent 配置）；运行中被撤权 → UNFULFILLED（SKILL_REVOKED_MIDRUN），
    强度语义与其他执行者一致。
    """
    pinned = _pinned_mention_items(mention_resolution, "skill")
    if not pinned:
        return
    readable = {str(slug) for slug in (readable_skills or [])}
    for slug, strength in pinned:
        if not readable or slug in readable:
            _record_mention_fulfillment(
                manifest,
                mention_type="skill",
                resource_id=slug,
                strength=strength,
                fulfilled=True,
                reason_code="SKILL_PREACTIVATED",
            )
        else:
            _record_mention_fulfillment(
                manifest,
                mention_type="skill",
                resource_id=slug,
                strength=strength,
                fulfilled=False,
                reason_code="SKILL_REVOKED_MIDRUN",
            )


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
    knowledge_contract: dict[str, Any] | None = None,
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
    content = normalize_markdown_tables(sanitize_visible_text(content))
    extra_metadata = redact_reasoning_metadata(msg_dict)
    additional_kwargs = msg_dict.get("additional_kwargs")
    already_guarded = bool(isinstance(additional_kwargs, dict) and additional_kwargs.get("locator_validation"))
    if knowledge_contract and knowledge_contract.get("status") != "SKIPPED" and not already_guarded:
        content, citation_validation = apply_citation_channel(
            content,
            knowledge_contract.get("citations") or [],
            locator=knowledge_contract.get("locator_resolution"),
            partition_intent=(knowledge_contract.get("locator_intent") or {}).get("partition_intent"),
            authority_policy=knowledge_contract.get("answer_policy"),
        )
        if citation_validation.get("changed"):
            extra_metadata["locator_validation"] = citation_validation
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


def _emergency_source_text(source_manifest) -> tuple[str, dict[str, Any]] | None:
    """中止/异常路径的应急终稿（零幻觉红线修复，v4）。

    golden 实测（run b005413c）：修复轮 2×墙钟触发的流中止发生在 finalize 完成
    之前，清理路径把**门禁前的流式草稿**当终稿落库——未核验文本上屏，门禁被
    墙钟超时绕过。修复：protected 轮的中止保存改用确定性内容——优先数据面
    投影，其次降级表；并同步给出 source_output_guard 元数据保持审计链完整。
    """
    if source_manifest is None:
        return None
    source_uses = list(getattr(source_manifest, "source_uses", []) or [])
    if not any(
        (item.get("adopted") if isinstance(item, dict) else getattr(item, "adopted", False)) for item in source_uses
    ):
        return None
    projection = project_data_plane(source_uses)
    if projection is not None:
        text = (
            "数据模式：SOURCE-ONLY\n\n"
            "（模型叙述未通过逐行核验，已改为直接呈现服务端核验的数据摘要）\n\n" + projection.blocks
        )
        metadata = {
            "schema_version": "answer-evidence-output-guard.v2",
            "status": "DEGRADED",
            "degraded_render": True,
            "degraded_via_projection": True,
            "data_plane_projection": {
                "kind": projection.kind,
                "audit_id": projection.audit_id,
                "fact_count": len(projection.used_fact_ids),
            },
        }
        return text, metadata
    degraded = render_degraded_fact_sheet(source_uses)
    if degraded is None:
        return None
    return degraded, {
        "schema_version": "answer-evidence-output-guard.v2",
        "status": "DEGRADED",
        "degraded_render": True,
    }


async def save_partial_message(
    conv_repo: ConversationRepository,
    thread_id: str,
    full_msg=None,
    error_message: str | None = None,
    error_type: str = "interrupted",
    trace_info: dict[str, Any] | None = None,
    run_id: str | None = None,
    request_id: str | None = None,
    knowledge_contract: dict[str, Any] | None = None,
    source_guard_metadata: dict[str, Any] | None = None,
):
    try:
        extra_metadata = {
            "error_type": error_type,
            "is_error": True,
            "error_message": error_message or f"发生错误: {error_type}",
        }
        if source_guard_metadata is not None:
            extra_metadata["source_output_guard"] = source_guard_metadata
        if full_msg:
            msg_dict = full_msg.model_dump() if hasattr(full_msg, "model_dump") else {}
            content = full_msg.content if hasattr(full_msg, "content") else str(full_msg)
            content = normalize_markdown_tables(
                sanitize_visible_text(content if isinstance(content, str) else str(content))
            )
            if (
                knowledge_contract
                and knowledge_contract.get("status") != "SKIPPED"
                # F5 卫生：citation 通道只管模型散文的文档主张。错误保存
                # （unexpected_error/model_connection_error 等）的正文是错误
                # 文案不是模型散文，不得贴"未定位到依据"加注（golden 实测：
                # 136 字符错误消息曾被错贴 citation 加注）。
                and error_type == "interrupted"
            ):
                content, citation_validation = apply_citation_channel(
                    content,
                    knowledge_contract.get("citations") or [],
                    locator=knowledge_contract.get("locator_resolution"),
                    partition_intent=(knowledge_contract.get("locator_intent") or {}).get("partition_intent"),
                    authority_policy=knowledge_contract.get("answer_policy"),
                )
                if citation_validation.get("changed"):
                    extra_metadata["locator_validation"] = citation_validation
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
        if isinstance(total_tokens, int | float) and not isinstance(total_tokens, bool):
            return int(total_tokens)
        input_tokens = model_usage.get("input_tokens", model_usage.get("prompt_tokens", 0))
        output_tokens = model_usage.get("output_tokens", model_usage.get("completion_tokens", 0))
        if all(
            isinstance(value, int | float) and not isinstance(value, bool) for value in (input_tokens, output_tokens)
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
    knowledge_contract: dict[str, Any] | None = None,
    final_content_override: str | None = None,
    run_metadata: dict[str, Any] | None = None,
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
                knowledge_contract=knowledge_contract,
            )
        elif msg_type == "tool":
            await _save_tool_message(conv_repo, msg_dict)

    if last_ai_message and final_content_override is not None:
        last_ai_message.content = normalize_markdown_tables(sanitize_visible_text(final_content_override))
    if last_ai_message and run_metadata:
        last_ai_message.extra_metadata = {
            **dict(last_ai_message.extra_metadata or {}),
            **dict(run_metadata),
        }
        await conv_repo.db.flush()
    if last_ai_message and knowledge_contract and knowledge_contract.get("status") != "SKIPPED":
        # 与确定性定位路径（citation_binding）同形落库：历史恢复可读回 Binding 与
        # figure_projection，刷新页面后定位芯片/图卡不丢失
        locator = knowledge_contract.get("locator_resolution")
        if isinstance(locator, dict) and locator:
            persisted = {
                **dict(last_ai_message.extra_metadata or {}),
                "knowledge_retrieval_id": knowledge_contract.get("retrieval_id"),
                "citation_binding": locator,
            }
            if str(locator.get("status") or "") == "VERIFIED":
                # 与流末 citation_ready 事件同一载荷（历史恢复只读它，不读审计用的 binding）
                persisted["citation_ready"] = _citation_ready_payload(locator)
            last_ai_message.extra_metadata = persisted
            await conv_repo.db.flush()

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
    raw_human_message = input_message.require_langchain_message()
    message_type = input_message.message_type
    # mention.v2（P0）：送给模型的文本剥离控制 token；历史/前端展示仍用原始问题。
    model_query = str(_frozen_mention_resolution(meta).get("clean_question") or "").strip() or query
    human_message = raw_human_message if model_query == query else _model_query_message(raw_human_message, model_query)

    if conf.enable_content_guard and await content_guard.check(query):
        yield make_chunk(
            status="error", error_type="content_guard_blocked", error_message="输入内容包含敏感词", meta=meta
        )
        return

    from yuxi.agents.mcp.artifact_materializer import begin_artifact_accumulation
    from yuxi.agents.mcp.execution import McpExecutionContext, set_mcp_execution_context
    from yuxi.services.principal import resolve_tenant_id

    mcp_context_token = set_mcp_execution_context(
        McpExecutionContext(
            tenant_id=int(meta.get("tenant_id") or await resolve_tenant_id(db, uid)),
            uid=uid,
            thread_id=thread_id,
            run_id=meta.get("run_id"),
            agent_slug=agent_slug,
        )
    )
    artifact_accumulation_token = begin_artifact_accumulation()

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
        from yuxi.agents.mcp.artifact_materializer import end_artifact_accumulation
        from yuxi.agents.mcp.execution import reset_mcp_execution_context

        reset_mcp_execution_context(mcp_context_token)
        end_artifact_accumulation(artifact_accumulation_token)
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
    # mention.v2：把冻结的提及解析结论绑定到 runtime，供 MODEL_DECIDES 路径的
    # 统一检索工具同样施加文献硬约束（@doc 不再只约束定位链）。
    input_context["_mention_resolution"] = _frozen_mention_resolution(meta)
    from yuxi.agents.mcp.service import list_builtin_mcp_slugs

    turn_plan = plan_turn(
        model_query,
        has_knowledge_scope=bool(knowledge_scope_snapshot.get("effective_kb_ids") or []),
        configured_mcps=list(input_context.get("mcps") or []),
        knowledge_strategy=str(knowledge_scope_snapshot.get("knowledge_strategy") or "MODEL_DECIDES"),
        has_image=bool(image_content),
        known_mcps=list_builtin_mcp_slugs(),
        mentioned_mcp_slugs=list(input_context["_mention_resolution"].get("mcp_slugs") or []),
    )
    source_manifest = _initial_source_manifest(turn_plan)
    input_context["_turn_execution_plan"] = turn_plan.public_dict()
    input_context["_run_source_manifest"] = source_manifest.model_dump(mode="json")
    meta["turn_plan_id"] = turn_plan.plan_id
    meta["source_policy"] = turn_plan.source.policy.value
    context = _build_agent_context(agent, input_context)
    _bind_knowledge_scope_to_context(context, knowledge_scope_snapshot)
    _apply_skill_plan_dispatch(context, turn_plan, source_manifest)
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
    knowledge_contract: dict[str, Any] | None = None
    citation_sensitive_output = False
    protected_output = turn_plan.buffers_output
    buffered_root_message_id: str | None = None
    buffered_root_metadata: dict[str, Any] | None = None
    source_output_validation: dict[str, Any] | None = None

    try:
        credential_context_token = await _activate_user_credential(db=db, uid=uid, meta=meta)
        conv_repo = ConversationRepository(db)
        await _ensure_thread_bound_agent(
            conv_repo=conv_repo,
            thread_id=thread_id,
            uid=uid,
            agent_item=agent_item,
        )
        await _persist_turn_runtime(
            db,
            meta.get("run_id"),
            plan=turn_plan,
            manifest=source_manifest,
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
                "turn_execution_plan": turn_plan.public_dict(),
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
                        "raw_message": raw_human_message.model_dump(),
                        "request_id": meta.get("request_id"),
                        "attachments": request_attachments,
                        "turn_execution_plan": turn_plan.public_dict(),
                    },
                )
            except Exception as e:
                logger.error(f"Error saving user message: {e}")

        if not turn_plan.satisfiable:
            failure_answer = _plan_failure_answer(turn_plan)
            source_manifest.status = "PLAN_REJECTED"
            source_manifest.error_code = turn_plan.error_code
            await _persist_turn_runtime(
                db,
                meta.get("run_id"),
                plan=turn_plan,
                manifest=source_manifest,
            )
            message_id = f"msg_{uuid.uuid4().hex}"
            ai_message = await conv_repo.add_message_by_thread_id(
                thread_id=thread_id,
                role="assistant",
                content=failure_answer,
                message_type="text",
                extra_metadata={
                    "id": message_id,
                    "turn_execution_plan": turn_plan.public_dict(),
                    "run_source_manifest": source_manifest.model_dump(mode="json"),
                    **get_trace_info(langfuse_run),
                },
                run_id=meta.get("run_id"),
                request_id=meta.get("request_id"),
            )
            if ai_message is not None and meta.get("run_id"):
                await AgentRunRepository(db).set_output_message(str(meta["run_id"]), ai_message.id)
            await db.commit()
            yield make_chunk(
                content=failure_answer,
                stream_event={
                    "type": "message_delta",
                    "message_id": message_id,
                    "content": failure_answer,
                    "thread_id": thread_id,
                    "namespace": [],
                },
                metadata={"turn_execution_plan": turn_plan.public_dict()},
                status="loading",
                thread_id=thread_id,
            )
            meta["time_cost"] = asyncio.get_event_loop().time() - start_time
            yield make_chunk(status="finished", meta=meta)
            return

        if turn_plan.task.primary_intent == TaskIntent.SEQUENCE_EXPORT:
            # Sequence export is a deterministic service path: the model never
            # decides whether to call RiceKB and never copies sequence bytes.
            from yuxi.knowledge.rendering.source_answer_renderer import render_source_answer
            from yuxi.services.rice_sequence_service import execute_rice_sequence_query

            sequence_result = await execute_rice_sequence_query(model_query)
            for index, call in enumerate(sequence_result.calls, start=1):
                call_id = f"seq_{index}_{uuid.uuid4().hex[:8]}"
                yield make_chunk(
                    status="loading",
                    stream_event={
                        "type": "tool_call",
                        "method": "tools",
                        "namespace": [],
                        "data": {
                            "event": "tool-call",
                            "tool_call_id": call_id,
                            "tool_name": call["tool"],
                            "args": call["arguments"],
                        },
                    },
                    meta=meta,
                )
                yield make_chunk(
                    status="loading",
                    stream_event={
                        "type": "tool_result",
                        "method": "tools",
                        "namespace": [],
                        "data": {
                            "event": "tool-finished",
                            "tool_call_id": call_id,
                            "tool_name": call["tool"],
                            "output": {"is_error": call["is_error"]},
                        },
                    },
                    meta=meta,
                )

            mcp_source_valid = await _finalize_mcp_manifest(
                db,
                run_id=meta.get("run_id"),
                plan=turn_plan,
                manifest=source_manifest,
                mention_resolution=_frozen_mention_resolution(meta),
            )
            _settle_source_manifest_status(turn_plan, source_manifest, mcp_source_valid)
            projection = project_data_plane(source_manifest.source_uses) if sequence_result.succeeded else None
            internal_content = projection.blocks if projection is not None else None
            if internal_content is not None:
                guarded_content, validation = guard_answer_for_evidence_level(
                    internal_content,
                    evidence_level=turn_plan.evidence.level,
                    source_uses=source_manifest.source_uses,
                    source_policy=turn_plan.source.policy.value,
                    requires_mcp=True,
                )
                source_output_validation = {
                    **validation,
                    "presentation_mode": "MCP_VALUE_ONLY",
                    "model_facts_published": False,
                    "data_plane_projection": {
                        "kind": projection.kind,
                        "audit_id": projection.audit_id,
                        "fact_count": len(projection.used_fact_ids),
                    },
                }
                if validation.get("status") == "PASSED":
                    public_content = render_source_answer(internal_content)
                else:
                    # The deterministic projection should satisfy this guard.
                    # If contracts drift, fail closed instead of force-marking
                    # a rejected value sheet as publishable.
                    internal_content = guarded_content
                    public_content = render_source_answer(guarded_content)
            else:
                provider_answer = render_provider_status_answer(source_manifest.source_uses)
                public_content = provider_answer or sequence_result.error_message or _plan_failure_answer(turn_plan)
                internal_content = public_content
                source_output_validation = {
                    "schema_version": "answer-evidence-output-guard.v2",
                    "status": "DEGRADED",
                    "presentation_mode": "MCP_VALUE_ONLY",
                    "model_facts_published": False,
                    "reason_code": sequence_result.status,
                }
            source_manifest.validation_results.append(source_output_validation)

            await _persist_turn_runtime(
                db,
                meta.get("run_id"),
                plan=turn_plan,
                manifest=source_manifest,
            )
            message_id = f"msg_{uuid.uuid4().hex}"
            ai_message = await conv_repo.add_message_by_thread_id(
                thread_id=thread_id,
                role="assistant",
                content=internal_content,
                message_type="text",
                extra_metadata={
                    "id": message_id,
                    "presentation_mode": "MCP_VALUE_ONLY",
                    "deterministic_sequence": True,
                    "source_output_guard": source_output_validation,
                    "turn_execution_plan": turn_plan.public_dict(),
                    "run_source_manifest": source_manifest.model_dump(mode="json"),
                    **get_trace_info(langfuse_run),
                },
                run_id=meta.get("run_id"),
                request_id=meta.get("request_id"),
            )
            if ai_message is not None and meta.get("run_id"):
                await AgentRunRepository(db).set_output_message(str(meta["run_id"]), ai_message.id)
            await db.commit()
            yield make_chunk(
                content=public_content,
                stream_event={
                    "type": "message_delta",
                    "message_id": message_id,
                    "content": public_content,
                    "thread_id": thread_id,
                    "namespace": [],
                },
                metadata={"deterministic_sequence": True, "presentation_mode": "MCP_VALUE_ONLY"},
                status="loading",
                thread_id=thread_id,
            )
            meta["time_cost"] = asyncio.get_event_loop().time() - start_time
            yield make_chunk(status="finished", meta=meta)
            return

        if turn_plan.requires_document_retrieval or Capability.CANONICAL_LOOKUP in turn_plan.required_capabilities:
            retrieval_id = f"kr_{uuid.uuid4().hex}"
            knowledge_contract = await prepare_knowledge_context(
                db,
                question=query,
                scope_snapshot=knowledge_scope_snapshot,
                run_id=meta.get("run_id"),
                request_id=meta.get("request_id"),
                retrieval_id=retrieval_id,
                image_bytes=_decode_image_bytes(image_content),
                mention_resolution=_frozen_mention_resolution(meta) or None,
            )
            input_context["_knowledge_contract"] = knowledge_contract
            setattr(context, "_knowledge_contract", knowledge_contract)
            knowledge_source_used = knowledge_contract.get("status") != "SKIPPED"
            citation_sensitive_output = knowledge_source_used and turn_plan.evidence.level in {
                EvidenceLevel.CLAIM_EVIDENCE,
                EvidenceLevel.VERBATIM_LOCATOR,
            }
            if knowledge_source_used:
                source_manifest.knowledge_retrieval_count = 1
                _append_knowledge_source_uses(
                    source_manifest,
                    plan=turn_plan,
                    contract=knowledge_contract,
                )
                source_manifest.status = "COMPLETED" if knowledge_contract.get("status") == "COMPLETED" else "DEGRADED"
                source_manifest.error_code = knowledge_contract.get("error_code")
            await _persist_turn_runtime(
                db,
                meta.get("run_id"),
                plan=turn_plan,
                manifest=source_manifest,
            )
        if knowledge_contract and knowledge_contract.get("status") != "SKIPPED":
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

        deterministic_locator_answer = (
            _deterministic_locator_answer(knowledge_contract) if knowledge_contract is not None else None
        )
        if deterministic_locator_answer is not None:
            # Locator answers are emitted once, after deterministic binding. No
            # unverified model token can transiently expose a wrong page.
            if conf.enable_content_guard and await content_guard.check(deterministic_locator_answer):
                yield make_chunk(
                    status="error",
                    error_type="content_guard_blocked",
                    error_message="输出内容包含敏感词",
                    meta=meta,
                )
                return
            message_id = f"msg_{uuid.uuid4().hex}"
            locator = knowledge_contract.get("locator_resolution") or {}
            citation_ready = _citation_ready_payload(locator) if locator.get("status") == "VERIFIED" else None
            ai_message = await conv_repo.add_message_by_thread_id(
                thread_id=thread_id,
                role="assistant",
                content=deterministic_locator_answer,
                message_type="text",
                extra_metadata={
                    "id": message_id,
                    "knowledge_retrieval_id": knowledge_contract.get("retrieval_id"),
                    "citation_binding": locator,
                    # 实际发布的 citation_ready 载荷（含/不含 figures）：历史恢复只读它——
                    # 刷新后还原的必须是当时发布的内容（暗发布期 figure_projection 恒存于
                    # citation_binding 供审计，但不得从那里漏出图卡）
                    **({"citation_ready": citation_ready} if citation_ready else {}),
                    "turn_execution_plan": turn_plan.public_dict(),
                    "run_source_manifest": source_manifest.model_dump(mode="json"),
                    **get_trace_info(langfuse_run),
                },
                run_id=meta.get("run_id"),
                request_id=meta.get("request_id"),
            )
            if ai_message is not None and meta.get("run_id"):
                await AgentRunRepository(db).set_output_message(str(meta["run_id"]), ai_message.id)
                await db.commit()
            yield make_chunk(
                content=deterministic_locator_answer,
                stream_event={
                    "type": "message_delta",
                    "message_id": message_id,
                    "content": deterministic_locator_answer,
                    "thread_id": thread_id,
                    "namespace": [],
                },
                metadata={"deterministic_locator": True},
                status="loading",
                thread_id=thread_id,
            )
            if citation_ready is not None:
                yield make_chunk(status="citation_ready", meta=meta, **citation_ready)
            candidate_documents = _candidate_documents(locator, knowledge_contract.get("answer_policy"))
            if len(candidate_documents) > 1:
                # 跨文献歧义：把候选文献清单交给前端渲染成可点选的 @doc 提及（只含文档身份）
                yield make_chunk(status="locator_candidates", candidates=candidate_documents, meta=meta)
            meta["time_cost"] = asyncio.get_event_loop().time() - start_time
            yield make_chunk(status="finished", meta=meta)
            return

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
                    buffered_root_message_id = str(stream_event.get("message_id") or buffered_root_message_id or "")
                    buffered_root_metadata = metadata
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
                            knowledge_contract=knowledge_contract,
                        )
                        meta["time_cost"] = asyncio.get_event_loop().time() - start_time
                        yield make_chunk(status="interrupted", message="检测到敏感内容，已中断输出", meta=meta)
                        return

                if protected_output and not is_subagent_chunk and stream_event.get("type") == "message_delta":
                    # Source-constrained/citation-bearing text is released only
                    # after the complete answer has passed server-side gates.
                    continue

                yield make_chunk(
                    content=content,
                    stream_event=stream_event,
                    metadata=metadata,
                    status="loading",
                    thread_id=chunk_thread_id,
                )

        frozen_mention_resolution = _frozen_mention_resolution(meta)
        mcp_source_valid = await _finalize_mcp_manifest(
            db,
            run_id=meta.get("run_id"),
            plan=turn_plan,
            manifest=source_manifest,
            mention_resolution=frozen_mention_resolution,
        )
        await _finalize_mention_subagents(
            db,
            run_id=meta.get("run_id"),
            manifest=source_manifest,
            mention_resolution=frozen_mention_resolution,
        )
        _finalize_mention_skills(
            source_manifest,
            mention_resolution=frozen_mention_resolution,
            readable_skills=getattr(context, "_readable_skills", None),
        )
        if _settle_source_manifest_status(turn_plan, source_manifest, mcp_source_valid):
            accumulated_content = [_plan_failure_answer(turn_plan)]
            plan_failure_answer_active = True
        else:
            plan_failure_answer_active = False
        glossary_contract_ready = (
            turn_plan.task.primary_intent.value == "GLOSSARY_LOOKUP" and knowledge_contract is not None
        )
        if plan_failure_answer_active:
            # 门禁分派：计划失败答复是系统生成的确定性文案，不是模型散文——
            # 不再过 source 门（否则同轮若有其他成功调用的事实清单，拒绝文案
            # 本身会被逐行核验拒绝/降级），也不进 citation 门。
            source_output_validation = {
                "schema_version": "answer-evidence-output-guard.v2",
                "status": "SKIPPED",
                "skip_reason": "PLAN_FAILURE_ANSWER",
                "evidence_level": getattr(turn_plan.evidence.level, "value", turn_plan.evidence.level),
            }
            source_manifest.validation_results.append(source_output_validation)
            buffered_root_metadata = {
                **dict(buffered_root_metadata or {}),
                "source_output_guard": source_output_validation,
            }
        elif accumulated_content or glossary_contract_ready or turn_plan.answer.mode == "MCP_VALUE_ONLY":
            if glossary_contract_ready:
                guarded_source_text, source_output_validation = guard_glossary_answer(
                    "".join(accumulated_content), contract=knowledge_contract
                )
            # MCP 值答案：模型只负责选工具，不参与最终事实措辞。终态正文必须由
            # 服务端直接从已采纳事实账本投影；无可投影事实时按五态语义给确定性
            # 终态文案（UNAVAILABLE 绝不伪装成未找到），两者都无才用兜底句。
            elif turn_plan.answer.mode == "MCP_VALUE_ONLY":
                projection = project_data_plane(source_manifest.source_uses)
                deterministic_text = (
                    projection.blocks
                    if projection is not None
                    else render_degraded_fact_sheet(source_manifest.source_uses)
                )
                if deterministic_text is None:
                    deterministic_text = render_provider_status_answer(source_manifest.source_uses)
                guarded_source_text = deterministic_text or "未获取到可发布的数据值。"
                _, deterministic_validation = guard_answer_for_evidence_level(
                    guarded_source_text,
                    evidence_level=turn_plan.evidence.level,
                    source_uses=source_manifest.source_uses,
                    source_policy=turn_plan.source.policy.value,
                    requires_mcp=turn_plan.requires_mcp,
                )
                source_output_validation = {
                    **deterministic_validation,
                    "status": "PASSED" if deterministic_text else "DEGRADED",
                    "presentation_mode": "MCP_VALUE_ONLY",
                    "model_facts_published": False,
                }
                if projection is not None:
                    source_output_validation["data_plane_projection"] = {
                        "kind": projection.kind,
                        "audit_id": projection.audit_id,
                        "fact_count": len(projection.used_fact_ids),
                    }
            else:
                guarded_source_text, source_output_validation = await _finalize_guarded_source_text(
                    "".join(accumulated_content),
                    evidence_level=turn_plan.evidence.level,
                    source_uses=source_manifest.source_uses,
                    repair_model_spec=meta.get("model_spec"),
                    source_policy=turn_plan.source.policy.value,
                    requires_mcp=turn_plan.requires_mcp,
                    # 修复预算纳入 run 剩余时限（v4）：修复抢不过看门狗就没有意义
                    repair_wall_budget=max(
                        0.0,
                        _RUN_STREAM_TOTAL_REFERENCE_SECONDS - (asyncio.get_event_loop().time() - start_time),
                    ),
                )
            # 数据面投影（呈现层 v3）：模型只写叙述，数据表由服务端从 manifest
            # 确定性构造（构造性 marker，降级表同款零幻觉链路）。组合文本整体
            # 复跑事实门禁，通过才发布；失败回退纯叙述并记日志，绝不带病拼接。
            if (
                turn_plan.answer.mode != "MCP_VALUE_ONLY"
                and str((source_output_validation or {}).get("status") or "") == "PASSED"
            ):
                projection = project_data_plane(source_manifest.source_uses)
                if projection is not None:
                    combined = guarded_source_text.rstrip() + "\n\n" + projection.blocks
                    _, combined_validation = guard_answer_for_evidence_level(
                        combined,
                        evidence_level=turn_plan.evidence.level,
                        source_uses=source_manifest.source_uses,
                        source_policy=turn_plan.source.policy.value,
                        requires_mcp=turn_plan.requires_mcp,
                    )
                    combined_grounding = combined_validation.get("fact_grounding") or {}
                    if not combined_grounding.get("required") or combined_grounding.get("passed"):
                        guarded_source_text = combined
                        source_output_validation = {
                            **combined_validation,
                            "data_plane_projection": {
                                "kind": projection.kind,
                                "audit_id": projection.audit_id,
                                "fact_count": len(projection.used_fact_ids),
                            },
                        }
                    else:
                        # 投影回退观测（v4）：validation 记录 + trace 事件——
                        # 否则"投影回退率"这个关键指标永远是盲的。
                        source_output_validation = {
                            **source_output_validation,
                            "data_plane_projection_fallback": True,
                        }
                        logger.warning("Data-plane projection combined text failed grounding; publishing prose only")
                        try:
                            from yuxi.trace import emit_trace

                            emit_trace(
                                category="ANSWER",
                                operation="render",
                                event_type="answer.render.applied",
                                attributes={
                                    "renderer_version": "projection-combined",
                                    "boundary": "publish",
                                    "eligible": True,
                                    "applied": False,
                                    "fallback_reason": "combined_grounding_failed",
                                },
                                visibility="USER",
                            )
                        except Exception:  # noqa: BLE001 —— 轨迹事件绝不影响发布路径
                            pass
            accumulated_content = [guarded_source_text]
            source_manifest.validation_results.append(source_output_validation)
            buffered_root_metadata = {
                **dict(buffered_root_metadata or {}),
                "source_output_guard": source_output_validation,
            }
            # 呈现边界定稿⑤：发布时**不**渲染（DB 存带 marker 原文，读取边界渲染），
            # 只发 trace 事件记录渲染器可用性；轨迹事件绝不影响发布路径。
            try:
                from yuxi.knowledge.rendering.source_answer_renderer import render_report
                from yuxi.trace import emit_trace

                report = render_report(guarded_source_text)
                emit_trace(
                    category="ANSWER",
                    operation="render",
                    event_type="answer.render.applied",
                    attributes={
                        "renderer_version": report["renderer_version"],
                        "boundary": "publish",
                        "eligible": bool(report["eligible"]),
                        "applied": False,
                    },
                    visibility="USER",
                )
            except Exception:  # noqa: BLE001 —— 轨迹事件绝不影响发布路径
                pass
        if turn_plan.requires_mcp or any(getattr(item, "adopted", False) for item in source_manifest.source_uses):
            # V4-5：有 adopted 源即持久化 run runtime——宽松轮（未点名 MCP）的
            # 读取边界 L2 引用增强（折叠附录带标签+值）不再缺失。
            await _persist_turn_runtime(
                db,
                meta.get("run_id"),
                plan=turn_plan,
                manifest=source_manifest,
            )

        source_guard_status = str((source_output_validation or {}).get("status") or "")
        if citation_sensitive_output and knowledge_contract is not None and accumulated_content:
            if source_guard_status in {"REJECTED", "DEGRADED", "SKIPPED"}:
                # 门禁分派：source 门已拒绝/降级（或计划失败答复）时文本为系统生成，
                # citation/locator 门只管模型散文的文档主张——拒绝文案上不贴定位芯片。
                full_msg = _ensure_full_msg(full_msg, accumulated_content)
                buffered_root_metadata = {
                    **dict(buffered_root_metadata or {}),
                    "citation_gate_dispatch": {
                        "applied": False,
                        "skip_reason": f"SOURCE_GUARD_{source_guard_status}",
                    },
                }
            else:
                guarded_content, locator_validation = _guard_knowledge_answer(
                    "".join(accumulated_content), knowledge_contract
                )
                accumulated_content = [guarded_content]
                full_msg = AIMessage(
                    id=buffered_root_message_id or f"msg_{uuid.uuid4().hex}",
                    content=guarded_content,
                    additional_kwargs={"locator_validation": locator_validation},
                )
        else:
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
                knowledge_contract=knowledge_contract,
            )
            meta["time_cost"] = asyncio.get_event_loop().time() - start_time
            yield make_chunk(status="interrupted", message="检测到敏感内容，已中断输出", meta=meta)
            return

        if protected_output and full_msg is not None and str(full_msg.content or ""):
            public_content = str(full_msg.content)
            if turn_plan.answer.mode == "MCP_VALUE_ONLY":
                from yuxi.knowledge.rendering.source_answer_renderer import render_source_answer

                public_content = render_source_answer(public_content)
                buffered_root_metadata = {
                    **dict(buffered_root_metadata or {}),
                    "presentation_mode": "MCP_VALUE_ONLY",
                }
            safe_message_id = str(getattr(full_msg, "id", None) or buffered_root_message_id or uuid.uuid4())
            yield make_chunk(
                content=public_content,
                stream_event={
                    "type": "message_delta",
                    "message_id": safe_message_id,
                    "content": public_content,
                    "thread_id": thread_id,
                    "namespace": [],
                },
                metadata=buffered_root_metadata or {},
                status="loading",
                thread_id=thread_id,
            )

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
            save_kwargs = {
                "agent_instance": agent,
                "thread_id": thread_id,
                "conv_repo": conv_repo,
                "config_dict": langgraph_config,
                "context": context,
                "trace_info": trace_info,
                "run_id": meta.get("run_id"),
                "request_id": meta.get("request_id"),
            }
            if protected_output:
                save_kwargs["final_content_override"] = str(full_msg.content or "") if full_msg else None
                save_kwargs["run_metadata"] = {
                    "turn_execution_plan": turn_plan.public_dict(),
                    "run_source_manifest": source_manifest.model_dump(mode="json"),
                }
                if source_output_validation is not None:
                    save_kwargs["run_metadata"]["source_output_guard"] = source_output_validation
            if citation_sensitive_output:
                save_kwargs["knowledge_contract"] = knowledge_contract
            await save_messages_from_langgraph_state(**save_kwargs)
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

        # 复合意图流的 citation_ready：定位行已验证并随守卫渲染进答案后发出，
        # 前端据此展示结构化引用（不重新在浏览器侧解析页码）。
        if (
            knowledge_contract is not None
            and (knowledge_contract.get("locator_resolution") or {}).get("status") == "VERIFIED"
        ):
            locator_terminal = knowledge_contract.get("locator_resolution") or {}
            yield make_chunk(status="citation_ready", meta=meta, **_citation_ready_payload(locator_terminal))
        if knowledge_contract is not None:
            _terminal_locator = knowledge_contract.get("locator_resolution") or {}
            if _terminal_locator.get("status") == "MULTIPLE_MATCHES":
                candidate_documents = _candidate_documents(_terminal_locator, knowledge_contract.get("answer_policy"))
                if len(candidate_documents) > 1:
                    yield make_chunk(status="locator_candidates", candidates=candidate_documents, meta=meta)

        yield make_chunk(status="finished", meta=meta)

    except (asyncio.CancelledError, ConnectionError) as e:
        abort_status, abort_error_type, abort_message = _stream_abort_details(e)
        logger.warning(f"Chat stream aborted: status={abort_status}, error_type={abort_error_type}, reason={e}")

        async def save_cleanup():
            nonlocal full_msg
            full_msg = _ensure_full_msg(full_msg, accumulated_content)
            # 零幻觉红线修复（v4）：protected 轮的中止保存绝不让门禁前的流式
            # 草稿当终稿落库——改存确定性降级内容（投影优先），并携带门禁元数据。
            guard_metadata = None
            if protected_output:
                emergency = _emergency_source_text(source_manifest)
                if emergency is not None:
                    full_msg = AIMessage(
                        id=f"msg_{uuid.uuid4().hex}",
                        content=emergency[0],
                    )
                    guard_metadata = emergency[1]

            async with pg_manager.get_async_session_context() as new_db:
                new_conv_repo = ConversationRepository(new_db)
                await save_partial_message(
                    new_conv_repo,
                    thread_id,
                    full_msg=full_msg,
                    error_message=abort_message,
                    error_type=abort_error_type,
                    trace_info=trace_info,
                    run_id=meta.get("run_id"),
                    request_id=meta.get("request_id"),
                    knowledge_contract=knowledge_contract,
                    source_guard_metadata=guard_metadata,
                )

        cleanup_task = asyncio.create_task(save_cleanup())
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error(f"Error during cleanup save: {exc}")

        abort_payload = {
            "status": abort_status,
            "message": abort_message,
            "error_type": abort_error_type,
            "meta": meta,
        }
        if abort_status == "error":
            abort_payload["error_message"] = abort_message
        yield make_chunk(**abort_payload)

    except Exception as e:
        logger.exception(f"Error streaming messages: {e}")

        if is_model_connection_error(e):
            # 连接类失败与通用异常分流：前者可重试且文案指向模型服务，
            # 不再让"检索/看门狗"类超时文案掩盖真实故障层。
            error_type = MODEL_CONNECTION_ERROR
            error_msg = MODEL_CONNECTION_ERROR_MESSAGE
        else:
            error_type = "unexpected_error"
            error_msg = f"Error streaming messages: {e}"

        full_msg = _ensure_full_msg(full_msg, accumulated_content)
        # 同一红线修复：通用异常路径的 protected 轮同样改存确定性降级内容。
        guard_metadata = None
        if protected_output:
            emergency = _emergency_source_text(source_manifest)
            if emergency is not None:
                full_msg = AIMessage(
                    id=f"msg_{uuid.uuid4().hex}",
                    content=emergency[0],
                )
                guard_metadata = emergency[1]

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
                knowledge_contract=knowledge_contract,
                source_guard_metadata=guard_metadata,
            )

        yield make_chunk(
            status="error",
            error_type=error_type,
            error_message=error_msg,
            message=error_msg,
            retryable=error_type == MODEL_CONNECTION_ERROR,
            meta=meta,
        )
    finally:
        if credential_context_token is not None:
            from yuxi.agents.models import reset_user_credential_override

            reset_user_credential_override(credential_context_token)
        from yuxi.agents.mcp.artifact_materializer import end_artifact_accumulation
        from yuxi.agents.mcp.execution import reset_mcp_execution_context

        reset_mcp_execution_context(mcp_context_token)
        end_artifact_accumulation(artifact_accumulation_token)
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
    from yuxi.agents.mcp.artifact_materializer import begin_artifact_accumulation
    from yuxi.agents.mcp.execution import McpExecutionContext, set_mcp_execution_context
    from yuxi.services.principal import resolve_tenant_id

    mcp_context_token = set_mcp_execution_context(
        McpExecutionContext(
            tenant_id=int(meta.get("tenant_id") or await resolve_tenant_id(db, uid)),
            uid=uid,
            thread_id=thread_id,
            run_id=meta.get("run_id"),
            agent_slug=meta.get("agent_slug"),
        )
    )
    artifact_accumulation_token = begin_artifact_accumulation()
    try:
        agent_item, agent, agent_config = await _resolve_agent_runtime(
            db=db,
            user=current_user,
            requested_agent_slug=None,
            thread_id=thread_id,
        )
    except ValueError as e:
        # 该分支同样尚未进入下方主 try/finally。
        from yuxi.agents.mcp.artifact_materializer import end_artifact_accumulation
        from yuxi.agents.mcp.execution import reset_mcp_execution_context

        reset_mcp_execution_context(mcp_context_token)
        end_artifact_accumulation(artifact_accumulation_token)
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
        abort_status, abort_error_type, abort_message = _stream_abort_details(e, resume=True)
        logger.warning(f"Resume stream aborted: status={abort_status}, error_type={abort_error_type}, reason={e}")

        async with pg_manager.get_async_session_context() as new_db:
            new_conv_repo = ConversationRepository(new_db)
            await save_partial_message(
                new_conv_repo,
                thread_id,
                error_message=abort_message,
                error_type=abort_error_type,
                trace_info=trace_info,
                run_id=meta.get("run_id"),
                request_id=meta.get("request_id"),
            )

        abort_payload = {
            "status": abort_status,
            "message": abort_message,
            "error_type": abort_error_type,
            "meta": meta,
        }
        if abort_status == "error":
            abort_payload["error_message"] = abort_message
        yield make_resume_chunk(**abort_payload)

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
        from yuxi.agents.mcp.artifact_materializer import end_artifact_accumulation
        from yuxi.agents.mcp.execution import reset_mcp_execution_context

        reset_mcp_execution_context(mcp_context_token)
        end_artifact_accumulation(artifact_accumulation_token)
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
