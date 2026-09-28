from __future__ import annotations

import pytest

from yuxi.knowledge.rendering.figure_ref_channel import build_caption_registry
from yuxi.knowledge.validation.figure_claim_validator import (
    REASON_CAPTION_SEMANTIC_MISMATCH,
    REASON_EXPERIMENT_CONDITION_MISMATCH,
    REASON_INTERNAL_COMPARISON_CONTRADICTION,
    REASON_NO_REGISTRY,
    REASON_PANEL_SEMANTIC_MISMATCH,
    validate_figure_claims,
)

pytestmark = [pytest.mark.unit]


def _citation(ref: str, label: str, caption: str) -> dict:
    quote = f"{label}. {caption}"
    return {
        "ref": ref,
        "evidence_id": f"ev_{ref}",
        "kb_id": "kb_1",
        "file_id": "file_1",
        "filename": "paper.pdf",
        "zone": "MAIN_TEXT",
        "page_numbers": [8],
        "quote_head": quote,
        "anchor_ids": [f"ea_{ref}"],
        "locatable": True,
        "toc_line": False,
        "_quote": quote,
        "_anchor_id": f"ea_{ref}",
        "_parse_revision_id": "rev_1",
        "_evidence_type": "caption",
    }


def _validate(text: str, citations: list[dict]):
    return validate_figure_claims(text, registry=build_caption_registry(citations), citations=citations)


def test_rejects_overexpression_claim_for_mutant_morphology_caption():
    citations = [_citation("E1", "Figure S4", "Comparison of plant morphology between WT and cr-myb73 mutants.")]
    cleaned, report = _validate("Figure S4 展示 OsMYB73 过表达系籽粒表型。", citations)
    assert cleaned == ""
    assert report["status"] == "DEGRADED"
    assert report["claims"][0]["reason_code"] == REASON_CAPTION_SEMANTIC_MISMATCH


def test_rejects_double_mutant_phenotype_claim_for_starch_particle_caption():
    citations = [_citation("E1", "Figure S6", "Rice starch particles of WT and cr-myb73 mutants.")]
    cleaned, report = _validate("Figure S6 展示了 OsMYB73 与其他基因的双突变体籽粒表型。", citations)
    assert cleaned == ""
    assert report["unsupported_count"] == 1


def test_accepts_double_mutant_claim_when_caption_encodes_genotype_pairs():
    citations = [
        _citation(
            "E1",
            "Figure 5",
            "Phenotypic evaluation of OsMYB73 + OsNF-YB1, OsMYB73 + OsISA2 and OsMYB73 + OsLTPL36 mutant grains.",
        )
    ]
    text = "Figure 5 比较了 OsMYB73 与上述基因的双突变体籽粒表型。"
    cleaned, report = _validate(text, citations)
    assert cleaned == text
    assert report["status"] == "PASS"


def test_rejects_heat_claim_when_table_caption_has_no_heat_condition():
    citations = [_citation("E1", "Table 1", "Physicochemical properties of rice grains across genotypes.")]
    cleaned, report = _validate("Table 1 显示热胁迫下各材料的直链淀粉和糊化温度变化。", citations)
    assert cleaned == ""
    assert report["claims"][0]["reason_code"] == REASON_EXPERIMENT_CONDITION_MISMATCH


def test_accepts_caption_compatible_claim():
    citations = [_citation("E1", "Figure S5", "SEM and TEM observations of mature endosperm in ZH11 and mutants.")]
    text = "Figure S5 展示了成熟胚乳的扫描电镜和透射电镜图像。"
    cleaned, report = _validate(text, citations)
    assert cleaned == text
    assert report["status"] == "PASS"


def test_navigation_only_mention_is_not_semantically_rejected():
    cleaned, report = _validate("详细结果见 Figure 99。", [])
    assert cleaned == "详细结果见 Figure 99。"
    assert report["checked_claim_count"] == 0


def test_assertive_unregistered_figure_is_removed():
    cleaned, report = _validate("Figure S8 展示了双突变体籽粒表型。", [])
    assert cleaned == ""
    assert report["claims"][0]["reason_code"] == REASON_NO_REGISTRY


def test_compact_supplementary_summary_is_checked():
    citations = [_citation("E1", "Figure S4", "Comparison of plant morphology between WT and cr-myb73 mutants.")]
    cleaned, report = _validate("综上可归纳为：S4（过表达反向证据）支持核心结论。", citations)
    assert cleaned == ""
    assert report["unsupported_count"] == 1


def test_reference_source_list_is_preserved_without_asserting_semantics():
    text = "依据来源：Figure S4、Figure S5。"
    cleaned, report = _validate(text, [])
    assert cleaned == text
    assert report["checked_claim_count"] == 0


def test_other_citation_cannot_launder_wrong_figure_identity():
    citations = [
        _citation("E1", "Figure S4", "Comparison of plant morphology between WT and cr-myb73 mutants."),
        _citation("E2", "Figure 6", "Phenotypes of OsMYB73 overexpression lines."),
    ]

    cleaned, report = _validate("Figure S4 展示 OsMYB73 过表达系籽粒表型。[E2]", citations)

    assert cleaned == ""
    assert report["claims"][0]["reason_code"] == REASON_CAPTION_SEMANTIC_MISMATCH


def test_rejects_opposite_phenotype_claim_when_both_sides_are_chalky():
    citations = [_citation("E1", "Figure 6", "Overexpression of OsMYB73 causes smaller and chalky grains.")]
    cleaned, report = _validate(
        "Figure 6 显示过表达籽粒变小且呈垩白，与敲除后粒长变长、腹白垩白形成方向相反的表型。",
        citations,
    )

    assert cleaned == ""
    assert report["claims"][0]["reason_code"] == REASON_INTERNAL_COMPARISON_CONTRADICTION


def test_rejects_universal_difference_inherited_from_previous_figure_sentence():
    citations = [
        _citation(
            "E1",
            "Figure 2",
            "Grain length, filling rate, grain width, thousand-grain weight, "
            "tillers and plant height between WT and mutants.",
        )
    ]
    cleaned, report = _validate(
        "Figure 2 系统比较了粒长、灌浆速率、粒宽、千粒重、分蘖数与株高。所有指标均存在突变体相对野生型的差异。",
        citations,
    )
    assert "系统比较" in cleaned
    assert "所有指标" not in cleaned
    assert report["unsupported_count"] == 1


def test_panel_metric_mapping_is_checked_against_caption():
    citations = [
        _citation(
            "E1",
            "Figure 2",
            "(e) grain length between WT and mutants; (f) grain filling rate; (h) grain width; (i) 1000-grain weight.",
        )
    ]
    valid = "Figure 2 比较了粒长（panel e）与灌浆速率（panel f）。"
    cleaned, report = _validate(valid, citations)
    assert cleaned == valid
    assert report["status"] == "PASS"

    cleaned, report = _validate("Figure 2 的粒长位于 panel h。", citations)
    assert cleaned == ""
    assert report["claims"][0]["reason_code"] == REASON_PANEL_SEMANTIC_MISMATCH


def test_multi_figure_summary_validates_against_union_of_range_captions():
    citations = [
        _citation("E1", "Figure S17", "Venn diagram of differential metabolites."),
        _citation("E2", "Figure S18", "KEGG classification of differential metabolites."),
        _citation("E3", "Figure S19", "KEGG enrichment of differential metabolites."),
        _citation("E4", "Figure S20", "GO and KEGG annotation of peak-related genes."),
    ]
    text = "综上，Figure S17、Figure S18、Figure S19、Figure S20 主要从代谢组和基因注释角度共同支持主结论。"
    cleaned, report = _validate(text, citations)
    assert cleaned == text
    assert report["status"] == "PASS"
    assert report["checked_claim_count"] == 4


def test_named_kegg_figure_cannot_inherit_auxin_identity_from_other_evidence():
    citations = [_citation("E1", "Figure S18", "KEGG classification bar chart of differential metabolites.")]

    cleaned, report = _validate(
        "Figure S18 展示生长素合成通路差异，支持 OsMYB73 的生长素调控机制。",
        citations,
    )

    assert cleaned == ""
    assert report["unsupported_count"] == 1
    assert report["claims"][0]["missing_concepts"] == ["auxin"]
