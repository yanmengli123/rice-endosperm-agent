from __future__ import annotations

from types import SimpleNamespace

import pytest

from yuxi.knowledge.evidence.caption_recall import caption_recall_score, rank_caption_candidates

pytestmark = [pytest.mark.unit]


def _span(index: int, label: str, quote: str):
    return SimpleNamespace(sentence_index=index, span_id=f"span_{index}", container_label=label, quote=quote)


def test_single_mutant_phenotype_prefers_direct_figure_over_comparison_and_overexpression():
    question = "请依据 CRISPR 突变体籽粒表型的图注，解释粒长变长、出现腹白垩白的原因，并注明哪个图。"
    figure_2 = _span(
        2,
        "Figure 2",
        "Figure 2 CRISPR/Cas9 mediated target mutagenesis of OsMYB73 and genotype and phenotype "
        "identification; grain length between wild-type and mutants; white-belly chalky endosperm.",
    )
    figure_5 = _span(
        5,
        "Figure 5",
        "Figure 5 CRISPR/Cas9 mutants of OsMYB73, OsNF-YB1 and OsMYB73 + OsNF-YB1 grains phenotypic evaluation.",
    )
    figure_6 = _span(6, "Figure 6", "Figure 6 Overexpression of OsMYB73 causes smaller and chalky grains.")

    ranked = rank_caption_candidates(question, [figure_5, figure_6, figure_2])

    assert [span.container_label for span in ranked] == ["Figure 2"]
    assert caption_recall_score(question, figure_2.quote) > caption_recall_score(question, figure_5.quote)


def test_unrelated_caption_question_has_no_recall_candidate():
    ranked = rank_caption_candidates(
        "请总结论文的统计方法",
        [_span(2, "Figure 2", "Figure 2 CRISPR mutant grain phenotype")],
    )
    assert ranked == []
