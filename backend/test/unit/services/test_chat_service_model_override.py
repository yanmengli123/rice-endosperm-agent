from __future__ import annotations

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
    assert input_context["tools"] == ["tavily_search"]
    assert input_context["subagents"] == ["web-search"]
