"""发布治理图谱段单测（P2）：决策冻结清单、水位哈希、策略修订挂接。

假件风格同 test_graph_governance.py，不触真实 PG/Neo4j。
"""

from types import SimpleNamespace

import pytest

from yuxi.knowledge.graphs.graph_governance_service import GraphGovernanceService
from yuxi.repositories.knowledge_graph_review_repository import KnowledgeGraphReviewRepository
from yuxi.services.knowledge_release_service import _build_graph_release_section, _ensure_graph_policy_revision


class _FakeScalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeExecResult:
    def __init__(self, rows):
        self._scalars = _FakeScalars(rows)

    def scalars(self):
        return self._scalars


class _FakeSession:
    """按 SQL 语句里出现的模型名分发假结果。"""

    def __init__(self, decision_rows, revision_rows=()):
        self._decision_rows = decision_rows
        self._revision_rows = list(revision_rows)
        self.added = []

    async def execute(self, stmt):
        statement = str(stmt)
        if "knowledge_graph_review_decisions" in statement:
            return _FakeExecResult(self._decision_rows)
        if "knowledge_retrieval_policy_revisions" in statement:
            return _FakeExecResult(self._revision_rows)
        return _FakeExecResult([])

    def add(self, obj):
        self.added.append(obj)


def _decision(target_id, action="APPROVE", version=2, quote="OsCIN2 正调控 Wx", kind="TRIPLE"):
    return SimpleNamespace(
        target_kind=kind,
        target_id=target_id,
        action=action,
        version=version,
        actor_uid="reviewer-1",
        pinned_chunk_id=f"chunk-{target_id}",
        pinned_quote=quote,
    )


@pytest.mark.asyncio
async def test_build_graph_release_section_freezes_watermark_and_rows(monkeypatch):
    kb = SimpleNamespace(
        kb_id="kb_rel",
        kb_type="milvus",
        tenant_id=7,
        additional_params={
            "graph_build_config": {
                "extractor_type": "llm_scientific",
                "extractor_options": {"model_spec": "gpt:test"},
                "created_at": "2026-01-01T00:00:00Z",
                "created_by": "admin",
            }
        },
    )

    async def fake_get_settings(self, kb_id):
        return {
            "review_policy": "approved_only",
            "batch_admission": "strict",
            "maker_checker": True,
            "review_sla_hours": None,
            "summary_cache_ttl_seconds": 15,
        }

    async def fake_integrity(self, kb_id, **kwargs):
        return {
            "decisions_total": 2,
            "I3_approved_triples_without_pinned_quote": 0,
            "I5_decisions_without_audit": 0,
            "I6_canonical_with_decision": 0,
            "rejected_triple_ids": [],
        }

    monkeypatch.setattr(GraphGovernanceService, "get_settings", fake_get_settings)
    monkeypatch.setattr(KnowledgeGraphReviewRepository, "integrity_counts", fake_integrity)

    session = _FakeSession([_decision("t1"), _decision("e1", action="REJECT", version=1, quote=None, kind="ENTITY")])
    section, rows = await _build_graph_release_section(session, kb)

    assert section["review_policy"] == "approved_only"
    assert section["extraction"]["extractor_type"] == "llm_scientific"
    assert section["extraction"]["model_spec"] == "gpt:test"
    watermark = section["decisions_watermark"]
    assert watermark["total"] == 2
    assert watermark["max_version"] == 2
    assert watermark["counts_by_kind_action"] == {"TRIPLE:APPROVE": 1, "ENTITY:REJECT": 1}
    assert len(watermark["manifest_sha256"]) == 64
    assert section["integrity_snapshot"]["decisions_total"] == 2

    # 冻结行：pinned_quote 只存哈希前 16 位，不落原文
    by_target = {row.target_id: row for row in rows}
    assert by_target["t1"].pinned_quote_sha and len(by_target["t1"].pinned_quote_sha) == 16
    assert by_target["e1"].pinned_quote_sha is None
    assert all(row.release_id == "" for row in rows)  # 调用方（build_release）flush 后回填


@pytest.mark.asyncio
async def test_ensure_graph_policy_revision_reuses_same_hash_and_creates_new():
    kb = SimpleNamespace(kb_id="kb_rel", tenant_id=7)

    policy = {"graph_review_policy": "approved_only", "decisions_manifest_sha256": "abc"}
    empty_session = _FakeSession([], revision_rows=[])
    revision_id = await _ensure_graph_policy_revision(empty_session, kb, policy, operator_id="admin")
    assert revision_id.startswith("rp_")
    assert len(empty_session.added) == 1

    existing = SimpleNamespace(revision_id="rp_existing")
    reuse_session = _FakeSession([], revision_rows=[existing])
    reused = await _ensure_graph_policy_revision(reuse_session, kb, policy, operator_id="admin")
    assert reused == "rp_existing"
    assert reuse_session.added == []  # 同 hash 复用，不新增修订
