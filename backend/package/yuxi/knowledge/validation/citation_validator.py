from __future__ import annotations

import re
from typing import Any

CITATION_VALIDATOR_VERSION = "1.0"

_NARRATIVE_IDENTIFIER_PATTERNS = {
    "PMID": re.compile(r"\bPMID\s*[:：]?\s*\d{5,10}\b", flags=re.IGNORECASE),
    "DOI": re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", flags=re.IGNORECASE),
    "EVIDENCE_ID": re.compile(
        r"\bevidence[_\s-]*id\s*[:：]?\s*[A-Z0-9][A-Z0-9_-]{7,}\b",
        flags=re.IGNORECASE,
    ),
}


def redact_narrative_citation_identifiers(text: str) -> str:
    result = str(text or "")
    for kind, pattern in _NARRATIVE_IDENTIFIER_PATTERNS.items():
        result = pattern.sub(f"[{kind}_IN_STRUCTURED_TABLE]", result)
    return result


def validate_narrative_citations(text: str) -> tuple[dict[str, Any], list[str]]:
    """LLM narrative must not render citation identifiers; the deterministic table owns them."""
    detected = {
        kind: list(dict.fromkeys(match.group(0) for match in pattern.finditer(str(text or ""))))
        for kind, pattern in _NARRATIVE_IDENTIFIER_PATTERNS.items()
    }
    detected = {kind: values for kind, values in detected.items() if values}
    status = "PASS" if not detected else "FAIL"
    result = {
        "validator_version": CITATION_VALIDATOR_VERSION,
        "status": status,
        "detected_identifiers": detected,
    }
    warnings = [] if status == "PASS" else ["模型叙述包含引用标识符，已改用无引用的确定性摘要。"]
    return result, warnings


def validate_structured_citations(
    structured_rows: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    """Ensure every rendered identifier is copied from the authoritative evidence records."""
    evidence_by_id = {str(item["evidence_id"]): item for item in evidence if item.get("evidence_id")}
    invalid_rows: list[str] = []
    for index, row in enumerate(structured_rows):
        evidence_ids = [str(value) for value in row.get("evidence_ids") or []]
        selected = [evidence_by_id[value] for value in evidence_ids if value in evidence_by_id]
        expected_pmids = list(dict.fromkeys(item.get("pmid") for item in selected if item.get("pmid")))
        expected_dois = list(dict.fromkeys(item.get("doi") for item in selected if item.get("doi")))
        valid = bool(
            evidence_ids
            and len(selected) == len(evidence_ids)
            and (row.get("pmids") or []) == expected_pmids
            and (row.get("dois") or []) == expected_dois
        )
        if not valid:
            invalid_rows.append(str(row.get("claim_id") or f"row:{index}"))

    status = "PASS" if not invalid_rows else "FAIL"
    result = {
        "validator_version": CITATION_VALIDATOR_VERSION,
        "status": status,
        "checked_row_count": len(structured_rows),
        "invalid_row_ids": invalid_rows,
    }
    warnings = [] if status == "PASS" else ["结构化结果包含无法从权威 Evidence 逐项复现的引用标识符。"]
    return result, warnings
