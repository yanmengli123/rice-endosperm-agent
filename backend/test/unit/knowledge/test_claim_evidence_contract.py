from yuxi.knowledge.contracts.schemas import claim_id, evidence_key, relation_group
from yuxi.knowledge.rendering.answer_context_builder import build_answer_context
from yuxi.knowledge.rendering.structured_renderer import render_structured_rows
from yuxi.knowledge.validation.completeness_validator import validate_completeness


def test_claim_identity_does_not_change_between_publications():
    first = claim_id("gene:GS3", "MUTANT_EFFECT", "phenotype:grain size")
    second = claim_id("gene:GS3", "MUTANT_EFFECT", "phenotype:grain size")

    assert first == second
    assert evidence_key(first, {"pmid": "12345678", "evidence_quote": "A"}) != evidence_key(
        second,
        {"pmid": "87654321", "evidence_quote": "B"},
    )


def test_relation_groups_do_not_upgrade_perturbation_to_functional_regulation():
    assert relation_group("PROMOTES_PHENOTYPE") == "FUNCTIONAL_REGULATION"
    assert relation_group("SUPPRESSES_PHENOTYPE") == "FUNCTIONAL_REGULATION"
    assert relation_group("REQUIRED_FOR") == "FUNCTIONAL_REGULATION"
    assert relation_group("MUTANT_EFFECT") == "PERTURBATION_EVIDENCE"
    assert relation_group("ASSOCIATED_WITH") == "ASSOCIATION_OR_CONTEXT"


def test_completeness_requires_claim_and_evidence_counts_to_match():
    status, warnings = validate_completeness(
        {
            "eligible_claim_count": 60,
            "returned_claim_count": 59,
            "eligible_evidence_count": 61,
            "returned_evidence_count": 61,
        }
    )

    assert status == "FAIL"
    assert len(warnings) == 1


def test_structured_renderer_reads_identifiers_from_evidence():
    rows = render_structured_rows(
        [
            {
                "claim_id": "claim-1",
                "subject": {"name": "GS3"},
                "predicate": "MUTANT_EFFECT",
                "object": {"name": "grain size"},
                "relation_group": "PERTURBATION_EVIDENCE",
                "evidence": [
                    {
                        "evidence_id": "evidence-1",
                        "pmid": "12345678",
                        "doi": "10.1000/rice",
                        "evidence_level": "HIGH",
                    }
                ],
            }
        ]
    )

    assert rows[0]["pmids"] == ["12345678"]
    assert rows[0]["dois"] == ["10.1000/rice"]
    assert rows[0]["evidence_ids"] == ["evidence-1"]


def test_llm_context_omits_publication_and_evidence_identifiers():
    context = build_answer_context(
        {
            "claims": [
                {
                    "claim_id": "claim-1",
                    "subject": {"name": "GS3"},
                    "predicate": "MUTANT_EFFECT",
                    "object": {"name": "grain size"},
                    "relation_group": "PERTURBATION_EVIDENCE",
                    "evidence": [{"evidence_id": "evidence-1"}],
                }
            ],
            "evidence": [
                {
                    "evidence_id": "evidence-1",
                    "pmid": "12345678",
                    "doi": "10.1000/rice",
                    "evidence_quote": "GS3 affects grain size (PMID: 12345678).",
                }
            ],
        }
    )

    assert "claim-1" in context
    assert "evidence-1" not in context
    assert "12345678" not in context
    assert "10.1000/rice" not in context
