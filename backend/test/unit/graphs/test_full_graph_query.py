"""全图查询收尾逻辑单测：截断判定、端点过滤、硬上限保护、语义去重。"""

from yuxi.knowledge.graphs.milvus_graph_service import (
    FULL_GRAPH_EDGE_CAP,
    FULL_GRAPH_NODE_CAP,
    _dedupe_edges_by_semantic_key,
    _finalize_full_graph_result,
)


def _node(node_id: str) -> dict:
    return {"id": node_id, "name": node_id, "type": "Entity"}


def _edge(edge_id: str, source_id: str, target_id: str, *, triple_id: str | None = None) -> dict:
    properties = {"triple_id": triple_id} if triple_id else {}
    return {
        "id": edge_id,
        "source_id": source_id,
        "target_id": target_id,
        "type": "RELATES_TO",
        "properties": properties,
    }


def test_finalize_keeps_everything_under_caps():
    nodes = [_node("a"), _node("b")]
    edges = [_edge("e1", "a", "b")]

    result = _finalize_full_graph_result(nodes, edges, FULL_GRAPH_NODE_CAP, FULL_GRAPH_EDGE_CAP)

    assert result["truncated"] is False
    assert len(result["nodes"]) == 2
    assert len(result["edges"]) == 1


def test_finalize_flags_and_applies_node_cap():
    # 查询层按 cap+1 取数：cap+1 个节点即视为触顶
    nodes = [_node(f"n{i}") for i in range(FULL_GRAPH_NODE_CAP + 1)]
    edges = [_edge("e1", "n0", "n1")]

    result = _finalize_full_graph_result(nodes, edges, FULL_GRAPH_NODE_CAP, FULL_GRAPH_EDGE_CAP)

    assert result["truncated"] is True
    assert len(result["nodes"]) == FULL_GRAPH_NODE_CAP


def test_finalize_flags_edge_cap_and_filters_dangling_edges():
    nodes = [_node("a"), _node("b")]
    edges = [_edge(f"e{i}", "a", "b") for i in range(FULL_GRAPH_EDGE_CAP + 1)]
    edges.append(_edge("dangling", "a", "missing_node"))

    result = _finalize_full_graph_result(nodes, edges, FULL_GRAPH_NODE_CAP, FULL_GRAPH_EDGE_CAP)

    assert result["truncated"] is True
    # 端点不在节点集的边被剔除，其余截断到上限
    assert len(result["edges"]) == FULL_GRAPH_EDGE_CAP
    assert all(edge["target_id"] != "missing_node" for edge in result["edges"])


def test_dedupe_prefers_triple_id_and_falls_back_to_semantic_key():
    # 同一 triple_id 的多条来源边（不同 chunk 投影）折叠为一条
    edges = [
        _edge("e1", "a", "b", triple_id="t1"),
        _edge("e2", "a", "b", triple_id="t1"),
        _edge("e3", "a", "b", triple_id="t2"),
        # 无 triple_id 时按 (source, type, target) 去重
        _edge("e4", "a", "c"),
        _edge("e5", "a", "c"),
        _edge("e6", "a", "c", triple_id="t3"),
    ]

    deduped = _dedupe_edges_by_semantic_key(edges)

    assert [edge["id"] for edge in deduped] == ["e1", "e3", "e4", "e6"]
