from yuxi.knowledge.validation.citation_validator import validate_narrative_citations, validate_structured_citations
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
