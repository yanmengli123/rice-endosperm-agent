"""R1 门禁送审队列与冲突队列的服务编排（无 DB：伪造仓储 + monkeypatch 编排依赖）。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from yuxi.knowledge.graphs.graph_review_service import GraphReviewService
from yuxi.repositories.knowledge_graph_review_repository import (
    CONFLICT_RESOLVE,
    GATE_REVIEW_DISCARD,
    GATE_REVIEW_PROMOTE,
)

_REVIEW = {
    "review_id": "rev-1",
    "kb_id": "kb1",
    "file_id": "f1",
    "chunk_id": "c1",
    "gate_code": "G9_NEGATION_REVIEW",
    "status": "PENDING",
    "candidate": {
        "subject": "OsPIP2;1",
        "subject_label": "Gene",
        "predicate": "EXPRESSION_IN",
        "object": "drought",
        "object_label": "Condition",
        "evidence_quote": "did not localize",
    },
    "resolution": None,
    "resolved_by": None,
}

_CONFLICT = {
    "conflict_id": "cf-1",
    "kb_id": "kb1",
    "kind": "DEFINITION",
    "subject_ref": "灌浆中期",
    "detail": {"entity": "灌浆中期", "intervals": [(10, 25), (12, 28)]},
    "status": "OPEN",
}


class _FakeReviewRepo:
    def __init__(self):
        self.gate_reviews: dict[str, dict] = {"rev-1": dict(_REVIEW)}
        self.conflicts: dict[str, dict] = {"cf-1": dict(_CONFLICT)}
        self.audits: list[dict] = []
        self.decisions: list[dict] = []
        self.gate_resolutions: list[tuple[str, str]] = []
        self.conflict_resolutions: list[tuple[str, str]] = []

    async def get_gate_review(self, review_id):
        return self.gate_reviews.get(review_id)

    async def resolve_gate_review_row(self, review_id, *, resolution, resolved_by):
        row = self.gate_reviews.get(review_id)
        if row is None:
            return None
        if row["status"] == "RESOLVED":
            return {
                "review_id": review_id,
                "status": "RESOLVED",
                "unchanged": row["resolution"] == resolution,
                "resolution": row["resolution"],
            }
        row["status"] = "RESOLVED"
        row["resolution"] = resolution
        return {"review_id": review_id, "status": "RESOLVED", "unchanged": False, "resolution": resolution}

    async def get_conflict(self, conflict_id):
        return self.conflicts.get(conflict_id)

    async def resolve_conflict_row(self, conflict_id, *, resolution, note, resolved_by):
        row = self.conflicts.get(conflict_id)
        if row is None:
            return None
        if row["status"] == "RESOLVED":
            return {
                "conflict_id": conflict_id,
                "status": "RESOLVED",
                "unchanged": True,
                "resolution": row["resolution"],
            }
        row["status"] = "RESOLVED"
        row["resolution"] = resolution
        return {"conflict_id": conflict_id, "status": "RESOLVED", "unchanged": False, "resolution": resolution}

    async def append_audit(self, **kwargs):
        self.audits.append(kwargs)

    async def save_decision(self, **kwargs):
        self.decisions.append(kwargs)
        return {"decision_id": "d1", "version": 1, **{k: v for k, v in kwargs.items() if k in ("action", "target_id")}}


def _service(repo: _FakeReviewRepo) -> GraphReviewService:
    service = GraphReviewService(
        review_repo=repo,
        graph_repo=SimpleNamespace(upsert_chunk_graph=AsyncMock()),
        chunk_repo=SimpleNamespace(get_by_chunk_id=AsyncMock()),
        kb_repo=SimpleNamespace(get_by_kb_id=AsyncMock(return_value=SimpleNamespace(kb_type="milvus"))),
    )
    service.graph = SimpleNamespace(apply_review_projection=MagicMock())
    service._tenant_id = AsyncMock(return_value=1)
    return service


# ── 门禁送审裁决 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_discard_is_idempotent_and_audited():
    repo = _FakeReviewRepo()
    service = _service(repo)
    result = await service.resolve_gate_review("kb1", "rev-1", action="DISCARD", actor_uid="u1")
    assert result["action"] == "DISCARD"
    assert repo.gate_reviews["rev-1"]["status"] == "RESOLVED"
    assert repo.audits and repo.audits[0]["action"] == GATE_REVIEW_DISCARD

    again = await service.resolve_gate_review("kb1", "rev-1", action="DISCARD", actor_uid="u2")
    assert again["unchanged"] is True
    assert len(repo.audits) == 1  # 幂等：不重复审计


@pytest.mark.asyncio
async def test_promote_rejects_when_quote_left_chunk_content():
    repo = _FakeReviewRepo()
    service = _service(repo)
    service._load_chunk = AsyncMock(return_value=SimpleNamespace(chunk_id="c1", content="OsPIP2;1 was expressed."))

    with pytest.raises(ValueError, match="候选引文已不在 chunk 原文"):
        await service.resolve_gate_review("kb1", "rev-1", action="PROMOTE", actor_uid="u1")
    assert repo.gate_reviews["rev-1"]["status"] == "PENDING"  # 校验失败不落裁决


@pytest.mark.asyncio
async def test_promote_happy_path_records_decision_and_closes_review():
    repo = _FakeReviewRepo()
    service = _service(repo)
    service._load_chunk = AsyncMock(
        return_value=SimpleNamespace(chunk_id="c1", content="OsPIP2;1 did not localize under drought stress.")
    )
    service._validate_relation_type = AsyncMock()
    service._materialize_snapshot = AsyncMock()
    service._entity_by_name = AsyncMock(
        side_effect=lambda kb_id, surface, label: (
            {
                "entity_id": f"e-{surface}",
                "normalized_name": surface.lower(),
                "label": label,
                "name": surface,
                "attributes": [],
            }
            if surface in ("OsPIP2;1", "drought")
            else None
        )
    )
    result = await service.resolve_gate_review("kb1", "rev-1", action="PROMOTE", actor_uid="u1")
    assert result["triple_id"]
    assert result["action"] == "PROMOTE"
    assert repo.gate_reviews["rev-1"]["status"] == "RESOLVED"
    assert repo.gate_reviews["rev-1"]["resolution"] == f"PROMOTED:{result['triple_id']}"
    assert repo.decisions and repo.decisions[0]["audit_action"] == GATE_REVIEW_PROMOTE
    service.graph.apply_review_projection.assert_called_once()


@pytest.mark.asyncio
async def test_promote_fails_when_endpoint_entities_missing():
    repo = _FakeReviewRepo()
    service = _service(repo)
    service._load_chunk = AsyncMock(
        return_value=SimpleNamespace(chunk_id="c1", content="OsPIP2;1 did not localize under drought stress.")
    )
    service._entity_by_name = AsyncMock(return_value=None)
    with pytest.raises(ValueError, match="端点实体不存在"):
        await service.resolve_gate_review("kb1", "rev-1", action="PROMOTE", actor_uid="u1")


@pytest.mark.asyncio
async def test_review_from_other_kb_is_rejected():
    repo = _FakeReviewRepo()
    service = _service(repo)
    with pytest.raises(ValueError, match="不存在"):
        await service.resolve_gate_review("kb-other", "rev-1", action="DISCARD", actor_uid="u1")


@pytest.mark.asyncio
async def test_invalid_action_rejected():
    service = _service(_FakeReviewRepo())
    with pytest.raises(ValueError, match="PROMOTE 或 DISCARD"):
        await service.resolve_gate_review("kb1", "rev-1", action="MAYBE", actor_uid="u1")


# ── 冲突裁决 ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_conflict_resolution_validates_enum_and_scope():
    service = _service(_FakeReviewRepo())
    with pytest.raises(ValueError, match="SUPERSEDED"):
        await service.resolve_conflict("kb1", "cf-1", resolution="DELETE", actor_uid="u1")
    with pytest.raises(ValueError, match="不存在"):
        await service.resolve_conflict("kb-other", "cf-1", resolution="CONTESTED", actor_uid="u1")


@pytest.mark.asyncio
async def test_conflict_resolution_is_idempotent_and_audited_once():
    repo = _FakeReviewRepo()
    service = _service(repo)
    result = await service.resolve_conflict("kb1", "cf-1", resolution="CONTESTED", actor_uid="u1", note="两口径并陈")
    assert result["resolution"] == "CONTESTED"
    assert repo.conflicts["cf-1"]["status"] == "RESOLVED"
    assert repo.audits and repo.audits[0]["action"] == CONFLICT_RESOLVE

    again = await service.resolve_conflict("kb1", "cf-1", resolution="CONTESTED", actor_uid="u2")
    assert again["unchanged"] is True
    assert len(repo.audits) == 1


@pytest.mark.asyncio
async def test_conflict_resolution_never_touches_evidence_rows():
    # 冲突裁决的落点只有冲突行 + 审计账本：断言仓储上没有任何图谱证据写调用
    repo = _FakeReviewRepo()
    service = _service(repo)
    await service.resolve_conflict("kb1", "cf-1", resolution="RECONCILED", actor_uid="u1")
    assert repo.decisions == []  # 不产生图谱决策
    assert set(repo.conflicts["cf-1"].keys()) == set(_CONFLICT.keys()) | {"resolution"}
