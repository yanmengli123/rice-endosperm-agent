import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from langchain_core.tools import ToolException

from yuxi.agents.middlewares import collaboration as middleware
from yuxi.services import collaboration_service


@pytest.fixture
def collaboration_tools(monkeypatch):
    @asynccontextmanager
    async def db_context():
        yield object()

    monkeypatch.setattr(middleware.pg_manager, "get_async_session_context", db_context)
    return lambda: {tool.name: tool for tool in middleware.collaboration_tools()}


@pytest.mark.parametrize("wait_all,ready", [(False, True), (True, False)])
async def test_multi_wait_distinguishes_partial_completion_and_timeout(
    collaboration_tools, monkeypatch, wait_all, ready
):
    async def tree(*_args, **_kwargs):
        return {
            "members": [
                {"run_id": "a", "status": "completed", "waiting_for_input": False},
                {"run_id": "b", "status": "running", "waiting_for_input": False},
            ]
        }

    monkeypatch.setattr(collaboration_service, "tree", tree)
    result = json.loads(
        await collaboration_tools()["subagent_wait"].coroutine(
            run_ids=["a", "b"],
            wait_all=wait_all,
            timeout_seconds=0,
            runtime=SimpleNamespace(context=SimpleNamespace(uid="alice", run_id="root")),
        )
    )
    assert result["completed_run_ids"] == ["a"]
    assert result["wait_timed_out"] is (not ready)


async def test_multi_wait_returns_human_waitpoint_without_claiming_completion(collaboration_tools, monkeypatch):
    async def tree(*_args, **_kwargs):
        return {"members": [{"run_id": "a", "status": "interrupted", "waiting_for_input": True}]}

    monkeypatch.setattr(collaboration_service, "tree", tree)
    result = json.loads(
        await collaboration_tools()["subagent_wait"].coroutine(
            run_ids=["a"],
            timeout_seconds=0,
            runtime=SimpleNamespace(context=SimpleNamespace(uid="alice", run_id="root")),
        )
    )
    assert result["waiting_for_input_run_ids"] == ["a"]
    assert result["completed_run_ids"] == [] and result["wait_timed_out"] is False
    with pytest.raises(ToolException, match="不属于"):
        await collaboration_tools()["subagent_wait"].coroutine(
            run_ids=["foreign"],
            timeout_seconds=0,
            runtime=SimpleNamespace(context=SimpleNamespace(uid="alice", run_id="root")),
        )


async def test_before_model_returns_checkpointed_delivery_ids(monkeypatch, collaboration_tools):
    seen = []

    async def guidance(db, *, uid, run_id, delivered_ids):
        seen.append((uid, run_id, delivered_ids))
        return ["message"], ["new-id"]

    monkeypatch.setattr(middleware, "pending_guidance", guidance)
    result = await middleware.CollaborationMiddleware().abefore_model(
        {"collaboration_delivered_ids": ["old-id"]},
        SimpleNamespace(context=SimpleNamespace(uid="alice", run_id="root")),
    )
    assert result == {"messages": ["message"], "collaboration_delivered_ids": ["old-id", "new-id"]}
    assert seen == [("alice", "root", {"old-id"})]
