from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
import yuxi.services.run_worker as run_worker


class _RaisingAsyncIter:
    def __init__(self, exc: Exception):
        self._exc = exc

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise self._exc


class _BytesAsyncIter:
    def __init__(self, values: list[bytes]):
        self._values = list(values)
        self._idx = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._idx >= len(self._values):
            raise StopAsyncIteration
        value = self._values[self._idx]
        self._idx += 1
        return value


class _NeverAsyncIter:
    def __init__(self):
        self.cancel_reason = None

    async def __anext__(self):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as exc:
            self.cancel_reason = exc.args[0] if exc.args else None
            raise


class _NeverCancelledContext:
    run_id = "run-never"

    async def wait_cancelled(self):
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_terminal_trace_and_run_status_share_transaction(monkeypatch: pytest.MonkeyPatch):
    order: list[tuple] = []
    db_token = object()

    class Recorder:
        run_id = "run-1"

        def close_running_spans(self, **kwargs):
            order.append(("close", kwargs["suffix"], kwargs["error_type"]))

        def record_run_terminal(self, status, **kwargs):
            order.append(("record", status, kwargs.get("error_type")))

        async def flush(self, *, transaction_action):
            order.append(("trace-staged",))
            await transaction_action(db_token)
            order.append(("transaction-committed",))

    async def mark(run_id, status, error_type=None, error_message=None, *, db=None):
        del error_message
        order.append(("run-terminal", run_id, status, error_type, db))

    async def acknowledge(run_id):
        order.append(("dispatch-ack", run_id))

    monkeypatch.setattr(run_worker, "mark_run_terminal", mark)
    monkeypatch.setattr(run_worker, "acknowledge_agent_run_dispatch", acknowledge)

    committed = await run_worker._persist_terminal_trace(
        Recorder(),
        "failed",
        error_type="safe_error",
    )

    assert committed is True
    assert order == [
        ("close", "interrupted", "safe_error"),
        ("record", "failed", "safe_error"),
        ("trace-staged",),
        ("run-terminal", "run-1", "failed", "safe_error", db_token),
        ("transaction-committed",),
        ("dispatch-ack", "run-1"),
    ]


@pytest.mark.asyncio
async def test_terminal_trace_failure_falls_back_to_business_status(monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple] = []

    class Recorder:
        run_id = "run-1"

        def close_running_spans(self, **kwargs):
            del kwargs

        def record_run_terminal(self, status, **kwargs):
            del status, kwargs

        async def flush(self, *, transaction_action):
            del transaction_action
            raise RuntimeError("trace database unavailable")

    async def mark(run_id, status, error_type=None, error_message=None, *, db=None):
        del error_message
        calls.append((run_id, status, error_type, db))

    acknowledged: list[str] = []

    async def acknowledge(run_id):
        acknowledged.append(run_id)

    monkeypatch.setattr(run_worker, "mark_run_terminal", mark)
    monkeypatch.setattr(run_worker, "acknowledge_agent_run_dispatch", acknowledge)

    committed = await run_worker._persist_terminal_trace(
        Recorder(),
        "failed",
        error_type="safe_error",
    )

    assert committed is False
    assert calls == [("run-1", "failed", "safe_error", None)]
    assert acknowledged == ["run-1"]


def _build_run() -> SimpleNamespace:
    return SimpleNamespace(
        id="run-1",
        status="pending",
        request_id="req-1",
        input_payload={"model_spec": "provider:model"},
        input_message_id=10,
        run_type="chat",
        agent_slug="ChatbotAgent",
        uid="user-1",
        tenant_id=None,
        conversation_thread_id="thread-1",
        created_by_run_id=None,
    )


def _patch_common(monkeypatch: pytest.MonkeyPatch, run_obj: SimpleNamespace):
    @asynccontextmanager
    async def fake_session_ctx():
        yield object()

    async def fake_noop(*args, **kwargs):
        del args, kwargs
        return None

    async def fake_trace_flush(self, *, transaction_action=None):
        del self
        if transaction_action is not None:
            await transaction_action(object())

    async def fake_get_run(run_id: str):
        del run_id
        return run_obj

    async def fake_load_user(uid: str):
        del uid
        return SimpleNamespace(id=1, uid="user-1")

    async def fake_load_input_message(message_id: int | None):
        assert message_id == 10
        return SimpleNamespace(content="hello", image_content=None, extra_metadata={})

    async def fake_not_cancelled(self):
        del self
        return False

    monkeypatch.setattr(run_worker.pg_manager, "get_async_session_context", fake_session_ctx)
    monkeypatch.setattr(run_worker, "_get_run", fake_get_run)
    monkeypatch.setattr(run_worker, "acknowledge_agent_run_dispatch", fake_noop)
    monkeypatch.setattr(run_worker, "_load_user", fake_load_user)
    monkeypatch.setattr(run_worker, "_load_input_message", fake_load_input_message)
    monkeypatch.setattr(run_worker, "mark_run_running", fake_noop)
    monkeypatch.setattr(run_worker, "clear_cancel_signal", fake_noop)
    monkeypatch.setattr(run_worker, "stream_agent_chat", lambda **kwargs: object())
    monkeypatch.setattr(run_worker.RunContext, "start", fake_noop)
    monkeypatch.setattr(run_worker.RunContext, "close", fake_noop)
    monkeypatch.setattr(run_worker.RunContext, "is_cancelled", fake_not_cancelled)
    # 轨迹记录器：单测无真实 PG/Redis，seed/flush/finalize 全部置空
    # （emit 是纯内存缓冲，保留真实路径以覆盖事件组装逻辑）。
    monkeypatch.setattr(run_worker.TraceRecorder, "seed", fake_noop)
    monkeypatch.setattr(run_worker.TraceRecorder, "flush", fake_trace_flush)
    monkeypatch.setattr(run_worker.TraceRecorder, "finalize", fake_noop)


@pytest.mark.asyncio
async def test_process_agent_run_restores_invocation_meta(monkeypatch: pytest.MonkeyPatch):
    run_obj = _build_run()
    _patch_common(monkeypatch, run_obj)

    captured: dict[str, object] = {}
    events: list[dict] = []
    terminal_statuses: list[str] = []

    async def fake_load_input_message(message_id: int | None):
        assert message_id == 10
        return SimpleNamespace(
            content="hello",
            image_content=None,
            extra_metadata={
                "source": "agent_call",
                "agent_invocation_meta": {"trace_id": "trace-1"},
                "evaluation": {"dataset_name": "legacy-top-level"},
                "custom_variables": {"system_prompt": "legacy"},
            },
        )

    async def fake_append_event(run_id: str, event_type: str, payload: dict, **kwargs):
        del kwargs
        events.append({"run_id": run_id, "event_type": event_type, "payload": payload})

    async def fake_mark_terminal(run_id: str, status: str, error_type=None, error_message=None, *, db=None):
        del run_id, error_type, error_message, db
        terminal_statuses.append(status)

    def fake_stream_agent_chat(**kwargs):
        captured.update(kwargs)
        return _BytesAsyncIter([b'{"status":"finished","request_id":"req-1","thread_id":"thread-1"}\n'])

    monkeypatch.setattr(run_worker, "_load_input_message", fake_load_input_message)
    monkeypatch.setattr(run_worker, "append_run_event", fake_append_event)
    monkeypatch.setattr(run_worker, "mark_run_terminal", fake_mark_terminal)
    monkeypatch.setattr(run_worker, "stream_agent_chat", fake_stream_agent_chat)

    await run_worker.process_agent_run({"job_try": 1}, "run-1")

    meta = captured["meta"]
    assert meta["source"] == "agent_call"
    assert meta["agent_invocation_meta"] == {"trace_id": "trace-1"}
    assert "evaluation" not in meta
    assert "custom_variables" not in meta
    metadata_event = next(event for event in events if event["event_type"] == "metadata")
    assert metadata_event["payload"]["agent_invocation_meta"] == {"trace_id": "trace-1"}
    assert "evaluation" not in metadata_event["payload"]
    assert "custom_variables" not in metadata_event["payload"]
    assert terminal_statuses == ["completed"]


@pytest.mark.asyncio
async def test_process_agent_run_commits_output_before_marking_completed(monkeypatch: pytest.MonkeyPatch):
    run_obj = _build_run()
    _patch_common(monkeypatch, run_obj)
    stream_session_exited = False

    @asynccontextmanager
    async def fake_session_ctx():
        nonlocal stream_session_exited
        try:
            yield object()
        finally:
            stream_session_exited = True

    async def fake_mark_terminal(run_id: str, status: str, error_type=None, error_message=None, *, db=None):
        del run_id, error_type, error_message, db
        assert status == "completed"
        assert stream_session_exited is True

    monkeypatch.setattr(run_worker.pg_manager, "get_async_session_context", fake_session_ctx)
    monkeypatch.setattr(run_worker, "mark_run_terminal", fake_mark_terminal)
    monkeypatch.setattr(
        run_worker,
        "stream_agent_chat",
        lambda **kwargs: _BytesAsyncIter([b'{"status":"finished","thread_id":"thread-1"}\n']),
    )

    await run_worker.process_agent_run({"job_try": 1}, "run-1")


@pytest.mark.asyncio
async def test_process_agent_run_non_retryable_error_marks_failed(monkeypatch: pytest.MonkeyPatch):
    run_obj = _build_run()
    _patch_common(monkeypatch, run_obj)

    terminal_statuses: list[str] = []
    events: list[str] = []

    async def fake_append_event(run_id: str, event_type: str, payload: dict, **kwargs):
        del run_id, payload, kwargs
        events.append(event_type)

    async def fake_mark_terminal(run_id: str, status: str, error_type=None, error_message=None, *, db=None):
        del run_id, error_type, error_message, db
        terminal_statuses.append(status)

    monkeypatch.setattr(run_worker, "append_run_event", fake_append_event)
    monkeypatch.setattr(run_worker, "mark_run_terminal", fake_mark_terminal)
    monkeypatch.setattr(
        run_worker,
        "_consume_stream_with_cancel",
        lambda stream, run_ctx: _RaisingAsyncIter(RuntimeError("boom")),
    )

    await run_worker.process_agent_run({"job_try": 1}, "run-1")

    assert "error" in events
    assert terminal_statuses == ["failed"]


@pytest.mark.asyncio
async def test_process_agent_run_retryable_error_fails_without_auto_retry(monkeypatch: pytest.MonkeyPatch):
    """可重试错误不再自动重跑：重跑会从 checkpoint 重复注入本轮输入，污染上下文。"""
    run_obj = _build_run()
    _patch_common(monkeypatch, run_obj)

    terminal_statuses: list[str] = []
    terminal_error_types: list[str | None] = []
    events: list[dict] = []

    async def fake_append_event(run_id: str, event_type: str, payload: dict, **kwargs):
        del run_id, kwargs
        events.append({"event_type": event_type, "payload": payload})

    async def fake_mark_terminal(run_id: str, status: str, error_type=None, error_message=None, *, db=None):
        del run_id, error_message, db
        terminal_statuses.append(status)
        terminal_error_types.append(error_type)

    def fake_consume(stream, run_ctx):
        del stream, run_ctx
        return _RaisingAsyncIter(ConnectionError("temporary failure"))

    monkeypatch.setattr(run_worker, "append_run_event", fake_append_event)
    monkeypatch.setattr(run_worker, "mark_run_terminal", fake_mark_terminal)
    monkeypatch.setattr(run_worker, "_consume_stream_with_cancel", fake_consume)

    await run_worker.process_agent_run({"job_try": 1}, "run-1")

    assert terminal_statuses == ["failed"]
    assert terminal_error_types == ["retryable_worker_error"]
    assert any(
        item["event_type"] == "error" and item["payload"]["chunk"].get("error_type") == "retryable_worker_error"
        for item in events
    )


@pytest.mark.asyncio
async def test_stream_wait_emits_progress_then_times_out(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(run_worker, "RUN_STREAM_PROGRESS_HEARTBEAT_SECONDS", 0.005)
    monkeypatch.setattr(run_worker, "RUN_STREAM_IDLE_TIMEOUT_SECONDS", 0.02)

    source = _NeverAsyncIter()
    stream = run_worker._consume_stream_with_cancel(source, _NeverCancelledContext())
    assert await stream.__anext__() is run_worker._STREAM_WAITING
    with pytest.raises(run_worker.RunStreamIdleTimeout):
        while True:
            await stream.__anext__()
    assert source.cancel_reason == "run_idle_timeout"


@pytest.mark.asyncio
async def test_stream_total_deadline_applies_even_when_idle_deadline_is_long(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(run_worker, "RUN_STREAM_PROGRESS_HEARTBEAT_SECONDS", 1.0)
    monkeypatch.setattr(run_worker, "RUN_STREAM_IDLE_TIMEOUT_SECONDS", 10.0)
    monkeypatch.setattr(run_worker, "RUN_STREAM_TOTAL_TIMEOUT_SECONDS", 0.01)

    source = _NeverAsyncIter()
    stream = run_worker._consume_stream_with_cancel(source, _NeverCancelledContext())
    with pytest.raises(run_worker.RunStreamTotalTimeout):
        await stream.__anext__()
    assert source.cancel_reason == "run_total_timeout"


@pytest.mark.asyncio
async def test_progress_publisher_emits_application_event(monkeypatch: pytest.MonkeyPatch):
    events: list[dict] = []
    done = asyncio.Event()

    async def fake_append_event(run_id: str, event_type: str, payload: dict, **kwargs):
        del kwargs
        events.append({"run_id": run_id, "event_type": event_type, "payload": payload})
        done.set()

    monkeypatch.setattr(run_worker, "RUN_STREAM_PROGRESS_HEARTBEAT_SECONDS", 0.005)
    monkeypatch.setattr(run_worker, "append_run_event", fake_append_event)

    await asyncio.wait_for(
        run_worker._publish_run_progress(
            run_id="run-1",
            thread_id="thread-1",
            request_id="req-1",
            done_event=done,
        ),
        timeout=0.1,
    )

    assert events[0]["event_type"] == "custom"
    assert events[0]["payload"]["name"] == "yuxi.progress"
    assert events[0]["payload"]["chunk"]["status"] == "progress"


@pytest.mark.asyncio
async def test_process_agent_run_idle_timeout_reaches_failed_terminal(monkeypatch: pytest.MonkeyPatch):
    run_obj = _build_run()
    _patch_common(monkeypatch, run_obj)
    events: list[dict] = []
    terminal_updates: list[tuple[str, str | None]] = []

    async def fake_append_event(run_id: str, event_type: str, payload: dict, **kwargs):
        del run_id, kwargs
        events.append({"event_type": event_type, "payload": payload})

    async def fake_mark_terminal(run_id: str, status: str, error_type=None, error_message=None, *, db=None):
        del run_id, error_message, db
        terminal_updates.append((status, error_type))

    def fake_consume(stream, run_ctx):
        del stream, run_ctx
        return _RaisingAsyncIter(run_worker.RunStreamIdleTimeout("idle"))

    monkeypatch.setattr(run_worker, "append_run_event", fake_append_event)
    monkeypatch.setattr(run_worker, "mark_run_terminal", fake_mark_terminal)
    monkeypatch.setattr(run_worker, "_consume_stream_with_cancel", fake_consume)

    await run_worker.process_agent_run({"job_try": 1}, "run-1")

    assert terminal_updates == [("failed", "run_idle_timeout")]
    assert any(
        event["event_type"] == "error" and event["payload"]["chunk"]["error_type"] == "run_idle_timeout"
        for event in events
    )


@pytest.mark.asyncio
async def test_process_subagent_run_restores_runtime_context(monkeypatch: pytest.MonkeyPatch):
    run_obj = _build_run()
    run_obj.run_type = "subagent"
    run_obj.agent_slug = "worker"
    run_obj.conversation_thread_id = "child-thread"
    run_obj.created_by_run_id = "parent-run"
    run_obj.input_payload = {
        "model_spec": "provider:model",
        "runtime": {
            "parent_thread_id": "parent-thread",
            "file_thread_id": "shared-file-thread",
            "skills_thread_id": "child-thread",
        },
    }
    _patch_common(monkeypatch, run_obj)

    captured: dict[str, object] = {}
    terminal_statuses: list[str] = []

    async def fake_append_event(run_id: str, event_type: str, payload: dict, **kwargs):
        del run_id, event_type, payload, kwargs

    async def fake_mark_terminal(run_id: str, status: str, error_type=None, error_message=None, *, db=None):
        del run_id, error_type, error_message, db
        terminal_statuses.append(status)

    def fake_stream_agent_chat(**kwargs):
        captured.update(kwargs)
        return _BytesAsyncIter([b'{"status":"finished","request_id":"req-1","thread_id":"child-thread"}\n'])

    monkeypatch.setattr(run_worker, "append_run_event", fake_append_event)
    monkeypatch.setattr(run_worker, "mark_run_terminal", fake_mark_terminal)
    monkeypatch.setattr(run_worker, "stream_agent_chat", fake_stream_agent_chat)

    await run_worker.process_agent_run({"job_try": 1}, "run-1")

    meta = captured["meta"]
    assert meta["run_type"] == "subagent"
    assert meta["parent_thread_id"] == "parent-thread"
    assert meta["file_thread_id"] == "shared-file-thread"
    assert meta["skills_thread_id"] == "child-thread"
    assert captured["agent_slug"] == "worker"
    assert captured["thread_id"] == "child-thread"
    assert captured["input_message"].content == "hello"
    assert captured["input_message"].langchain_message.content == "hello"
    assert "image_content" not in captured
    assert terminal_statuses == ["completed"]


@pytest.mark.asyncio
async def test_process_agent_run_rejects_unknown_run_type(monkeypatch: pytest.MonkeyPatch):
    run_obj = _build_run()
    run_obj.run_type = "unknown"
    _patch_common(monkeypatch, run_obj)

    terminal_errors: list[dict] = []
    events: list[tuple[str, dict]] = []

    async def fake_append_event(run_id: str, event_type: str, payload: dict, **kwargs):
        del run_id, kwargs
        events.append((event_type, payload))

    async def fake_mark_terminal(run_id: str, status: str, error_type=None, error_message=None, *, db=None):
        del db
        terminal_errors.append(
            {
                "run_id": run_id,
                "status": status,
                "error_type": error_type,
                "error_message": error_message,
            }
        )

    def fail_stream_agent_chat(**kwargs):
        del kwargs
        raise AssertionError("unknown run_type must not enter chat stream")

    monkeypatch.setattr(run_worker, "mark_run_terminal", fake_mark_terminal)
    monkeypatch.setattr(run_worker, "append_run_event", fake_append_event)
    monkeypatch.setattr(run_worker, "stream_agent_chat", fail_stream_agent_chat)

    await run_worker.process_agent_run({"job_try": 1}, "run-1")

    assert terminal_errors == [
        {
            "run_id": "run-1",
            "status": "failed",
            "error_type": "invalid_run_type",
            "error_message": "不支持的 run_type: unknown",
        }
    ]
    assert [event_type for event_type, _ in events] == ["error", "end"]
    assert events[-1][1]["trace_status"] == "COMMITTED"
    assert "reason" not in events[-1][1]


@pytest.mark.asyncio
async def test_process_agent_run_rejects_invalid_raw_input_message(monkeypatch: pytest.MonkeyPatch):
    run_obj = _build_run()
    _patch_common(monkeypatch, run_obj)

    terminal_errors: list[dict] = []
    events: list[tuple[str, dict]] = []

    async def fake_append_event(run_id: str, event_type: str, payload: dict, **kwargs):
        del run_id, kwargs
        events.append((event_type, payload))

    async def fake_load_input_message(message_id: int | None):
        assert message_id == 10
        return SimpleNamespace(
            content="hello",
            image_content=None,
            extra_metadata={"raw_message": {"type": "human", "content": object()}},
        )

    async def fake_mark_terminal(run_id: str, status: str, error_type=None, error_message=None, *, db=None):
        del db
        terminal_errors.append(
            {
                "run_id": run_id,
                "status": status,
                "error_type": error_type,
                "error_message": error_message,
            }
        )

    def fail_stream_agent_chat(**kwargs):
        del kwargs
        raise AssertionError("invalid input message must not enter chat stream")

    monkeypatch.setattr(run_worker, "_load_input_message", fake_load_input_message)
    monkeypatch.setattr(run_worker, "mark_run_terminal", fake_mark_terminal)
    monkeypatch.setattr(run_worker, "append_run_event", fake_append_event)
    monkeypatch.setattr(run_worker, "stream_agent_chat", fail_stream_agent_chat)

    await run_worker.process_agent_run({"job_try": 1}, "run-1")

    assert terminal_errors == [
        {
            "run_id": "run-1",
            "status": "failed",
            "error_type": "invalid_input_message",
            "error_message": "invalid raw_message for chat input message",
        }
    ]
    assert [event_type for event_type, _ in events] == ["error", "end"]
    assert events[-1][1]["trace_status"] == "COMMITTED"


@pytest.mark.asyncio
async def test_process_agent_run_cancelled_before_start_has_terminal_trace_status(
    monkeypatch: pytest.MonkeyPatch,
):
    run_obj = _build_run()
    run_obj.status = "cancel_requested"
    _patch_common(monkeypatch, run_obj)
    events: list[tuple[str, dict]] = []
    terminal_statuses: list[str] = []

    async def fake_append_event(run_id: str, event_type: str, payload: dict, **kwargs):
        del run_id, kwargs
        events.append((event_type, payload))

    async def fake_mark_terminal(run_id: str, status: str, error_type=None, error_message=None, *, db=None):
        del run_id, error_type, error_message, db
        terminal_statuses.append(status)

    monkeypatch.setattr(run_worker, "append_run_event", fake_append_event)
    monkeypatch.setattr(run_worker, "mark_run_terminal", fake_mark_terminal)

    await run_worker.process_agent_run({"job_try": 1}, "run-1")

    assert [event_type for event_type, _ in events] == ["interrupt", "end"]
    assert events[-1][1]["status"] == "cancelled"
    assert events[-1][1]["reason"] == "cancelled_before_start"
    assert events[-1][1]["trace_status"] == "COMMITTED"
    assert terminal_statuses == ["cancelled"]


@pytest.mark.asyncio
async def test_chunked_event_writer_flushes_loading_chunks_by_thread(monkeypatch: pytest.MonkeyPatch):
    events: list[dict] = []

    async def fake_append_run_event(run_id: str, event_type: str, payload: dict, *, thread_id: str | None = None):
        events.append({"run_id": run_id, "event_type": event_type, "payload": payload, "thread_id": thread_id})

    monkeypatch.setattr(run_worker, "append_run_event", fake_append_run_event)

    writer = run_worker.ChunkedEventWriter("run-1", "parent-thread")
    await writer.append({"status": "loading", "response": "parent", "thread_id": "parent-thread"})
    await writer.append({"status": "loading", "response": "child", "thread_id": "child-thread"})
    await writer.flush()

    assert events == [
        {
            "run_id": "run-1",
            "event_type": "messages",
            "payload": {"items": [{"status": "loading", "response": "parent", "thread_id": "parent-thread"}]},
            "thread_id": "parent-thread",
        },
        {
            "run_id": "run-1",
            "event_type": "messages",
            "payload": {"items": [{"status": "loading", "response": "child", "thread_id": "child-thread"}]},
            "thread_id": "child-thread",
        },
    ]


@pytest.mark.asyncio
async def test_chunked_event_writer_flushes_semantic_tool_call_immediately(monkeypatch: pytest.MonkeyPatch):
    events: list[dict] = []

    async def fake_append_run_event(run_id: str, event_type: str, payload: dict, *, thread_id: str | None = None):
        events.append({"run_id": run_id, "event_type": event_type, "payload": payload, "thread_id": thread_id})

    monkeypatch.setattr(run_worker, "append_run_event", fake_append_run_event)

    writer = run_worker.ChunkedEventWriter("run-1", "parent-thread")
    chunk = {
        "status": "loading",
        "response": "",
        "thread_id": "parent-thread",
        "stream_event": {
            "type": "tool_call",
            "message_id": "msg-1",
            "tool_call_id": "call-1",
            "name": "task",
            "args": {"description": "do work"},
            "index": 0,
            "thread_id": "parent-thread",
            "namespace": [],
        },
    }
    await writer.append(chunk)

    assert events == [
        {
            "run_id": "run-1",
            "event_type": "messages",
            "payload": {"items": [chunk]},
            "thread_id": "parent-thread",
        }
    ]


def test_chunk_thread_id_uses_fallback_for_unstable_nested_metadata():
    assert (
        run_worker._chunk_thread_id(
            {"metadata": {"configurable": {"thread_id": "child-thread"}}},
            "parent-thread",
        )
        == "parent-thread"
    )


@pytest.mark.asyncio
async def test_worker_startup_ensures_builtin_mcp_servers(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []

    def fake_initialize():
        calls.append("initialize")

    async def fake_create_business_tables():
        calls.append("create_business_tables")

    async def fake_ensure_business_schema():
        calls.append("ensure_business_schema")

    async def fake_initialize_knowledge_base():
        calls.append("initialize_knowledge_base")

    async def fake_reconcile_stale_agent_runs():
        calls.append("reconcile_stale_agent_runs")

    async def fake_recover_stale_scientific_pdf_ingests():
        calls.append("recover_stale_scientific_pdf_ingests")

    async def fake_reconcile_dynamic_wikis():
        calls.append("reconcile_dynamic_wikis")

    async def fake_ensure_builtin_mcp_servers_in_db():
        calls.append("ensure_builtin_mcp_servers_in_db")

    @asynccontextmanager
    async def fake_session_ctx():
        yield object()

    async def fake_init_builtin_skills(session):
        del session
        calls.append("init_builtin_skills")

    async def fake_ensure_builtin_ocr_provider_in_db(session):
        del session
        calls.append("ensure_builtin_ocr_provider_in_db")

    async def fake_get_all_ocr_providers(session):
        del session
        calls.append("get_all_ocr_providers")
        return []

    def fake_rebuild_ocr_cache(_providers):
        calls.append("rebuild_ocr_cache")

    def fake_start_runtime_sync():
        calls.append("start_runtime_sync")

    monkeypatch.setattr(run_worker.pg_manager, "initialize", fake_initialize)
    monkeypatch.setattr(run_worker.pg_manager, "create_business_tables", fake_create_business_tables)
    monkeypatch.setattr(run_worker.pg_manager, "ensure_business_schema", fake_ensure_business_schema)
    monkeypatch.setattr(run_worker, "reconcile_stale_agent_runs", fake_reconcile_stale_agent_runs)
    monkeypatch.setattr(
        run_worker,
        "recover_stale_scientific_pdf_ingests",
        fake_recover_stale_scientific_pdf_ingests,
    )
    monkeypatch.setattr(run_worker, "reconcile_dynamic_wikis", fake_reconcile_dynamic_wikis)
    monkeypatch.setattr(
        "yuxi.knowledge.runtime.knowledge_base.initialize",
        fake_initialize_knowledge_base,
    )
    monkeypatch.setattr(run_worker.pg_manager, "get_async_session_context", fake_session_ctx)
    monkeypatch.setattr(run_worker, "ensure_builtin_mcp_servers_in_db", fake_ensure_builtin_mcp_servers_in_db)
    monkeypatch.setattr(run_worker, "init_builtin_skills", fake_init_builtin_skills)
    monkeypatch.setattr(run_worker.sys_config, "start_runtime_sync", fake_start_runtime_sync)
    monkeypatch.setattr(
        "yuxi.services.ocr_provider_service.ensure_builtin_ocr_provider_in_db",
        fake_ensure_builtin_ocr_provider_in_db,
    )
    monkeypatch.setattr("yuxi.services.ocr_provider_service.get_all_ocr_providers", fake_get_all_ocr_providers)
    monkeypatch.setattr("yuxi.knowledge.parser.credential_cache.ocr_credential_cache.rebuild", fake_rebuild_ocr_cache)

    await run_worker._worker_startup({})

    assert calls == [
        "initialize",
        "create_business_tables",
        "ensure_business_schema",
        "initialize_knowledge_base",
        "reconcile_stale_agent_runs",
        "recover_stale_scientific_pdf_ingests",
        "reconcile_dynamic_wikis",
        "ensure_builtin_mcp_servers_in_db",
        "init_builtin_skills",
        "ensure_builtin_ocr_provider_in_db",
        "get_all_ocr_providers",
        "rebuild_ocr_cache",
        "start_runtime_sync",
    ]


def test_map_chunk_normalizes_unclassified_connection_error():
    """旁路生产者未分类的连接类错误：映射层兜底归一为 model_connection_error。"""
    chunk = {
        "status": "error",
        "error_type": "unexpected_error",
        "error_message": (
            "Error streaming messages: Model call failed after 3 attempts with APIConnectionError: Connection error."
        ),
    }
    event, payload = run_worker._map_chunk_to_run_event(chunk)
    assert event == "error"
    assert payload["chunk"]["error_type"] == "model_connection_error"
    assert payload["retryable"] is True


def test_map_chunk_preserves_classified_error_types():
    chunk = {
        "status": "error",
        "error_type": "run_idle_timeout",
        "error_message": "服务端长时间未收到检索或模型输出，已安全结束本次任务，请重试。",
        "retryable": True,
    }
    event, payload = run_worker._map_chunk_to_run_event(chunk)
    assert event == "error"
    assert payload["chunk"]["error_type"] == "run_idle_timeout"
