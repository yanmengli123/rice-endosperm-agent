from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import replace

from deepagents.middleware._utils import append_to_system_message
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, ToolMessage

from yuxi.knowledge.rendering.answer_context_builder import build_answer_context
from yuxi.knowledge.validation.citation_validator import (
    NARRATIVE_CITATION_MARKER,
    sanitize_narrative_citations,
)

_STALE_RUNTIME_STATE = re.compile(
    r"(?:当前|本次|这个)?(?:会话)?.{0,12}(?:没有挂载|未挂载|知识库为空|无法访问|不可用).{0,12}知识库|"
    r"no knowledge base|knowledge base.{0,12}(?:empty|unavailable|not mounted)",
    flags=re.IGNORECASE,
)


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


def _sanitize_message_content(content, *, contract: dict):
    if isinstance(content, str):
        sanitized, validation, warnings = sanitize_narrative_citations(content)
        if validation.get("source_status") == "FAIL" and not _has_substantive_narrative(sanitized):
            sanitized = _deterministic_claim_fallback(contract)
            validation["action"] = "DETERMINISTIC_CLAIM_FALLBACK"
        return sanitized, validation, warnings

    _, aggregate_validation, aggregate_warnings = sanitize_narrative_citations(_narrative_text(content))
    sanitized_blocks = []
    for block in content:
        if isinstance(block, str):
            sanitized, _, _ = sanitize_narrative_citations(block)
            sanitized_blocks.append(sanitized)
        elif isinstance(block, dict) and block.get("type") in {"text", "output_text"}:
            sanitized, _, _ = sanitize_narrative_citations(str(block.get("text") or ""))
            sanitized_blocks.append({**block, "text": sanitized})
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
    return sanitized_blocks, aggregate_validation, aggregate_warnings


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
        sanitized_content, validation, warnings = _sanitize_message_content(content, contract=contract)
        if validation is None or validation.get("source_status") != "FAIL":
            guarded_messages.append(message)
            continue
        changed = True
        additional_kwargs = dict(message.additional_kwargs or {})
        additional_kwargs["citation_validation"] = validation
        additional_kwargs["citation_validation_warnings"] = warnings
        guarded_messages.append(
            message.model_copy(
                update={
                    "content": sanitized_content,
                    "additional_kwargs": additional_kwargs,
                }
            )
        )
    if not changed:
        return response
    guarded_response = replace(model_response, result=guarded_messages)
    return replace(response, model_response=guarded_response) if nested is model_response else guarded_response


def _sanitize_messages(messages, *, contract: dict | None = None):
    sanitized = []
    changed = False
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
        if not isinstance(message, AIMessage) or not isinstance(message.content, str):
            sanitized.append(message)
            continue
        if not _STALE_RUNTIME_STATE.search(message.content):
            sanitized.append(message)
            continue
        changed = True
        sanitized.append(
            message.model_copy(
                update={
                    "content": "[HISTORICAL_RUNTIME_STATE; NON_AUTHORITATIVE]\n" + message.content,
                }
            )
        )
    return sanitized if changed else messages


class KnowledgeContextMiddleware(AgentMiddleware):
    """注入本 Run 权威 Scope/Contract，并隔离历史运行状态污染。"""

    def _prepare_request(self, request: ModelRequest) -> ModelRequest:
        context = request.runtime.context
        scope = getattr(context, "_effective_knowledge_scope", None)
        contract = getattr(context, "_knowledge_contract", None)
        system_message = request.system_message
        if isinstance(scope, dict):
            system_message = append_to_system_message(system_message, _authoritative_scope_prompt(scope))
        if isinstance(contract, dict) and contract.get("status") != "SKIPPED":
            limit = int((scope.get("retrieval_policy") or {}).get("narrative_evidence_limit") or 10)
            system_message = append_to_system_message(
                system_message,
                build_answer_context(contract, narrative_evidence_limit=limit),
            )
        messages = _sanitize_messages(request.messages, contract=contract)
        tools = list(request.tools or [])
        if isinstance(contract, dict) and contract.get("status") != "SKIPPED":
            tools = [tool for tool in tools if getattr(tool, "name", "") not in {"query_knowledge_scope", "query_kb"}]
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
