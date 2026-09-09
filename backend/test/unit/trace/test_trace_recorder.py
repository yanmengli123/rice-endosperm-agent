"""TraceRecorder 单测：缓冲、span 生命周期、contextvar 激活（不触数据库）。"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from yuxi.trace import emit_trace
from yuxi.trace.recorder import TraceRecorder, current_recorder

pytestmark = [pytest.mark.unit]


def _recorder() -> TraceRecorder:
    return TraceRecorder(
        run_id="run-1",
        thread_id="thread-1",
        tenant_id=1,
        uid="u1",
        agent_slug="chatbot",
        run_type="chat",
        request_id="req-1",
    )


def test_emit_without_recorder_is_noop():
    # 无活跃 recorder 时（API 进程/构建期），深层埋点调用必须零副作用
    emit_trace(category="TOOL", operation="execution", event_type="tool.execution.started")
    assert current_recorder() is None


def test_emit_buffers_event_and_assigns_dense_sequence():
    recorder = _recorder()
    recorder.activate()
    try:
        first = recorder.emit(category="TOOL", operation="execution", event_type="tool.execution.started", span_id="t1")
        second = recorder.emit(
            category="TOOL", operation="execution", event_type="tool.execution.completed", span_id="t1"
        )
        assert first["sequence"] == 1
        assert second["sequence"] == 2
        assert len(recorder._buffer) == 2
        assert recorder._summary["tool_calls"] == 1
    finally:
        recorder.deactivate()
    assert current_recorder() is None


def test_redaction_applies_before_buffering():
    recorder = _recorder()
    event = recorder.emit(
        category="TOOL",
        operation="execution",
        event_type="tool.execution.started",
        span_id="t1",
        title="调用失败 key=sk-abcdefghijklmnop1234",
        attributes={"api_key": "secret", "args_digest": "sha256:abc"},
    )
    assert "api_key" not in event["attributes"]
    assert event["attributes"]["args_digest"] == "sha256:abc"
    assert "sk-abcdefghijklmnop1234" not in (event["title"] or "")


def test_span_lifecycle_via_start_finish():
    recorder = _recorder()
    span_id = recorder.start_span(
        category="MODEL",
        operation="generation",
        span_id="model-1",
        title="模型生成",
    )
    assert len(span_id) == 16
    recorder.finish_span(span_id, suffix="completed", summary="生成完成")
    assert recorder._spans[span_id]["status"] == "COMPLETED"
    assert recorder._spans[span_id]["duration_ms"] >= 0


def test_record_run_terminal_sets_summary_status():
    recorder = _recorder()
    recorder.start_span(category="RUN", operation="execution", span_id="run", title="本轮执行")
    recorder.record_run_terminal("failed", error_type="run_idle_timeout", error_message="超时")
    assert recorder._summary["status"] == "failed"
    assert recorder._spans[recorder.root_span_id]["status"] == "FAILED"


def test_close_running_spans_only_touches_open_spans():
    recorder = _recorder()
    recorder.start_span(category="MODEL", operation="generation", span_id="model-1")
    closed_tool = recorder.start_span(category="TOOL", operation="execution", span_id="tool-1")
    recorder.finish_span(closed_tool, suffix="completed")
    recorder.start_span(category="KNOWLEDGE", operation="search", span_id="kr_1")
    recorder.start_span(category="RUN", operation="execution", span_id="run")

    closed = recorder.close_running_spans(suffix="interrupted", error_type="worker_lost")
    assert closed == 2  # model-1 与 kr_1；run 与已闭合 tool-1 不动
    assert recorder._spans[recorder._span_aliases["model-1"]]["status"] == "INTERRUPTED"
    assert recorder._spans[recorder._span_aliases["model-1"]]["error_type"] == "worker_lost"
    assert recorder._spans[recorder._span_aliases["kr_1"]]["status"] == "INTERRUPTED"
    assert recorder._spans[recorder._span_aliases["tool-1"]]["status"] == "COMPLETED"
    assert recorder._spans[recorder.root_span_id]["status"] == "RUNNING"


def test_next_span_id_generates_unique_per_prefix():
    recorder = _recorder()
    values = {recorder.next_span_id("model"), recorder.next_span_id("model"), recorder.next_span_id("tool")}
    assert len(values) == 3
    assert all(len(value) == 16 for value in values)


def test_emit_trace_routes_to_active_recorder():
    recorder = _recorder()
    recorder.activate()
    try:
        emit_trace(
            category="KNOWLEDGE",
            operation="search",
            event_type="knowledge.search.started",
            span_id="kr_1",
        )
        assert any(event["event_type"] == "knowledge.search.started" for event in recorder._buffer)
    finally:
        recorder.deactivate()


def test_once_per_run_events_are_deduplicated():
    recorder = TraceRecorder(
        run_id="run-1",
        thread_id="t",
        tenant_id=1,
        uid="u1",
        agent_slug="chatbot",
        run_type="chat",
        request_id="req",
    )
    first = recorder.emit(
        category="SKILL",
        operation="runtime",
        event_type="skill.runtime.resolved",
        attributes={"prompt_skills": ["a"]},
    )
    duplicate = recorder.emit(
        category="SKILL",
        operation="runtime",
        event_type="skill.runtime.resolved",
        attributes={"prompt_skills": ["a", "b"]},
    )
    assert first is not None
    assert duplicate is None  # 幂等状态事件每 run 只记首次
    # 其他事件不受影响
    assert (
        recorder.emit(category="TOOL", operation="execution", event_type="tool.execution.started", span_id="t1")
        is not None
    )


@pytest.mark.asyncio
async def test_seed_failure_disables_partial_projection_writes(monkeypatch: pytest.MonkeyPatch):
    from yuxi.storage.postgres.manager import pg_manager

    @asynccontextmanager
    async def broken_session():
        raise RuntimeError("database unavailable")
        yield

    monkeypatch.setattr(pg_manager, "get_async_session_context", broken_session)
    recorder = _recorder()

    await recorder.seed()

    assert recorder._disabled is True
    assert (
        recorder.emit(category="TOOL", operation="execution", event_type="tool.execution.started", span_id="t1") is None
    )
    with pytest.raises(RuntimeError, match="disabled after seed failure"):
        await recorder.flush(transaction_action=lambda db: None)
