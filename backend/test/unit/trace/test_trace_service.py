"""Trace service recovery, visibility, and maintenance boundaries."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from yuxi.services import trace_service
from yuxi.storage.postgres.manager import pg_manager

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def _outbox_row(sequence: int, *, visibility: str = "USER") -> SimpleNamespace:
    return SimpleNamespace(
        id=sequence,
        run_id="run-1",
        sequence=sequence,
        lease_id="lease-1",
        payload={
            "sequence": sequence,
            "thread_id": "thread-1",
            "visibility": visibility,
        },
    )


async def test_relay_acknowledges_claim_after_ordered_publish(monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple] = []
    rows = [_outbox_row(2, visibility="ADMIN"), _outbox_row(1)]

    @asynccontextmanager
    async def session_context():
        yield object()

    class Repo:
        def __init__(self, db):
            del db

        async def claim_pending_outbox(self, *, limit: int):
            assert limit == 100
            calls.append(("claim",))
            return rows

        async def mark_outbox_processed(self, ids, *, lease_id):
            calls.append(("ack", ids, lease_id))

        async def fail_outbox_claim(self, ids, *, lease_id, error):
            calls.append(("fail", ids, lease_id, error))

    async def append(run_id, event_type, payload, *, thread_id):
        calls.append(("publish", run_id, event_type, payload["trace"]["sequence"], thread_id))

    monkeypatch.setattr(pg_manager, "get_async_session_context", session_context)
    monkeypatch.setattr(trace_service, "TraceRepository", Repo)
    monkeypatch.setattr("yuxi.services.run_queue_service.append_run_stream_event", append)

    handled = await trace_service.relay_trace_outbox()

    assert handled == 2
    assert calls == [
        ("claim",),
        ("publish", "run-1", "trace", 1, "thread-1"),
        ("ack", [2, 1], "lease-1"),
    ]


async def test_relay_reopens_matching_lease_after_publish_failure(monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple] = []
    rows = [_outbox_row(1), _outbox_row(2)]

    @asynccontextmanager
    async def session_context():
        yield object()

    class Repo:
        def __init__(self, db):
            del db

        async def claim_pending_outbox(self, *, limit: int):
            del limit
            return rows

        async def mark_outbox_processed(self, ids, *, lease_id):
            calls.append(("ack", ids, lease_id))

        async def fail_outbox_claim(self, ids, *, lease_id, error):
            calls.append(("fail", ids, lease_id, error))

    async def append(*args, **kwargs):
        del args, kwargs
        raise ConnectionError("redis unavailable")

    monkeypatch.setattr(pg_manager, "get_async_session_context", session_context)
    monkeypatch.setattr(trace_service, "TraceRepository", Repo)
    monkeypatch.setattr("yuxi.services.run_queue_service.append_run_stream_event", append)

    assert await trace_service.relay_trace_outbox() == 0
    assert calls == [("fail", [1, 2], "lease-1", "redis_publish_failed")]


async def test_user_event_cursor_advances_across_admin_only_rows(monkeypatch: pytest.MonkeyPatch):
    rows = [
        SimpleNamespace(sequence=6, visibility="ADMIN"),
        SimpleNamespace(sequence=7, visibility="ADMIN"),
    ]

    async def require_run(*args, **kwargs):
        del args, kwargs
        return SimpleNamespace(id="run-1")

    class Repo:
        def __init__(self, db):
            del db

        async def list_events(self, *args, **kwargs):
            del args, kwargs
            return rows

    monkeypatch.setattr(trace_service, "_require_run", require_run)
    monkeypatch.setattr(trace_service, "TraceRepository", Repo)
    monkeypatch.setattr(
        trace_service,
        "serialize_trace_event",
        lambda row: {"sequence": row.sequence, "visibility": row.visibility},
    )

    page = await trace_service.list_run_trace_events(
        run_id="run-1",
        current_uid="user-1",
        db=object(),
        after_sequence=5,
        limit=2,
    )

    assert page["events"] == []
    assert page["scanned_through_sequence"] == 7
    assert page["next_after_sequence"] == 7


async def test_snapshot_locks_head_before_reading_projection(monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple] = []
    head = SimpleNamespace(last_sequence=0, projection_sequence=0)

    async def require_run(*args, **kwargs):
        del args, kwargs
        return SimpleNamespace(id="run-1", tenant_id=1)

    class Repo:
        def __init__(self, db):
            del db

        async def get_head(self, run_id, *, for_share=False):
            calls.append(("head", run_id, for_share))
            return head

        async def get_summary(self, run_id):
            calls.append(("summary", run_id))
            return None

        async def list_spans(self, run_id, *, visibility):
            calls.append(("spans", run_id, visibility))
            return []

        async def max_sequence(self, run_id):
            calls.append(("max", run_id))
            return 0

    monkeypatch.setattr(trace_service, "_require_run", require_run)
    monkeypatch.setattr(trace_service, "TraceRepository", Repo)

    snapshot = await trace_service.get_run_trace_snapshot(
        run_id="run-1",
        current_uid="user-1",
        db=object(),
    )

    assert snapshot["projection_sequence"] == 0
    assert calls == [
        ("head", "run-1", True),
        ("summary", "run-1"),
        ("spans", "run-1", "USER"),
        ("max", "run-1"),
    ]


async def test_projection_rebuild_replays_more_than_ten_thousand_events(
    monkeypatch: pytest.MonkeyPatch,
):
    total = 10_001
    requested_after: list[int] = []
    head = SimpleNamespace(projection_sequence=0)

    class Repo:
        def __init__(self, db):
            del db

        async def get_summary(self, run_id):
            del run_id
            return None

        async def list_events(self, run_id, *, after_sequence, limit):
            del run_id
            requested_after.append(after_sequence)
            stop = min(total, after_sequence + limit)
            return [SimpleNamespace(sequence=value) for value in range(after_sequence + 1, stop + 1)]

        async def delete_projection(self, run_id):
            del run_id

        async def upsert_spans(self, run_id, spans, *, tenant_id):
            del run_id, spans, tenant_id

        async def upsert_summary(self, run_id, summary):
            del run_id
            assert summary["last_sequence"] == total

        async def get_head(self, run_id):
            del run_id
            return head

    class RunRepo:
        def __init__(self, db):
            del db

        async def get_run(self, run_id):
            del run_id
            return SimpleNamespace(tenant_id=1)

    monkeypatch.setattr(trace_service, "TraceRepository", Repo)
    monkeypatch.setattr(trace_service, "AgentRunRepository", RunRepo)
    monkeypatch.setattr(trace_service.TraceRecorder, "row_to_event", lambda row: {"sequence": row.sequence})
    monkeypatch.setattr(
        trace_service.projector,
        "replay_events",
        lambda events, header: ({}, {**header, "last_sequence": len(events)}),
    )

    replayed = await trace_service.rebuild_run_trace_projection(run_id="run-1", db=object())

    assert replayed == total
    assert requested_after[-1] == 10_000
    assert head.projection_sequence == total


async def test_retention_calls_bounded_database_function(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, object] = {"calls": []}
    results = iter((12, 0))

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one(self):
            return self.value

    class Db:
        async def execute(self, statement, params):
            captured["sql"] = str(statement)
            captured["params"] = params
            captured["calls"].append(params)
            return Result(next(results))

    @asynccontextmanager
    async def session_context():
        yield Db()

    monkeypatch.setattr(pg_manager, "get_async_session_context", session_context)
    monkeypatch.setattr(trace_service, "TRACE_RETENTION_DAYS", 30)
    monkeypatch.setattr(trace_service, "TRACE_RETENTION_BATCH_RUNS", 25)
    monkeypatch.setattr(trace_service, "TRACE_RETENTION_MAX_BATCHES", 5)

    assert await trace_service.purge_expired_trace_runs() == 12
    assert "yuxi_purge_trace_runs" in captured["sql"]
    assert captured["params"]["batch_limit"] == 25
    assert len(captured["calls"]) == 2


async def test_retention_stops_at_configured_batch_cap(monkeypatch: pytest.MonkeyPatch):
    calls = 0

    class Result:
        def scalar_one(self):
            return 7

    class Db:
        async def execute(self, statement, params):
            nonlocal calls
            del statement, params
            calls += 1
            return Result()

    @asynccontextmanager
    async def session_context():
        yield Db()

    monkeypatch.setattr(pg_manager, "get_async_session_context", session_context)
    monkeypatch.setattr(trace_service, "TRACE_RETENTION_MAX_BATCHES", 3)

    assert await trace_service.purge_expired_trace_runs() == 21
    assert calls == 3
