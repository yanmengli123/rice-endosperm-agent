from __future__ import annotations

from typing import Any


def render_structured_rows(claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """由后端生成不可由 LLM 改写的科研结果行。"""
    rows: list[dict[str, Any]] = []
    for claim in claims:
        evidence_items = claim.get("evidence") or []
        rows.append(
            {
                "claim_id": claim.get("claim_id"),
                "subject": (claim.get("subject") or {}).get("name"),
                "relation": claim.get("predicate"),
                "relation_group": claim.get("relation_group"),
                "object": (claim.get("object") or {}).get("name"),
                "evidence_level": next(
                    (item.get("evidence_level") for item in evidence_items if item.get("evidence_level")), None
                ),
                "pmids": list(dict.fromkeys(item.get("pmid") for item in evidence_items if item.get("pmid"))),
                "dois": list(dict.fromkeys(item.get("doi") for item in evidence_items if item.get("doi"))),
                "evidence_ids": [item.get("evidence_id") for item in evidence_items if item.get("evidence_id")],
                "conditions": list(
                    dict.fromkeys(item.get("condition") for item in evidence_items if item.get("condition"))
                ),
            }
        )
    return rows
