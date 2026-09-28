from __future__ import annotations

import importlib
import hashlib
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.utils.auth_middleware import get_db, get_required_user
from yuxi.knowledge import evidence as evidence_module

agent_router_module = importlib.import_module("server.routers.agent_router")


class _RunRepository:
    run = None

    def __init__(self, _db):
        pass

    async def get_run_for_user(self, run_id: str, uid: str):
        if self.run and self.run.id == run_id and self.run.uid == uid:
            return self.run
        return None


class _KnowledgeManager:
    async def get_databases_by_user(self, _user):
        return {
            "databases": [
                {"kb_id": "kb_current"},
                {"kb_id": "kb_not_frozen"},
            ]
        }


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _FakeDb:
    message_extra = None

    async def execute(self, _statement):
        return _ScalarResult(self.message_extra)


def _client(monkeypatch) -> TestClient:
    monkeypatch.setattr(agent_router_module, "AgentRunRepository", _RunRepository)
    monkeypatch.setattr(agent_router_module, "knowledge_base", _KnowledgeManager())
    app = FastAPI()
    app.include_router(agent_router_module.agent_router, prefix="/api")

    async def fake_db():
        return _FakeDb()

    async def fake_user():
        return SimpleNamespace(uid="user-1", role="user", department_id=1)

    app.dependency_overrides[get_db] = fake_db
    app.dependency_overrides[get_required_user] = fake_user
    return TestClient(app)


def _published_snapshot(kb_id: str = "kb_current") -> dict:
    return {
        "schema": "graph_snapshot_v1",
        "retrieval_id": "kr-export",
        "outcome": "HIT",
        "review_policy": "approved_only",
        "projection_hash": "a" * 64,
        "nodes": [
            {
                "entity_id": "e1",
                "kb_id": kb_id,
                "name": "GS3",
                "label": "Gene",
                "canonical_identity": "gs3",
                "is_seed": True,
            },
            {
                "entity_id": "e2",
                "kb_id": kb_id,
                "name": "grain size",
                "label": "Trait",
                "canonical_identity": "grain size",
                "is_seed": False,
            },
        ],
        "edges": [
            {
                "triple_id": "t1",
                "kb_id": kb_id,
                "source_entity_id": "e1",
                "target_entity_id": "e2",
                "predicate": "regulates",
                "review_status": "APPROVED",
            }
        ],
    }


def test_evidence_route_intersects_frozen_and_current_access(monkeypatch):
    _RunRepository.run = SimpleNamespace(
        id="run-1",
        uid="user-1",
        input_payload={
            "knowledge_scope_snapshot": {
                "effective_kb_ids": ["kb_current", "kb_revoked"],
            }
        },
    )
    captured = {}

    async def assemble(_db, run_id, *, allowed_kb_ids, question_text=None):
        captured.update({"run_id": run_id, "allowed_kb_ids": allowed_kb_ids})
        return {"run_id": run_id, "evidence": []}

    monkeypatch.setattr(evidence_module, "assemble_evidence_for_run", assemble)
    response = _client(monkeypatch).get("/api/agent/runs/run-1/evidence")

    assert response.status_code == 200, response.text
    assert captured == {"run_id": "run-1", "allowed_kb_ids": {"kb_current"}}


def test_evidence_route_hides_foreign_run(monkeypatch):
    _RunRepository.run = SimpleNamespace(id="run-1", uid="other-user", input_payload={})
    response = _client(monkeypatch).get("/api/agent/runs/run-1/evidence")

    assert response.status_code == 404
    assert response.json()["detail"] == "运行任务不存在"


def test_graph_snapshot_export_is_private_byte_hashed_and_conditional(monkeypatch):
    _RunRepository.run = SimpleNamespace(
        id="run-export",
        uid="user-1",
        output_message_id="message-1",
        input_payload={"knowledge_scope_snapshot": {"effective_kb_ids": ["kb_current"]}},
    )
    _FakeDb.message_extra = {"graph_snapshot": _published_snapshot()}
    client = _client(monkeypatch)

    first = client.get("/api/agent/runs/run-export/graph-snapshot-export?format=json")
    assert first.status_code == 200, first.text
    assert first.headers["cache-control"] == "private, no-cache"
    assert first.headers["vary"] == "Authorization"
    assert first.headers["etag"] == f'"{hashlib.sha256(first.content).hexdigest()}"'
    assert json.loads(first.content)["run_id"] == "run-export"

    cached = client.get(
        "/api/agent/runs/run-export/graph-snapshot-export?format=json",
        headers={"If-None-Match": f"W/{first.headers['etag']}"},
    )
    assert cached.status_code == 304
    assert cached.content == b""
    assert cached.headers["etag"] == first.headers["etag"]


def test_graph_snapshot_export_hides_snapshot_after_kb_access_revoked(monkeypatch):
    _RunRepository.run = SimpleNamespace(
        id="run-revoked",
        uid="user-1",
        output_message_id="message-1",
        input_payload={"knowledge_scope_snapshot": {"effective_kb_ids": ["kb_revoked"]}},
    )
    _FakeDb.message_extra = {"graph_snapshot": _published_snapshot("kb_revoked")}

    response = _client(monkeypatch).get("/api/agent/runs/run-revoked/graph-snapshot-export")
    assert response.status_code == 404
    assert response.json()["detail"] == "运行任务不存在"


def test_mcp_only_run_reports_document_evidence_not_requested(monkeypatch):
    _RunRepository.run = SimpleNamespace(
        id="run-mcp",
        uid="user-1",
        input_message_id=None,
        input_payload={
            "knowledge_scope_snapshot": {"effective_kb_ids": ["kb_current"]},
            "turn_execution_plan": {"plan_id": "tp-mcp", "source": {"policy": "MCP_ONLY"}},
            "run_source_manifest": {
                "plan_id": "tp-mcp",
                "source_policy": "MCP_ONLY",
                "document_evidence_requested": False,
                "used_planes": ["MCP_DATA"],
            },
        },
    )

    async def assemble(_db, run_id, *, allowed_kb_ids, question_text=None):
        del allowed_kb_ids, question_text
        return {"run_id": run_id, "evidence": [], "projection_status": "NO_RETRIEVAL"}

    monkeypatch.setattr(evidence_module, "assemble_evidence_for_run", assemble)
    payload = _client(monkeypatch).get("/api/agent/runs/run-mcp/evidence").json()

    assert payload["projection_status"] == "NOT_REQUESTED"
    assert payload["turn_execution_plan"]["source"]["policy"] == "MCP_ONLY"
    assert payload["source_manifest"]["used_planes"] == ["MCP_DATA"]


def test_required_document_evidence_failure_is_not_reported_as_no_retrieval(monkeypatch):
    _RunRepository.run = SimpleNamespace(
        id="run-no-scope",
        uid="user-1",
        input_message_id=None,
        input_payload={
            "knowledge_scope_snapshot": {"effective_kb_ids": []},
            "run_source_manifest": {
                "document_evidence_requested": True,
                "status": "PLAN_REJECTED",
                "error_code": "SOURCE_UNAVAILABLE",
            },
        },
    )

    async def assemble(_db, run_id, *, allowed_kb_ids, question_text=None):
        del allowed_kb_ids, question_text
        return {"run_id": run_id, "evidence": [], "projection_status": "NO_RETRIEVAL"}

    monkeypatch.setattr(evidence_module, "assemble_evidence_for_run", assemble)
    payload = _client(monkeypatch).get("/api/agent/runs/run-no-scope/evidence").json()

    assert payload["projection_status"] == "EVIDENCE_UNAVAILABLE"
