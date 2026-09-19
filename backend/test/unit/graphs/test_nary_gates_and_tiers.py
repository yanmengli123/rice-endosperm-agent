"""P0-b/P1：N 元组门禁三路由、mention 列化、谓词分级与检索剪枝。"""

from __future__ import annotations

from yuxi.knowledge.graphs.extraction_gates import (
    G7_TRIGGER_UNVERIFIED,
    G9_NEGATION_REVIEW,
    GateStats,
    apply_gates,
)
from yuxi.knowledge.graphs.extraction_units import ExtractionWindow
from yuxi.knowledge.graphs.extractors.llm_scientific import LLMScientificGraphExtractor
from yuxi.knowledge.graphs.graph_utils import (
    TIER_A,
    TIER_B,
    TIER_C,
    condition_key,
    derive_mention_polarity,
    predicate_tier,
)
from yuxi.knowledge.graphs.lexicon import pre_annotate
from yuxi.knowledge.retrieval.neo4j_path_retriever import (
    apply_hub_policy,
    budget_filter,
    resolve_graph_expansion_policy,
)
from yuxi.repositories.knowledge_graph_repository import build_triple_mention_rows


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
        relation_types={
            "TRANSCRIPTIONAL_REGULATION",
            "PROMOTES_PROCESS",
            "INHIBITS_PROCESS",
            "EXPRESSION_IN",
            "OBSERVED_BY",
            "UNDER_CONDITION",
        },
        **kwargs,
    )


# ── G9 否定送审（三路由：PASS/REVIEW/DROP）────────────────────────


def test_negation_without_negative_polarity_routes_to_review():
    window = _window("OsPIP2;1 did not localize to the plasma membrane under drought.")
    unit = {
        "entities": [
            {"surface": "OsPIP2;1", "type": "Gene", "normalized_name": "OsPIP2;1"},
            {"surface": "drought", "type": "Condition", "normalized_name": "drought"},
        ],
        "relations": [
            {
                "subject": "OsPIP2;1",
                "predicate": "EXPRESSION_IN",
                "object": "drought",
                "evidence_quote": "OsPIP2;1 did not localize to the plasma membrane under drought",
                "confidence": 0.9,
                "hedge": False,
                "context": {"condition": "drought"},
            }
        ],
    }
    outcome = _apply(unit, window)
    assert outcome.relations == []
    assert outcome.reviews and outcome.reviews[0]["gate_code"] == G9_NEGATION_REVIEW
    assert outcome.stats.review_routed.get(G9_NEGATION_REVIEW) == 1
    assert outcome.stats.to_metadata()["review_total"] == 1


def test_negation_with_negative_polarity_passes():
    window = _window("OsPIP2;1 did not localize to the plasma membrane under drought.")
    unit = {
        "entities": [
            {"surface": "OsPIP2;1", "type": "Gene", "normalized_name": "OsPIP2;1"},
            {"surface": "drought", "type": "Condition", "normalized_name": "drought"},
        ],
        "relations": [
            {
                "subject": "OsPIP2;1",
                "predicate": "EXPRESSION_IN",
                "object": "drought",
                "evidence_quote": "OsPIP2;1 did not localize to the plasma membrane under drought",
                "confidence": 0.9,
                "hedge": False,
                "context": {"condition": "drought", "polarity": "negative"},
            }
        ],
    }
    outcome = _apply(unit, window)
    assert outcome.relations and outcome.relations[0]["context"]["polarity"] == "negative"
    assert outcome.reviews == []


def test_strict_trigger_failure_routes_to_review_not_drop():
    # 句中无 PROMOTES_PROCESS 触发词（starch 只是被提及）
    window = _window("The starch content and OsNF-YB1 protein were measured in the endosperm.")
    unit = {
        "entities": [
            {"surface": "OsNF-YB1", "type": "Gene", "normalized_name": "OsNF-YB1"},
            {"surface": "endosperm", "type": "Tissue", "normalized_name": "endosperm"},
        ],
        "relations": [
            {
                "subject": "OsNF-YB1",
                "predicate": "PROMOTES_PROCESS",
                "object": "endosperm",
                "evidence_quote": "OsNF-YB1 protein were measured in the endosperm",
                "confidence": 0.5,
                "hedge": False,
                "context": {},
            }
        ],
    }
    outcome = _apply(unit, window, strict_triggers=True)
    assert outcome.relations == []
    assert outcome.reviews and outcome.reviews[0]["gate_code"] == G7_TRIGGER_UNVERIFIED
    # 老行为是静默 DROP：rejected 计数；现在改为 REVIEW：不进 rejected
    assert G7_TRIGGER_UNVERIFIED not in outcome.stats.rejected


def test_context_polarity_validation_drops_invalid_values():
    window = _window("OsPIP2;1 protein was detected in the root under ABA treatment.")
    unit = {
        "entities": [
            {"surface": "OsPIP2;1", "type": "Gene", "normalized_name": "OsPIP2;1"},
            {"surface": "root", "type": "Tissue", "normalized_name": "root"},
        ],
        "relations": [
            {
                "subject": "OsPIP2;1",
                "predicate": "EXPRESSION_IN",
                "object": "root",
                "evidence_quote": "OsPIP2;1 protein was detected in the root",
                "confidence": 0.9,
                "hedge": False,
                "context": {"polarity": "very-positive", "magnitude": "huge", "condition": "ABA"},
            }
        ],
    }
    outcome = _apply(unit, window)
    context = outcome.relations[0]["context"]
    assert "polarity" not in context  # 非法枚举丢弃
    assert "magnitude" not in context
    assert context["condition"] == "ABA"


def test_hallucination_rate_denominator_includes_review_routed():
    stats = GateStats()
    stats.relation_candidates = 10
    stats.rejected["G2_VERBATIM_QUOTE"] = 2
    stats.review_routed[G9_NEGATION_REVIEW] = 3
    metadata = stats.to_metadata()
    assert metadata["hallucination_rate"] == 0.2
    assert metadata["review_total"] == 3


# ── 极性推导与条件键（D6 冲突判定基元）───────────────────────────


def test_derive_mention_polarity_priority_chain():
    assert derive_mention_polarity("REGULATES_PROCESS", {"polarity": "negative"}) == "negative"
    assert derive_mention_polarity("REGULATES_PROCESS", {"direction": "inhibits"}) == "negative"
    assert derive_mention_polarity("REGULATES_PROCESS", {"direction": "activates"}) == "positive"
    assert derive_mention_polarity("TRANSCRIPTIONAL_REPRESSION", {}) == "negative"
    assert derive_mention_polarity("PROMOTES_PROCESS", {}) == "positive"
    assert derive_mention_polarity("REGULATES_PROCESS", {}) is None


def test_condition_key_separates_comparison_from_conflict():
    assert condition_key("HT") != condition_key("CT")
    assert condition_key(None) == condition_key("") == "_"
    assert condition_key(" 高温 ") == condition_key("高温")


# ── mention 行 N 元组列化 + 对比组（仓储纯函数）────────────────────


def _triple(triple_id, source_entity_id, relation_type, context):
    return {
        "triple_id": triple_id,
        "source_entity_id": source_entity_id,
        "relation_type": relation_type,
        "text": "quote",
        "context": context,
    }


def test_mention_rows_carry_nary_columns():
    rows = build_triple_mention_rows(
        "kb1",
        "file1",
        "chunk1",
        [
            _triple(
                "t1",
                "gene-1",
                "TRANSCRIPTIONAL_REGULATION",
                {"condition": "高温", "baseline": "常温", "polarity": "Positive", "magnitude": "significant"},
            )
        ],
    )
    row = rows[0]
    assert row["condition_text"] == "高温"
    assert row["condition_entity_id"] and len(row["condition_entity_id"]) == 32
    assert row["baseline_text"] == "常温"
    assert row["polarity"] == "positive"  # 归一小写
    assert row["magnitude"] == "significant"


def test_comparison_group_binds_ct_ht_pair_from_same_statement():
    rows = build_triple_mention_rows(
        "kb1",
        "file1",
        "chunk1",
        [
            _triple("t1", "gene-1", "EXPRESSION_IN", {"condition": "常温", "polarity": "negative"}),
            _triple("t2", "gene-1", "EXPRESSION_IN", {"condition": "高温", "polarity": "positive"}),
            _triple("t3", "gene-2", "EXPRESSION_IN", {"condition": "高温", "polarity": "positive"}),
        ],
    )
    by_id = {row["triple_id"]: row for row in rows}
    assert by_id["t1"]["comparison_group_id"] is not None
    assert by_id["t1"]["comparison_group_id"] == by_id["t2"]["comparison_group_id"]
    assert by_id["t3"]["comparison_group_id"] is None  # 不同 subject 不组队


# ── D9 谓词分级与 hub 剪枝 ──────────────────────────────────────


def test_predicate_tiers_classify_closed_set():
    assert predicate_tier("TRANSCRIPTIONAL_ACTIVATION") == TIER_A
    assert predicate_tier("EXPRESSION_IN") == TIER_B
    assert predicate_tier("OBSERVED_BY") == TIER_C
    assert predicate_tier("UNDER_CONDITION") == TIER_C
    assert predicate_tier("SOME_IMPORTED_RELATION") == TIER_B  # 未知谓词可用不优先


def test_hub_policy_uses_real_full_graph_degrees():
    # R3 修复：治理依据是全图真实度数（real_degrees），不是召回集内统计
    hub = "hub-node"
    edges = [
        {
            "id": str(index),
            "source_id": hub,
            "target_id": f"n{index}",
            "type": "EXPRESSION_IN",
            "properties": {"type": "EXPRESSION_IN"},
        }
        for index in range(6)
    ] + [
        {
            "id": "a1",
            "source_id": hub,
            "target_id": "n900",
            "type": "TRANSCRIPTIONAL_ACTIVATION",
            "properties": {"type": "TRANSCRIPTIONAL_ACTIVATION"},
        },
    ]
    governed, hubs = apply_hub_policy(edges, {hub: 12_000}, hub_cap=200)
    assert hubs == {hub}
    assert len(governed) == 1
    assert governed[0]["predicate_tier"] == TIER_A


def test_hub_policy_fail_open_when_degree_unknown():
    edges = [
        {
            "id": "1",
            "source_id": "a",
            "target_id": "b",
            "type": "EXPRESSION_IN",
            "properties": {"type": "EXPRESSION_IN"},
        },
    ]
    governed, hubs = apply_hub_policy(edges, {}, hub_cap=200)  # 度数查询失败 → 不治理
    assert hubs == set()
    assert len(governed) == 1
    assert governed[0]["predicate_tier"] == TIER_B


def test_budget_policy_resolves_with_clamps_and_auto_hub_cap():
    default_policy = resolve_graph_expansion_policy(None)
    assert default_policy.hop_budget == 2
    assert default_policy.effective_hub_cap() == max(50, default_policy.node_budget * 2)

    custom = resolve_graph_expansion_policy(
        {
            "graph_expansion": {
                "hop_budget": 9,
                "node_budget": 30,
                "hub_cap": 500,
                "tier_whitelist": ["TIER_A", "TIER_X"],
            }
        }
    )
    assert custom.hop_budget == 3  # 收敛到安全区间
    assert custom.node_budget == 30
    assert custom.effective_hub_cap() == 500
    assert custom.tier_whitelist == (TIER_A,)  # TIER_C/X 不入白名单

    broken = resolve_graph_expansion_policy({"graph_expansion": {"hop_budget": "bad"}})
    assert broken.hop_budget == 2  # 坏值回落默认


def test_budget_filter_trims_edges_and_keeps_nodes_consistent():
    nodes = [{"id": str(index), "kb_id": "kb1"} for index in range(10)]
    edges = [
        {
            "id": str(index),
            "kb_id": "kb1",
            "source_id": str(index % 10),
            "target_id": "0",
            "type": "EXPRESSION_IN",
            "predicate_tier": TIER_B,
        }
        for index in range(50)
    ]
    final_nodes, final_edges = budget_filter(nodes, edges, node_budget=5, edge_budget=10)
    assert len(final_nodes) == 5
    assert len(final_edges) <= 10
    node_ids = {(str(node.get("kb_id")), str(node.get("id"))) for node in final_nodes}
    assert all(
        (edge.get("kb_id"), edge.get("source_id")) in node_ids
        and (edge.get("kb_id"), edge.get("target_id")) in node_ids
        for edge in final_edges
    )


# ── Pass 2 prompt 注入（文档词典）────────────────────────────────


def test_doclex_entries_injected_into_prompt_per_window():
    extractor = LLMScientificGraphExtractor({"model_spec": "gpt-test"})
    window = ExtractionWindow(index=0, main_text="该品种在高温下灌浆速率下降。")
    prompt = extractor.build_prompt(
        [window],
        {0: pre_annotate(window.main_text)},
        [{"surface": "该品种", "resolved_name": "秋田小町", "resolved_label": "Cultivar", "kind": "COREFERENCE"}],
    )
    assert "文档词典" in prompt
    assert "该品种=秋田小町" in prompt


def test_prompt_without_doclex_entries_has_no_dictionary_block():
    extractor = LLMScientificGraphExtractor({"model_spec": "gpt-test"})
    window = ExtractionWindow(index=0, main_text="OsPIP2;1 was expressed in the root.")
    prompt = extractor.build_prompt([window], {0: []})
    assert "文档词典（本文档内" not in prompt
    assert "文档词典（本单元相关）：无" in prompt
