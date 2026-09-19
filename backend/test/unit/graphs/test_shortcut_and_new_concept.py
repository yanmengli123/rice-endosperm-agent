"""R5：G9 span-supports、NEW_CONCEPT 逃生口、可疑传递边检测。"""

from __future__ import annotations

from yuxi.knowledge.graphs.extraction_gates import (
    G9_SPAN_SUPPORT,
    NEW_CONCEPT_LABEL,
    apply_gates,
)
from yuxi.knowledge.graphs.extraction_units import ExtractionWindow
from yuxi.knowledge.graphs.shortcut_detector import find_shortcut_suspects, quote_mentions_entity
from yuxi.knowledge.graphs.lexicon import pre_annotate


def _window(main: str) -> ExtractionWindow:
    return ExtractionWindow(index=0, main_text=main)


def _apply(unit, window, **kwargs):
    return apply_gates(
        unit,
        window,
        pre_annotate(window.main_text),
        entity_types={
            "Gene",
            "Protein",
            "Condition",
            "Method",
            "Process",
            "Phenotype",
            "Tissue",
            "DevelopmentStage",
            "Cultivar",
            "AlleleMutant",
            "Experiment",
            "Publication",
            "Pathway",
            "QTL",
            "CisElement",
            "RNA",
        },
        relation_types={"EXPRESSION_IN", "TRANSCRIPTIONAL_ACTIVATION"},
        **kwargs,
    )


# ── R5a G9 span-supports：引文必须覆盖端点 ───────────────────────


def test_quote_missing_endpoint_routes_to_review():
    window = _window("OsPIP2;1 protein was detected in the root tip cells of the endosperm.")
    unit = {
        "entities": [
            {"surface": "OsPIP2;1", "type": "Gene"},
            {"surface": "root", "type": "Tissue"},
        ],
        "relations": [
            {
                "subject": "OsPIP2;1",
                "predicate": "EXPRESSION_IN",
                "object": "root",
                "evidence_quote": "detected in the root",  # 不含 subject surface
            }
        ],
    }
    outcome = _apply(unit, window)
    assert outcome.relations == []
    assert outcome.reviews and outcome.reviews[0]["gate_code"] == G9_SPAN_SUPPORT


def test_quote_covering_both_endpoints_still_passes():
    window = _window("OsPIP2;1 was detected in the root under ABA treatment.")
    unit = {
        "entities": [
            {"surface": "OsPIP2;1", "type": "Gene"},
            {"surface": "root", "type": "Tissue"},
        ],
        "relations": [
            {
                "subject": "OsPIP2;1",
                "predicate": "EXPRESSION_IN",
                "object": "root",
                "evidence_quote": "OsPIP2;1 was detected in the root",
            }
        ],
    }
    outcome = _apply(unit, window)
    assert outcome.relations and outcome.relations[0]["subject"] == "OsPIP2;1"
    assert outcome.reviews == []


# ── R5b NEW_CONCEPT 逃生口 ──────────────────────────────────────


def test_new_concept_entity_routes_to_review_not_rejected():
    window = _window("The soil microbiome composition shifted after straw returning.")
    unit = {
        "entities": [
            {"surface": "soil microbiome", "type": "NEW_CONCEPT", "normalized_name": "soil microbiome"},
        ],
        "relations": [],
    }
    outcome = _apply(unit, window)
    assert outcome.entities == []  # 不进图谱
    assert outcome.reviews and outcome.reviews[0]["gate_code"] == NEW_CONCEPT_LABEL
    assert outcome.reviews[0]["candidate_kind"] == "ENTITY"
    assert NEW_CONCEPT_LABEL not in outcome.stats.rejected


def test_fabricated_type_is_still_rejected():
    window = _window("Some metabolite was measured.")
    unit = {"entities": [{"surface": "metabolite", "type": "MetaboliteType"}], "relations": []}
    outcome = _apply(unit, window)
    assert outcome.entities == []
    assert outcome.reviews == []  # 编造类型仍拒（只有显式 NEW_CONCEPT 是逃生口）


# ── R5c 可疑传递边 ─────────────────────────────────────────────


def test_shortcut_detected_when_quote_never_mentions_bridge():
    triples = [
        {
            "triple_id": "t_ac",
            "source_entity_id": "a",
            "target_entity_id": "c",
            "relation_type": "REGULATES_PROCESS",
            "content": "a → c",
        },
        {
            "triple_id": "t_ab",
            "source_entity_id": "a",
            "target_entity_id": "b",
            "relation_type": "REGULATES_PROCESS",
            "content": "a → b",
        },
        {
            "triple_id": "t_bc",
            "source_entity_id": "b",
            "target_entity_id": "c",
            "relation_type": "REGULATES_PROCESS",
            "content": "b → c",
        },
    ]
    quotes = {"t_ac": ["gene a regulates phenotype c directly"]}
    tokens = {"b": {"gif1"}}
    suspects = find_shortcut_suspects(triples, quotes, tokens)
    assert len(suspects) == 1
    assert suspects[0]["triple_id"] == "t_ac"
    assert suspects[0]["bridge_entity_id"] == "b"


def test_no_shortcut_when_quote_mentions_bridge():
    triples = [
        {
            "triple_id": "t_ac",
            "source_entity_id": "a",
            "target_entity_id": "c",
            "relation_type": "REGULATES_PROCESS",
            "content": "a → c",
        },
        {
            "triple_id": "t_ab",
            "source_entity_id": "a",
            "target_entity_id": "b",
            "relation_type": "REGULATES_PROCESS",
            "content": "a → b",
        },
        {
            "triple_id": "t_bc",
            "source_entity_id": "b",
            "target_entity_id": "c",
            "relation_type": "REGULATES_PROCESS",
            "content": "b → c",
        },
    ]
    quotes = {"t_ac": ["gene a acts through GIF1 to affect phenotype c"]}
    tokens = {"b": {"gif1"}}
    assert find_shortcut_suspects(triples, quotes, tokens) == []


def test_no_bridge_no_report():
    triples = [
        {
            "triple_id": "t_ac",
            "source_entity_id": "a",
            "target_entity_id": "c",
            "relation_type": "REGULATES_PROCESS",
            "content": "a → c",
        },
    ]
    quotes = {"t_ac": ["a regulates c"]}
    assert find_shortcut_suspects(triples, quotes, {}) == []


def test_quote_mentions_entity_without_tokens_fails_open():
    assert quote_mentions_entity(set(), "anything") is True  # 无法排除 → 不报
