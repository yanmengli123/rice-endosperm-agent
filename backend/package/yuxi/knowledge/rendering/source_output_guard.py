"""Output guards for non-document source planes."""

from __future__ import annotations

import re
from typing import Any

from yuxi.knowledge.planning.turn_execution_plan import EvidenceLevel
from yuxi.knowledge.rendering.authority_markers import authority_marker_pattern

_EVIDENCE_CHIP = authority_marker_pattern()
_EVIDENCE_REF = re.compile(r"\[E\d{1,3}\]")
_ANCHOR_ID = re.compile(r"\b(?:ea|ev|evs)_[0-9a-f]{12,64}\b", re.I)
_PAGE = re.compile(r"第\s*\d{1,4}\s*页|\bp\.\s*\d{1,4}\b|\bpages?\s+\d{1,4}\b", re.I)
_REFERENCE_BLOCK = re.compile(r"\n*【证据引用】[^\n]*\n(?:-\s*E\d+[^\n]*(?:\n|$))*", re.IGNORECASE)
_SOURCE_ONLY = re.compile(r"数据模式\s*[：:]\s*SOURCE-ONLY", re.I)


def guard_non_document_source_answer(text: str) -> tuple[str, dict[str, int | str]]:
    """Remove document-evidence affordances from MCP/data-only answers."""
    source = str(text or "")
    counts = {
        "evidence_chips_removed": len(_EVIDENCE_CHIP.findall(source)),
        "evidence_refs_removed": len(_EVIDENCE_REF.findall(source)),
        "anchor_ids_removed": len(_ANCHOR_ID.findall(source)),
        "pdf_pages_removed": len(_PAGE.findall(source)),
        "reference_blocks_removed": len(_REFERENCE_BLOCK.findall(source)),
    }
    guarded = _REFERENCE_BLOCK.sub("", source)
    guarded = _EVIDENCE_CHIP.sub("", guarded)
    guarded = _EVIDENCE_REF.sub("", guarded)
    guarded = _ANCHOR_ID.sub("", guarded)
    guarded = _PAGE.sub("（当前数据来源不提供 PDF 物理页码）", guarded)
    guarded = re.sub(r"[ \t]+\n", "\n", guarded)
    guarded = re.sub(r"\n{3,}", "\n\n", guarded).strip()
    return guarded, {
        "schema_version": "non-document-source-guard.v1",
        **counts,
    }


def _source_use_value(source_use: Any, field: str) -> Any:
    if isinstance(source_use, dict):
        return source_use.get(field)
    return getattr(source_use, field, None)


def _has_adopted_ricekb_proof(source_uses: list[Any] | None) -> bool:
    for source_use in source_uses or []:
        provider = str(_source_use_value(source_use, "provider_id") or "").casefold()
        status = str(_source_use_value(source_use, "status") or "").casefold()
        adopted = bool(_source_use_value(source_use, "adopted"))
        if provider == "ricekb" and status == "success" and adopted:
            return True
    return False


def guard_answer_for_evidence_level(
    text: str,
    *,
    evidence_level: EvidenceLevel | str,
    source_uses: list[Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Enforce answer affordances from the frozen evidence level and source ledger.

    E0/E1/E2 may not claim PDF/page/quote evidence.  A model-emitted
    ``SOURCE-ONLY`` label additionally requires an adopted, successful ricekb
    audit row; the label is an attestation, not a formatting preference.
    """
    try:
        level = EvidenceLevel(evidence_level)
    except ValueError:
        level = EvidenceLevel.NONE

    source = str(text or "")
    document_guard: dict[str, Any] = {"applied": False}
    if level in {EvidenceLevel.NONE, EvidenceLevel.DATA_PROVENANCE, EvidenceLevel.BIBLIOGRAPHIC}:
        source, document_guard = guard_non_document_source_answer(source)
        document_guard["applied"] = True

    source_only_declared = bool(_SOURCE_ONLY.search(source))
    source_only_verified = not source_only_declared or _has_adopted_ricekb_proof(source_uses)
    if source_only_declared and not source_only_verified:
        source = (
            "当前回答未取得可审计的 RiceKB 成功调用记录，无法发布 SOURCE-ONLY 数据结论。"
            "系统未使用模型记忆补写数据库事实。"
        )

    return source, {
        "schema_version": "answer-evidence-output-guard.v1",
        "evidence_level": level.value,
        "document_affordance_guard": document_guard,
        "source_only_declared": source_only_declared,
        "source_only_verified": source_only_verified,
        "status": "PASSED" if source_only_verified else "REJECTED",
    }


def guard_glossary_answer(text: str, *, contract: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Publish glossary authority states deterministically, never from model prose."""
    decision = contract.get("authority_decision")
    decision = decision if isinstance(decision, dict) else {}
    outcome = str(decision.get("outcome") or "UNAVAILABLE").upper()
    scope = contract.get("knowledge_scope_snapshot")
    scope = scope if isinstance(scope, dict) else {}
    allowed_kb_ids = {str(item) for item in scope.get("kb_ids") or [] if str(item)}
    rows = [
        row
        for row in contract.get("evidence") or []
        if isinstance(row, dict)
        and row.get("evidence_id")
        and (not allowed_kb_ids or str(row.get("kb_id") or "") in allowed_kb_ids)
    ]
    dropped = len([row for row in contract.get("evidence") or [] if isinstance(row, dict)]) - len(rows)
    terms = [str(item) for item in decision.get("lookup_terms") or [] if str(item).strip()]
    term_label = "、".join(f"“{item}”" for item in terms) or "该术语"

    if outcome == "HIT" and not rows:
        outcome = "UNAVAILABLE"
    if outcome == "HIT":
        heading = "词典核验：已收录"
    elif outcome == "MISS":
        rendered = f"词典核验：未收录\n\n当前运行范围内的活动术语词典版本未收录{term_label}。"
        return rendered, _glossary_guard_audit(outcome, rows, dropped, text)
    elif outcome == "UNAVAILABLE":
        rendered = "词典核验：不可用\n\n当前运行没有可用且已发布的术语词典；系统未使用文献或模型记忆代答。"
        return rendered, _glossary_guard_audit(outcome, rows, dropped, text)
    elif outcome == "AMBIGUOUS":
        heading = "词典核验：存在多个候选，无法唯一确定"
    elif outcome == "CONFLICT":
        heading = "词典核验：活动版本存在冲突记录，以下按来源分别列出"
    else:
        rendered = "词典核验：不可用\n\n词典返回了无法发布的状态；系统未使用模型记忆补写。"
        return rendered, _glossary_guard_audit("UNAVAILABLE", rows, dropped, text)

    blocks = [heading]
    for index, row in enumerate(rows, start=1):
        content = str(row.get("content") or row.get("evidence_quote") or "").strip()
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        blocks.append(
            f"{index}. {content}\n"
            f"   数据来源：kb_id={row.get('kb_id')}；record_key={row.get('record_key') or metadata.get('record_key')}；"
            f"row={row.get('row_number') or metadata.get('row_number')}；"
            f"revision={row.get('revision_id') or metadata.get('revision_id')}"
        )
    return "\n\n".join(blocks), _glossary_guard_audit(outcome, rows, dropped, text)


def _glossary_guard_audit(
    outcome: str, rows: list[dict[str, Any]], dropped: int, model_text: str
) -> dict[str, Any]:
    return {
        "schema_version": "glossary-output-guard.v1",
        "status": "PASSED" if outcome in {"HIT", "MISS", "UNAVAILABLE", "AMBIGUOUS", "CONFLICT"} else "REJECTED",
        "authority_outcome": outcome,
        "published_evidence_count": len(rows),
        "out_of_scope_evidence_removed": dropped,
        "model_text_replaced": bool(str(model_text or "").strip()),
    }


__all__ = [
    "guard_answer_for_evidence_level",
    "guard_glossary_answer",
    "guard_non_document_source_answer",
]
