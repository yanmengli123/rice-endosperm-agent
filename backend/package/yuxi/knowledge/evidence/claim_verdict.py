"""Claim 核验裁决层（ADR-0006 §5）：与来源裁决分离的双层状态机。

- 层 1 来源裁决（已有，``AuthorityOutcome``）：HIT/MISS/UNAVAILABLE/AMBIGUOUS/CONFLICT，
  回答「权威源怎么说」。
- 层 2 Claim 核验裁决（本模块）：SUPPORTED/INSUFFICIENT/REJECTED/NEEDS_REVIEW/
  CONFLICT_DEFERRED，回答「这条候选 Claim 是否达到了它的证据义务」。

两条不可违反的硬规则：

1. **谓词不可变**：核验器不得把证据不足的 ``regulates`` 改写成
   ``coexpressed_with``——那是新关系、新证据义务，只能另建候选 Claim。
   ``assert_claim_identity`` 对 (subject, predicate, object) 做逐字节校验。
2. **模型不能批准自己**：``MODEL_ASSIST`` 层只能排序与建议，任何
   ``SUPPORTED`` 裁决必须由 DETERMINISTIC / RULE_MATRIX / HUMAN_REVIEW 层给出；
   高风险类别（因果调控、直接互作、跨物种外推）强制人工复核。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from yuxi.knowledge.evidence.dimensions import EVIDENCE_DIMENSION_VOCAB_VERSION, ClaimClass

CLAIM_VERDICT_SCHEMA_VERSION = "claim-verdict.v1"


class ClaimVerdict(StrEnum):
    SUPPORTED = "SUPPORTED"
    INSUFFICIENT = "INSUFFICIENT"
    REJECTED = "REJECTED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    CONFLICT_DEFERRED = "CONFLICT_DEFERRED"


class VerificationLayer(StrEnum):
    """给出裁决的执行层；MODEL_ASSIST 永不产生终态批准。"""

    DETERMINISTIC = "DETERMINISTIC"
    RULE_MATRIX = "RULE_MATRIX"
    MODEL_ASSIST = "MODEL_ASSIST"
    HUMAN_REVIEW = "HUMAN_REVIEW"


class ClaimIntegrityError(ValueError):
    """候选 Claim 在核验过程中被改写（谓词/主客体漂移）。"""


@dataclass(frozen=True, slots=True)
class CandidateClaim:
    """候选机制 Claim：核验的输入与输出必须保持身份不变。"""

    subject: str
    predicate: str
    object: str
    claim_class: str = ClaimClass.ASSOCIATION
    cross_species: bool = False
    qualifiers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class VerdictDecision:
    """一条候选 Claim 的终态裁决；可投影进运行清单与复核队列。"""

    claim: CandidateClaim
    verdict: ClaimVerdict
    layer: VerificationLayer
    reason_code: str
    evidence_ids: tuple[str, ...] = ()
    rule_matrix_version: str = EVIDENCE_DIMENSION_VOCAB_VERSION
    schema_version: str = CLAIM_VERDICT_SCHEMA_VERSION


def assert_claim_identity(candidate: CandidateClaim, decided: CandidateClaim) -> None:
    """核验输出与候选输入的 (subject, predicate, object) 必须逐字节相等。"""
    for attr in ("subject", "predicate", "object"):
        if getattr(candidate, attr) != getattr(decided, attr):
            raise ClaimIntegrityError(
                f"claim {attr} rewritten during verification: "
                f"{getattr(candidate, attr)!r} -> {getattr(decided, attr)!r}; "
                "关系改写不是降级，必须另建候选 Claim 并重新走证据流程"
            )


# 高风险 Claim 类别：因果与直接互作结论必须经人工复核才能发布
HIGH_RISK_CLAIM_CLASSES = frozenset({ClaimClass.CAUSAL_REGULATION, ClaimClass.MOLECULAR_INTERACTION})

REASON_FORCED_HUMAN_REVIEW = "HIGH_RISK_FORCED_HUMAN_REVIEW"
REASON_MODEL_ASSIST_NO_APPROVAL = "MODEL_ASSIST_CANNOT_APPROVE"


def requires_human_review(claim_class: str, *, cross_species: bool = False) -> bool:
    return cross_species or claim_class in HIGH_RISK_CLAIM_CLASSES


def adjudicate(
    candidate: CandidateClaim,
    *,
    proposed: ClaimVerdict,
    layer: VerificationLayer,
    reason_code: str,
    evidence_ids: tuple[str, ...] = (),
) -> VerdictDecision:
    """汇总一层校验的提议裁决，施加模型禁批与高风险强制复核两条门禁。"""
    if layer == VerificationLayer.MODEL_ASSIST and proposed == ClaimVerdict.SUPPORTED:
        raise ClaimIntegrityError(REASON_MODEL_ASSIST_NO_APPROVAL)
    verdict = proposed
    final_reason = str(reason_code or "").strip() or "UNSPECIFIED"
    if proposed == ClaimVerdict.SUPPORTED and requires_human_review(
        candidate.claim_class, cross_species=candidate.cross_species
    ):
        if layer != VerificationLayer.HUMAN_REVIEW:
            verdict = ClaimVerdict.NEEDS_REVIEW
            final_reason = REASON_FORCED_HUMAN_REVIEW
    return VerdictDecision(
        claim=candidate,
        verdict=verdict,
        layer=layer,
        reason_code=final_reason,
        evidence_ids=tuple(evidence_ids),
    )


__all__ = [
    "CLAIM_VERDICT_SCHEMA_VERSION",
    "HIGH_RISK_CLAIM_CLASSES",
    "CandidateClaim",
    "ClaimIntegrityError",
    "ClaimVerdict",
    "VerdictDecision",
    "VerificationLayer",
    "adjudicate",
    "assert_claim_identity",
    "requires_human_review",
]
