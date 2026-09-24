from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from server.routers import graph_router, knowledge_eval_router
from server.utils import knowledge_access


def _router_dependencies(router):
    return {item.dependency for item in router.dependencies}


def test_graph_and_evaluation_routers_install_knowledge_guard():
    # 图谱路由使用能力感知守卫（accessible/manageable ∪ KB 成员能力），评估路由沿用原守卫
    assert knowledge_access.authorize_graph_path in _router_dependencies(graph_router.graph)
    assert knowledge_access.authorize_knowledge_path in _router_dependencies(knowledge_eval_router.evaluation)


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


@pytest.mark.asyncio
async def test_graph_guard_falls_back_to_member_capability_for_reads(monkeypatch):
    """图卡证据抽屉依赖的租户门：accessible 拒绝时，KB 成员 viewer 仍可读，非成员 404。

    锁定 /api/graph/evidence/* 的授权语义——聊天关系图展开证据时不得因 share_config
    不可见而误杀已授权审阅者，也不得向跨租户调用者泄露资源存在性。
    """
    async def denied(_user, _kb_id):
        return False

    monkeypatch.setattr(knowledge_access.knowledge_base, "check_accessible", denied)

    async def capability_of_viewer(self, kb_id, uid):
        return "viewer"

    async def capability_of_none(self, kb_id, uid):
        return None

    request = SimpleNamespace(
        method="GET",
        path_params={},
        query_params={"kb_id": "kb-graph"},
        url=SimpleNamespace(path="/api/graph/evidence/triple"),
    )
    user = SimpleNamespace(uid="reviewer-1")

    monkeypatch.setattr(
        "yuxi.repositories.knowledge_graph_review_repository.KnowledgeGraphReviewRepository.get_member_capability",
        capability_of_viewer,
    )
    await knowledge_access.authorize_graph_path(request, current_user=user)  # viewer 读放行，不抛

    monkeypatch.setattr(
        "yuxi.repositories.knowledge_graph_review_repository.KnowledgeGraphReviewRepository.get_member_capability",
        capability_of_none,
    )
    with pytest.raises(HTTPException) as exc_info:
        await knowledge_access.authorize_graph_path(request, current_user=user)
    assert exc_info.value.status_code == 404
