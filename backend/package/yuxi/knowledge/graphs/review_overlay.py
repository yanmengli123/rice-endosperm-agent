"""人工审核决策叠加层的纯逻辑：决策索引 + 重放计划（无 I/O，可单测）。

图谱行是机器产物（reset/重抽会反复再生），决策是人类知识（只增不减）。每个 chunk
写入之后按本次涉及的三元组/实体身份查决策，产出一份 ``ReplayPlan``：

- 缓存状态：APPROVE → APPROVED，REJECT/SUPERSEDE → REJECTED；
- 投影清理：REJECTED 三元组删 Neo4j 平行边 + Milvus 向量；REJECTED 实体 DETACH DELETE
  并级联其全部三元组；
- 证据恢复：决策 pinned 在本 chunk、而本次再生成没有产出该对象时，用决策里的快照把
  三元组/实体连同 ``human_pinned`` 引文补回（chunk 仍含该引文才补，G2 同款校验）；
- 展示覆盖：RENAME/RETYPE 只改 Neo4j 展示属性，不改内容哈希身份。

身份是 ``compute_triple_id``/``compute_entity_id`` 的内容哈希，因此同一句话再抽一次
得到同一 ID——这正是决策能跨再生成幂等重放的前提。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

OVERLAY_VERSION = "review_overlay_v1"

KIND_TRIPLE = "TRIPLE"
KIND_ENTITY = "ENTITY"

ACTION_APPROVE = "APPROVE"
ACTION_REJECT = "REJECT"
ACTION_SUPERSEDE = "SUPERSEDE"
ACTION_RENAME = "RENAME"
ACTION_RETYPE = "RETYPE"
# R4c 词典晋升：文档级别名（doclex）经人工审核升为 KB 级官方别名。
# 不映射缓存状态（别名不是图谱行），重放时只重写别名表。
ACTION_ALIAS_PROMOTE = "ALIAS_PROMOTE"
KIND_ALIAS = "ALIAS"
REVIEW_ACTIONS = frozenset(
    {ACTION_APPROVE, ACTION_REJECT, ACTION_SUPERSEDE, ACTION_RENAME, ACTION_RETYPE, ACTION_ALIAS_PROMOTE}
)

STATUS_CANDIDATE = "CANDIDATE"
STATUS_APPROVED = "APPROVED"
STATUS_REJECTED = "REJECTED"
STATUS_CANONICAL = "CANONICAL"
REVIEW_STATUSES = frozenset({STATUS_CANDIDATE, STATUS_APPROVED, STATUS_REJECTED, STATUS_CANONICAL})

# 标准拒绝/裁决原因代码（闭集）：结构化原因便于统计与审计检索；说明文本仍必填可读细节
REASON_CODE_DIRECTION_ERROR = "DIRECTION_ERROR"
REASON_CODE_NEGATION_MISREAD = "NEGATION_MISREAD"
REASON_CODE_OVER_GENERALIZATION = "OVER_GENERALIZATION"
REASON_CODE_SOURCE_UNTRUSTWORTHY = "SOURCE_UNTRUSTWORTHY"
REASON_CODE_WRONG_ENDPOINT = "WRONG_ENDPOINT"
REASON_CODE_SCOPE_MISMATCH = "SCOPE_MISMATCH"
REASON_CODE_DUPLICATE = "DUPLICATE"
REASON_CODE_INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
REASON_CODE_OTHER = "OTHER"
REASON_CODES = frozenset(
    {
        REASON_CODE_DIRECTION_ERROR,
        REASON_CODE_NEGATION_MISREAD,
        REASON_CODE_OVER_GENERALIZATION,
        REASON_CODE_SOURCE_UNTRUSTWORTHY,
        REASON_CODE_WRONG_ENDPOINT,
        REASON_CODE_SCOPE_MISMATCH,
        REASON_CODE_DUPLICATE,
        REASON_CODE_INSUFFICIENT_EVIDENCE,
        REASON_CODE_OTHER,
    }
)


def compose_reason(reason_code: str | None, note: str | None) -> str:
    """把原因代码与说明合成审计 reason 字符串：`[CODE] 说明`。

    代码非法时忽略代码只留说明；两者皆空返回空串（拒绝理由必填校验在上游）。
    """
    code = (reason_code or "").strip().upper()
    text = (note or "").strip()
    if code in REASON_CODES and text:
        return f"[{code}] {text}"
    return text


# 决策 action → 缓存状态
_STATUS_BY_ACTION = {
    ACTION_APPROVE: STATUS_APPROVED,
    ACTION_REJECT: STATUS_REJECTED,
    ACTION_SUPERSEDE: STATUS_REJECTED,
}


class ReviewDecisionIndex:
    """一次构建加载一遍全部决策（用户建议：不逐 chunk 打 PG），按 (kind, id) 查。"""

    def __init__(self, decisions: list[dict[str, Any]] | None = None):
        self._by_key: dict[tuple[str, str], dict[str, Any]] = {}
        for decision in decisions or []:
            self._by_key[(decision["target_kind"], decision["target_id"])] = decision

    def __len__(self) -> int:
        return len(self._by_key)

    def get(self, kind: str, target_id: str) -> dict[str, Any] | None:
        return self._by_key.get((kind, target_id))

    def pinned_on_chunk(self, chunk_id: str) -> list[dict[str, Any]]:
        return [decision for decision in self._by_key.values() if decision.get("pinned_chunk_id") == chunk_id]

    def all(self) -> list[dict[str, Any]]:
        return list(self._by_key.values())


@dataclass
class ReplayPlan:
    triple_status: dict[str, str] = field(default_factory=dict)
    entity_status: dict[str, str] = field(default_factory=dict)
    reject_triple_ids: list[str] = field(default_factory=list)
    reject_entity_ids: list[str] = field(default_factory=list)
    # 已被本次再生成产出、需要重新打 pinned 标记的 mention：(kind, target_id)
    repin: list[tuple[str, str]] = field(default_factory=list)
    # 本次再生成没产出、需按决策快照补回的对象（决策 dict 原样携带）
    restore_triples: list[dict[str, Any]] = field(default_factory=list)
    restore_entities: list[dict[str, Any]] = field(default_factory=list)
    entity_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not (
            self.triple_status
            or self.entity_status
            or self.repin
            or self.restore_triples
            or self.restore_entities
            or self.entity_overrides
        )


def status_for_action(action: str) -> str | None:
    return _STATUS_BY_ACTION.get(action)


def plan_replay(
    index: ReviewDecisionIndex,
    *,
    chunk_id: str,
    chunk_content: str,
    entity_records: list[dict[str, Any]],
    triple_records: list[dict[str, Any]],
) -> ReplayPlan:
    """按本 chunk 刚写入的记录 + pinned 在本 chunk 的决策，产出重放计划。"""
    plan = ReplayPlan()
    written_entity_ids = {record["entity_id"] for record in entity_records}
    written_triple_ids = {record["triple_id"] for record in triple_records}
    triples_by_endpoint: dict[str, list[str]] = {}
    for record in triple_records:
        triples_by_endpoint.setdefault(record["source_entity_id"], []).append(record["triple_id"])
        triples_by_endpoint.setdefault(record["target_entity_id"], []).append(record["triple_id"])

    for entity_id in written_entity_ids:
        decision = index.get(KIND_ENTITY, entity_id)
        if decision is None:
            continue
        action = decision["action"]
        if action == ACTION_REJECT:
            plan.entity_status[entity_id] = STATUS_REJECTED
            plan.reject_entity_ids.append(entity_id)
            for triple_id in triples_by_endpoint.get(entity_id, []):
                plan.triple_status[triple_id] = STATUS_REJECTED
                if triple_id not in plan.reject_triple_ids:
                    plan.reject_triple_ids.append(triple_id)
        elif action == ACTION_APPROVE:
            plan.entity_status[entity_id] = STATUS_APPROVED
            if decision.get("pinned_chunk_id") == chunk_id:
                plan.repin.append((KIND_ENTITY, entity_id))
        elif action in (ACTION_RENAME, ACTION_RETYPE):
            plan.entity_overrides[entity_id] = dict(decision.get("payload") or {})

    for triple_id in written_triple_ids:
        if triple_id in plan.triple_status:
            continue
        decision = index.get(KIND_TRIPLE, triple_id)
        if decision is None:
            continue
        status = status_for_action(decision["action"])
        if status is None:
            continue
        plan.triple_status[triple_id] = status
        if status == STATUS_REJECTED:
            plan.reject_triple_ids.append(triple_id)
        elif decision.get("pinned_chunk_id") == chunk_id:
            plan.repin.append((KIND_TRIPLE, triple_id))

    # 决策 pinned 在本 chunk，但本次再生成没有产出该对象 → 用快照补回（chunk 仍含引文才补）
    for decision in index.pinned_on_chunk(chunk_id):
        if decision["action"] != ACTION_APPROVE:
            continue
        quote = str(decision.get("pinned_quote") or "")
        if not quote or quote not in (chunk_content or ""):
            continue
        payload = decision.get("payload") or {}
        if decision["target_kind"] == KIND_TRIPLE and decision["target_id"] not in written_triple_ids:
            if payload.get("triple") and payload.get("source") and payload.get("target"):
                plan.restore_triples.append(decision)
                plan.triple_status[decision["target_id"]] = STATUS_APPROVED
        elif decision["target_kind"] == KIND_ENTITY and decision["target_id"] not in written_entity_ids:
            if payload.get("entity"):
                plan.restore_entities.append(decision)
                plan.entity_status[decision["target_id"]] = STATUS_APPROVED
    return plan


def triple_snapshot(triple: dict[str, Any], source: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    """APPROVE/SUPERSEDE 决策的恢复快照：再生成不再产出该三元组时据此重建（含两端实体）。"""
    return {
        "triple": {
            key: triple.get(key)
            for key in ("triple_id", "kb_id", "source_entity_id", "target_entity_id", "relation_type", "content")
        },
        "source": entity_snapshot(source),
        "target": entity_snapshot(target),
    }


def entity_snapshot(entity: dict[str, Any]) -> dict[str, Any]:
    return {
        key: entity.get(key)
        for key in ("entity_id", "kb_id", "canonical_identity", "normalized_name", "label", "name", "attributes")
    }
