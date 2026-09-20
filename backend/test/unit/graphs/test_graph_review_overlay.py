"""人工审核决策叠加层：重放计划、投影过滤、审核服务（验证/拒绝/编辑/补关系/重抽）、并发与幂等。

核心验收：拒绝过的三元组被 LLM 再次抽出（单 chunk 重抽 / 全量重建）后，重放钩子让它仍是 REJECTED
且不回到 Neo4j/Milvus；验证过的三元组即便再生成没产出，也按快照连同 pinned 引文补回。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from yuxi.knowledge.graphs.graph_review_service import AUDIT_ADD_RELATION, AUDIT_REEXTRACT_CHUNK, GraphReviewService
from yuxi.knowledge.graphs.graph_utils import compute_triple_id
from yuxi.knowledge.graphs.milvus_graph_service import (
    REVIEW_POLICY_APPROVED_ONLY,
    REVIEW_POLICY_CANDIDATES_VISIBLE,
    MilvusGraphService,
    filter_edges_by_policy,
    normalized_result_from_snapshot,
)
from yuxi.knowledge.graphs.review_overlay import (
    ACTION_APPROVE,
    ACTION_REJECT,
    ACTION_RENAME,
    ACTION_SUPERSEDE,
    KIND_ENTITY,
    KIND_TRIPLE,
    STATUS_APPROVED,
    STATUS_REJECTED,
    ReviewDecisionIndex,
    plan_replay,
    triple_snapshot,
)
from yuxi.repositories.knowledge_graph_review_repository import (
    ReviewConflictError,
    ReviewTargetReadOnlyError,
    _status_update,
    decision_key,
)

_CONTENT = "GIF1 encodes a cell-wall invertase. Overexpression of GIF1 increased grain weight in Nipponbare."
_QUOTE = "Overexpression of GIF1 increased grain weight"

_SOURCE = {
    "entity_id": "e_gif1",
    "kb_id": "kb",
    "canonical_identity": "name:gif1",
    "normalized_name": "gif1",
    "label": "Gene",
    "name": "GIF1",
    "attributes": [],
    "review_status": "CANDIDATE",
    "review_version": 0,
}
_TARGET = {
    **_SOURCE,
    "entity_id": "e_w",
    "canonical_identity": "name:grain weight",
    "normalized_name": "grain weight",
    "label": "Phenotype",
    "name": "grain weight",
}
_TRIPLE = {
    "triple_id": "t1",
    "kb_id": "kb",
    "source_entity_id": "e_gif1",
    "target_entity_id": "e_w",
    "relation_type": "OVEREXPRESSION_EFFECT",
    "content": "gif1 → OVEREXPRESSION_EFFECT → grain weight",
    "support_count": 1,
    "literature_count": 1,
    "review_status": "CANDIDATE",
    "review_version": 0,
}
_MENTION = {
    "chunk_id": "c1",
    "file_id": "f1",
    "quote": _QUOTE,
    "chunk_content": _CONTENT,
    "pinned_by": None,
    "extractor_type": "llm_scientific",
}


def _decision(kind, target_id, action, *, pinned_chunk_id=None, pinned_quote=None, payload=None, actor="reviewer"):
    return {
        "target_kind": kind,
        "target_id": target_id,
        "action": action,
        "payload": payload,
        "pinned_chunk_id": pinned_chunk_id,
        "pinned_quote": pinned_quote,
        "actor_uid": actor,
        "version": 1,
    }


def _entity_record(entity_id, name, label):
    return {"entity_id": entity_id, "name": name, "label": label, "normalized_name": name.lower()}


def _triple_record(triple_id, source_id, target_id):
    return {"triple_id": triple_id, "source_entity_id": source_id, "target_entity_id": target_id}


# ── 重放计划（核心不变式）──────────────────────────────────────


def test_plan_replay_keeps_rejected_triple_rejected_after_regeneration():
    """拒绝 X → 重抽/重建再次产出 X → 仍 REJECTED 且进投影清理列表。"""
    index = ReviewDecisionIndex([_decision(KIND_TRIPLE, "t1", ACTION_REJECT)])

    plan = plan_replay(
        index,
        chunk_id="c1",
        chunk_content=_CONTENT,
        entity_records=[_entity_record("e_gif1", "GIF1", "Gene"), _entity_record("e_w", "grain weight", "Phenotype")],
        triple_records=[_triple_record("t1", "e_gif1", "e_w"), _triple_record("t2", "e_gif1", "e_w")],
    )

    assert plan.triple_status == {"t1": STATUS_REJECTED}
    assert plan.reject_triple_ids == ["t1"]
    assert plan.restore_triples == [] and plan.repin == []


def test_plan_replay_supersede_rejects_old_identity():
    index = ReviewDecisionIndex([_decision(KIND_TRIPLE, "t1", ACTION_SUPERSEDE, payload={"new_triple_id": "t_new"})])

    plan = plan_replay(
        index, chunk_id="c1", chunk_content=_CONTENT, entity_records=[], triple_records=[_triple_record("t1", "a", "b")]
    )

    assert plan.triple_status == {"t1": STATUS_REJECTED}
    assert plan.reject_triple_ids == ["t1"]


def test_plan_replay_repins_approved_triple_when_regenerated_on_pinned_chunk():
    index = ReviewDecisionIndex(
        [_decision(KIND_TRIPLE, "t1", ACTION_APPROVE, pinned_chunk_id="c1", pinned_quote=_QUOTE)]
    )

    plan = plan_replay(
        index, chunk_id="c1", chunk_content=_CONTENT, entity_records=[], triple_records=[_triple_record("t1", "a", "b")]
    )

    assert plan.triple_status == {"t1": STATUS_APPROVED}
    assert plan.repin == [(KIND_TRIPLE, "t1")]
    assert plan.restore_triples == []


def test_plan_replay_restores_approved_triple_from_snapshot_when_not_regenerated():
    """验证过但再生成没产出（模型漂移）→ 按快照补回，前提是 chunk 仍含 pinned 引文。"""
    payload = triple_snapshot(_TRIPLE, _SOURCE, _TARGET)
    decision = _decision(KIND_TRIPLE, "t1", ACTION_APPROVE, pinned_chunk_id="c1", pinned_quote=_QUOTE, payload=payload)

    plan = plan_replay(
        ReviewDecisionIndex([decision]), chunk_id="c1", chunk_content=_CONTENT, entity_records=[], triple_records=[]
    )
    assert plan.restore_triples == [decision]
    assert plan.triple_status == {"t1": STATUS_APPROVED}

    # 引文已不在原文里（chunk 被改写）→ 不补回，避免制造无法回验的边
    drifted = plan_replay(
        ReviewDecisionIndex([decision]),
        chunk_id="c1",
        chunk_content="totally different text",
        entity_records=[],
        triple_records=[],
    )
    assert drifted.is_empty

    # pinned 在别的 chunk → 本 chunk 不负责补回
    other = plan_replay(
        ReviewDecisionIndex([decision]), chunk_id="c9", chunk_content=_CONTENT, entity_records=[], triple_records=[]
    )
    assert other.is_empty


def test_plan_replay_entity_reject_cascades_and_rename_overrides():
    index = ReviewDecisionIndex(
        [
            _decision(KIND_ENTITY, "e_gif1", ACTION_REJECT),
            _decision(KIND_ENTITY, "e_w", ACTION_RENAME, payload={"display_name": "Grain weight (GW)"}),
        ]
    )

    plan = plan_replay(
        index,
        chunk_id="c1",
        chunk_content=_CONTENT,
        entity_records=[_entity_record("e_gif1", "GIF1", "Gene"), _entity_record("e_w", "grain weight", "Phenotype")],
        triple_records=[_triple_record("t1", "e_gif1", "e_w"), _triple_record("t3", "e_other", "e_w")],
    )

    assert plan.entity_status == {"e_gif1": STATUS_REJECTED}
    assert plan.reject_entity_ids == ["e_gif1"]
    assert plan.triple_status == {"t1": STATUS_REJECTED}  # t3 不含被拒实体，不受影响
    assert plan.reject_triple_ids == ["t1"]
    assert plan.entity_overrides == {"e_w": {"display_name": "Grain weight (GW)"}}


def test_plan_replay_without_decisions_is_empty():
    plan = plan_replay(
        ReviewDecisionIndex([]),
        chunk_id="c1",
        chunk_content=_CONTENT,
        entity_records=[_entity_record("e", "x", "Gene")],
        triple_records=[],
    )
    assert plan.is_empty


def test_normalized_result_from_snapshot_rebuilds_same_identity():
    payload = triple_snapshot(_TRIPLE, _SOURCE, _TARGET)

    normalized = normalized_result_from_snapshot(payload, _QUOTE, extractor_type="manual")

    assert normalized["metadata"]["extractor_type"] == "manual"
    assert [entity["text"] for entity in normalized["entities"]] == ["GIF1", "grain weight"]
    assert all(entity["mention_quote"] == _QUOTE for entity in normalized["entities"])
    relation = normalized["relations"][0]
    assert relation["label"] == "OVEREXPRESSION_EFFECT" and relation["text"] == _QUOTE
    # 与抽取轨同一身份函数 → 重建后 triple_id 不变
    assert compute_triple_id(
        "kb", "gif1", "Gene", "OVEREXPRESSION_EFFECT", "grain weight", "Phenotype"
    ) == compute_triple_id(
        "kb",
        relation["source"]["text"].lower(),
        relation["source"]["label"],
        relation["label"],
        relation["target"]["text"].lower(),
        relation["target"]["label"],
    )


# ── 投影策略 ───────────────────────────────────────────────────


def test_filter_edges_by_policy_hides_candidates_only_in_approved_only_mode():
    """approved_only 可见性（fail-closed）：显式状态或托管导入标记，缺属性一律隐藏。"""
    result = {
        "nodes": [{"id": "a"}],
        "edges": [
            {"id": "1", "properties": {"review_status": "CANDIDATE"}},
            {"id": "2", "properties": {"review_status": "APPROVED"}},
            {"id": "3", "properties": {}},  # 缺状态属性的脏边 → 隐藏（I7 计数暴露）
            {"id": "4", "properties": {"managed_projection": True}},  # 托管导入显式标记 → 可见
        ],
    }

    assert filter_edges_by_policy(result, REVIEW_POLICY_CANDIDATES_VISIBLE) is result
    assert [edge["id"] for edge in filter_edges_by_policy(result, REVIEW_POLICY_APPROVED_ONLY)["edges"]] == ["2", "4"]


def test_status_update_never_touches_canonical_rows():
    from sqlalchemy.dialects import postgresql

    sql = str(_status_update(KIND_TRIPLE, ["t1"], STATUS_REJECTED, 3).compile(dialect=postgresql.dialect()))
    assert "knowledge_graph_triples.review_status != " in sql
    assert "review_version" in sql
    assert decision_key("kb", KIND_TRIPLE, "t1") == decision_key("kb", KIND_TRIPLE, "t1")
    assert decision_key("kb", KIND_TRIPLE, "t1") != decision_key("kb", KIND_ENTITY, "t1")


# ── 服务层重放执行 ─────────────────────────────────────────────


class _FakeReviewRepo:
    def __init__(self, *, triple=None, entity=None, decision=None, mentions=None):
        self._triple, self._entity, self._decision = triple, entity, decision
        self._mentions = mentions if mentions is not None else [dict(_MENTION)]
        self.saved: list[dict] = []
        self.pinned: list[tuple] = []
        self.status_updates: list[tuple] = []
        self.audits: list[dict] = []
        self.aliases: list[tuple] = []

    async def list_decisions(self, kb_id):
        return []

    async def get_triple_with_endpoints(self, kb_id, triple_id):
        if self._triple is None:
            return None
        return {"triple": self._triple, "source": _SOURCE, "target": _TARGET, "mentions": self._mentions}

    async def get_entity(self, kb_id, entity_id):
        if self._entity is None:
            return None
        return {"entity": self._entity, "mentions": self._mentions, "triple_ids": ["t1", "t9"]}

    async def get_decision(self, kb_id, kind, target_id):
        return self._decision

    async def save_decision(self, **kwargs):
        if kwargs.get("if_version") == 99:
            raise ReviewConflictError("stale")
        self.saved.append(kwargs)
        return {"version": len(self.saved), "action": kwargs["action"], "target_id": kwargs["target_id"]}

    async def pin_mention(self, kind, target_id, chunk_id, actor_uid):
        self.pinned.append((kind, target_id, chunk_id))
        return 1

    async def set_review_status(self, kind, ids, status):
        self.status_updates.append((kind, list(ids), status))
        return len(ids)

    async def append_audit(self, **kwargs):
        self.audits.append(kwargs)

    async def add_entity_aliases(self, kb_id, entity_id, name, aliases, **kwargs):
        self.aliases.append((entity_id, list(aliases)))
        return len(aliases)

    async def delete_chunk_references(self, chunk_id, *, keep_pinned=True):
        assert keep_pinned is True
        return {
            "affected_triple_ids": ["t1"],
            "affected_entity_ids": ["e_gif1"],
            "orphan_triple_ids": ["t1"],
            "orphan_entity_ids": [],
        }


class _FakeGraph:
    def __init__(self):
        self.projections: list[dict] = []
        self.deleted_chunks: list[str] = []
        self.graph_vector_store = SimpleNamespace(
            delete_graph_records=AsyncMock(), insert_missing_graph_records=AsyncMock()
        )

    def apply_review_projection(self, kb_id, **kwargs):
        self.projections.append(kwargs)

    def delete_chunk_graph_from_neo4j(self, kb_id, chunk_id):
        self.deleted_chunks.append(chunk_id)

    def write_chunk_graph(self, kb_id, chunk, normalized):
        entities = [
            {"entity_id": f"e_{e['text'].lower().replace(' ', '_')}", "name": e["text"]} for e in normalized["entities"]
        ]
        triples = [{"triple_id": "t_new"}] if normalized["relations"] else []
        return entities, triples


def _service(review_repo, graph=None, *, scientific=True, chunk=None):
    kb = SimpleNamespace(
        embedding_model_spec="siliconflow-cn:BAAI/bge-m3",
        additional_params={"graph_build_config": {"extractor_type": "llm_scientific" if scientific else "llm"}},
    )
    chunk = chunk or SimpleNamespace(chunk_id="c1", kb_id="kb", file_id="f1", content=_CONTENT)
    service = GraphReviewService(
        graph_service=graph or _FakeGraph(),
        review_repo=review_repo,
        graph_repo=SimpleNamespace(upsert_chunk_graph=AsyncMock()),
        chunk_repo=SimpleNamespace(
            get_by_chunk_id=AsyncMock(return_value=chunk), reset_graph_state_by_chunk_id=AsyncMock(return_value=1)
        ),
        kb_repo=SimpleNamespace(get_by_kb_id=AsyncMock(return_value=kb)),
    )
    service._tenant_id = AsyncMock(return_value=1)
    return service


@pytest.mark.asyncio
async def test_replay_review_for_chunk_applies_rejection_to_projection_and_vectors():
    review_repo = _FakeReviewRepo()
    vector_store = SimpleNamespace(delete_graph_records=AsyncMock(), insert_missing_graph_records=AsyncMock())
    service = MilvusGraphService(
        review_repo=review_repo, graph_vector_store=vector_store, neo4j_connection=SimpleNamespace(driver=MagicMock())
    )
    service.apply_review_projection = MagicMock()
    index = ReviewDecisionIndex([_decision(KIND_TRIPLE, "t1", ACTION_REJECT)])
    chunk = SimpleNamespace(chunk_id="c1", kb_id="kb", file_id="f1", content=_CONTENT)

    summary = await service.replay_review_for_chunk(
        SimpleNamespace(embedding_model_spec="m"), chunk, [], [_triple_record("t1", "a", "b")], index
    )

    assert summary == {"triples_rejected": 1}
    assert review_repo.status_updates == [(KIND_TRIPLE, ["t1"], STATUS_REJECTED)]
    service.apply_review_projection.assert_called_once()
    assert service.apply_review_projection.call_args.kwargs["reject_triple_ids"] == ["t1"]
    vector_store.delete_graph_records.assert_awaited_once_with("kb", entity_ids=[], triple_ids=["t1"])


@pytest.mark.asyncio
async def test_replay_review_for_chunk_restores_pinned_approved_triple():
    review_repo = _FakeReviewRepo()
    service = MilvusGraphService(review_repo=review_repo, neo4j_connection=SimpleNamespace(driver=MagicMock()))
    service.apply_review_projection = MagicMock()
    service.restore_from_decision = AsyncMock()
    decision = _decision(
        KIND_TRIPLE,
        "t1",
        ACTION_APPROVE,
        pinned_chunk_id="c1",
        pinned_quote=_QUOTE,
        payload=triple_snapshot(_TRIPLE, _SOURCE, _TARGET),
    )
    chunk = SimpleNamespace(chunk_id="c1", kb_id="kb", file_id="f1", content=_CONTENT)

    summary = await service.replay_review_for_chunk(
        SimpleNamespace(embedding_model_spec="m"), chunk, [], [], ReviewDecisionIndex([decision])
    )

    assert summary == {"triples_restored": 1, "triples_approved": 1}
    service.restore_from_decision.assert_awaited_once()
    assert review_repo.status_updates == [(KIND_TRIPLE, ["t1"], STATUS_APPROVED)]
    assert service.apply_review_projection.call_args.kwargs["triple_status"] == {"t1": STATUS_APPROVED}


# ── 审核服务 ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_approve_pins_evidence_and_is_idempotent():
    review_repo = _FakeReviewRepo(triple=_TRIPLE)
    graph = _FakeGraph()
    service = _service(review_repo, graph)

    result = await service.approve("kb", KIND_TRIPLE, "t1", actor_uid="u1", note="looks right")

    assert result["unchanged"] is False
    saved = review_repo.saved[0]
    assert saved["action"] == ACTION_APPROVE and saved["cache_status"] == STATUS_APPROVED
    assert saved["pinned_chunk_id"] == "c1" and saved["pinned_quote"] == _QUOTE
    assert saved["payload"]["triple"]["triple_id"] == "t1" and saved["payload"]["source"]["name"] == "GIF1"
    assert saved["tenant_id"] == 1
    # 三元组本身 + 两端实体的引文都被 pin 住
    assert set(review_repo.pinned) == {
        (KIND_TRIPLE, "t1", "c1"),
        (KIND_ENTITY, "e_gif1", "c1"),
        (KIND_ENTITY, "e_w", "c1"),
    }
    assert graph.projections == [{"triple_status": {"t1": STATUS_APPROVED}}]

    # 已有同 pin 的 APPROVE 决策 → 幂等：不再写决策/审计
    review_repo._decision = {"action": ACTION_APPROVE, "pinned_chunk_id": "c1", "version": 1}
    again = await service.approve("kb", KIND_TRIPLE, "t1", actor_uid="u1")
    assert again["unchanged"] is True and len(review_repo.saved) == 1


@pytest.mark.asyncio
async def test_approve_requires_quote_and_respects_pinned_chunk_choice():
    review_repo = _FakeReviewRepo(triple=_TRIPLE, mentions=[{**_MENTION, "quote": None}])
    with pytest.raises(ValueError, match="没有可固定的原文引文"):
        await _service(review_repo).approve("kb", KIND_TRIPLE, "t1", actor_uid="u1")

    review_repo = _FakeReviewRepo(triple=_TRIPLE)
    with pytest.raises(ValueError, match="不是该对象的原文来源"):
        await _service(review_repo).approve("kb", KIND_TRIPLE, "t1", actor_uid="u1", pinned_chunk_id="c_other")


@pytest.mark.asyncio
async def test_reject_requires_reason_and_clears_projection():
    review_repo = _FakeReviewRepo(triple=_TRIPLE)
    graph = _FakeGraph()
    service = _service(review_repo, graph)

    with pytest.raises(ValueError, match="理由"):
        await service.reject("kb", KIND_TRIPLE, "t1", actor_uid="u1", reason="  ")

    result = await service.reject("kb", KIND_TRIPLE, "t1", actor_uid="u1", reason="方向反了")

    assert review_repo.saved[0]["action"] == ACTION_REJECT and review_repo.saved[0]["cache_status"] == STATUS_REJECTED
    assert graph.projections == [{"reject_triple_ids": ["t1"]}]
    graph.graph_vector_store.delete_graph_records.assert_awaited_once_with("kb", entity_ids=[], triple_ids=["t1"])
    assert result["cascaded_triple_ids"] == []


@pytest.mark.asyncio
async def test_reject_entity_cascades_to_its_triples():
    review_repo = _FakeReviewRepo(entity=_SOURCE)
    graph = _FakeGraph()

    result = await _service(review_repo, graph).reject("kb", KIND_ENTITY, "e_gif1", actor_uid="u1", reason="not a gene")

    assert result["cascaded_triple_ids"] == ["t1", "t9"]
    assert review_repo.status_updates == [(KIND_TRIPLE, ["t1", "t9"], STATUS_REJECTED)]
    assert graph.projections == [{"reject_entity_ids": ["e_gif1"]}]
    graph.graph_vector_store.delete_graph_records.assert_awaited_once_with(
        "kb", entity_ids=["e_gif1"], triple_ids=["t1", "t9"]
    )


@pytest.mark.asyncio
async def test_canonical_targets_are_read_only_and_missing_targets_404_semantics():
    canonical = _FakeReviewRepo(triple={**_TRIPLE, "review_status": "CANONICAL"})
    with pytest.raises(ReviewTargetReadOnlyError):
        await _service(canonical).approve("kb", KIND_TRIPLE, "t1", actor_uid="u1")

    missing = _FakeReviewRepo(triple=None)
    with pytest.raises(ValueError, match="不存在"):
        await _service(missing).reject("kb", KIND_TRIPLE, "nope", actor_uid="u1", reason="x")


@pytest.mark.asyncio
async def test_batch_skips_version_conflicts_per_item():
    review_repo = _FakeReviewRepo(triple=_TRIPLE)
    service = _service(review_repo)

    result = await service.batch(
        "kb",
        ACTION_APPROVE,
        [
            {"kind": KIND_TRIPLE, "id": "t1"},
            {"kind": KIND_TRIPLE, "id": "t1", "if_version": 99},
            {"kind": "WEIRD", "id": "x"},
        ],
        actor_uid="u1",
    )

    assert [item["id"] for item in result["succeeded"]] == ["t1"]
    assert [(item["id"], item["reason"]) for item in result["skipped"]] == [
        ("t1", "version_conflict"),
        ("x", "invalid"),
    ]
    assert result["batch_id"]


@pytest.mark.asyncio
async def test_edit_triple_supersedes_old_identity_with_new_approved_triple():
    review_repo = _FakeReviewRepo(triple=_TRIPLE)
    graph = _FakeGraph()
    service = _service(review_repo, graph)
    service._materialize_snapshot = AsyncMock()

    result = await service.edit_triple(
        "kb", "t1", actor_uid="u1", reverse=True, relation_type="promotes_phenotype", note="方向反了"
    )

    expected_new = compute_triple_id("kb", "grain weight", "Phenotype", "PROMOTES_PHENOTYPE", "gif1", "Gene")
    assert result == {"old_triple_id": "t1", "new_triple_id": expected_new, "decision": result["decision"]}
    actions = [(item["target_id"], item["action"], item["cache_status"]) for item in review_repo.saved]
    assert actions == [("t1", ACTION_SUPERSEDE, STATUS_REJECTED), (expected_new, ACTION_APPROVE, STATUS_APPROVED)]
    assert review_repo.saved[0]["payload"]["new_triple_id"] == expected_new
    assert review_repo.saved[1]["pinned_quote"] == _QUOTE
    service._materialize_snapshot.assert_awaited_once()
    assert graph.projections == [{"triple_status": {expected_new: STATUS_APPROVED}, "reject_triple_ids": ["t1"]}]
    graph.graph_vector_store.delete_graph_records.assert_awaited_once_with("kb", entity_ids=[], triple_ids=["t1"])


@pytest.mark.asyncio
async def test_edit_triple_rejects_noop_and_closed_vocabulary_violation():
    # 无改动检测依赖真实内容哈希身份：夹具用与抽取轨同一函数算出的 triple_id
    real_id = compute_triple_id("kb", "gif1", "Gene", "OVEREXPRESSION_EFFECT", "grain weight", "Phenotype")
    review_repo = _FakeReviewRepo(triple={**_TRIPLE, "triple_id": real_id})
    service = _service(review_repo)

    with pytest.raises(ValueError, match="未做任何修改"):
        await service.edit_triple("kb", real_id, actor_uid="u1")
    with pytest.raises(ValueError, match="不在科研闭集词表内"):
        await service.edit_triple("kb", real_id, actor_uid="u1", relation_type="MAGIC")


@pytest.mark.asyncio
async def test_add_triple_enforces_verbatim_quote_and_materializes_manual_triple():
    review_repo = _FakeReviewRepo(entity=_SOURCE)
    graph = _FakeGraph()
    service = _service(review_repo, graph)

    async def get_entity(kb_id, entity_id):
        entity = _SOURCE if entity_id == "e_gif1" else _TARGET
        return {"entity": entity, "mentions": [], "triple_ids": []}

    review_repo.get_entity = get_entity

    with pytest.raises(ValueError, match="逐字子串"):
        await service.add_triple(
            "kb",
            actor_uid="u1",
            source_entity_id="e_gif1",
            target_entity_id="e_w",
            relation_type="PROMOTES_PHENOTYPE",
            chunk_id="c1",
            evidence_quote="GIF1 boosts yield",
        )

    result = await service.add_triple(
        "kb",
        actor_uid="u1",
        source_entity_id="e_gif1",
        target_entity_id="e_w",
        relation_type="promotes_phenotype",
        chunk_id="c1",
        evidence_quote=_QUOTE,
    )

    expected = compute_triple_id("kb", "gif1", "Gene", "PROMOTES_PHENOTYPE", "grain weight", "Phenotype")
    assert result["triple_id"] == expected
    saved = review_repo.saved[0]
    assert saved["audit_action"] == AUDIT_ADD_RELATION and saved["action"] == ACTION_APPROVE
    assert saved["pinned_chunk_id"] == "c1" and saved["pinned_quote"] == _QUOTE
    # 走了与抽取相同的写入路径：Neo4j → PG → Milvus，并 pin 了两端实体与新三元组
    service.graph_repo.upsert_chunk_graph.assert_awaited_once()
    graph.graph_vector_store.insert_missing_graph_records.assert_awaited_once()
    assert (KIND_TRIPLE, "t_new", "c1") in review_repo.pinned
    assert graph.projections == [{"triple_status": {expected: STATUS_APPROVED}}]


@pytest.mark.asyncio
async def test_edit_entity_records_display_override_without_changing_identity():
    review_repo = _FakeReviewRepo(entity=_SOURCE)
    graph = _FakeGraph()

    await _service(review_repo, graph).edit_entity(
        "kb", "e_gif1", actor_uid="u1", display_name="GIF1 (cell-wall invertase)", aliases=["OsCIN2"]
    )

    saved = review_repo.saved[0]
    assert saved["action"] == ACTION_RENAME and saved.get("cache_status") is None
    assert saved["payload"] == {"display_name": "GIF1 (cell-wall invertase)", "aliases": ["OsCIN2"]}
    assert review_repo.aliases == [("e_gif1", ["OsCIN2"])]
    assert graph.projections == [{"entity_overrides": {"e_gif1": saved["payload"]}}]


@pytest.mark.asyncio
async def test_prepare_reextract_keeps_pinned_and_resets_chunk():
    review_repo = _FakeReviewRepo()
    graph = _FakeGraph()
    service = _service(review_repo, graph)

    result = await service.prepare_reextract("kb", "c1", actor_uid="u1")

    assert result == {
        "chunk_id": "c1",
        "file_id": "f1",
        "affected_triple_ids": 1,
        "affected_entity_ids": 1,
        "orphan_triple_ids": 1,
        "orphan_entity_ids": 0,
    }
    assert graph.deleted_chunks == ["c1"]
    graph.graph_vector_store.delete_graph_records.assert_awaited_once_with("kb", entity_ids=[], triple_ids=["t1"])
    service.chunk_repo.reset_graph_state_by_chunk_id.assert_awaited_once_with("c1")
    assert review_repo.audits[0]["action"] == AUDIT_REEXTRACT_CHUNK and review_repo.audits[0]["target_id"] == "c1"
