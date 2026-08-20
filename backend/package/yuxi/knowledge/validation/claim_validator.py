from __future__ import annotations

from typing import Any

from yuxi.knowledge.contracts.schemas import CLAIM_VALIDATOR_VERSION

_RELATION_GROUPS = {
    "FUNCTIONAL_REGULATION",
    "PERTURBATION_EVIDENCE",
    "ASSOCIATION_OR_CONTEXT",
}


def validate_deterministic_claims(claims: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    """Validate claims before a deterministic result can be presented as scientific fact."""
    invalid_claim_ids: list[str] = []
    for index, claim in enumerate(claims):
        subject = claim.get("subject") or {}
        target = claim.get("object") or {}
        evidence = claim.get("evidence") or []
        valid = bool(
            claim.get("claim_id")
            and subject.get("id")
            and subject.get("name")
            and claim.get("predicate")
            and target.get("id")
            and target.get("name")
            and claim.get("relation_group") in _RELATION_GROUPS
            and claim.get("claim_eligible") is True
            and evidence
            and all(item.get("evidence_id") and item.get("claim_eligible") is True for item in evidence)
        )
        if not valid:
            invalid_claim_ids.append(str(claim.get("claim_id") or f"row:{index}"))

    status = "PASS" if not invalid_claim_ids else "FAIL"
    result = {
        "validator_version": CLAIM_VALIDATOR_VERSION,
        "status": status,
        "checked_claim_count": len(claims),
        "invalid_claim_ids": invalid_claim_ids,
    }
    warnings = [] if status == "PASS" else ["确定性结果中存在缺少合格证据或规范端点的 Claim。"]
    return result, warnings
