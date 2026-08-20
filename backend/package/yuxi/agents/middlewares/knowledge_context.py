from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import replace

from deepagents.middleware._utils import append_to_system_message
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, ToolMessage

from yuxi.knowledge.rendering.answer_context_builder import build_answer_context
from yuxi.knowledge.validation.citation_validator import validate_narrative_citations

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


def _citation_guard_fallback(contract: dict) -> str:
    completeness = contract.get("completeness") or {}
    claim_count = len(contract.get("claims") or [])
    evidence_count = len(contract.get("evidence") or [])
    return (
        "本次科研引用由后端结构化证据表确定性呈现。模型叙述中的引用标识未通过校验，已自动拦截。\n\n"
        f"当前返回 {claim_count} 条可引用 Claim、{evidence_count} 条合格 Evidence；"
        f"完整性状态为 {completeness.get('status') or 'UNVERIFIED'}。请展开上方“规范科研结果”逐项核验。"
    )


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
        if isinstance(content, str):
            narrative_text = content
        elif isinstance(content, list):
            narrative_text = "\n".join(
                str(block.get("text") or "")
                for block in content
                if isinstance(block, dict) and block.get("type") in {"text", "output_text"}
            )
        else:
            narrative_text = ""
        if not narrative_text or getattr(message, "tool_calls", None):
            guarded_messages.append(message)
            continue
        validation, warnings = validate_narrative_citations(narrative_text)
        if validation["status"] == "PASS":
            guarded_messages.append(message)
            continue
        changed = True
        additional_kwargs = dict(message.additional_kwargs or {})
        additional_kwargs["citation_validation"] = validation
        additional_kwargs["citation_validation_warnings"] = warnings
        guarded_messages.append(
            message.model_copy(
                update={
                    "content": _citation_guard_fallback(contract),
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
