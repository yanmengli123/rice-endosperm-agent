"""图谱治理服务单测：设置审计化、批量准入、fail-closed 过滤、风险分、发布门禁、maker-checker。

全部使用手写内存假件（同 test_graph_review_overlay.py 风格），不依赖真实 PG/Neo4j。
"""

from types import SimpleNamespace

import pytest

from yuxi.knowledge.graphs import graph_governance_service as governance_module
from yuxi.knowledge.graphs.graph_governance_service import (
    GraphGovernanceService,
    evaluate_batch_admission,
    invalidate_governance_summary,
    normalize_governance_settings,
    pick_best_pinned_chunk_id,
)
from yuxi.knowledge.graphs.graph_utils import compute_triple_risk_score
from yuxi.knowledge.graphs.milvus_graph_service import (
    REVIEW_POLICY_APPROVED_ONLY,
    filter_edges_by_policy,
)
from yuxi.knowledge.graphs.review_overlay import compose_reason


# ── 纯函数 ────────────────────────────────────────────────────


def test_normalize_governance_settings_fills_defaults_and_drops_invalid():
    settings = normalize_governance_settings(
        {"review_policy": "approved_only", "batch_admission": "relaxed", "maker_checker": True, "garbage": 1}
    )
    assert settings["review_policy"] == "approved_only"
    assert settings["batch_admission"] == "relaxed"
    assert settings["maker_checker"] is True
    assert settings["review_sla_hours"] is None
    assert settings["summary_cache_ttl_seconds"] == 15
    assert "garbage" not in settings

    bad = normalize_governance_settings(
        {"review_policy": "bogus", "batch_admission": "nope", "summary_cache_ttl_seconds": 1}
    )
    assert bad["review_policy"] == "candidates_visible"
    assert bad["batch_admission"] == "strict"
    assert bad["summary_cache_ttl_seconds"] == 15


@pytest.mark.parametrize(
    "admission,trust,summary,conflict,expected",
    [
        # 无 OK 引文：任何档位都不准入（没有可固定证据的候选必须逐条人工）
        ("relaxed", "VERIFIED_CORROBORATED", {"OK": 0, "DEGRADED": 1}, None, (False, "no_ok_quote")),
        # 冲突中的候选：任何档位都不准入
        ("relaxed", "VERIFIED_CORROBORATED", {"OK": 3}, "CONTESTED", (False, "conflict_open")),
        # strict：只有多源机器验证可批量
        ("strict", "VERIFIED_SINGLE", {"OK": 2}, None, (False, "below_threshold")),
        ("strict", "VERIFIED_CORROBORATED", {"OK": 2}, None, (True, "corroborated")),
        # standard：单源机器验证也放行
        ("standard", "VERIFIED_SINGLE", {"OK": 1}, None, (True, "single_source")),
        ("standard", "CANDIDATE", {"OK": 1}, None, (False, "below_threshold")),
        # relaxed：只要 OK 引文
        ("relaxed", "CANDIDATE", {"OK": 1}, None, (True, "ok_quote")),
    ],
)
def test_evaluate_batch_admission_levels(admission, trust, summary, conflict, expected):
    assert (
        evaluate_batch_admission(admission, trust_tier=trust, verification_summary=summary, conflict_status=conflict)
        == expected
    )


def test_pick_best_pinned_chunk_id_prefers_pinned_then_first_ok():
    mentions = [
        {"chunk_id": "c1", "verification": "DEGRADED", "quote": "旧引文"},
        {"chunk_id": "c2", "verification": "OK", "quote": "乙引文"},
        {"chunk_id": "c3", "verification": "OK", "quote": "甲引文", "pinned_by": "u1"},
    ]
    assert pick_best_pinned_chunk_id(mentions) == "c3"
    assert pick_best_pinned_chunk_id(mentions[:2]) == "c2"
    assert pick_best_pinned_chunk_id([{"chunk_id": "c1", "verification": "MISSING"}]) is None


def test_filter_edges_fail_closed_on_missing_status():
    result = {
        "edges": [
            {"id": "1", "properties": {"review_status": "APPROVED"}},
            {"id": "2", "properties": {}},
            {"id": "3", "properties": {"managed_projection": True}},
            {"id": "4", "properties": {"review_status": "CANDIDATE"}},
        ]
    }
    visible = [edge["id"] for edge in filter_edges_by_policy(result, REVIEW_POLICY_APPROVED_ONLY)["edges"]]
    assert visible == ["1", "3"]


def test_compute_triple_risk_score_signals():
    low = compute_triple_risk_score(
        hedge_any=False,
        machine_verified_any=True,
        confidence_max=0.95,
        literature_count=3,
        conflict_status="NONE",
    )
    high = compute_triple_risk_score(
        hedge_any=True,
        machine_verified_any=False,
        confidence_max=0.4,
        literature_count=1,
        conflict_status="CONTESTED",
    )
    assert low < high
    assert high == pytest.approx(1.0 + 1.5 + 1.5 + 1.0 + 1.0 + 2.0)
    assert low == pytest.approx(1.0 - 0.5)


def test_compose_reason_formats_and_ignores_invalid_codes():
    assert compose_reason("direction_error", "方向反了") == "[DIRECTION_ERROR] 方向反了"
    assert compose_reason("NOT_A_CODE", "说明") == "说明"
    assert compose_reason(None, "说明") == "说明"
    assert compose_reason("OTHER", "") == ""


# ── 服务层（假件）────────────────────────────────────────────


class _FakeKbRepo:
    def __init__(self, settings=None):
        self.record = SimpleNamespace(
            kb_id="kb_gov", kb_type="milvus", graph_governance_settings=settings, graph_view_settings={}
        )
        self.updates = []

    async def get_by_kb_id(self, kb_id):
        return self.record if kb_id == self.record.kb_id else None

    async def update(self, kb_id, data):
        self.updates.append(data)
        self.record.graph_governance_settings = data["graph_governance_settings"]
        return self.record


class _FakeReviewRepo:
    def __init__(self):
        self.audits = []
        self.gate_page = {"counts": {"PENDING": {"G9_NEGATION_REVIEW": 2, "_total": 2}}}
        self.conflict_page = {"counts": {"OPEN": {"DIRECTION": 1, "_total": 1}}}
        self.integrity = {
            "decisions_total": 3,
            "I3_approved_triples_without_pinned_quote": 0,
            "I3_approved_entities_without_pinned_quote": 0,
            "I5_decisions_without_audit": 0,
            "I6_canonical_with_decision": 0,
            "rejected_triple_ids": ["t_rej"],
        }
        self.status_counts = {"triples": {"CANDIDATE": 10}, "entities": {"CANDIDATE": 5}}
        self.decision_actors = {"reviewer-1"}
        self.last_audit = "2026-09-19T00:00:00+00:00"

    async def append_audit(self, **kwargs):
        self.audits.append(kwargs)

    async def list_gate_reviews(self, kb_id, **kwargs):
        return self.gate_page

    async def list_conflicts(self, kb_id, **kwargs):
        return self.conflict_page

    async def integrity_counts(self, kb_id, **kwargs):
        return dict(self.integrity)

    async def count_statuses(self, kb_id):
        return dict(self.status_counts)

    async def last_audit_at(self, kb_id):
        return self.last_audit

    async def list_decision_actors(self, kb_id):
        return set(self.decision_actors)


class _FakeGraphService:
    def __init__(self):
        self.chunk_repo = SimpleNamespace(count_graph_dead_by_kb_id=self._dead)

    async def _dead(self, kb_id):
        return 1

    async def get_status(self, kb_id):
        return {
            "configured": True,
            "locked": True,
            "total_chunks": 100,
            "pending_chunks": 8,
            "indexed_chunks": 92,
            "dead_chunks": 1,
            "stale_cached_chunks": 0,
            "build_task_status": None,
            "build_task_progress": 0,
        }


def _service(kb_settings=None):
    service = GraphGovernanceService(
        kb_repo=_FakeKbRepo(kb_settings),
        review_repo=_FakeReviewRepo(),
        graph_service=_FakeGraphService(),
    )
    service._tenant_id = _fake_tenant_id
    return service


async def _fake_tenant_id(actor_uid):
    return 1


@pytest.mark.asyncio
async def test_update_settings_persists_and_appends_audit(monkeypatch):
    service = _service()
    monkeypatch.setattr(
        GraphGovernanceService,
        "_release_summary",
        lambda self, kb_id: {"active_release_id": None, "last_release": None},
    )
    governance_module._SUMMARY_CACHE["kb_gov"] = (9999999999.0, {"stale": True})

    result = await service.update_settings(
        "kb_gov", actor_uid="admin-1", changes={"review_policy": "approved_only", "batch_admission": "standard"}
    )

    assert result["unchanged"] is False
    assert result["settings"]["review_policy"] == "approved_only"
    assert (
        service.kb_repo.updates
        and service.kb_repo.updates[0]["graph_governance_settings"]["batch_admission"] == "standard"
    )
    # 审计：action + before/after 快照 + 操作者
    assert len(service.review_repo.audits) == 1
    audit = service.review_repo.audits[0]
    assert audit["action"] == "GOVERNANCE_SETTINGS_UPDATE"
    assert audit["actor_uid"] == "admin-1"
    assert audit["before_snapshot"]["review_policy"] == "candidates_visible"
    assert audit["after_snapshot"]["review_policy"] == "approved_only"
    # 缓存被失效
    assert "kb_gov" not in governance_module._SUMMARY_CACHE


@pytest.mark.asyncio
async def test_update_settings_unchanged_writes_nothing():
    service = _service(kb_settings={"review_policy": "approved_only"})
    result = await service.update_settings("kb_gov", actor_uid="admin-1", changes={"review_policy": "approved_only"})
    assert result["unchanged"] is True
    assert service.kb_repo.updates == []
    assert service.review_repo.audits == []


@pytest.mark.asyncio
async def test_summary_aggregates_and_uses_ttl_cache(monkeypatch):
    async def _fake_release_summary(self, kb_id):
        return {"active_release_id": None, "last_release": None}

    service = _service(kb_settings={"batch_admission": "standard"})
    monkeypatch.setattr(GraphGovernanceService, "_release_summary", _fake_release_summary)
    first = await service.summary("kb_gov")
    assert first["cached"] is False
    assert first["counts"]["triples"]["CANDIDATE"] == 10
    assert first["gates"]["pending"]["_total"] == 2
    assert first["conflicts"]["open"]["_total"] == 1
    assert first["integrity"]["I4_rejected_triple_ids_count"] == 1  # 轻量口径：不重验引文
    assert first["build"]["dead_chunks"] == 1
    assert first["settings"]["batch_admission"] == "standard"

    second = await service.summary("kb_gov")
    assert second["cached"] is True

    refreshed = await service.summary("kb_gov", refresh=True)
    assert refreshed["cached"] is False

    invalidate_governance_summary("kb_gov")
    rebuilt = await service.summary("kb_gov")
    assert rebuilt["cached"] is False  # 失效后重建


@pytest.mark.asyncio
async def test_batch_preview_admission_flow():
    class _FakeEvidenceService:
        async def triple_evidence(self, kb_id, triple_id):
            if triple_id == "missing":
                raise ValueError(f"三元组 {triple_id} 不存在")
            if triple_id == "weak":
                return {
                    "trust_tier": "VERIFIED_SINGLE",
                    "verification_summary": {"OK": 1, "DEGRADED": 0},
                    "conflict_status": "NONE",
                    "mentions": [{"chunk_id": "c_weak", "verification": "OK", "quote": "q"}],
                }
            return {
                "trust_tier": "VERIFIED_CORROBORATED",
                "verification_summary": {"OK": 2},
                "conflict_status": "NONE",
                "mentions": [{"chunk_id": "c_good", "verification": "OK", "quote": "q", "pinned_by": "x"}],
            }

    service = _service()  # 默认 strict
    service._evidence_service = _FakeEvidenceService()
    result = await service.batch_preview("kb_gov", [{"id": "good"}, {"id": "weak"}, {"id": "missing"}])
    assert result["admission_level"] == "strict"
    assert result["admissible_count"] == 1
    assert result["blocked_count"] == 2
    good = next(item for item in result["items"] if item["target_id"] == "good")
    weak = next(item for item in result["items"] if item["target_id"] == "weak")
    missing = next(item for item in result["items"] if item["target_id"] == "missing")
    assert good["admissible"] is True and good["pinned_chunk_id"] == "c_good"
    assert weak["admissible"] is False and weak["reason"] == "below_threshold"
    assert missing["admissible"] is False and missing["reason"] == "not_found"

    # standard 档放行单源机器验证
    result_standard = await service.batch_preview("kb_gov", [{"id": "weak"}], admission="standard")
    assert result_standard["items"][0]["admissible"] is True


@pytest.mark.asyncio
async def test_evaluate_publish_gates_blockers_and_warnings(monkeypatch):
    service = _service(kb_settings={"maker_checker": True})

    gates = await service.evaluate_publish_gates("kb_gov", operator_uid="reviewer-1")
    # reviewer-1 在决策 actor 集合中 → maker-checker 阻断
    assert gates["go"] is False
    codes = {blocker["code"] for blocker in gates["blockers"]}
    assert "maker_checker_violation" in codes
    warning_codes = {warning["code"] for warning in gates["warnings"]}
    assert {"gate_reviews_pending", "conflicts_open", "dead_chunks"} <= warning_codes

    # 完整性违规 → blocker
    service.review_repo.integrity["I5_decisions_without_audit"] = 2
    gates = await service.evaluate_publish_gates("kb_gov", operator_uid="publisher-9")
    assert gates["go"] is False
    assert any(blocker["code"] == "integrity_violation" for blocker in gates["blockers"])

    # 全部干净 + 非决策 actor → go
    clean = _service(kb_settings={"maker_checker": True})
    clean.review_repo.integrity["rejected_triple_ids"] = []
    clean.review_repo.gate_page = {"counts": {}}
    clean.review_repo.conflict_page = {"counts": {}}
    gates = await clean.evaluate_publish_gates("kb_gov", operator_uid="publisher-9")
    assert gates["go"] is True and gates["blockers"] == []
