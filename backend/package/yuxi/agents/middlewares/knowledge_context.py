from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from deepagents.middleware._utils import append_to_system_message
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, ToolMessage

from yuxi.agents.mcp.capability_registry import is_mcp_tool, profile_for_tool
from yuxi.knowledge.planning.turn_execution_plan import SourceClass, SourcePolicy, TurnExecutionPlan
from yuxi.knowledge.rendering.answer_context_builder import build_answer_context
from yuxi.knowledge.rendering.answer_draft import render_answer_draft
from yuxi.knowledge.rendering.citation_channel import (
    apply_citation_channel,
    sanitize_history_text,
)
from yuxi.knowledge.validation.citation_validator import (
    NARRATIVE_CITATION_MARKER,
    sanitize_narrative_citations,
)

_STALE_RUNTIME_STATE = re.compile(
    r"(?:当前|本次|这个)?(?:会话)?.{0,12}(?:没有挂载|未挂载|知识库为空|无法访问|不可用).{0,12}知识库|"
    r"no knowledge base|knowledge base.{0,12}(?:empty|unavailable|not mounted)",
    flags=re.IGNORECASE,
)

_KNOWLEDGE_SOURCE_TOOLS = {
    "list_kbs",
    "get_mindmap",
    "query_knowledge_scope",
    "query_kb",
    "deepen_evidence",
    "grep_evidence",
    "open_kb_document",
    "find_kb_document",
}
_WEB_SOURCE_TOOLS = {"tavily_search", "web_search", "search_web"}


def _mention_directive_prompt(context) -> str | None:
    """mention.v2 执行指令：把冻结的执行者类提及变成模型的硬约束声明。

    @subagent 的指令由子智能体中间件在其 system prompt 中注入（收窄可用集后
    点名），此处只负责 @mcp 与 @skill 的运行时指令。措辞与 Run 创建时的
    服务端鉴权结论一致：声明"用户指定了什么"，不声明"只能用什么"。
    """
    resolution = getattr(context, "_mention_resolution", None)
    if not isinstance(resolution, dict) or str(resolution.get("status") or "").upper() not in {"RESOLVED", "DEGRADED"}:
        return None
    mcp_slugs = [str(value) for value in resolution.get("mcp_slugs") or [] if str(value).strip()]
    skill_slugs = [str(value) for value in resolution.get("skill_slugs") or [] if str(value).strip()]
    if not mcp_slugs and not skill_slugs:
        return None
    lines = ["<USER_MENTIONS>", "用户在本轮显式选择了以下资源（服务端已鉴权并冻结）："]
    if mcp_slugs:
        lines.append(
            f"- MCP 服务器：{', '.join(mcp_slugs)}。涉及相应数据库/工具事实时必须优先调用这些服务器的"
            "工具核验，不得凭记忆作答；指定不排除其他已配置工具。"
        )
    if skill_slugs:
        lines.append(f"- 技能：{', '.join(skill_slugs)}。已按用户要求预先激活，严格遵循其规范执行。")
    lines.append("</USER_MENTIONS>")
    return "\n".join(lines)


def _turn_plan_prompt(plan: TurnExecutionPlan) -> str:
    return (
        "<AUTHORITATIVE_TURN_EXECUTION_PLAN>\n"
        + json.dumps(plan.public_dict(), ensure_ascii=False, separators=(",", ":"))
        + "\n</AUTHORITATIVE_TURN_EXECUTION_PLAN>\n"
        "本计划是本轮来源权限与证据要求的唯一权威。只能使用已暴露且满足 required_capabilities 的工具；"
        "禁止静默切换到 forbidden_sources。MCP 数据只能描述其数据来源，不能伪造 PDF 页码或文献证据引用。"
    )


def filter_tools_by_turn_plan(tools: list[Any], plan: TurnExecutionPlan) -> list[Any]:
    """Apply the server-authored source/capability plan to every tool set.

    This function is public so middleware that adds tools dynamically (notably
    SkillsMiddleware) cannot bypass the same authorization decision that was
    applied to the base agent tool set.
    """
    policy = plan.source.policy
    required = set(plan.required_capabilities)
    filtered: list[Any] = []
    for tool in tools:
        name = str(getattr(tool, "name", "") or "")
        mcp_tool = is_mcp_tool(tool)
        if mcp_tool and plan.required_server:
            metadata = getattr(tool, "metadata", None) or {}
            if str(metadata.get("server") or "") != plan.required_server:
                continue
        if policy == SourcePolicy.MCP_ONLY:
            if not mcp_tool:
                continue
            profile = profile_for_tool(tool)
            if required and not profile:
                continue
            if profile and required.isdisjoint(profile.capabilities):
                continue
            filtered.append(tool)
            continue
        if mcp_tool:
            profile = profile_for_tool(tool)
            profile_source = (
                SourceClass.BIBLIOGRAPHY
                if profile and profile.source_class == "BIBLIOGRAPHY"
                else SourceClass.DISCOVERY
                if profile and profile.source_class == "DISCOVERY"
                else SourceClass.STRUCTURED_DATABASE
            )
            if profile_source not in plan.source.allowed_sources:
                continue
            if (
                policy == SourcePolicy.HYBRID_EXPLICIT
                and required
                and (not profile or required.isdisjoint(profile.capabilities))
            ):
                continue
            filtered.append(tool)
            continue
        if name in _KNOWLEDGE_SOURCE_TOOLS:
            # Retrieval/GREP is orchestrator-owned once a frozen evidence plan
            # exists. This prevents hidden post-freeze evidence.
            if (
                name != "query_knowledge_scope"
                or plan.evidence.required
                or SourceClass.LOCAL_DOCUMENT not in plan.source.allowed_sources
            ):
                continue
        if name in _WEB_SOURCE_TOOLS and SourceClass.WEB not in plan.source.allowed_sources:
            continue
        filtered.append(tool)
    return filtered


def _authoritative_scope_prompt(scope: dict) -> str:
    payload = {
        "source": "AGENT_RUN_SNAPSHOT",
        "scope_id": scope.get("scope_id"),
        "scope_version": scope.get("scope_version"),
        "authoritative_for_this_run": True,
        "knowledge_strategy": scope.get("knowledge_strategy"),
        "retrieval_mode": scope.get("retrieval_mode"),
        "allow_web": bool(scope.get("allow_web", False)),
        "kb_ids": scope.get("effective_kb_ids") or [],
    }
    return (
        "<AUTHORITATIVE_RUN_KNOWLEDGE_SCOPE>\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n</AUTHORITATIVE_RUN_KNOWLEDGE_SCOPE>\n"
        "这是本 Run 唯一权威的知识库状态。历史对话中关于知识库为空、未挂载、不可用或索引状态的描述"
        "均为历史运行状态，不得覆盖本快照。知识库能力状态只能读取后端 Contract，不得自行推断。"
    )


def _compact_tool_contract(contract: dict) -> str:
    completeness = contract.get("completeness") or {}
    return json.dumps(
        {
            "retrieval_id": contract.get("retrieval_id"),
            "status": contract.get("status"),
            "intent": (contract.get("retrieval_plan") or {}).get("intent"),
            "claim_count": len(contract.get("claims") or []),
            "evidence_count": len(contract.get("evidence") or []),
            "completeness": completeness,
            "note": "完整 Contract 已由 KnowledgeContextMiddleware 以受控上下文注入。",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _deterministic_claim_fallback(contract: dict) -> str:
    """Render a useful answer when model prose contains no text beyond citation IDs."""
    completeness = contract.get("completeness") or {}
    claims = contract.get("claims") or []
    lines = ["基于本次冻结知识范围，后端已验证的可引用结果如下："]
    for claim in claims[:20]:
        subject = str((claim.get("subject") or {}).get("name") or "未命名实体")
        predicate = str(claim.get("predicate") or "相关")
        target = str((claim.get("object") or {}).get("name") or "未命名对象")
        relation_group = str(claim.get("relation_group") or "未分类关系")
        evidence_count = len(claim.get("evidence") or [])
        lines.append(f"- **{subject}** — `{predicate}` → {target}（{relation_group}；{evidence_count} 条证据）")
    if len(claims) > 20:
        lines.append(f"- 其余 {len(claims) - 20} 条结果请在“规范科研结果”中展开核验。")
    if not claims:
        lines.append("- 本次未返回可渲染的规范 Claim；请调整问题或知识范围后重试。")
    lines.append(f"\n完整性状态：{completeness.get('status') or 'UNVERIFIED'}。引文编号由“规范科研结果”确定性呈现。")
    return "\n".join(lines)


def _has_substantive_narrative(text: str) -> bool:
    without_markers = str(text or "").replace(NARRATIVE_CITATION_MARKER, "")
    normalized = re.sub(r"[\s.,，。;；:：、/\\|()（）\[\]{}<>《》\-—_`*#]+", "", without_markers)
    return len(normalized) >= 4


def _narrative_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") in {"text", "output_text"}:
            parts.append(str(block.get("text") or ""))
    return "\n".join(parts)


def _apply_citation_guard(text: str, *, contract: dict | None) -> tuple[str, dict[str, Any] | None]:
    """引用通道输出门禁：剥离自写页码、重写伪造芯片、验证展开 [E#] 提议。

    locator_resolution（QUOTE_LOCATOR 确定性解析）已 VERIFIED 时，模型自写的
    页码被纠正为后端定位芯片。未变更时返回 (原文, None)。
    """
    if not str(text or "").strip():
        return text, None
    contract = contract or {}
    rendered, draft_validation = render_answer_draft(text)
    guarded, validation = apply_citation_channel(
        rendered,
        contract.get("citations") or [],
        locator=contract.get("locator_resolution"),
        partition_intent=(contract.get("locator_intent") or {}).get("partition_intent"),
        authority_policy=contract.get("answer_policy"),
    )
    validation["answer_draft"] = draft_validation
    if not validation.get("changed") and rendered == text:
        return text, None
    return guarded, validation


def _sanitize_message_content(content, *, contract: dict | None):
    if isinstance(content, str):
        sanitized, validation, warnings = sanitize_narrative_citations(content)
        guarded, citation_validation = _apply_citation_guard(sanitized, contract=contract)
        if validation.get("source_status") == "FAIL" and not _has_substantive_narrative(guarded):
            guarded = _deterministic_claim_fallback(contract)
            validation["action"] = "DETERMINISTIC_CLAIM_FALLBACK"
            citation_validation = None  # 兜底文本由后端生成，无需再过引用通道
        return guarded, validation, warnings, citation_validation

    _, aggregate_validation, aggregate_warnings = sanitize_narrative_citations(_narrative_text(content))
    sanitized_blocks = []
    aggregate_citation_validation: dict[str, Any] | None = None
    for block in content:
        if isinstance(block, str):
            sanitized, _, _ = sanitize_narrative_citations(block)
            guarded, citation_validation = _apply_citation_guard(sanitized, contract=contract)
            aggregate_citation_validation = citation_validation or aggregate_citation_validation
            sanitized_blocks.append(guarded)
        elif isinstance(block, dict) and block.get("type") in {"text", "output_text"}:
            sanitized, _, _ = sanitize_narrative_citations(str(block.get("text") or ""))
            guarded, citation_validation = _apply_citation_guard(sanitized, contract=contract)
            aggregate_citation_validation = citation_validation or aggregate_citation_validation
            sanitized_blocks.append({**block, "text": guarded})
        else:
            sanitized_blocks.append(block)

    sanitized_text = _narrative_text(sanitized_blocks)
    if aggregate_validation.get("source_status") == "FAIL" and not _has_substantive_narrative(sanitized_text):
        non_text_blocks = [
            block
            for block in sanitized_blocks
            if not isinstance(block, str)
            and not (isinstance(block, dict) and block.get("type") in {"text", "output_text"})
        ]
        sanitized_blocks = [{"type": "text", "text": _deterministic_claim_fallback(contract)}, *non_text_blocks]
        aggregate_validation["action"] = "DETERMINISTIC_CLAIM_FALLBACK"
        aggregate_citation_validation = None
    return sanitized_blocks, aggregate_validation, aggregate_warnings, aggregate_citation_validation


def _guard_model_response(response: ModelResponse, *, contract: dict | None) -> ModelResponse:
    if not isinstance(contract, dict) or contract.get("status") == "SKIPPED":
        return response
    nested = getattr(response, "model_response", None)
    model_response = nested if isinstance(nested, ModelResponse) else response
    if not isinstance(model_response, ModelResponse):
        return response

    guarded_messages = []
    changed = False
    for message in model_response.result or []:
        content = message.content if isinstance(message, AIMessage) else None
        narrative_text = _narrative_text(content)
        if not narrative_text or getattr(message, "tool_calls", None):
            guarded_messages.append(message)
            continue
        guarded_content, validation, warnings, citation_validation = _sanitize_message_content(
            content, contract=contract
        )
        identifier_failed = bool(validation and validation.get("source_status") == "FAIL")
        if not identifier_failed and citation_validation is None and guarded_content == content:
            guarded_messages.append(message)
            continue
        changed = True
        additional_kwargs = dict(message.additional_kwargs or {})
        if identifier_failed:
            additional_kwargs["citation_validation"] = validation
            additional_kwargs["citation_validation_warnings"] = warnings
        if citation_validation is not None:
            # 引用通道审计：模型自写页码被剥离 / [E#] 被展开的次数可观测
            additional_kwargs["locator_validation"] = citation_validation
        guarded_messages.append(
            message.model_copy(
                update={
                    "content": guarded_content,
                    "additional_kwargs": additional_kwargs,
                }
            )
        )
    if not changed:
        return response
    guarded_response = replace(model_response, result=guarded_messages)
    return replace(response, model_response=guarded_response) if nested is model_response else guarded_response


def _sanitize_history_content(content, *, changed_flag: list[bool]):
    """历史 AIMessage 投影净化：渲染产物（芯片/附录/失败关闭标记）不回流模型。

    折叠为 ``[citation omitted]`` 而非 ``[E#]``——ref 是单次 retrieval run 的
    局部编号，回流会让模型把它当稳定引用标识，制造下一轮错绑。
    """
    if isinstance(content, str):
        cleaned = sanitize_history_text(content)
        if cleaned != content:
            changed_flag[0] = True
        return cleaned
    if isinstance(content, list):
        blocks = []
        for block in content:
            if isinstance(block, str):
                cleaned = sanitize_history_text(block)
                if cleaned != block:
                    changed_flag[0] = True
                blocks.append(cleaned)
            elif isinstance(block, dict) and block.get("type") in {"text", "output_text"}:
                cleaned = sanitize_history_text(str(block.get("text") or ""))
                if cleaned != str(block.get("text") or ""):
                    changed_flag[0] = True
                blocks.append({**block, "text": cleaned})
            else:
                blocks.append(block)
        return blocks
    return content


def _sanitize_messages(messages, *, contract: dict | None = None):
    sanitized = []
    changed = False
    changed_flag = [False]
    for message in messages or []:
        if (
            isinstance(message, ToolMessage)
            and message.name == "query_knowledge_scope"
            and message.additional_kwargs.get("knowledge_first") is True
            and isinstance(contract, dict)
        ):
            changed = True
            sanitized.append(message.model_copy(update={"content": _compact_tool_contract(contract)}))
            continue
        if isinstance(message, AIMessage):
            cleaned_content = _sanitize_history_content(message.content, changed_flag=changed_flag)
            content_changed = cleaned_content != message.content
            if content_changed or _STALE_RUNTIME_STATE.search(_narrative_text(cleaned_content)):
                changed = True
                final_content = cleaned_content
                if isinstance(final_content, str) and _STALE_RUNTIME_STATE.search(final_content):
                    final_content = "[HISTORICAL_RUNTIME_STATE; NON_AUTHORITATIVE]\n" + final_content
                sanitized.append(message.model_copy(update={"content": final_content}))
                continue
            sanitized.append(message)
            continue
        sanitized.append(message)
    changed = changed or changed_flag[0]
    return sanitized if changed else messages


class KnowledgeContextMiddleware(AgentMiddleware):
    """注入本 Run 权威 Scope/Contract，并隔离历史运行状态污染。"""

    def _prepare_request(self, request: ModelRequest) -> ModelRequest:
        context = request.runtime.context
        scope = getattr(context, "_effective_knowledge_scope", None)
        contract = getattr(context, "_knowledge_contract", None)
        raw_plan = getattr(context, "_turn_execution_plan", None)
        plan = TurnExecutionPlan.model_validate(raw_plan) if isinstance(raw_plan, dict) else None
        system_message = request.system_message
        if plan is not None:
            system_message = append_to_system_message(system_message, _turn_plan_prompt(plan))
        mention_directive = _mention_directive_prompt(context)
        if mention_directive:
            system_message = append_to_system_message(system_message, mention_directive)
        if isinstance(scope, dict) and (
            plan is None
            or SourceClass.CANONICAL_RECORD in plan.source.allowed_sources
            or SourceClass.LOCAL_DOCUMENT in plan.source.allowed_sources
            or SourceClass.KNOWLEDGE_GRAPH in plan.source.allowed_sources
        ):
            system_message = append_to_system_message(system_message, _authoritative_scope_prompt(scope))
        if isinstance(contract, dict) and contract.get("status") != "SKIPPED":
            limit = int((scope.get("retrieval_policy") or {}).get("narrative_evidence_limit") or 10)
            system_message = append_to_system_message(
                system_message,
                build_answer_context(contract, narrative_evidence_limit=limit),
            )
        messages = _sanitize_messages(request.messages, contract=contract)
        tools = list(request.tools or [])
        if plan is not None:
            tools = filter_tools_by_turn_plan(tools, plan)
        if isinstance(contract, dict) and contract.get("status") != "SKIPPED":
            tools = [tool for tool in tools if getattr(tool, "name", "") not in _KNOWLEDGE_SOURCE_TOOLS]
        return request.override(system_message=system_message, messages=messages, tools=tools)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        prepared = self._prepare_request(request)
        response = handler(prepared)
        return _guard_model_response(
            response,
            contract=getattr(prepared.runtime.context, "_knowledge_contract", None),
        )

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        prepared = self._prepare_request(request)
        response = await handler(prepared)
        return _guard_model_response(
            response,
            contract=getattr(prepared.runtime.context, "_knowledge_contract", None),
        )
