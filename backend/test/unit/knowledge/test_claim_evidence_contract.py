from yuxi.knowledge.contracts.schemas import claim_id, evidence_key, relation_group
from yuxi.knowledge.rendering.answer_context_builder import (
    build_answer_context,
    build_answer_output_profile,
)
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


def test_llm_context_distinguishes_claims_from_unique_genes():
    context = build_answer_context(
        {
            "claims": [
                {
                    "claim_id": "claim-1",
                    "subject": {"id": "gene-1", "name": "OsNF-YB1"},
                    "predicate": "REQUIRED_FOR",
                    "object": {"name": "endosperm development"},
                    "relation_group": "FUNCTIONAL_REGULATION",
                    "evidence": [],
                },
                {
                    "claim_id": "claim-2",
                    "subject": {"id": "gene-1", "name": "OsNF-YB1"},
                    "predicate": "RNAI_EFFECT",
                    "object": {"name": "endosperm development"},
                    "relation_group": "PERTURBATION_EVIDENCE",
                    "evidence": [],
                },
                {
                    "claim_id": "claim-3",
                    "subject": {"id": "gene-2", "name": "FLO7"},
                    "predicate": "REQUIRED_FOR",
                    "object": {"name": "endosperm development"},
                    "relation_group": "FUNCTIONAL_REGULATION",
                    "evidence": [],
                },
            ]
        }
    )

    assert '"citable_claims":3' in context
    assert '"distinct_subjects":2' in context
    assert '"FUNCTIONAL_REGULATION":{"citable_claims":2,"distinct_subjects":2}' in context
    assert '"PERTURBATION_EVIDENCE":{"citable_claims":1,"distinct_subjects":1}' in context
    assert "回答基因数量只能使用 distinct_subjects" in context
    assert "不得暴露 JSON 字段名" in context
    assert "不得称为知识库收录总数" in context


def test_llm_context_exposes_full_citation_channel_budget_and_primary_figure_rule():
    citations = [
        {
            "ref": f"E{index}",
            "filename": "paper.pdf",
            "zone": "MAIN_TEXT",
            "quote_head": f"Figure {index}. Caption {index}",
            "locatable": True,
        }
        for index in range(1, 18)
    ]

    context = build_answer_context({"claims": [], "evidence": [], "citations": citations})

    assert '"ref":"E16"' in context
    assert '"ref":"E17"' not in context
    assert "单突变体问题优先单突变体图" in context
    assert "不得用双突变体比较图、过表达图或工作模型图冒充主证据" in context


def test_figure_question_receives_parallel_answer_output_contract():
    context = build_answer_context(
        {
            "claims": [],
            "evidence": [],
            "retrieval_summary": {"query": "请解释 Figure 2 的表型，并注明依据来自哪个图。"},
        }
    )

    assert '"mode":"FIGURE_PARALLEL"' in context
    assert '"required_sections":["结论","逐图依据","正文解释","证据边界"]' in context
    assert "每张被采用的图单独一个 bullet" in context
    assert "不得把正文机制冒充图中所示" in context
    assert '"schema_version":"answer-draft.v2"' in context


def test_table_question_receives_coordinate_complete_output_contract():
    context = build_answer_context(
        {
            "claims": [],
            "evidence": [],
            "retrieval_summary": {"query": "依据 Table 1 和 Table 2 比较热胁迫下各基因型。"},
        }
    )

    assert '"mode":"TABLE_PARALLEL"' in context
    assert '"required_sections":["结论","逐表数据","数据含义","证据边界"]' in context
    assert "对象、条件、指标、值和单位" in context
    assert "成对操作数齐全时输出" in context


def test_phenotype_word_alone_does_not_trigger_table_profile():
    context = build_answer_context(
        {
            "claims": [],
            "evidence": [],
            "retrieval_summary": {"query": "解释突变体籽粒表型变化的原因。"},
        }
    )

    assert '"mode":"STANDARD"' in context
    assert "逐表数据" not in context


def test_mixed_figure_table_question_requires_both_evidence_sections():
    context = build_answer_context(
        {
            "claims": [],
            "evidence": [],
            "retrieval_summary": {"query": "结合 Figure 2 和 Table 1 解释差异。"},
        }
    )

    assert '"mode":"FIGURE_TABLE_PARALLEL"' in context
    assert '"required_sections":["结论","逐图依据","逐表数据","综合解释","证据边界"]' in context


def test_frozen_answer_output_profile_is_reusable_in_contract_hash_input():
    contract = {
        "retrieval_summary": {"query": "Figure 2 展示什么？"},
        "answer_output": {
            "schema": "answer-output-profile.v1",
            "mode": "STANDARD",
            "required_sections": [],
        },
    }

    assert build_answer_output_profile(contract)["mode"] == "FIGURE_PARALLEL"
    assert '"mode":"STANDARD"' in build_answer_context(contract)
