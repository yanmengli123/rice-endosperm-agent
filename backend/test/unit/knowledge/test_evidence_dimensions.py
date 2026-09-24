"""维度 × Claim 类别许可矩阵 golden（ADR-0006 §2 验收）。"""

from yuxi.knowledge.evidence.dimensions import (
    EVIDENCE_DIMENSION_VOCAB_VERSION,
    AssayType,
    ClaimClass,
    MeasurementKind,
    SupportType,
    evaluate_claim_support,
)


def test_rnaseq_coexpression_is_experimental_but_only_association():
    """用户核心反例：RNA-seq 共表达是实验测量，但仍只是关联，不能支撑调控。"""
    decision = evaluate_claim_support(
        MeasurementKind.EXPERIMENTAL_ASSAY,
        SupportType.ASSOCIATION,
        ClaimClass.CAUSAL_REGULATION,
        assay_types=frozenset({AssayType.RNA_SEQ}),
    )
    assert not decision.supported
    assert decision.reason_code == "REJECT_ASSOCIATION_CANNOT_PROVE_MECHANISM"
    assert decision.vocab_version == EVIDENCE_DIMENSION_VOCAB_VERSION


def test_rnaseq_coexpression_supports_association_claim():
    decision = evaluate_claim_support(
        MeasurementKind.EXPERIMENTAL_ASSAY,
        SupportType.ASSOCIATION,
        ClaimClass.ASSOCIATION,
        assay_types=frozenset({AssayType.RNA_SEQ}),
    )
    assert decision.supported
    assert decision.reason_code == "OK_ASSOCIATION_CLAIM"


def test_computational_prediction_never_supports_mechanism():
    for claim in (ClaimClass.MOLECULAR_INTERACTION, ClaimClass.PTM_MODIFICATION, ClaimClass.CAUSAL_REGULATION):
        decision = evaluate_claim_support(
            MeasurementKind.COMPUTATIONAL_PREDICTION,
            SupportType.DIRECT,
            claim,
            assay_types=frozenset({AssayType.YEAST_TWO_HYBRID}),
        )
        assert not decision.supported
        assert decision.reason_code == "REJECT_PREDICTION_CANNOT_PROVE_MECHANISM"


def test_direct_y2h_supports_molecular_interaction():
    decision = evaluate_claim_support(
        MeasurementKind.EXPERIMENTAL_ASSAY,
        SupportType.DIRECT,
        ClaimClass.MOLECULAR_INTERACTION,
        assay_types=frozenset({AssayType.YEAST_TWO_HYBRID}),
    )
    assert decision.supported
    assert decision.reason_code == "OK_DIRECT_ASSAY"


def test_knockout_supports_causal_regulation_but_not_ptm():
    knockout = frozenset({AssayType.KNOCKOUT})
    causal = evaluate_claim_support(
        MeasurementKind.EXPERIMENTAL_ASSAY, SupportType.DIRECT, ClaimClass.CAUSAL_REGULATION, assay_types=knockout
    )
    assert causal.supported
    ptm = evaluate_claim_support(
        MeasurementKind.EXPERIMENTAL_ASSAY, SupportType.DIRECT, ClaimClass.PTM_MODIFICATION, assay_types=knockout
    )
    assert not ptm.supported
    assert ptm.reason_code == "REJECT_ASSAY_TYPE_MISMATCH"


def test_phosphoproteomics_supports_ptm_modification():
    decision = evaluate_claim_support(
        MeasurementKind.EXPERIMENTAL_ASSAY,
        SupportType.DIRECT,
        ClaimClass.PTM_MODIFICATION,
        assay_types=frozenset({AssayType.PHOSPHOPROTEOMICS}),
    )
    assert decision.supported


def test_text_mining_and_ortholog_cannot_prove_mechanism():
    text_mining = evaluate_claim_support(
        MeasurementKind.LITERATURE_ASSERTION, SupportType.TEXT_MINING, ClaimClass.CAUSAL_REGULATION
    )
    assert not text_mining.supported
    assert text_mining.reason_code == "REJECT_TEXT_MINING_CANNOT_PROVE_MECHANISM"
    ortholog = evaluate_claim_support(
        MeasurementKind.CURATED_ANNOTATION, SupportType.ORTHOLOG_INFERENCE, ClaimClass.CAUSAL_REGULATION
    )
    assert not ortholog.supported
    assert ortholog.reason_code == "REJECT_ORTHOLOG_INFERENCE_REQUIRES_DIRECT_CONFIRMATION"


def test_curated_annotation_needs_primary_assay_for_mechanism():
    decision = evaluate_claim_support(
        MeasurementKind.CURATED_ANNOTATION, SupportType.DIRECT, ClaimClass.MOLECULAR_INTERACTION
    )
    assert not decision.supported
    assert decision.reason_code == "REJECT_CURATED_ANNOTATION_NEEDS_PRIMARY_ASSAY"


def test_low_obligation_claims_pass_for_any_measurement_kind():
    for kind in MeasurementKind:
        decision = evaluate_claim_support(kind, SupportType.TEXT_MINING, ClaimClass.RECORD_FACT)
        assert decision.supported
        assert decision.reason_code == "OK_LOW_OBLIGATION"
