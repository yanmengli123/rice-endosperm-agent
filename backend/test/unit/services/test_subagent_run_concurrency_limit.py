from __future__ import annotations

from types import SimpleNamespace

import pytest
from yuxi.agents.context import DEFAULT_MAX_CONCURRENT_SUBAGENT_RUNS, HARD_MAX_CONCURRENT_SUBAGENT_RUNS
from yuxi.repositories.agent_repository import AgentRepository
from yuxi.services.subagent_run_service import (
    SubagentRunConcurrencyLimit,
    SubagentRunService,
    resolve_max_concurrent_subagent_runs,
)


def _agent_with_limit(value):
    return SimpleNamespace(config_json={"context": {"max_concurrent_subagent_runs": value}})


@pytest.mark.parametrize(
    ("agent", "expected"),
    [
        (None, DEFAULT_MAX_CONCURRENT_SUBAGENT_RUNS),
        (SimpleNamespace(config_json={"context": {}}), DEFAULT_MAX_CONCURRENT_SUBAGENT_RUNS),
        (SimpleNamespace(config_json=None), DEFAULT_MAX_CONCURRENT_SUBAGENT_RUNS),
        (_agent_with_limit("abc"), DEFAULT_MAX_CONCURRENT_SUBAGENT_RUNS),
        (_agent_with_limit(5), 5),
        (_agent_with_limit("2"), 2),
        (_agent_with_limit(0), 1),
        (_agent_with_limit(-3), 1),
        (_agent_with_limit(99), HARD_MAX_CONCURRENT_SUBAGENT_RUNS),
    ],
)
def test_resolve_max_concurrent_subagent_runs_clamps_and_falls_back(agent, expected):
    assert resolve_max_concurrent_subagent_runs(agent) == expected


def test_concurrency_limit_payload_is_recoverable_not_failure():
    payload = SubagentRunConcurrencyLimit(limit=2, active_run_ids=("r1", "r2")).to_payload()
    assert payload["status"] == "concurrency_limit"
    assert payload["limit"] == 2
    assert payload["active_run_ids"] == ["r1", "r2"]
    assert "subagent_await" in payload["message"]


def _run(run_id: str, thread_id: str):
    return SimpleNamespace(id=run_id, conversation_thread_id=thread_id)


@pytest.fixture
def service(monkeypatch):
    svc = SubagentRunService(db=SimpleNamespace())

    async def get_by_slug(_self, _slug):
        return _agent_with_limit(2)

    monkeypatch.setattr(AgentRepository, "get_by_slug", get_by_slug)
    return svc


@pytest.mark.asyncio
async def test_enforce_limit_raises_when_other_threads_reach_limit(service, monkeypatch):
    async def active_runs(_created_by_run_id, _uid):
        return [_run("r1", "child-a"), _run("r2", "child-b")]

    monkeypatch.setattr(service.run_repo, "list_active_child_runs_for_user", active_runs)

    with pytest.raises(SubagentRunConcurrencyLimit) as excinfo:
        await service._enforce_concurrency_limit(
            creator_run=SimpleNamespace(id="parent", agent_slug="orchestrator"),
            uid="u1",
            child_thread_id="child-new",
        )
    assert excinfo.value.limit == 2
    assert excinfo.value.active_run_ids == ("r1", "r2")


@pytest.mark.asyncio
async def test_enforce_limit_ignores_same_thread_run_so_busy_semantics_win(service, monkeypatch):
    async def active_runs(_created_by_run_id, _uid):
        return [_run("r1", "child-a"), _run("r2", "child-same")]

    monkeypatch.setattr(service.run_repo, "list_active_child_runs_for_user", active_runs)

    await service._enforce_concurrency_limit(
        creator_run=SimpleNamespace(id="parent", agent_slug="orchestrator"),
        uid="u1",
        child_thread_id="child-same",
    )


@pytest.mark.asyncio
async def test_enforce_limit_passes_under_limit(service, monkeypatch):
    async def active_runs(_created_by_run_id, _uid):
        return [_run("r1", "child-a")]

    monkeypatch.setattr(service.run_repo, "list_active_child_runs_for_user", active_runs)

    await service._enforce_concurrency_limit(
        creator_run=SimpleNamespace(id="parent", agent_slug="orchestrator"),
        uid="u1",
        child_thread_id="child-new",
    )
