from __future__ import annotations

import pytest

from yuxi.knowledge.planning.scientific_intent import (
    ScientificAction,
    ScientificDeliverable,
    parse_scientific_intent,
)
from yuxi.knowledge.planning.turn_execution_plan import Capability, TaskIntent, plan_turn


@pytest.mark.parametrize(
    "question",
    [
        "Wx的在NCBI上给我官网地址",
        "通过MCP服务，Wx的在NCBI上给我官网地址",
        "查一下 Wx 的 NCBI 官方主页",
    ],
)
def test_ncbi_official_link_variants_have_one_deterministic_frame(question: str):
    frame = parse_scientific_intent(question)
    assert frame.action == ScientificAction.OFFICIAL_LINK
    assert frame.entity == "Wx"
    assert frame.organism == "Oryza sativa"
    assert frame.provider == "NCBI"
    assert frame.deliverable == ScientificDeliverable.LINK_CARD

    plan = plan_turn(
        question,
        has_knowledge_scope=False,
        configured_mcps=["gene-authority", "ricekb", "ricekb-profile"],
    )
    assert plan.task.primary_intent == TaskIntent.OFFICIAL_LINK
    assert plan.required_server == "gene-authority"
    assert plan.required_capabilities == [Capability.OFFICIAL_LINK_LOOKUP]
    assert plan.answer.mode == "MCP_VALUE_ONLY"


@pytest.mark.parametrize(
    ("question", "sequence_type"),
    [
        ("Wx的CDS序列给我", "cds"),
        ("Wx 的 CDS 序列给我", "cds"),
        ("Wx的转录本序列给我", "transcript"),
        ("Wx的蛋白序列给我", "protein"),
    ],
)
def test_chinese_adjacent_sequence_types_are_stable(question: str, sequence_type: str):
    frame = parse_scientific_intent(question)
    assert frame.action == ScientificAction.SEQUENCE_EXPORT
    assert frame.entity == "Wx"
    assert frame.sequence_type == sequence_type


def test_full_dossier_detail_level_is_explicit():
    frame = parse_scientific_intent("通过 MCP 查 Wx，给我详细的基因档案")
    assert frame.action == ScientificAction.FULL_GENE_DOSSIER
    assert frame.detail_level == "FULL"
