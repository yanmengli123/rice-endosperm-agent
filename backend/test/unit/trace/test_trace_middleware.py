from __future__ import annotations

from types import SimpleNamespace

import pytest
from yuxi.agents.middlewares.trace import TraceMiddleware, _model_display_name
from yuxi.trace.recorder import TraceRecorder

pytestmark = [pytest.mark.unit]


def _recorder() -> TraceRecorder:
    return TraceRecorder(
        run_id="run-1",
        thread_id="thread-1",
        tenant_id=1,
        uid="user-1",
    )


@pytest.mark.asyncio
async def test_structured_tool_error_closes_span_as_failed():
    recorder = _recorder()
    recorder.activate()
    request = SimpleNamespace(
        tool_call={"id": "call-1", "name": "lookup", "args": {"gene": "Os01g"}},
        tool=SimpleNamespace(metadata={"server": "rice", "mcp_tool_name": "lookup"}),
    )

    async def handler(_request):
        return SimpleNamespace(status="success", artifact={"is_error": True})

    try:
        result = await TraceMiddleware().awrap_tool_call(request, handler)
    finally:
        recorder.deactivate()

    assert result.artifact["is_error"] is True
    assert [event["event_type"] for event in recorder._buffer] == [
        "mcp.execution.started",
        "mcp.execution.failed",
    ]
    assert recorder._spans[recorder._span_aliases["call-1"]]["error_type"] == "tool_result_error"


@pytest.mark.asyncio
async def test_successful_structured_tool_result_closes_span_as_completed():
    recorder = _recorder()
    recorder.activate()
    request = SimpleNamespace(
        tool_call={"id": "call-1", "name": "lookup", "args": {}},
        tool=SimpleNamespace(metadata={"server": "rice", "mcp_tool_name": "lookup"}),
    )

    async def handler(_request):
        return SimpleNamespace(status="success", artifact={"is_error": False})

    try:
        await TraceMiddleware().awrap_tool_call(request, handler)
    finally:
        recorder.deactivate()

    assert [event["event_type"] for event in recorder._buffer] == [
        "mcp.execution.started",
        "mcp.execution.completed",
    ]


def test_model_display_name_never_stringifies_provider_object():
    class ProviderModel:
        def __str__(self):
            raise AssertionError("provider object must not be stringified into trace")

    assert _model_display_name(SimpleNamespace(model=ProviderModel())) == "ProviderModel"
