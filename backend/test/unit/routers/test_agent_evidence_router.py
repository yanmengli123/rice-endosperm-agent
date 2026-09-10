from __future__ import annotations

import importlib
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


def _client(monkeypatch) -> TestClient:
    monkeypatch.setattr(agent_router_module, "AgentRunRepository", _RunRepository)
    monkeypatch.setattr(agent_router_module, "knowledge_base", _KnowledgeManager())
    app = FastAPI()
    app.include_router(agent_router_module.agent_router, prefix="/api")

    async def fake_db():
        return object()

    async def fake_user():
        return SimpleNamespace(uid="user-1", role="user", department_id=1)

    app.dependency_overrides[get_db] = fake_db
    app.dependency_overrides[get_required_user] = fake_user
    return TestClient(app)


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

    async def assemble(_db, run_id, *, allowed_kb_ids):
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
