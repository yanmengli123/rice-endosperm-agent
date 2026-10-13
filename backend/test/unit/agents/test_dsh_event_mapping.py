"""Regression tests for terminal semantics and successful clarification boundaries."""

import pytest
from langchain_core.messages import AIMessage
from yuxi.agents.buildin.dsh.events import ASK_USER_TOOL_NAME, DshRunState, map_session_event
from yuxi.agents.buildin.dsh.mcp_bridge import ESCALATION_MARKER
from yuxi.agents.buildin.dsh.rpc import DshProcessError


def _state():
    return DshRunState(thread_id="t-1", run_id="run-1", session_id="session-1")


def _message(text, usage=None):
    return {
        "type": "assistant/message",
        "data": {
            "message": {"id": "am-1", "content": [{"type": "text", "text": text}]},
            "usage": usage,
        },
    }


def test_message_duplicate_and_cache_usage():
    state = _state()
    event = _message("科研回答", {"inputTokens": 10, "outputTokens": 5, "cacheReadTokens": 7})
    items = map_session_event(event, state)
    assert isinstance(items[0][1][0], AIMessage)
    assert state.total_tokens == 22
    assert map_session_event(event, state) == []
    assert state.total_tokens == 22


def test_clarification_is_created_only_after_successful_tool_result():
    state = _state()
    call = {
        "type": "tool/call",
        "data": {
            "callId": "c1",
            "name": ASK_USER_TOOL_NAME,
            "arguments": '{"question":"研究哪种作物？","options":["水稻","小麦"]}',
        },
    }
    assert map_session_event(call, state)[0][1]["data"]["event"] == "tool-call"
    assert state.pending_interrupt is None
    result = {
        "type": "tool/result",
        "data": {
            "message": {
                "toolCallId": "c1",
                "content": [{"type": "text", "text": ESCALATION_MARKER}],
            }
        },
    }
    map_session_event(result, state)
    assert state.pending_interrupt["questions"][0]["question"] == "研究哪种作物？"
    assert map_session_event(_message("不应发布", {"totalTokens": 7}), state) == []
    assert state.token_view()["token_usage"]["run_model_usage"]["total_tokens"] == 7


def test_failed_clarification_does_not_interrupt():
    state = _state()
    map_session_event(
        {
            "type": "tool/call",
            "data": {
                "callId": "c1",
                "name": ASK_USER_TOOL_NAME,
                "arguments": '{"question":"作物？"}',
            },
        },
        state,
    )
    with pytest.raises(DshProcessError, match="bridge failed"):
        map_session_event(
            {
                "type": "tool/result",
                "data": {
                    "message": {
                        "toolCallId": "c1",
                        "isError": True,
                        "content": [],
                    }
                },
            },
            state,
        )
    assert state.pending_interrupt is None


@pytest.mark.parametrize("kind", ["error", "max-tokens", "aborted", "interrupted", "blocked", "future"])
def test_unsuccessful_terminal_is_not_completion(kind):
    with pytest.raises(DshProcessError):
        map_session_event({"type": "turn/end", "data": {"reason": {"kind": kind}}}, _state())


def test_success_terminal_and_unknown_event_admission():
    state = _state()
    map_session_event({"type": "turn/end", "data": {"reason": {"kind": "completed"}}}, state)
    assert state.terminal_reason == "completed"
    with pytest.raises(DshProcessError, match="critical"):
        map_session_event({"type": "future-critical", "data": {}}, state)
    assert map_session_event({"type": "future-metric", "ignorable": True}, state) == []


def test_unapproved_tool_never_executes():
    with pytest.raises(DshProcessError, match="allowlist"):
        map_session_event({"type": "tool/call", "data": {"callId": "c", "name": "bash", "arguments": "{}"}}, _state())
