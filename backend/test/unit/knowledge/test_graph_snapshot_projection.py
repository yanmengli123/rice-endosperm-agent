from __future__ import annotations

import pytest
from sqlalchemy import BigInteger, Integer, MetaData
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.knowledge.contracts.graph_snapshot_projection import project_graph_snapshot
from yuxi.storage.postgres import models_knowledge as m


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    metadata = MetaData()
    for table in m.Base.metadata.sorted_tables:
        copied = table.to_metadata(metadata)
        for column in copied.columns:
            if column.primary_key and isinstance(column.type, BigInteger):
                column.type = Integer()
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


_CLOSED_MEMBERS = [{"kb_id": "kb", "structured_enabled": True}]
_CANDIDATE_MEMBERS = [{"kb_id": "kb", "structured_enabled": True, "evidence_candidate": True}]


async def _seed_graph(db):
    db.add(m.KnowledgeBase(kb_id="kb", tenant_id=7, name="KB", kb_type="milvus", share_config={}))
    db.add_all(
        [
            m.KnowledgeGraphEntity(
                entity_id="e1",
                kb_id="kb",
                canonical_identity="gene:gs3",
                normalized_name="gs3",
                label="Gene",
                name="GS3",
                review_status="APPROVED",
            ),
            m.KnowledgeGraphEntity(
                entity_id="e2",
                kb_id="kb",
                canonical_identity="trait:grain-size",
                normalized_name="grain size",
                label="Trait",
                name="grain size",
                review_status="APPROVED",
            ),
            m.KnowledgeGraphEntity(
                entity_id="e3",
                kb_id="kb",
                canonical_identity="trait:yield",
                normalized_name="yield",
                label="Trait",
                name="yield",
                review_status="CANDIDATE",
            ),
            m.KnowledgeGraphTriple(
                triple_id="t-approved",
                kb_id="kb",
                source_entity_id="e1",
                target_entity_id="e2",
                relation_type="regulates",
                content="GS3 regulates grain size",
                support_count=3,
                literature_count=2,
                review_status="APPROVED",
                review_version=4,
            ),
            m.KnowledgeGraphTriple(
                triple_id="t-candidate",
                kb_id="kb",
                source_entity_id="e1",
                target_entity_id="e3",
                relation_type="affects",
                content="candidate relation",
                review_status="CANDIDATE",
            ),
        ]
    )
    await db.flush()


def _contract():
    return {
        "retrieval_id": "kr-1",
        "status": "COMPLETED",
        "knowledge_scope_snapshot": {"scope_version": 8},
        "resolved_entities": [{"entity_id": "e1", "name": "GS3"}],
        "claims": [],
    }


async def test_projection_is_review_gated_bounded_and_stable(db):
    await _seed_graph(db)
    contract = _contract()

    first = await project_graph_snapshot(db, contract, members=_CLOSED_MEMBERS)
    second = await project_graph_snapshot(db, contract, members=_CLOSED_MEMBERS)

    assert first == second
    assert first["schema"] == "graph_snapshot_v1"
    assert first["outcome"] == "HIT"
    assert first["scope_version"] == 8
    assert first["review_policy"] == "approved_only"
    assert first["seed_entity_ids"] == ["e1"]
    assert first["seed_names"] == ["GS3"]
    assert [edge["triple_id"] for edge in first["edges"]] == ["t-approved"]
    assert {node["entity_id"] for node in first["nodes"]} == {"e1", "e2"}
    assert first["suppressed"]["review_policy"] == 1
    assert len(first["projection_hash"]) == 64


async def test_projection_reports_pending_review_when_only_candidates_exist(db):
    """候选被策略全拦时必须是 PENDING_REVIEW 裁决并携带计数，不得伪装成 MISS。"""
    db.add(m.KnowledgeBase(kb_id="kb", tenant_id=7, name="KB", kb_type="milvus", share_config={}))
    db.add_all(
        [
            m.KnowledgeGraphEntity(
                entity_id="e1",
                kb_id="kb",
                canonical_identity="gene:x",
                normalized_name="x",
                label="Gene",
                name="X",
                review_status="CANDIDATE",
            ),
            m.KnowledgeGraphEntity(
                entity_id="e2",
                kb_id="kb",
                canonical_identity="trait:y",
                normalized_name="y",
                label="Trait",
                name="Y",
                review_status="CANDIDATE",
            ),
            m.KnowledgeGraphTriple(
                triple_id="t-c1",
                kb_id="kb",
                source_entity_id="e1",
                target_entity_id="e2",
                relation_type="affects",
                content="c1",
                review_status="CANDIDATE",
            ),
            m.KnowledgeGraphTriple(
                triple_id="t-c2",
                kb_id="kb",
                source_entity_id="e1",
                target_entity_id="e2",
                relation_type="affects",
                content="c2",
                review_status="CANDIDATE",
            ),
            m.KnowledgeGraphTriple(
                triple_id="t-r1",
                kb_id="kb",
                source_entity_id="e1",
                target_entity_id="e2",
                relation_type="affects",
                content="r1",
                review_status="REJECTED",
            ),
        ]
    )
    await db.flush()
    snapshot = await project_graph_snapshot(db, _contract(), members=_CLOSED_MEMBERS)

    assert snapshot["outcome"] == "PENDING_REVIEW"
    assert snapshot["edges"] == []
    assert snapshot["nodes"] == []
    assert snapshot["suppressed"]["review_policy"] == 2
    assert snapshot["suppressed"]["rejected"] == 1


async def test_projection_admits_candidates_only_with_member_policy(db):
    """成员开启 evidence_candidate 后：候选边分层发布（verified 排序在前，review_status 保留）。"""
    await _seed_graph(db)
    snapshot = await project_graph_snapshot(db, _contract(), members=_CANDIDATE_MEMBERS)

    assert snapshot["outcome"] == "HIT"
    assert snapshot["review_policy"] == "approved_plus_candidate"
    assert [edge["triple_id"] for edge in snapshot["edges"]] == ["t-approved", "t-candidate"]
    assert snapshot["edges"][1]["review_status"] == "CANDIDATE"
    assert {node["entity_id"] for node in snapshot["nodes"]} == {"e1", "e2", "e3"}
    assert snapshot["suppressed"]["review_policy"] == 0


async def test_projection_is_scope_restricted_and_reports_miss(db):
    """种子/成员为空（闭世界无数据）时 MISS 且不发明边；成员不含该 KB 时同样查不到。"""
    await _seed_graph(db)
    empty = await project_graph_snapshot(db, _contract(), members=[])
    assert empty["outcome"] == "MISS"
    assert empty["nodes"] == []
    assert empty["edges"] == []

    foreign = await project_graph_snapshot(db, _contract(), members=[{"kb_id": "kb-other", "structured_enabled": True}])
    assert foreign["outcome"] == "MISS"
    assert foreign["edges"] == []


def test_graph_snapshot_is_hash_covered():
    """挂接点回归锁：graph_snapshot 属于 contract_hash 稳定域——快照变化必须改变
    契约哈希，防止后续有人把该键排除出 _hash_contract（镜像 figure 投影的同款锁）。
    """
    from yuxi.knowledge.orchestration.retrieval_orchestrator import _hash_contract

    base = {
        "retrieval_id": "kr-hash",
        "status": "COMPLETED",
        "claims": [{"triple_ids": ["t-1"]}],
        "graph_snapshot": {"schema": "graph_snapshot_v1", "edges": [{"triple_id": "t-1"}]},
    }
    changed = {**base, "graph_snapshot": {"schema": "graph_snapshot_v1", "edges": [{"triple_id": "t-2"}]}}
    assert _hash_contract(base) != _hash_contract(changed)


async def test_projection_aggregates_parallel_bilingual_edges(db):
    """双语平行断言（调控/regulates → 同一 normalized 目标）聚合为一组展示边。"""
    db.add(m.KnowledgeBase(kb_id="kb", tenant_id=7, name="KB", kb_type="milvus", share_config={}))
    db.add_all(
        [
            m.KnowledgeGraphEntity(
                entity_id="s1",
                kb_id="kb",
                canonical_identity="gene:x",
                normalized_name="x",
                label="Gene",
                name="X",
                review_status="CANDIDATE",
            ),
            m.KnowledgeGraphEntity(
                entity_id="t1",
                kb_id="kb",
                canonical_identity="trait:chalky",
                normalized_name="chalkiness",
                label="Trait",
                name="chalkiness",
                review_status="CANDIDATE",
            ),
            m.KnowledgeGraphEntity(
                entity_id="t2",
                kb_id="kb",
                canonical_identity="trait:chalky-alt",
                normalized_name="chalkiness",
                label="Trait",
                name="chalkiness ",
                review_status="CANDIDATE",
            ),
            m.KnowledgeGraphTriple(
                triple_id="p-zh",
                kb_id="kb",
                source_entity_id="s1",
                target_entity_id="t1",
                relation_type="调控",
                content="zh",
                review_status="CANDIDATE",
                support_count=5,
            ),
            m.KnowledgeGraphTriple(
                triple_id="p-en",
                kb_id="kb",
                source_entity_id="s1",
                target_entity_id="t2",
                relation_type="regulates",
                content="en",
                review_status="CANDIDATE",
                support_count=2,
            ),
            m.KnowledgeGraphTriple(
                triple_id="p-seed-loop",
                kb_id="kb",
                source_entity_id="s1",
                target_entity_id="s1",
                relation_type="regulates",
                content="loop",
                review_status="CANDIDATE",
            ),
        ]
    )
    await db.flush()
    snapshot = await project_graph_snapshot(
        db,
        {
            "retrieval_id": "kr-agg",
            "status": "COMPLETED",
            "retrieval_plan": {"intent": "RELATION_LOOKUP", "target_mention": "X"},
            "resolved_entities": [{"entity_id": "s1", "name": "X"}],
            "claims": [],
        },
        members=_CANDIDATE_MEMBERS,
    )

    assert snapshot["outcome"] == "HIT"
    assert len(snapshot["edges"]) == 1
    edge = snapshot["edges"][0]
    assert edge["parallel_count"] == 2
    assert edge["predicates"] == ["regulates", "调控"]
    assert edge["triple_id"] == "p-zh"  # 代表边取支持度最高者
    assert edge["relation_group"] == "FUNCTIONAL_REGULATION"
    # 目标变体折叠为一个展示节点 + 一个种子节点；自环边不展示
    assert len(snapshot["nodes"]) == 2
    assert snapshot["total_raw_edge_count"] == 3
    assert snapshot["aggregation"]["group_count"] == 1
    assert snapshot["seed_variant_count"] == 1


async def test_projection_mixed_review_group_keeps_reviewed_representative(db):
    """候选支持度更高也不能把含已审核事实的聚合组整体降级。"""
    db.add(m.KnowledgeBase(kb_id="kb", tenant_id=7, name="KB", kb_type="milvus", share_config={}))
    db.add_all(
        [
            m.KnowledgeGraphEntity(
                entity_id="s1",
                kb_id="kb",
                canonical_identity="gene:x",
                normalized_name="x",
                label="Gene",
                name="X",
                review_status="APPROVED",
            ),
            m.KnowledgeGraphEntity(
                entity_id="t1",
                kb_id="kb",
                canonical_identity="trait:y",
                normalized_name="y",
                label="Trait",
                name="Y",
                review_status="APPROVED",
            ),
            m.KnowledgeGraphTriple(
                triple_id="reviewed",
                kb_id="kb",
                source_entity_id="s1",
                target_entity_id="t1",
                relation_type="regulates",
                content="reviewed",
                review_status="APPROVED",
                support_count=1,
            ),
            m.KnowledgeGraphTriple(
                triple_id="candidate",
                kb_id="kb",
                source_entity_id="s1",
                target_entity_id="t1",
                relation_type="调控",
                content="candidate",
                review_status="CANDIDATE",
                support_count=99,
            ),
        ]
    )
    await db.flush()

    snapshot = await project_graph_snapshot(
        db,
        {
            "retrieval_id": "kr-mixed",
            "status": "COMPLETED",
            "retrieval_plan": {"intent": "RELATION_LOOKUP", "target_mention": "X"},
            "resolved_entities": [{"entity_id": "s1", "name": "X"}],
            "claims": [],
        },
        members=_CANDIDATE_MEMBERS,
    )

    edge = snapshot["edges"][0]
    assert edge["triple_id"] == "reviewed"
    assert edge["review_status"] == "APPROVED"
    assert edge["triple_ids"] == ["candidate", "reviewed"]
    assert edge["reviewed_triple_ids"] == ["reviewed"]
    assert edge["candidate_triple_ids"] == ["candidate"]
    assert edge["reviewed_parallel_count"] == 1
    assert edge["candidate_parallel_count"] == 1


async def test_projection_never_aggregates_parallel_edges_across_knowledge_bases(db):
    """同名实体和同类谓词也必须保留知识库来源边界。"""
    db.add_all(
        [
            m.KnowledgeBase(kb_id="kb-a", tenant_id=7, name="A", kb_type="milvus", share_config={}),
            m.KnowledgeBase(kb_id="kb-b", tenant_id=7, name="B", kb_type="milvus", share_config={}),
            m.KnowledgeGraphEntity(
                entity_id="seed-a",
                kb_id="kb-a",
                canonical_identity="gene:x:a",
                normalized_name="x",
                label="Gene",
                name="X",
                review_status="APPROVED",
            ),
            m.KnowledgeGraphEntity(
                entity_id="seed-b",
                kb_id="kb-b",
                canonical_identity="gene:x:b",
                normalized_name="x",
                label="Gene",
                name="X",
                review_status="APPROVED",
            ),
            m.KnowledgeGraphEntity(
                entity_id="target-a",
                kb_id="kb-a",
                canonical_identity="trait:y:a",
                normalized_name="y",
                label="Trait",
                name="Y",
                review_status="APPROVED",
            ),
            m.KnowledgeGraphEntity(
                entity_id="target-b",
                kb_id="kb-b",
                canonical_identity="trait:y:b",
                normalized_name="y",
                label="Trait",
                name="Y",
                review_status="APPROVED",
            ),
            m.KnowledgeGraphTriple(
                triple_id="edge-a",
                kb_id="kb-a",
                source_entity_id="seed-a",
                target_entity_id="target-a",
                relation_type="regulates",
                content="a",
                review_status="APPROVED",
            ),
            m.KnowledgeGraphTriple(
                triple_id="edge-b",
                kb_id="kb-b",
                source_entity_id="seed-b",
                target_entity_id="target-b",
                relation_type="regulates",
                content="b",
                review_status="APPROVED",
            ),
        ]
    )
    await db.flush()

    snapshot = await project_graph_snapshot(
        db,
        {
            "retrieval_id": "kr-cross-kb",
            "status": "COMPLETED",
            "retrieval_plan": {"intent": "RELATION_LOOKUP", "target_mention": "X"},
            "resolved_entities": [
                {"entity_id": "seed-a", "name": "X"},
                {"entity_id": "seed-b", "name": "X"},
            ],
            "claims": [],
        },
        members=[{"kb_id": "kb-a"}, {"kb_id": "kb-b"}],
    )

    assert snapshot["aggregation"]["strategy"].startswith("kb_id+")
    assert snapshot["aggregation"]["group_count"] == 2
    assert {edge["kb_id"] for edge in snapshot["edges"]} == {"kb-a", "kb-b"}
    assert {edge["triple_id"] for edge in snapshot["edges"]} == {"edge-a", "edge-b"}
    assert {node["entity_id"] for node in snapshot["nodes"]} == {"seed-a", "target-a", "target-b"}
