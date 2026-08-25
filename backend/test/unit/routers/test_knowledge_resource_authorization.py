from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from server.routers import graph_router, knowledge_eval_router
from server.utils import knowledge_access


def _router_dependencies(router):
    return {item.dependency for item in router.dependencies}


def test_graph_and_evaluation_routers_install_knowledge_guard():
    assert knowledge_access.authorize_knowledge_path in _router_dependencies(graph_router.graph)
    assert knowledge_access.authorize_knowledge_path in _router_dependencies(
        knowledge_eval_router.evaluation
    )


@pytest.mark.asyncio
async def test_knowledge_guard_distinguishes_read_and_manage(monkeypatch):
    calls = []

    async def accessible(user, kb_id):
        calls.append(("read", user["uid"], kb_id))
        return True

    async def manageable(user, kb_id):
        calls.append(("manage", user["uid"], kb_id))
        return True

    monkeypatch.setattr(knowledge_access.knowledge_base, "check_accessible", accessible)
    monkeypatch.setattr(knowledge_access.knowledge_base, "check_manageable", manageable)
    user = SimpleNamespace(uid="alice", role="admin", department_id=3)

    await knowledge_access.authorize_knowledge_resource(user, "kb-a", manage=False)
    await knowledge_access.authorize_knowledge_resource(user, "kb-a", manage=True)

    assert calls == [("read", "alice", "kb-a"), ("manage", "alice", "kb-a")]


@pytest.mark.asyncio
async def test_knowledge_guard_hides_foreign_resource(monkeypatch):
    async def denied(_user, _kb_id):
        return False

    monkeypatch.setattr(knowledge_access.knowledge_base, "check_accessible", denied)
    user = SimpleNamespace(uid="alice", role="admin", department_id=3)

    with pytest.raises(HTTPException) as exc_info:
        await knowledge_access.authorize_knowledge_resource(user, "kb-foreign", manage=False)

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_router_guard_reads_kb_id_from_query_without_declaring_parameter(monkeypatch):
    captured = {}

    async def authorize(user, kb_id, *, manage):
        captured.update(uid=user.uid, kb_id=kb_id, manage=manage)

    monkeypatch.setattr(knowledge_access, "authorize_knowledge_resource", authorize)
    request = SimpleNamespace(
        method="PUT",
        path_params={},
        query_params={"kb_id": "kb-query"},
        url=SimpleNamespace(path="/api/graph/settings"),
    )
    user = SimpleNamespace(uid="alice")

    await knowledge_access.authorize_knowledge_path(request, current_user=user)

    assert captured == {"uid": "alice", "kb_id": "kb-query", "manage": True}


@pytest.mark.asyncio
async def test_dataset_id_only_operation_resolves_owning_kb(monkeypatch):
    captured = {}

    class Service:
        async def get_dataset_kb_id(self, dataset_id):
            captured["dataset_id"] = dataset_id
            return "kb-owner"

    async def authorize(user, kb_id, *, manage):
        captured.update(uid=user.uid, kb_id=kb_id, manage=manage)

    monkeypatch.setattr(knowledge_eval_router, "authorize_knowledge_resource", authorize)
    user = SimpleNamespace(uid="alice")
    await knowledge_eval_router._authorize_dataset(Service(), "dataset-1", user, manage=True)

    assert captured == {
        "dataset_id": "dataset-1",
        "uid": "alice",
        "kb_id": "kb-owner",
        "manage": True,
    }
