"""Deterministic conversation graph projection.

The projection is a bounded, immutable view of PostgreSQL canonical relations.  It
never accepts model-authored nodes/edges and never treats Neo4j element ids as
business identities.  The same frozen relation universe also grounds the answer
text, so the graph shown below an answer cannot diverge from what the answer says.

Admission follows the scope-member evidence policy (the same vocabulary the claim
channel uses via ``_policy_allows``): ``APPROVED``/``CANONICAL`` relations always
publish; ``CANDIDATE`` relations publish only for members that explicitly enable
``evidence_candidate``; everything else is suppressed and counted.  A turn whose
only relations were suppressed candidates yields ``PENDING_REVIEW`` — a verdict,
not a silent miss.

Display-level aggregation keeps "all relations" honest and readable on fragmenting
LLM-extraction graphs: the mention-matched seed variants collapse into one seed
node, and parallel raw edges merge per ``(relation_group, target normalized
identity, direction)`` into one display edge that carries ``parallel_count`` and
the distinct predicates it aggregates.  Budgets apply to aggregated edges, so the
bounded snapshot still shows every distinct assertion for realistic graphs.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.knowledge.contracts.schemas import relation_group
from yuxi.storage.postgres.models_knowledge import KnowledgeGraphEntity, KnowledgeGraphTriple

GRAPH_SNAPSHOT_SCHEMA = "graph_snapshot_v1"
# 聚合口径下的预算：439 组真实断言（731 条原始抽取）全量进快照仍受此上限保护。
DEFAULT_NODE_LIMIT = 400
DEFAULT_EDGE_LIMIT = 500
ALWAYS_PUBLISHABLE_STATUSES = {"APPROVED", "CANONICAL"}
CANDIDATE_STATUS = "CANDIDATE"
AGGREGATION_STRATEGY = "kb_id+relation_group+target_normalized+direction"

# 展示层中文谓词同义映射：claim 通道的 relation_group 标记词表只有英文，
# 「调控/regulates」这类双语平行断言会分落两个桶而无法聚合。此映射仅作用于
# 对话图的显示聚合，不改动 claim 语义桶（relation_group 本体保持不动）。
_ZH_PERTURBATION_MARKERS = ("突变", "敲除", "敲低", "敲减", "过表达")
_ZH_FUNCTIONAL_MARKERS = ("调控", "调节", "促进", "抑制", "激活", "阻遏", "增强", "控制")


def _display_relation_group(relation_type: str | None) -> str:
    value = str(relation_type or "")
    if any(marker in value for marker in _ZH_PERTURBATION_MARKERS):
        return "PERTURBATION_EVIDENCE"
    if any(marker in value for marker in _ZH_FUNCTIONAL_MARKERS):
        return "FUNCTIONAL_REGULATION"
    return relation_group(value)


def _projection_hash(payload: dict[str, Any]) -> str:
    stable = {key: value for key, value in payload.items() if key != "projection_hash"}
    raw = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _member_allows_candidates(member: dict[str, Any] | None) -> bool:
    return bool((member or {}).get("evidence_candidate"))


def _seed_entities(contract: dict[str, Any]) -> dict[str, str]:
    """entity_id → best-effort display name; authoritative names come from the entity table."""
    seeds: dict[str, str] = {}
    for item in contract.get("resolved_entities") or []:
        if isinstance(item, dict) and item.get("entity_id"):
            name = item.get("name") or item.get("canonical_name") or item.get("normalized_name")
            seeds[str(item["entity_id"])] = str(name or item["entity_id"])
    if not seeds:
        for claim in contract.get("claims") or []:
            if not isinstance(claim, dict):
                continue
            subject = claim.get("subject") or {}
            if subject.get("id"):
                seeds.setdefault(str(subject["id"]), str(subject.get("name") or subject["id"]))
    return seeds


class _EntityInfo:
    __slots__ = ("entity_id", "kb_id", "name", "normalized_name", "label")

    def __init__(self, entity_id: str, kb_id: str, name: str, normalized_name: str, label: str) -> None:
        self.entity_id = entity_id
        self.kb_id = kb_id
        self.name = name
        self.normalized_name = normalized_name
        self.label = label


async def _load_entity_info(db: AsyncSession, *, kb_ids: list[str], entity_ids: set[str]) -> dict[str, _EntityInfo]:
    if not entity_ids or not kb_ids:
        return {}
    rows = (
        await db.execute(
            select(
                KnowledgeGraphEntity.entity_id,
                KnowledgeGraphEntity.kb_id,
                KnowledgeGraphEntity.name,
                KnowledgeGraphEntity.normalized_name,
                KnowledgeGraphEntity.label,
            ).where(
                KnowledgeGraphEntity.kb_id.in_(kb_ids),
                KnowledgeGraphEntity.entity_id.in_(sorted(entity_ids)),
            )
        )
    ).all()
    return {
        str(entity_id): _EntityInfo(
            str(entity_id), str(kb_id), str(name or entity_id), str(normalized_name or ""), str(label or "")
        )
        for entity_id, kb_id, name, normalized_name, label in rows
    }


async def project_graph_snapshot(
    db: AsyncSession,
    contract: dict[str, Any],
    *,
    members: list[dict[str, Any]],
    node_limit: int = DEFAULT_NODE_LIMIT,
    edge_limit: int = DEFAULT_EDGE_LIMIT,
) -> dict[str, Any]:
    """Project a bounded, policy-gated, display-aggregated graph around the resolved entities.

    ``members`` is the frozen scope membership (kb_id + evidence policy flags); the
    one-hop query is restricted to those KBs, so the snapshot can never widen the
    run's frozen knowledge scope.
    """
    member_by_kb = {str(item.get("kb_id")): item for item in members if isinstance(item, dict) and item.get("kb_id")}
    kb_ids = list(member_by_kb)
    seeds = _seed_entities(contract)
    seed_ids = set(seeds)

    # 权威名字来自实体表（解析器字典的键形不稳定，曾把种子名回落成 entity_id 哈希）。
    seed_infos = await _load_entity_info(db, kb_ids=kb_ids, entity_ids=seed_ids)
    for entity_id, info in seed_infos.items():
        seeds[entity_id] = info.name
    mention = str(((contract.get("retrieval_plan") or {}).get("target_mention")) or "").strip()
    mention_key = mention.casefold()
    candidates = sorted(seeds.values())
    seed_display_name = next(
        (
            name
            for entity_id, name in sorted(seeds.items())
            if seed_infos.get(entity_id) and seed_infos[entity_id].normalized_name.casefold() == mention_key
        ),
        min(candidates, key=len) if candidates else "",
    )
    # 展示用规范种子：mention 精确匹配的实体，否则名字最短的种子；其余变体折叠进它。
    display_seed_id = next(
        (
            entity_id
            for entity_id in sorted(seed_ids)
            if seed_infos.get(entity_id) and seed_infos[entity_id].normalized_name.casefold() == mention_key
        ),
        min(sorted(seed_ids), key=lambda entity_id: len(seeds.get(entity_id) or entity_id)) if seed_ids else "",
    )

    publishable: list[tuple[int, KnowledgeGraphTriple]] = []
    suppressed_candidates = 0
    suppressed_rejected = 0
    if kb_ids and seed_ids:
        raw_rows = list(
            (
                await db.execute(
                    select(KnowledgeGraphTriple)
                    .where(
                        KnowledgeGraphTriple.kb_id.in_(kb_ids),
                        or_(
                            KnowledgeGraphTriple.source_entity_id.in_(seed_ids),
                            KnowledgeGraphTriple.target_entity_id.in_(seed_ids),
                        ),
                    )
                    .order_by(KnowledgeGraphTriple.triple_id)
                )
            )
            .scalars()
            .all()
        )
        for row in raw_rows:
            status = str(row.review_status or "").upper()
            if status in ALWAYS_PUBLISHABLE_STATUSES:
                publishable.append((0, row))
            elif status == CANDIDATE_STATUS:
                if _member_allows_candidates(member_by_kb.get(str(row.kb_id))):
                    publishable.append((1, row))
                else:
                    suppressed_candidates += 1
            else:
                suppressed_rejected += 1

    other_ids = {
        str(endpoint)
        for _tier, row in publishable
        for endpoint in (row.source_entity_id, row.target_entity_id)
        if str(endpoint) not in seed_ids
    }
    endpoint_infos = await _load_entity_info(db, kb_ids=kb_ids, entity_ids=other_ids)

    # 每个知识库内的 normalized 身份选一个展示代表节点（确定性：entity_id 最小者）。
    # 不跨 KB 合并：不同来源可能有不同准入策略、审核状态与证据归属。
    representative_by_identity: dict[tuple[str, str], str] = {}
    for entity_id in sorted(endpoint_infos):
        info = endpoint_infos[entity_id]
        representative_by_identity.setdefault((info.kb_id, info.normalized_name.casefold()), entity_id)

    # 平行边只在同一知识库内聚合；种子间互指边不展示（自环噪声）。
    groups: dict[tuple[str, str, str, bool], list[tuple[int, KnowledgeGraphTriple]]] = {}
    for tier, row in publishable:
        outward = str(row.source_entity_id) in seed_ids
        other = str(row.target_entity_id) if outward else str(row.source_entity_id)
        if other in seed_ids:
            continue
        info = endpoint_infos.get(other)
        if info is None:
            continue
        key = (str(row.kb_id), _display_relation_group(row.relation_type), info.normalized_name.casefold(), outward)
        groups.setdefault(key, []).append((tier, row))

    def _aggregate_sort_key(
        key: tuple[str, str, str, bool], group_members: list[tuple[int, KnowledgeGraphTriple]]
    ) -> tuple:
        rep_tier = min(tier for tier, _row in group_members)
        rep_support = max(int(row.support_count or 0) for _tier, row in group_members)
        return (rep_tier, -rep_support, key[0], key[1], key[2], key[3])

    ordered_groups = sorted(groups.items(), key=lambda item: _aggregate_sort_key(item[0], item[1]))
    selected_groups = ordered_groups[: max(1, int(edge_limit))]

    # 贪心节点预算：规范种子优先，其余名额按聚合边排名喂给目标代表节点。
    node_cap = max(1, int(node_limit))
    allowed_ids: set[str] = set()
    visible_groups: list[tuple[tuple[str, str, str, bool], list[tuple[int, KnowledgeGraphTriple]]]] = []
    # 无可发布边时 nodes 置空：种子身份由 seed_entity_ids/seed_names 承载，
    # PENDING_REVIEW/MISS 载荷不携带部分图（且不附卡，仅入审计）。
    if selected_groups:
        if display_seed_id:
            allowed_ids.add(display_seed_id)
        for key, group_members in selected_groups:
            target_id = representative_by_identity.get((key[0], key[2]), "")
            if target_id and target_id not in allowed_ids and len(allowed_ids) < node_cap:
                allowed_ids.add(target_id)
            if display_seed_id in allowed_ids and target_id in allowed_ids:
                visible_groups.append((key, group_members))

    display_node_ids = sorted(allowed_ids)
    display_infos = await _load_entity_info(db, kb_ids=kb_ids, entity_ids=set(display_node_ids))

    def _node_payload(entity_id: str) -> dict[str, Any]:
        info = display_infos[entity_id]
        return {
            "entity_id": info.entity_id,
            "kb_id": info.kb_id,
            "canonical_identity": info.normalized_name,
            "name": info.name,
            "label": info.label,
            "is_seed": entity_id == display_seed_id,
        }

    edges_payload: list[dict[str, Any]] = []
    for key, group_members in visible_groups:
        ordered_members = sorted(
            group_members,
            # 已审核/规范边必须优先成为展示代表；否则同组高支持候选边会把整组
            # 降级为 CANDIDATE，进而错误关闭本可访问的规范证据抽屉。
            key=lambda item: (item[0], -int(item[1].support_count or 0), str(item[1].triple_id)),
        )
        _rep_tier, rep = ordered_members[0]
        target_id = representative_by_identity.get((key[0], key[2]), "")
        source_id, target_display = (display_seed_id, target_id) if key[3] else (target_id, display_seed_id)
        triple_ids = sorted(str(row.triple_id) for _tier, row in group_members)
        candidate_triple_ids = sorted(
            str(row.triple_id)
            for _tier, row in group_members
            if str(row.review_status or "").upper() == CANDIDATE_STATUS
        )
        reviewed_triple_ids = sorted(set(triple_ids) - set(candidate_triple_ids))
        edges_payload.append(
            {
                "triple_id": str(rep.triple_id),
                # 聚合边仍保留全部规范身份，供导出、审计和后续逐条证据查看；
                # triple_id 是当前可直接打开证据的已审核优先代表。
                "triple_ids": triple_ids,
                "reviewed_triple_ids": reviewed_triple_ids,
                "candidate_triple_ids": candidate_triple_ids,
                "kb_id": str(rep.kb_id),
                "source_entity_id": str(source_id),
                "target_entity_id": str(target_display),
                "predicate": str(rep.relation_type),
                "relation_group": key[1],
                "predicates": sorted({str(row.relation_type) for _tier, row in group_members}),
                "parallel_count": len(group_members),
                "reviewed_parallel_count": len(reviewed_triple_ids),
                "candidate_parallel_count": len(candidate_triple_ids),
                # 混合聚合组不是“待审核关系”：已审核事实可正常发布，候选仅以
                # 附加计数披露。只有整组均为候选时才标记 CANDIDATE。
                "review_status": (CANDIDATE_STATUS if not reviewed_triple_ids else str(rep.review_status).upper()),
                "review_version": int(rep.review_version or 0),
                "support_count": max(int(row.support_count or 0) for _tier, row in group_members),
                "literature_count": max(int(row.literature_count or 0) for _tier, row in group_members),
                "best_evidence_level": rep.best_evidence_level,
                "conflict_status": "CONTESTED"
                if any(str(row.conflict_status or "NONE") != "NONE" for _tier, row in group_members)
                else str(rep.conflict_status or "NONE"),
                "risk_score": rep.risk_score,
            }
        )

    error_code = str(contract.get("error_code") or "")
    if error_code.startswith("AMBIGUOUS"):
        outcome = "AMBIGUOUS"
    elif error_code.endswith("UNAVAILABLE") or contract.get("status") == "FAILED":
        outcome = "UNAVAILABLE"
    elif edges_payload:
        outcome = "HIT"
    elif suppressed_candidates:
        outcome = "PENDING_REVIEW"
    else:
        outcome = "MISS"

    payload: dict[str, Any] = {
        "schema": GRAPH_SNAPSHOT_SCHEMA,
        "retrieval_id": contract.get("retrieval_id"),
        "outcome": outcome,
        "scope_version": (contract.get("knowledge_scope_snapshot") or {}).get("scope_version"),
        "review_policy": (
            "approved_plus_candidate"
            if any(_member_allows_candidates(member) for member in member_by_kb.values())
            else "approved_only"
        ),
        "seed_entity_ids": sorted(seed_ids),
        "seed_names": sorted(seeds.values()),
        "seed_display_name": seed_display_name,
        "seed_variant_count": len(seed_ids),
        "depth": 1,
        "truncated": len(ordered_groups) > len(visible_groups),
        "conflict_present": any(edge["conflict_status"] != "NONE" for edge in edges_payload),
        "total_raw_edge_count": len(publishable),
        "aggregation": {
            "strategy": AGGREGATION_STRATEGY,
            "group_count": len(ordered_groups),
            "seed_collapsed": len(seed_ids) > 1,
        },
        "nodes": [_node_payload(entity_id) for entity_id in display_node_ids],
        "edges": edges_payload,
        "suppressed": {
            "review_policy": suppressed_candidates,
            "rejected": suppressed_rejected,
            "edge_budget": max(0, len(ordered_groups) - len(visible_groups)),
            "node_budget": max(0, len(representative_by_identity) + 1 - len(display_node_ids)),
        },
    }
    payload["projection_hash"] = _projection_hash(payload)
    return payload


__all__ = ["GRAPH_SNAPSHOT_SCHEMA", "project_graph_snapshot"]
