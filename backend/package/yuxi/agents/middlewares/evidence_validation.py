"""科研知识回答的代码级 Evidence Contract 校验。"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, ToolMessage

_CLAIM_PATTERN = re.compile(
    r"是|属于|包含|编码|形成|调控|促进|抑制|激活|结合|互作|导致|参与|影响|上调|下调|表达|定位|"
    r"regulat(?:e|es|ed|ion)|activat(?:e|es|ed|ion)|inhibit(?:s|ed|ion)?|"
    r"bind(?:s|ing)?|interact(?:s|ed|ion)?|express(?:es|ed|ion)?|\b(?:is|are|encodes?|contains?)\b",
    re.IGNORECASE,
)
_DOMAIN_PATTERN = re.compile(
    r"水稻|胚乳|籽粒|基因|蛋白|转录|染色质|突变体|等位基因|"
    r"rice|endosperm|seed|gene|protein|transcri|chromatin|mutant|allele|\bOs[A-Za-z0-9_.-]+",
    re.IGNORECASE,
)
_INSUFFICIENT_PATTERN = re.compile(
    r"证据不足|未检索到|没有足够|无法确认|尚不能确认|unknown|insufficient", re.IGNORECASE
)
_CANDIDATE_PATTERN = re.compile(r"候选|推测|假设|尚待验证|candidate|hypothesis|putative", re.IGNORECASE)
_REJECTED_PATTERN = re.compile(r"拒绝|否定|不支持|驳回|无效|rejected|refuted|not supported", re.IGNORECASE)
_CONFLICT_PATTERN = re.compile(r"冲突|分歧|不一致|矛盾|conflict|disagree", re.IGNORECASE)


def _has_scientific_claim(text: str) -> bool:
    return bool(_DOMAIN_PATTERN.search(text) and _CLAIM_PATTERN.search(text))


def _tool_payload(message: ToolMessage) -> dict[str, Any] | None:
    content = message.content
    if isinstance(content, dict):
        return content
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def latest_scope_evidence(messages: list[Any]) -> list[dict[str, Any]] | None:
    """读取最后一次统一检索结果；None 表示本轮尚未调用统一检索。"""
    for message in reversed(messages or []):
        if not isinstance(message, ToolMessage) or str(getattr(message, "name", "")) != "query_knowledge_scope":
            continue
        payload = _tool_payload(message)
        if payload is None:
            return []
        evidence = payload.get("evidence")
        return [item for item in evidence if isinstance(item, dict)] if isinstance(evidence, list) else []
    return None


def validate_answer_evidence(answer: str, evidence: list[dict[str, Any]] | None) -> dict[str, Any]:
    """对确定性科研段落执行引用、候选、拒绝和冲突语义校验。"""
    text = str(answer or "").strip()
    if evidence is None:
        if _has_scientific_claim(text) and not _INSUFFICIENT_PATTERN.search(text):
            return {"valid": False, "violations": ["SCIENTIFIC_CLAIM_WITHOUT_SCOPE_QUERY"], "cited_ids": []}
        return {"valid": True, "violations": [], "cited_ids": []}

    evidence_by_id = {
        str(item.get("evidence_id")): item for item in evidence if str(item.get("evidence_id") or "").strip()
    }
    cited_ids = sorted(evidence_id for evidence_id in evidence_by_id if evidence_id in text)
    violations: list[str] = []

    if not evidence_by_id:
        if _has_scientific_claim(text) and not _INSUFFICIENT_PATTERN.search(text):
            violations.append("SCIENTIFIC_CLAIM_WITHOUT_EVIDENCE")
        return {"valid": not violations, "violations": violations, "cited_ids": []}

    if not cited_ids and not _INSUFFICIENT_PATTERN.search(text):
        violations.append("MISSING_EVIDENCE_CITATION")

    # 以 Markdown 段落为最小论断单元，要求科研确定性论断与证据 ID 同段出现。
    for paragraph in re.split(r"\n\s*\n|(?<=[。！？!?])\s*", text):
        paragraph = paragraph.strip()
        if not paragraph or not _has_scientific_claim(paragraph) or _INSUFFICIENT_PATTERN.search(paragraph):
            continue
        paragraph_ids = [evidence_id for evidence_id in evidence_by_id if evidence_id in paragraph]
        if not paragraph_ids:
            violations.append("CLAIM_WITHOUT_LOCAL_EVIDENCE")
            continue
        for evidence_id in paragraph_ids:
            item = evidence_by_id[evidence_id]
            status = str(item.get("evidence_status") or "").upper()
            if status == "CANDIDATE" and not _CANDIDATE_PATTERN.search(paragraph):
                violations.append(f"CANDIDATE_NOT_LABELED:{evidence_id}")
            if status == "REJECTED" and not _REJECTED_PATTERN.search(paragraph):
                violations.append(f"REJECTED_USED_AS_SUPPORT:{evidence_id}")
            if item.get("conflict") and not _CONFLICT_PATTERN.search(paragraph):
                violations.append(f"CONFLICT_NOT_DISCLOSED:{evidence_id}")

    violations = list(dict.fromkeys(violations))
    return {"valid": not violations, "violations": violations, "cited_ids": cited_ids}


def _blocked_answer(report: dict[str, Any]) -> str:
    codes = "、".join(report.get("violations") or ["EVIDENCE_CONTRACT_FAILED"])
    return (
        "本次回答未通过科研证据校验，因此已阻止输出未经引用的确定性结论。\n\n"
        f"校验项：`{codes}`。请重新发起检索；系统会要求每个关键科研论断引用当前知识范围内的 evidence_id。"
    )


def _validate_response(request: ModelRequest, response: ModelResponse) -> ModelResponse:
    context = getattr(getattr(request, "runtime", None), "context", None)
    scope = getattr(context, "_effective_knowledge_scope", None)
    if not isinstance(scope, dict):
        return response

    results = list(response.result or [])
    if not results or not isinstance(results[-1], AIMessage) or results[-1].tool_calls:
        return response
    answer = results[-1]
    if not isinstance(answer.content, str):
        return response

    report = validate_answer_evidence(answer.content, latest_scope_evidence(list(request.messages or [])))
    metadata = dict(answer.additional_kwargs or {})
    metadata["knowledge_claim_validation"] = report
    results[-1] = answer.model_copy(
        update={
            "content": answer.content if report["valid"] else _blocked_answer(report),
            "additional_kwargs": metadata,
        }
    )
    return ModelResponse(result=results, structured_response=response.structured_response)


def _disable_streaming_for_validation(request: ModelRequest) -> ModelRequest:
    """在科研范围内先得到完整模型答复，再校验后向客户端发送。"""
    context = getattr(getattr(request, "runtime", None), "context", None)
    if not isinstance(getattr(context, "_effective_knowledge_scope", None), dict):
        return request
    latest_human_text = ""
    for message in reversed(list(getattr(request, "messages", None) or [])):
        if getattr(message, "type", None) == "human" and isinstance(getattr(message, "content", None), str):
            latest_human_text = message.content
            break
    if latest_scope_evidence(list(getattr(request, "messages", None) or [])) is None and not _DOMAIN_PATTERN.search(
        latest_human_text
    ):
        return request
    model = getattr(request, "model", None)
    if not hasattr(model, "model_copy"):
        return request
    try:
        return request.override(model=model.model_copy(update={"disable_streaming": True}))
    except Exception:  # noqa: BLE001
        return request


class EvidenceValidationMiddleware(AgentMiddleware):
    """Scope 生效时阻断缺少有效证据引用的科研确定性回答。"""

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        scoped_request = _disable_streaming_for_validation(request)
        return _validate_response(scoped_request, handler(scoped_request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        scoped_request = _disable_streaming_for_validation(request)
        return _validate_response(scoped_request, await handler(scoped_request))
