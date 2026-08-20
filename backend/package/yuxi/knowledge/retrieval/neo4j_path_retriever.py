from __future__ import annotations

import asyncio
import re
from typing import Any

from yuxi.knowledge.graphs.milvus_graph_service import MilvusGraphService
from yuxi.utils.logging_config import logger

NEO4J_PATH_RETRIEVER_VERSION = "1.0"

_STOPWORDS = {
    "what",
    "which",
    "does",
    "how",
    "mechanism",
    "pathway",
    "gene",
    "genes",
    "through",
    "affect",
    "regulate",
}


def _seed_keywords(question: str) -> list[str]:
    gene_like = re.findall(
        r"(?<![A-Za-z0-9_-])(?:Os[A-Za-z0-9_-]{2,}|[A-Z][A-Z0-9_-]{2,})(?![A-Za-z0-9_-])",
        question,
    )
    if gene_like:
        return list(dict.fromkeys(gene_like))[:2]
    biological_phrases = re.findall(
        r"(?:淀粉合成|胚乳发育|籽粒大小|籽粒产量|粒型|垩白|灌浆|产量|"
        r"grain\s+(?:size|weight|yield|filling)|starch\s+synthesis)",
        question,
        flags=re.IGNORECASE,
    )
    if biological_phrases:
        return list(dict.fromkeys(biological_phrases))[:2]
    terms = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}|[\u4e00-\u9fff]{2,8}", question)
    result: list[str] = []
    for value in gene_like + terms:
        if value.casefold() in _STOPWORDS or value in result:
            continue
        result.append(value)
    return result[:3]


async def retrieve_neo4j_paths(
    *,
    question: str,
    members: list[dict[str, Any]],
    max_depth: int = 2,
    max_nodes: int = 40,
) -> dict[str, Any]:
    """Return bounded Neo4j projection paths as context, never as canonical scientific claims."""
    enabled_members = [member for member in members if member.get("graph_enabled")]
    seeds = _seed_keywords(question)
    if not enabled_members or not seeds:
        return {
            "retriever_version": NEO4J_PATH_RETRIEVER_VERSION,
            "seeds": seeds,
            "nodes": [],
            "edges": [],
            "source_status": [],
        }

    service = MilvusGraphService()

    async def query(member: dict[str, Any], seed: str):
        try:
            result = await service.query_nodes(
                member["kb_id"],
                keyword=seed,
                max_depth=min(max(int(max_depth), 1), 3),
                max_nodes=min(max(int(max_nodes), 1), 100),
                exclude_chunk=True,
            )
            return member, seed, result, None
        except Exception as exc:  # noqa: BLE001
            logger.exception("Neo4j path retrieval failed for kb=%s seed=%s", member["kb_id"], seed)
            return member, seed, {"nodes": [], "edges": []}, str(exc)

    results = await asyncio.gather(*(query(member, seed) for member in enabled_members for seed in seeds))
    node_by_id: dict[str, dict[str, Any]] = {}
    edge_by_id: dict[str, dict[str, Any]] = {}
    errors_by_kb: dict[str, list[str]] = {}
    for member, _seed, graph, error in results:
        kb_id = str(member["kb_id"])
        if error:
            errors_by_kb.setdefault(kb_id, []).append(error)
        for node in graph.get("nodes") or []:
            node_id = str(node.get("id") or (node.get("properties") or {}).get("entity_id") or "")
            if node_id:
                node_by_id[f"{kb_id}:{node_id}"] = {**node, "kb_id": kb_id}
        for edge in graph.get("edges") or []:
            edge_id = str(
                edge.get("id")
                or (edge.get("properties") or {}).get("triple_id")
                or f"{edge.get('source_id')}:{edge.get('type')}:{edge.get('target_id')}"
            )
            edge_by_id[f"{kb_id}:{edge_id}"] = {**edge, "kb_id": kb_id}
    nodes = sorted(node_by_id.values(), key=lambda item: (str(item.get("kb_id")), str(item.get("id"))))[
        : max(int(max_nodes), 1)
    ]
    selected_node_ids = {(str(item.get("kb_id")), str(item.get("id"))) for item in nodes}
    edges = [
        item
        for item in sorted(edge_by_id.values(), key=lambda value: (str(value.get("kb_id")), str(value.get("id"))))
        if (str(item.get("kb_id")), str(item.get("source_id"))) in selected_node_ids
        and (str(item.get("kb_id")), str(item.get("target_id"))) in selected_node_ids
    ][: max(int(max_nodes), 1) * 2]

    source_status = []
    for member in enabled_members:
        kb_id = str(member["kb_id"])
        errors = errors_by_kb.get(kb_id) or []
        source_status.append(
            {
                "kb_id": kb_id,
                "kb_name": member.get("kb_name") or kb_id,
                "source": "NEO4J_PROJECTION",
                "capability_status": "AVAILABLE",
                "query_status": "ERROR" if errors else "SUCCESS",
                "hit_count": sum(1 for edge in edges if str(edge.get("kb_id")) == kb_id),
                "errors": errors,
            }
        )
    return {
        "retriever_version": NEO4J_PATH_RETRIEVER_VERSION,
        "seeds": seeds,
        "nodes": nodes,
        "edges": edges,
        "source_status": source_status,
    }
