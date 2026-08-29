from yuxi.knowledge.validation.citation_validator import (
    NARRATIVE_CITATION_MARKER,
    sanitize_narrative_citations,
    validate_narrative_citations,
    validate_structured_citations,
)
from yuxi.knowledge.validation.claim_validator import validate_deterministic_claims


def _claim():
    evidence = {
        "evidence_id": "ev_1",
        "claim_eligible": True,
        "pmid": "12345678",
        "doi": "10.1000/example",
    }
    return {
        "claim_id": "claim_1",
        "subject": {"id": "gene_1", "name": "GS3"},
        "predicate": "MUTANT_EFFECT",
        "object": {"id": "phenotype_1", "name": "grain size"},
        "relation_group": "PERTURBATION_EVIDENCE",
        "claim_eligible": True,
        "evidence": [evidence],
    }


def test_claim_validator_requires_eligible_evidence():
    result, warnings = validate_deterministic_claims([_claim()])
    assert result["status"] == "PASS"
    assert warnings == []

    broken = _claim()
    broken["evidence"] = []
    result, warnings = validate_deterministic_claims([broken])
    assert result["status"] == "FAIL"
    assert warnings


def test_citation_validator_rejects_model_invented_identifier():
    evidence = _claim()["evidence"]
    row = {
        "claim_id": "claim_1",
        "evidence_ids": ["ev_1"],
        "pmids": ["12345678"],
        "dois": ["10.1000/example"],
    }
    result, _ = validate_structured_citations([row], evidence)
    assert result["status"] == "PASS"

    row["pmids"] = ["99999999"]
    result, warnings = validate_structured_citations([row], evidence)
    assert result["status"] == "FAIL"
    assert warnings


def test_narrative_citation_validator_reserves_identifiers_for_structured_renderer():
    result, warnings = validate_narrative_citations("GS3 has supporting evidence; see PMID: 12345678.")

    assert result["status"] == "FAIL"
    assert result["detected_identifiers"]["PMID"] == ["PMID: 12345678"]
    assert warnings


def test_narrative_citation_sanitizer_preserves_scientific_answer_and_official_ids():
    text = (
        "Wx 对应 NCBI Gene ID 4340018、RAP/MSU LOC_Os06g04200 和 UniProt Q0DEV5，"
        "编码颗粒结合型淀粉合酶 I（PMID: 12345678；DOI: 10.1000/wx-study；"
        "evidence_id: ev_12345678）。"
    )

    sanitized, result, warnings = sanitize_narrative_citations(text)

    assert "NCBI Gene ID 4340018" in sanitized
    assert "LOC_Os06g04200" in sanitized
    assert "Q0DEV5" in sanitized
    assert "编码颗粒结合型淀粉合酶 I" in sanitized
    assert "12345678" not in sanitized
    assert "10.1000/wx-study" not in sanitized
    assert "ev_12345678" not in sanitized
    assert NARRATIVE_CITATION_MARKER in sanitized
    assert result["source_status"] == "FAIL"
    assert result["status"] == "PASS"
    assert result["post_sanitization_status"] == "PASS"
    assert warnings
