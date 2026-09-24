"""Claim 核验裁决层 golden（ADR-0006 §5 验收）。"""

import pytest

from yuxi.knowledge.evidence.claim_verdict import (
    CLAIM_VERDICT_SCHEMA_VERSION,
    CandidateClaim,
    ClaimIntegrityError,
    ClaimVerdict,
    VerificationLayer,
    adjudicate,
    assert_claim_identity,
    requires_human_review,
)


def _regulates_claim() -> CandidateClaim:
    return CandidateClaim(
        subject="OsMADS1",
        predicate="regulates",
        object="Wx",
        claim_class="CAUSAL_REGULATION",
    )


def test_predicate_rewrite_is_not_a_downgrade_and_must_raise():
    """证据不足时把 regulates 改写成 coexpressed_with 是新关系，必须抛错。"""
    candidate = _regulates_claim()
    rewritten = CandidateClaim(
        subject="OsMADS1",
        predicate="coexpressed_with",
        object="Wx",
        claim_class="ASSOCIATION",
    )
    with pytest.raises(ClaimIntegrityError, match="predicate rewritten"):
        assert_claim_identity(candidate, rewritten)


def test_insufficient_evidence_keeps_claim_identity():
    candidate = _regulates_claim()
    decision = adjudicate(
        candidate,
        proposed=ClaimVerdict.INSUFFICIENT,
        layer=VerificationLayer.RULE_MATRIX,
        reason_code="REJECT_ASSOCIATION_CANNOT_PROVE_MECHANISM",
    )
    assert decision.verdict == ClaimVerdict.INSUFFICIENT
    assert decision.claim.predicate == "regulates"
    assert decision.schema_version == CLAIM_VERDICT_SCHEMA_VERSION


def test_model_assist_can_never_approve():
    with pytest.raises(ClaimIntegrityError, match="MODEL_ASSIST_CANNOT_APPROVE"):
        adjudicate(
            CandidateClaim(subject="A", predicate="interacts_with", object="B", claim_class="RECORD_FACT"),
            proposed=ClaimVerdict.SUPPORTED,
            layer=VerificationLayer.MODEL_ASSIST,
            reason_code="model_suggestion",
        )


def test_high_risk_supported_is_forced_to_needs_review():
    decision = adjudicate(
        _regulates_claim(),
        proposed=ClaimVerdict.SUPPORTED,
        layer=VerificationLayer.RULE_MATRIX,
        reason_code="OK_DIRECT_ASSAY",
        evidence_ids=("evgraph_1",),
    )
    assert decision.verdict == ClaimVerdict.NEEDS_REVIEW
    assert decision.reason_code == "HIGH_RISK_FORCED_HUMAN_REVIEW"
    assert decision.evidence_ids == ("evgraph_1",)


def test_high_risk_supported_passes_only_via_human_review():
    decision = adjudicate(
        _regulates_claim(),
        proposed=ClaimVerdict.SUPPORTED,
        layer=VerificationLayer.HUMAN_REVIEW,
        reason_code="reviewer_approved",
    )
    assert decision.verdict == ClaimVerdict.SUPPORTED


def test_cross_species_inference_always_needs_review():
    assert requires_human_review("ASSOCIATION", cross_species=True)
    assert requires_human_review("CAUSAL_REGULATION")
    assert requires_human_review("MOLECULAR_INTERACTION")
    assert not requires_human_review("RECORD_FACT")


def test_deterministic_layer_can_approve_low_risk_claim():
    decision = adjudicate(
        CandidateClaim(subject="Wx", predicate="has_accession", object="P0C585", claim_class="RECORD_FACT"),
        proposed=ClaimVerdict.SUPPORTED,
        layer=VerificationLayer.DETERMINISTIC,
        reason_code="OK_LOW_OBLIGATION",
        evidence_ids=("mcp-f-1",),
    )
    assert decision.verdict == ClaimVerdict.SUPPORTED
    assert decision.layer == VerificationLayer.DETERMINISTIC
