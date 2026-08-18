from yuxi.knowledge.research_evidence import (
    build_evidence_semantics,
    candidate_explicitly_requested,
    classify_outcome,
    is_yield_gene_query,
    sanitize_scientific_identifier_notation,
    validate_pmid,
)


def test_pmid_is_lossless_string_and_scientific_notation_is_never_repaired():
    assert validate_pmid("39552084") == ("39552084", "VALID")
    assert validate_pmid("3.95521e+07") == (None, "INVALID_SCIENTIFIC_NOTATION")
    assert "39552100" not in sanitize_scientific_identifier_notation("PMID 3.95521e+07")


def test_yield_outcomes_are_semantically_separated():
    assert classify_outcome("grain yield") == ("DIRECT_YIELD", "FIELD_YIELD")
    assert classify_outcome("grain yield", "HIGH_TEMPERATURE") == (
        "CONDITION_SPECIFIC_YIELD",
        "FIELD_YIELD",
    )
    assert classify_outcome("1000-grain weight") == ("YIELD_COMPONENT", "THOUSAND_GRAIN_WEIGHT")
    assert classify_outcome("grain size") == ("GRAIN_MORPHOLOGY", None)
    assert classify_outcome("grain filling") == ("GRAIN_FILLING", None)


def test_sttm_construct_is_not_modeled_as_the_mirna_gene():
    semantics = build_evidence_semantics(
        source_name="STTM1432",
        source_label="Construct",
        relation_type="INCREASES",
        target_name="grain yield",
        quote="STTM1432 increased grain yield in field trials.",
        direction="POSITIVE",
    )

    assert semantics["experimental_subject_type"] == "STTM_CONSTRUCT"
    assert semantics["perturbs"] == "miR1432"
    assert semantics["inferred_gene_function"] is None


def test_intervention_relation_overrides_generic_gene_material_type():
    knockout = build_evidence_semantics(
        source_name="OsPUKI",
        source_label="Gene",
        relation_type="KNOCKOUT_EFFECT",
        target_name="grain yield",
        quote="Knockout of OsPUKI decreased grain yield.",
        direction="NEGATIVE",
    )
    overexpression = build_evidence_semantics(
        source_name="OsSGL",
        source_label="Gene",
        relation_type="OVEREXPRESSION_EFFECT",
        target_name="grain weight",
        quote="Overexpression of OsSGL increased grain weight.",
        direction="POSITIVE",
    )

    assert knockout["experimental_subject_type"] == "KNOCKOUT_LINE"
    assert knockout["subject_material"] == "OsPUKI knockout line"
    assert overexpression["experimental_subject_type"] == "OVEREXPRESSION_LINE"
    assert overexpression["subject_material"] == "OsSGL overexpression line"


def test_yield_planner_and_candidate_switch_require_explicit_query_language():
    assert is_yield_gene_query("grain yield 涉及哪些基因") is True
    assert candidate_explicitly_requested("grain yield 涉及哪些基因") is False
    assert candidate_explicitly_requested("有哪些候选 grain-yield genes") is True
