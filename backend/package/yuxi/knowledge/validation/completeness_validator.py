from __future__ import annotations

from typing import Any


def validate_completeness(completeness: dict[str, Any] | None) -> tuple[str, list[str]]:
    values = completeness or {}
    warnings: list[str] = []
    expected_claims = int(values.get("eligible_claim_count") or 0)
    returned_claims = int(values.get("returned_claim_count") or 0)
    expected_evidence = int(values.get("eligible_evidence_count") or 0)
    returned_evidence = int(values.get("returned_evidence_count") or 0)
    if expected_claims != returned_claims:
        warnings.append(f"规范 Claim 返回不完整：期望 {expected_claims}，实际 {returned_claims}。")
    if expected_evidence != returned_evidence:
        warnings.append(f"规范 Evidence 返回不完整：期望 {expected_evidence}，实际 {returned_evidence}。")
    return ("PASS" if not warnings else "FAIL"), warnings
