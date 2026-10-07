"""可视化子图的计数、截断与结构身份回归。"""

from yuxi.knowledge.graphs.milvus_graph_service import MilvusGraphService

import pytest


def node(identifier, label="Gene"):
    return {
        "id": identifier,
        "labels": ["Entity", "MilvusKB", "kb_test"],
        "properties": {"label": label, "entity_id": f"entity-{identifier}"},
    }


def edge(identifier, predicate="REGULATES", triple_id="t1", status="APPROVED"):
    return {
        "id": identifier,
        "source_id": "a",
        "target_id": "b",
        "type": predicate,
        "properties": {"triple_id": triple_id, "review_status": status},
    }


def test_subgraph_counts_a_fact_once_across_chunks():
    service = MilvusGraphService()
    result = service._process_subgraph_record(
        {"nodes": [node("a"), node("b", "Phenotype")], "edges": [edge("e1"), edge("e2"), edge("e3")]}, 10, "kb_test"
    )
    assert len(result["edges"]) == 1
    assert result["truncated"] is False


def test_retrieval_subgraph_keeps_existing_source_edge_weights():
    service = MilvusGraphService()
    result = service._process_subgraph_record(
        {"nodes": [node("a"), node("b")], "edges": [edge("e1"), edge("e2"), edge("e3")]},
        10,
        "kb_test",
        dedupe_facts=False,
    )
    assert len(result["edges"]) == 3


def test_edge_budget_reports_truncation_when_node_budget_is_not_full():
    service = MilvusGraphService()
    result = service._process_subgraph_record(
        {"nodes": [node("a"), node("b")], "edges": [edge(f"e{i}", triple_id=f"t{i}") for i in range(21)]}, 10, "kb_test"
    )
    assert len(result["nodes"]) == 2
    assert len(result["edges"]) == 20
    assert result["truncated"] is True
    assert "edge_limit" in result["truncation_reasons"]


def test_rejected_candidates_do_not_consume_approved_edge_budget():
    service = MilvusGraphService()
    result = service._process_subgraph_record(
        {
            "nodes": [node("a"), node("b")],
            "edges": [edge(f"c{i}", triple_id=f"c{i}", status="CANDIDATE") for i in range(21)] + [edge("approved")],
        },
        10,
        "kb_test",
        review_policy="approved_only",
    )
    assert [item["properties"]["triple_id"] for item in result["edges"]] == ["t1"]
    assert result["truncated"] is False


def test_node_and_query_budgets_are_exposed():
    service = MilvusGraphService()
    result = service._process_subgraph_record(
        {
            "nodes": [node("a"), node("b"), node("c")],
            "edges": [],
            "seed_limit_reached": True,
            "path_limit_reached": True,
        },
        2,
        "kb_test",
    )
    assert result["truncated"] is True
    assert set(result["truncation_reasons"]) == {"node_limit", "seed_limit", "path_limit"}


def test_open_business_labels_do_not_replace_structural_roles():
    service = MilvusGraphService()
    entity = service._normalize_node(node("a", "Chunk"), "kb_test")
    relation = service._normalize_edge({**edge("e1", "MENTIONS"), "relationship_type": "RELATION"})
    assert entity["node_kind"] == "entity"
    assert entity["entity_id"] == "entity-a"
    assert relation["edge_kind"] == "relation"
    assert relation["triple_id"] == "t1"


@pytest.mark.parametrize("dedupe_facts", [True, False])
def test_visualization_subgraph_uses_database_timeout_without_changing_retrieval(dedupe_facts):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from neo4j import Query

    session = MagicMock()
    session.run.return_value.single.return_value = {"nodes": [], "edges": []}
    driver = MagicMock()
    driver.session.return_value.__enter__.return_value = session
    service = MilvusGraphService(neo4j_connection=SimpleNamespace(driver=driver))
    service._query_nodes_sync("kb_test", "kb_test", "*", 10, 1, True, "approved_only", dedupe_facts)
    query = session.run.call_args.args[0]
    if dedupe_facts:
        assert isinstance(query, Query)
        assert 0 < query.timeout < 30
    else:
        assert isinstance(query, str)
