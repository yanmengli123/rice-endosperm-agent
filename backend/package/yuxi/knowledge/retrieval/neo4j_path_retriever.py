"""Neo4j 投影路径检索：有界多跳上下文（永不作为 canonical claim）。

R3 检索期治理（修正 P1 版「召回集内统计度数」的空转缺陷）：

- **两段 hub 治理**：第一段照常召回子图；对召回集内度数较高的候选节点，
  批量查一次 Neo4j **全图真实度数**；真实度数超过预算感知 cap 的超级 hub，
  只保留其 TIER_A（因果/调控）边——「水稻」这类万边节点的无关邻域不再灌进
  LLM 上下文，而被截断的 TIER_A 主力邻域由定向 1 跳补查询回填。
- **预算策略**（``GraphExpansionPolicy``）：hop/node 预算、TIER 白名单、hub cap
  可由调用方注入（数据落 KnowledgeRetrievalPolicyRevision.policy_json）；
  hub cap 默认 ``max(50, node_budget * 2)``——hub 的定义是「邻域规模超出本次
  预算可承载的节点」，不是固定数。
- TIER_C 结构边（OBSERVED_BY/UNDER_CONDITION）保留为多跳中继并带
  ``predicate_tier`` 标注；渲染层将其降级为方法/条件附注（answer_context_builder
  分流 structural_edges），永不作为证据行。
"""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from yuxi.knowledge.graphs.graph_utils import TIER_A, TIER_B, predicate_tier
from yuxi.knowledge.graphs.milvus_graph_service import MilvusGraphService
from yuxi.utils.logging_config import logger

NEO4J_PATH_RETRIEVER_VERSION = "1.2"
# 召回集内达到该度数的节点才会触发全图真实度数核查（省查询）
HUB_CANDIDATE_LOCAL_DEGREE = 8
# 定向补查每个 hub 的 TIER_A 边上限
TIER_A_BACKFILL_LIMIT = 30

# 允许进入扩展白名单的 tier（TIER_C 永不入白名单：结构边只作中继，由渲染层降级）
_ALLOWED_POLICY_TIERS = (TIER_A, TIER_B)


@dataclass(frozen=True)
class GraphExpansionPolicy:
    """graph_expansion 检索预算策略（来源：KnowledgeRetrievalPolicyRevision.policy_json）。"""

    hop_budget: int = 2
    node_budget: int = 40
    edge_budget: int = 80
    tier_whitelist: tuple[str, ...] = (TIER_A, TIER_B)
    # hub_cap=None 表示预算感知：max(50, node_budget * 2)
    hub_cap: int | None = None

    def effective_hub_cap(self) -> int:
        return int(self.hub_cap) if self.hub_cap is not None else max(50, self.node_budget * 2)


def resolve_graph_expansion_policy(policy_json: dict[str, Any] | None) -> GraphExpansionPolicy:
    """宽容解析策略 JSON：未知字段忽略、越界值收敛到安全区间（策略坏了不炸检索）。"""
    raw = policy_json if isinstance(policy_json, dict) else {}
    graph = raw.get("graph_expansion") if isinstance(raw.get("graph_expansion"), dict) else raw

    def _clamp_int(key: str, default: int, low: int, high: int) -> int:
        try:
            value = int(graph.get(key, default))
        except (TypeError, ValueError):
            return default
        return max(low, min(high, value))

    tiers_raw = graph.get("tier_whitelist")
    tiers = (
        tuple(tier for tier in (str(item).upper() for item in tiers_raw) if tier in _ALLOWED_POLICY_TIERS)
        if isinstance(tiers_raw, (list, tuple))
        else ()
    )
    return GraphExpansionPolicy(
        hop_budget=_clamp_int("hop_budget", 2, 1, 3),
        node_budget=_clamp_int("node_budget", 40, 10, 100),
        edge_budget=_clamp_int("edge_budget", 80, 10, 400),
        tier_whitelist=tiers or (TIER_A, TIER_B),
        hub_cap=_clamp_int("hub_cap", -1, 10, 100_000) if graph.get("hub_cap") is not None else None,
    )


def apply_hub_policy(
    edges: list[dict[str, Any]],
    real_degrees: dict[str, int],
    *,
    hub_cap: int,
) -> tuple[list[dict[str, Any]], set[str]]:
    """按全图真实度数治理 hub：超 cap 节点只保留 TIER_A 边。

    返回 (治理后边集, 被治理的 hub 节点集合——供定向补查)。度数未知（查询失败）
    的节点不治理（fail-open 于检索增强，剪枝是优化不是正确性闸门）。
    """
    hubs = {node_id for node_id, degree in real_degrees.items() if int(degree) > hub_cap}
    if not hubs:
        return _annotate(edges), set()
    kept: list[dict[str, Any]] = []
    for edge in edges:
        relation_type = str((edge.get("properties") or {}).get("type") or edge.get("type") or "")
        tier = predicate_tier(relation_type)
        annotated = {**edge, "predicate_tier": tier}
        touches_hub = str(edge.get("source_id")) in hubs or str(edge.get("target_id")) in hubs
        if touches_hub and tier != TIER_A:
            continue
        kept.append(annotated)
    return kept, hubs


def budget_filter(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    *,
    node_budget: int,
    edge_budget: int,
    tier_whitelist: tuple[str, ...] = (TIER_A, TIER_B),
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """预算过滤：超支时先丢白名单外的边、再丢 TIER_B、最后截断（保 TIER_A 主力边）。"""
    whitelisted = [
        edge
        for edge in edges
        if edge.get("predicate_tier") in tier_whitelist
        or predicate_tier(str((edge.get("properties") or {}).get("type") or edge.get("type") or "")) in tier_whitelist
    ]
    trimmed = whitelisted[: max(1, edge_budget)] if len(whitelisted) > edge_budget else whitelisted
    if len(trimmed) == edge_budget:
        # 同预算下优先保 TIER_A
        trimmed.sort(key=lambda edge: 0 if edge.get("predicate_tier") == TIER_A else 1)
        trimmed = trimmed[:edge_budget]
    final_nodes = nodes[: max(1, node_budget)]
    node_ids = {(str(item.get("kb_id")), str(item.get("id"))) for item in final_nodes}
    final_edges = [
        edge
        for edge in trimmed
        if (str(edge.get("kb_id")), str(edge.get("source_id"))) in node_ids
        and (str(edge.get("kb_id")), str(edge.get("target_id"))) in node_ids
    ]
    return final_nodes, final_edges


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
    policy_json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return bounded Neo4j projection paths as context, never as canonical scientific claims."""
    policy = resolve_graph_expansion_policy(policy_json)
    max_depth = min(max(max_depth, 1), policy.hop_budget)
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
                max_depth=max_depth,
                max_nodes=min(max(max_nodes, 1), policy.node_budget),
                exclude_chunk=True,
                raise_on_error=True,
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
    ]

    # ── 两段 hub 治理：召回集内高连接节点 → 全图真实度数核查 → 剪枝 + TIER_A 回填 ──
    edges, governed_hubs = await _apply_real_degree_hub_governance(service, nodes, edges, policy)
    nodes, edges = budget_filter(
        nodes,
        edges,
        node_budget=max(int(max_nodes), 1),
        edge_budget=policy.edge_budget,
        tier_whitelist=policy.tier_whitelist,
    )

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
        "hub_governance": {"hub_cap": policy.effective_hub_cap(), "governed_hubs": sorted(governed_hubs)[:20]},
        "source_status": source_status,
    }


async def _apply_real_degree_hub_governance(
    service: MilvusGraphService,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    policy: GraphExpansionPolicy,
) -> tuple[list[dict[str, Any]], set[str]]:
    """召回集内度数 ≥ ``HUB_CANDIDATE_LOCAL_DEGREE`` 的节点按 kb 批量查全图真实度数，
    超 cap 的 hub 剪掉非 TIER_A 边，再对每个 hub 定向补查 TIER_A 邻域（第一段可能被
    path_limit 截断）。任何一步失败都降级为「不治理」（fail-open）。"""
    local_degree: Counter[str] = Counter()
    node_id_by_key: dict[str, str] = {}
    node_kb: dict[str, str] = {}
    for node in nodes:
        raw_id = str(node.get("id") or (node.get("properties") or {}).get("entity_id") or "")
        if not raw_id:
            continue
        key = f"{node.get('kb_id')}:{raw_id}"
        node_id_by_key[key] = raw_id
        node_kb[key] = str(node.get("kb_id"))
    for edge in edges:
        for endpoint in (str(edge.get("source_id")), str(edge.get("target_id"))):
            if endpoint in {raw_id for raw_id in node_id_by_key.values()}:
                local_degree[endpoint] += 1
    candidates = {node_id for node_id, degree in local_degree.items() if degree >= HUB_CANDIDATE_LOCAL_DEGREE}
    if not candidates:
        return _annotate(edges), set()

    hub_cap = policy.effective_hub_cap()
    real_degrees: dict[str, int] = {}
    by_kb: dict[str, list[str]] = {}
    for node_id in candidates:
        kb = next((kb for key, kb in node_kb.items() if node_id_by_key.get(key) == node_id), None)
        if kb:
            by_kb.setdefault(kb, []).append(node_id)
    try:
        for kb, entity_ids in by_kb.items():
            degrees = await service.batch_query_node_degrees(kb, entity_ids)
            real_degrees.update(degrees)
    except Exception as exc:  # noqa: BLE001
        logger.warning("hub 真实度数查询失败，跳过治理（fail-open）: {}", exc)
        return _annotate(edges), set()

    governed, hubs = apply_hub_policy(_annotate(edges), real_degrees, hub_cap=hub_cap)
    if not hubs:
        return governed, hubs

    backfilled: dict[str, dict[str, Any]] = {}
    for hub in list(hubs)[:20]:
        kb = next((node_kb[key] for key, raw_id in node_id_by_key.items() if raw_id == hub), None)
        if not kb:
            continue
        try:
            for edge in await service.query_tier_a_edges(kb, hub, limit=TIER_A_BACKFILL_LIMIT):
                edge_id = str(
                    edge.get("id")
                    or (edge.get("properties") or {}).get("triple_id")
                    or f"{edge.get('source_id')}:{edge.get('type')}:{edge.get('target_id')}"
                )
                backfilled.setdefault(f"{kb}:{edge_id}", {**edge, "kb_id": kb, "predicate_tier": TIER_A})
        except Exception:  # noqa: BLE001
            continue
    merged: dict[str, dict[str, Any]] = {str(edge.get("id")): edge for edge in governed}
    merged.update(backfilled)
    return list(merged.values()), hubs


def _annotate(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """边附谓词分级标注（未标注路径统一补齐）。"""
    annotated = []
    for edge in edges:
        if edge.get("predicate_tier"):
            annotated.append(edge)
            continue
        relation_type = str((edge.get("properties") or {}).get("type") or edge.get("type") or "")
        annotated.append({**edge, "predicate_tier": predicate_tier(relation_type)})
    return annotated
