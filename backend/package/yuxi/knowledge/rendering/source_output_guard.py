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
_FACT_MARKER = re.compile(r"\[MCP-F:(\d+):(f_[0-9a-f]{16})\]", re.I)
_FACT_LEDGER_BLOCK = re.compile(r"\s*<YUXI_MCP_FACT_LEDGER>.*?</YUXI_MCP_FACT_LEDGER>\s*", re.S)
_STANDALONE_NUMBER = re.compile(r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?(?![\w.])")


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


def _adopted_mcp_sources(source_uses: list[Any] | None) -> list[Any]:
    adopted_sources: list[Any] = []
    for source_use in source_uses or []:
        status = str(_source_use_value(source_use, "status") or "").casefold()
        adopted = bool(_source_use_value(source_use, "adopted"))
        source_use_id = str(_source_use_value(source_use, "source_use_id") or "")
        provenance = _source_use_value(source_use, "provenance") or {}
        if status == "success" and adopted and (source_use_id.startswith("mcp:") or "mcp_call_audit_id" in provenance):
            adopted_sources.append(source_use)
    return adopted_sources


def _fact_catalog(source_uses: list[Any] | None) -> dict[tuple[int, str], dict[str, Any]]:
    catalog: dict[tuple[int, str], dict[str, Any]] = {}
    for source_use in _adopted_mcp_sources(source_uses):
        provenance = _source_use_value(source_use, "provenance") or {}
        if not isinstance(provenance, dict):
            continue
        try:
            audit_id = int(provenance.get("mcp_call_audit_id"))
        except (TypeError, ValueError):
            continue
        manifest = provenance.get("fact_manifest")
        if not isinstance(manifest, dict):
            continue
        for fact in manifest.get("facts") or []:
            if isinstance(fact, dict) and re.fullmatch(r"f_[0-9a-f]{16}", str(fact.get("id") or ""), re.I):
                catalog[(audit_id, str(fact["id"]).lower())] = fact
    return catalog


def _line_requires_fact_marker(line: str) -> bool:
    stripped = line.strip()
    if not stripped or _SOURCE_ONLY.search(stripped):
        return False
    if stripped.startswith("#") or re.fullmatch(r"[| :\-]+", stripped):
        return False
    if stripped.endswith(("：", ":")) and len(stripped) <= 80:
        return False
    if stripped.startswith("|") and any(label in stripped for label in ("字段", "来源", "状态", "Field", "Source")):
        return False
    return bool(re.search(r"[A-Za-z0-9\u4e00-\u9fff]", _FACT_MARKER.sub("", stripped)))


def _normalize_number(raw: str) -> str:
    value = raw.replace(",", "")
    try:
        number = float(value)
    except ValueError:
        return value
    return str(int(number)) if number.is_integer() else format(number, ".15g")


def _validate_fact_grounding(text: str, source_uses: list[Any] | None) -> dict[str, Any]:
    adopted_sources = _adopted_mcp_sources(source_uses)
    catalog = _fact_catalog(source_uses)
    invalid_markers: list[str] = []
    ungrounded_lines: list[int] = []
    unsupported_numbers: list[dict[str, Any]] = []
    marker_count = 0
    for line_number, line in enumerate(text.splitlines(), start=1):
        markers = [(int(audit_id), fact_id.lower()) for audit_id, fact_id in _FACT_MARKER.findall(line)]
        marker_count += len(markers)
        invalid_markers.extend(
            f"{audit_id}:{fact_id}" for audit_id, fact_id in markers if (audit_id, fact_id) not in catalog
        )
        if _line_requires_fact_marker(line) and not markers:
            ungrounded_lines.append(line_number)
            continue
        cited_numbers = {
            _normalize_number(str(catalog[key]["numeric_value"]))
            for key in markers
            if key in catalog and "numeric_value" in catalog[key]
        }
        for key in markers:
            if key in catalog:
                cited_numbers.update(
                    _normalize_number(str(value)) for value in catalog[key].get("numeric_tokens") or []
                )
        visible_line = _FACT_MARKER.sub("", line)
        visible_line = re.sub(r"^\s*\d+[.)、]\s+", "", visible_line)
        for raw_number in _STANDALONE_NUMBER.findall(visible_line):
            normalized = _normalize_number(raw_number)
            if normalized not in cited_numbers:
                unsupported_numbers.append({"line": line_number, "value": raw_number})
    return {
        "schema_version": "mcp-fact-grounding.v1",
        "required": bool(adopted_sources),
        "adopted_mcp_source_count": len(adopted_sources),
        "available_fact_count": len(catalog),
        "marker_count": marker_count,
        "invalid_markers": invalid_markers[:20],
        "ungrounded_lines": ungrounded_lines[:20],
        "unsupported_numbers": unsupported_numbers[:20],
        "passed": bool(adopted_sources)
        and bool(catalog)
        and marker_count > 0
        and not invalid_markers
        and not ungrounded_lines
        and not unsupported_numbers,
    }


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

    source = _FACT_LEDGER_BLOCK.sub("", str(text or "")).strip()
    document_guard: dict[str, Any] = {"applied": False}
    if level in {EvidenceLevel.NONE, EvidenceLevel.DATA_PROVENANCE, EvidenceLevel.BIBLIOGRAPHIC}:
        source, document_guard = guard_non_document_source_answer(source)
        document_guard["applied"] = True

    source_only_declared = bool(_SOURCE_ONLY.search(source))
    adopted_sources = _adopted_mcp_sources(source_uses)
    source_only_verified = not source_only_declared or bool(adopted_sources)
    fact_grounding = _validate_fact_grounding(source, source_uses)
    fact_grounding_verified = not fact_grounding["required"] or fact_grounding["passed"]
    if (source_only_declared and not source_only_verified) or not fact_grounding_verified:
        source = (
            "当前回答未通过 MCP 事实级核验，无法发布数据库结论。"
            "系统已阻止无有效事实引用、越权数字或模型自行补写的内容；请重试并让每条事实引用本次工具返回的 MCP-F 标记。"
        )

    return source, {
        "schema_version": "answer-evidence-output-guard.v2",
        "evidence_level": level.value,
        "document_affordance_guard": document_guard,
        "source_only_declared": source_only_declared,
        "source_only_verified": source_only_verified,
        "fact_grounding": fact_grounding,
        "status": "PASSED" if source_only_verified and fact_grounding_verified else "REJECTED",
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
