from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

from yuxi.agents.context import BaseContext
from yuxi.services import chat_service as svc


def test_apply_model_override_sets_model_from_meta():
    input_context = {"model": "agent-default"}
    svc._apply_model_override(input_context, {"model_spec": "user-pick"})
    assert input_context["model"] == "user-pick"


def test_apply_model_override_noop_without_model_spec():
    input_context = {"model": "agent-default"}
    svc._apply_model_override(input_context, {"request_id": "r1"})
    svc._apply_model_override(input_context, None)
    assert input_context["model"] == "agent-default"


def test_scope_snapshot_replaces_knowledge_ids_and_blocks_web_capabilities():
    input_context = {
        "knowledges": ["kb-outside-scope"],
        "tools": ["calculator", "tavily_search", "WEB_SEARCH", "search_web"],
        "subagents": ["researcher", "web-search"],
    }

    svc._apply_knowledge_scope_snapshot(
        input_context,
        {"effective_kb_ids": ["kb-rice"], "allow_web": False},
    )

    assert input_context["knowledges"] == ["kb-rice"]
    assert input_context["_effective_knowledge_scope"] == {
        "effective_kb_ids": ["kb-rice"],
        "allow_web": False,
    }
    assert input_context["tools"] == ["calculator"]
    assert input_context["subagents"] == ["researcher"]


def test_scope_snapshot_keeps_web_capabilities_only_when_explicitly_allowed():
    input_context = {
        "knowledges": [],
        "tools": ["tavily_search"],
        "subagents": ["web-search"],
    }

    svc._apply_knowledge_scope_snapshot(
        input_context,
        {"effective_kb_ids": ["kb-rice"], "allow_web": True},
    )

    assert input_context["knowledges"] == ["kb-rice"]
    assert input_context["_effective_knowledge_scope"] == {
        "effective_kb_ids": ["kb-rice"],
        "allow_web": True,
    }
    assert input_context["tools"] == ["tavily_search"]
    assert input_context["subagents"] == ["web-search"]


def test_scope_snapshot_survives_base_agent_context_reconstruction():
    snapshot = {"scope_id": "scope-rice", "scope_version": 4, "effective_kb_ids": ["kb-rice"]}
    input_context = {}
    svc._apply_knowledge_scope_snapshot(input_context, snapshot)

    context = BaseContext()
    context.update_from_dict(input_context)

    assert context._effective_knowledge_scope == snapshot


@pytest.mark.parametrize("member", [False, True])
async def test_resume_entry_resolves_member_backend_and_restores_shared_file_context(monkeypatch, member):
    from yuxi.agents.mcp import execution, artifact_materializer

    class ContextReached(Exception):
        pass

    meta = {
        "tenant_id": 1,
        "run_type": "resume",
        "subagent_runtime": member,
        "parent_thread_id": "parent",
        "file_thread_id": "shared",
        "skills_thread_id": "skills",
    }
    captured = {}

    async def resolve(**kwargs):
        assert kwargs["agent_kind"] == ("subagent" if member else "main")
        return SimpleNamespace(slug="researcher", backend_id="SubAgentBackend"), object(), {}

    def build(_agent, context):
        captured.update(context)
        raise ContextReached

    monkeypatch.setattr(execution, "set_mcp_execution_context", lambda _: None)
    monkeypatch.setattr(artifact_materializer, "begin_artifact_accumulation", lambda: None)
    monkeypatch.setattr(svc, "_resolve_agent_runtime", resolve)
    monkeypatch.setattr(svc, "_ensure_knowledge_scope_snapshot", AsyncMock(return_value={}))
    monkeypatch.setattr(svc, "build_agent_input_context", AsyncMock(return_value={}))
    monkeypatch.setattr(svc, "_build_agent_context", build)
    with pytest.raises(ContextReached):
        async for _ in svc.stream_agent_resume(
            thread_id="child",
            resume_input={"q": "yes"},
            meta=meta,
            current_user=SimpleNamespace(uid="alice"),
            db=SimpleNamespace(commit=AsyncMock()),
        ):
            pass
    if member:
        assert captured["parent_thread_id"] == "parent" and captured["file_thread_id"] == "shared"
        assert captured["skills_thread_id"] == "skills" and captured["is_subagent_runtime"] is True
    else:
        assert "is_subagent_runtime" not in captured and "file_thread_id" not in captured
