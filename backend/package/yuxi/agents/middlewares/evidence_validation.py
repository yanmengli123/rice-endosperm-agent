"""科研知识回答的代码级 Evidence Contract 校验。"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, ToolMessage

from yuxi.knowledge.research_evidence import SCIENTIFIC_IDENTIFIER_IN_TEXT, detect_condition

_CLAIM_PATTERN = re.compile(
    r"是|属于|包含|编码|形成|调控|促进|抑制|激活|结合|互作|导致|参与|影响|上调|下调|表达|定位|"
    r"regulat(?:e|es|ed|ion)|activat(?:e|es|ed|ion)|inhibit(?:s|ed|ion)?|"
    r"increas(?:e|es|ed|ing)|decreas(?:e|es|ed|ing)|affect(?:s|ed|ing)?|promot(?:e|es|ed|ing)|suppress(?:es|ed|ing)?|"
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
_PMID_CITATION_PATTERN = re.compile(r"\bPMID\s*[:：]?\s*([0-9]{6,10})\b", re.IGNORECASE)
_DOI_CITATION_PATTERN = re.compile(r"\b(?:DOI\s*[:：]?\s*)?(10\.\d{4,9}/[^\s,;，；)\]}>]+)", re.IGNORECASE)
_YIELD_ASSERTION_PATTERN = re.compile(r"grain yield|籽粒产量|单株产量|田间产量", re.IGNORECASE)
_TARGET_DISTINCTION_PATTERN = re.compile(r"不等于|不能.{0,12}(?:等同|视为)|not (?:equal|equivalent)", re.IGNORECASE)
_ACTIVATION_ASSERTION_PATTERN = re.compile(
    r"激活|转录激活|促进.{0,8}表达|抑制.{0,8}表达|转录抑制|activat|repress|upregulat|downregulat",
    re.IGNORECASE,
)
_INFERENCE_PATTERN = re.compile(r"推断|提示|可能|likely|suggest|infer", re.IGNORECASE)
_MATERIAL_PATTERN = re.compile(
    r"突变|mutant|allele|构建体|construct|STTM|RNAi|CRISPR|knockout|knockdown|loss-of-function|"
    r"transgenic|过表达|overexpression",
    re.IGNORECASE,
)
_POSITIVE_EFFECT_PATTERN = re.compile(r"增加|提高|促进|上升|increas|enhanc|promot", re.IGNORECASE)
_NEGATIVE_EFFECT_PATTERN = re.compile(r"降低|减少|抑制|下降|decreas|reduc|suppress|inhibit", re.IGNORECASE)


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
    """Validate every scientific claim against its locally cited evidence."""
    text = str(answer or "").strip()
    empty_report = {"valid": True, "violations": [], "cited_ids": [], "validated_claims": []}
    if evidence is None:
        if _has_scientific_claim(text) and not _INSUFFICIENT_PATTERN.search(text):
            return {**empty_report, "valid": False, "violations": ["SCIENTIFIC_CLAIM_WITHOUT_SCOPE_QUERY"]}
        return empty_report

    evidence_by_id = {
        str(item.get("evidence_id")): item for item in evidence if str(item.get("evidence_id") or "").strip()
    }
    cited_ids = sorted(evidence_id for evidence_id in evidence_by_id if evidence_id in text)
    violations: list[str] = []
    validated_claims: list[dict[str, Any]] = []
    if SCIENTIFIC_IDENTIFIER_IN_TEXT.search(text):
        violations.append("INVALID_IDENTIFIER_SCIENTIFIC_NOTATION")

    if not evidence_by_id:
        if _has_scientific_claim(text) and not _INSUFFICIENT_PATTERN.search(text):
            violations.append("SCIENTIFIC_CLAIM_WITHOUT_EVIDENCE")
        return {**empty_report, "valid": not violations, "violations": violations}
    if not cited_ids and not _INSUFFICIENT_PATTERN.search(text):
        violations.append("MISSING_EVIDENCE_CITATION")

    for paragraph_index, paragraph in enumerate(re.split(r"\n+|(?<=[。！？.!?])\s+", text), start=1):
        paragraph = paragraph.strip()
        if not paragraph or not _has_scientific_claim(paragraph) or _INSUFFICIENT_PATTERN.search(paragraph):
            continue
        paragraph_ids = [evidence_id for evidence_id in evidence_by_id if evidence_id in paragraph]
        if not paragraph_ids:
            violations.append("CLAIM_WITHOUT_LOCAL_EVIDENCE")
            continue
        cited_evidence = [evidence_by_id[evidence_id] for evidence_id in paragraph_ids]
        eligible_evidence = [item for item in cited_evidence if item.get("claim_eligible") is True]
        if not eligible_evidence:
            violations.append(f"CLAIM_WITHOUT_ELIGIBLE_EVIDENCE:paragraph-{paragraph_index}")

        allowed_pmids = {str(item.get("pmid")) for item in cited_evidence if item.get("pmid")}
        allowed_dois = {str(item.get("doi")).casefold() for item in cited_evidence if item.get("doi")}
        paragraph_pmids = set(_PMID_CITATION_PATTERN.findall(paragraph))
        paragraph_dois = {value.rstrip(".").casefold() for value in _DOI_CITATION_PATTERN.findall(paragraph)}
        for pmid in sorted(paragraph_pmids - allowed_pmids):
            violations.append(f"PMID_NOT_IN_CITED_EVIDENCE:{pmid}")
        for doi in sorted(paragraph_dois - allowed_dois):
            violations.append(f"DOI_NOT_IN_CITED_EVIDENCE:{doi}")

        for evidence_id in paragraph_ids:
            item = evidence_by_id[evidence_id]
            status = str(item.get("evidence_status") or "").upper()
            if status == "CANDIDATE" and not _CANDIDATE_PATTERN.search(paragraph):
                violations.append(f"CANDIDATE_NOT_LABELED:{evidence_id}")
            if status == "REJECTED" and not _REJECTED_PATTERN.search(paragraph):
                violations.append(f"REJECTED_USED_AS_SUPPORT:{evidence_id}")
            if item.get("conflict") and not _CONFLICT_PATTERN.search(paragraph):
                violations.append(f"CONFLICT_NOT_DISCLOSED:{evidence_id}")

        outcome_classes = {str(item.get("outcome_class") or "OTHER").upper() for item in eligible_evidence}
        if (
            _YIELD_ASSERTION_PATTERN.search(paragraph)
            and not _TARGET_DISTINCTION_PATTERN.search(paragraph)
            and outcome_classes
            and outcome_classes.isdisjoint({"DIRECT_YIELD", "CONDITION_SPECIFIC_YIELD"})
        ):
            violations.append(f"UNSUPPORTED_TARGET_SUBSTITUTION:paragraph-{paragraph_index}")

        predicates = {
            str(item.get("observed_relation") or item.get("predicate") or "").upper() for item in eligible_evidence
        }
        binding_only = predicates and all(
            any(marker in predicate for marker in ("BIND", "INTERACT", "COEXPRESSION")) for predicate in predicates
        )
        if _ACTIVATION_ASSERTION_PATTERN.search(paragraph) and binding_only:
            violations.append(f"UNSUPPORTED_RELATION_SUBSTITUTION:paragraph-{paragraph_index}")

        conditions = {str(item.get("condition")) for item in eligible_evidence if item.get("condition")}
        for condition in conditions:
            if condition not in paragraph and detect_condition(paragraph) != condition:
                violations.append(f"CONDITION_OMITTED:{condition}:paragraph-{paragraph_index}")

        material_types = {
            str(item.get("experimental_subject_type") or "").upper()
            for item in eligible_evidence
            if item.get("experimental_subject_type")
        }
        if (
            material_types
            & {
                "ALLELE_MUTANT",
                "KNOCKOUT_LINE",
                "KNOCKDOWN_LINE",
                "OVEREXPRESSION_LINE",
                "RNAI_CONSTRUCT",
                "STTM_CONSTRUCT",
                "CRISPR_LINE",
                "TRANSGENIC_LINE",
            }
            and not _MATERIAL_PATTERN.search(paragraph)
            and not _INFERENCE_PATTERN.search(paragraph)
        ):
            violations.append(f"EXPERIMENTAL_MATERIAL_OMITTED:paragraph-{paragraph_index}")

        perturbed_targets = {str(item.get("perturbs")) for item in eligible_evidence if item.get("perturbs")}
        subject_materials = {
            str(item.get("subject_material")) for item in eligible_evidence if item.get("subject_material")
        }
        if (
            perturbed_targets
            and any(target.casefold() in paragraph.casefold() for target in perturbed_targets)
            and not _INFERENCE_PATTERN.search(paragraph)
            and not any(subject.casefold() in paragraph.casefold() for subject in subject_materials)
        ):
            violations.append(f"UNSUPPORTED_CONSTRUCT_TO_GENE_INFERENCE:paragraph-{paragraph_index}")

        directions = {
            str(item.get("direction") or item.get("observed_effect") or "").upper() for item in eligible_evidence
        }
        if _POSITIVE_EFFECT_PATTERN.search(paragraph) and directions and directions <= {"NEGATIVE", "DECREASE", "DOWN"}:
            violations.append(f"UNSUPPORTED_EFFECT_DIRECTION:paragraph-{paragraph_index}")
        if _NEGATIVE_EFFECT_PATTERN.search(paragraph) and directions and directions <= {"POSITIVE", "INCREASE", "UP"}:
            violations.append(f"UNSUPPORTED_EFFECT_DIRECTION:paragraph-{paragraph_index}")

        validated_claims.append(
            {
                "paragraph": paragraph_index,
                "evidence_ids": paragraph_ids,
                "eligible_evidence_ids": [str(item.get("evidence_id")) for item in eligible_evidence],
                "pmids": sorted(paragraph_pmids),
                "dois": sorted(paragraph_dois),
            }
        )

    violations = list(dict.fromkeys(violations))
    return {
        "valid": not violations,
        "violations": violations,
        "cited_ids": cited_ids,
        "validated_claims": validated_claims,
    }


def _blocked_answer(report: dict[str, Any]) -> str:
    codes = "、".join(report.get("violations") or ["EVIDENCE_CONTRACT_FAILED"])
    return (
        "本次回答未通过科研证据校验，因此已阻止输出未经引用的确定性结论。\n\n"
        f"校验项：`{codes}`。请重新发起检索；系统会要求每个关键科研论断引用当前知识范围内的 evidence_id。"
    )


_OUTCOME_SECTION_LABELS = {
    "DIRECT_YIELD": "直接产量",
    "CONDITION_SPECIFIC_YIELD": "条件特异产量",
    "YIELD_COMPONENT": "产量构成",
    "GRAIN_FILLING": "灌浆",
    "GRAIN_MORPHOLOGY": "粒型",
    "QUALITY": "品质",
    "OTHER": "其他支持",
}


def _grounded_evidence_fallback(evidence: list[dict[str, Any]] | None) -> str | None:
    """Build a deterministic, citable answer when model prose violates the contract."""

    eligible = [item for item in evidence or [] if item.get("claim_eligible") is True and item.get("evidence_id")]
    if not eligible:
        return None

    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in eligible:
        grouped.setdefault(str(item.get("outcome_class") or "OTHER").upper(), []).append(item)

    lines = [
        "模型生成的叙述未通过科研证据校验；系统已自动降级为仅含可引用记录的结构化证据清单。",
        "",
    ]
    ordered_categories = [
        "DIRECT_YIELD",
        "CONDITION_SPECIFIC_YIELD",
        "YIELD_COMPONENT",
        "GRAIN_FILLING",
        "GRAIN_MORPHOLOGY",
        "QUALITY",
        "OTHER",
    ]
    for category in ordered_categories:
        rows = grouped.get(category) or []
        if not rows:
            continue
        lines.extend([f"## {_OUTCOME_SECTION_LABELS[category]}", ""])
        for item in rows:
            subject = str((item.get("subject") or {}).get("name") or "未命名对象")
            target = str((item.get("object") or {}).get("name") or "未记录")
            fields = [
                f"**{subject}**",
                f"分类 `{category}`",
                f"表型/对象 `{target}`",
            ]
            relation = str(item.get("observed_relation") or item.get("predicate") or "").strip()
            if relation:
                fields.append(f"记录关系 `{relation}`")
            effect = str(item.get("observed_effect") or item.get("direction") or "").strip()
            if effect:
                fields.append(f"观察效应 `{effect}`")
            material_parts = [
                str(item.get("experimental_subject_type") or "").strip(),
                str(item.get("subject_material") or "").strip(),
            ]
            material = " / ".join(part for part in material_parts if part)
            if material:
                fields.append(f"实验材料 `{material}`")
            condition = str(item.get("condition") or "").strip()
            if condition:
                fields.append(f"实验条件 `{condition}`")
            if item.get("conflict"):
                fields.append("证据冲突：是")
            evidence_id = str(item["evidence_id"])
            identifiers = []
            if item.get("pmid"):
                identifiers.append(f"PMID: {item['pmid']}")
            if item.get("doi"):
                identifiers.append(f"DOI: {item['doi']}")
            citation = f"；{'；'.join(identifiers)}" if identifiers else ""
            lines.append(f"- {'｜'.join(fields)}｜evidence_id `{evidence_id}`{citation}")
        lines.append("")
    lines.append("详细原始证据可在本次“统一知识范围检索”工具卡中展开核验。")
    return "\n".join(lines).strip()


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

    evidence = latest_scope_evidence(list(request.messages or []))
    report = validate_answer_evidence(answer.content, evidence)
    metadata = dict(answer.additional_kwargs or {})
    metadata["knowledge_claim_validation"] = report
    content = answer.content
    if not report["valid"]:
        fallback = _grounded_evidence_fallback(evidence)
        fallback_report = validate_answer_evidence(fallback, evidence) if fallback else None
        if fallback and fallback_report and fallback_report["valid"]:
            content = fallback
            metadata["knowledge_claim_validation_original"] = report
            metadata["knowledge_claim_validation"] = {
                **fallback_report,
                "fallback_generated": True,
            }
        else:
            content = _blocked_answer(report)
    results[-1] = answer.model_copy(
        update={
            "content": content,
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
