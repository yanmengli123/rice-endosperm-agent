from __future__ import annotations

import hashlib
import re
from typing import Any

CONTRACT_SCHEMA_VERSION = "1.0"
CLAIM_VALIDATOR_VERSION = "1.0"

_PERTURBATION_MARKERS = (
    "mutant",
    "knockout",
    "knockdown",
    "overexpression",
    "rnai",
    "crispr",
    "allele",
    "loss_of_function",
    "gain_of_function",
    "effect",
)
_FUNCTIONAL_MARKERS = (
    "regulate",
    "regulates",
    "promote",
    "promotes",
    "inhibit",
    "inhibits",
    "activate",
    "activates",
    "repress",
    "represses",
    "suppress",
    "suppresses",
    "control",
    "controls",
    "required_for",
    "requires",
    "contribute",
    "enhance",
)


def _normalized(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().casefold())


def _stable_id(prefix: str, *parts: Any) -> str:
    raw = "\x1f".join(_normalized(part) for part in parts)
    return f"{prefix}_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


def claim_id(subject_identity: str, predicate: str, object_identity: str) -> str:
    """Claim 身份不包含 DOI/PMID；一条 Claim 可以绑定多条独立证据。"""
    return _stable_id("claim", subject_identity, predicate, object_identity)


def evidence_key(claim_key: str, evidence: dict[str, Any]) -> str:
    literature_identity = evidence.get("doi") or evidence.get("pmid") or evidence.get("literature_id")
    return _stable_id(
        "evidence",
        claim_key,
        literature_identity,
        evidence.get("evidence_quote"),
        evidence.get("condition"),
        evidence.get("subject_material"),
    )


def relation_group(relation_type: str | None) -> str:
    value = _normalized(relation_type).replace("-", "_").replace(" ", "_")
    if any(marker in value for marker in _PERTURBATION_MARKERS):
        return "PERTURBATION_EVIDENCE"
    if any(marker in value for marker in _FUNCTIONAL_MARKERS):
        return "FUNCTIONAL_REGULATION"
    return "ASSOCIATION_OR_CONTEXT"
