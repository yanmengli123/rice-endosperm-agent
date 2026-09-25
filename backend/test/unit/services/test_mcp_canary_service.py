"""MCP live canary 的 cron 持久化与故障隔离契约。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from yuxi.agents.mcp.execution import (
    McpExecutionContext,
    get_mcp_execution_context,
    reset_mcp_execution_context,
    set_mcp_execution_context,
)
from yuxi.services import mcp_canary_service


class _ScalarResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return self

    def all(self):
        return list(self._values)


class _Session:
    def __init__(self, slugs):
        self.slugs = slugs
        self.added = []

    async def execute(self, _statement):
        return _ScalarResult(self.slugs)

    def add(self, value):
        self.added.append(value)


class _Recorder:
    instances = []

    def __init__(self, **fields):
        self.fields = fields
        self.activated = False
        self.finalized = False
        self.__class__.instances.append(self)

    def activate(self):
        self.activated = True

    async def finalize(self):
        self.finalized = True


@pytest.mark.asyncio
async def test_canary_persists_admin_trace_system_run_and_isolates_probe_failures(monkeypatch):
    from yuxi.agents.mcp import service as mcp_service
    from yuxi.storage.postgres.manager import pg_manager
    import yuxi.trace as trace_module
    import yuxi.trace.recorder as recorder_module

    session = _Session([])

    @asynccontextmanager
    async def _session_context():
        yield session

    async def _config(slug):
        return None if slug == "gramene" else {"slug": slug}

    async def _discovery(slug, _runtime_config):
        if slug == "ricekb":
            raise TimeoutError("probe timed out")
        return True, 9, ""

    async def _data(slug, _runtime_config):
        return (True, "NOT_FOUND", 25) if slug == "gene-authority" else (True, "OK", 10)

    events = []
    _Recorder.instances.clear()
    monkeypatch.setattr(pg_manager, "get_async_session_context", _session_context)
    monkeypatch.setattr(mcp_service, "get_enabled_mcp_server_config", _config)
    monkeypatch.setattr(mcp_service, "build_runtime_config", lambda slug, config: {"slug": slug, **config})
    monkeypatch.setattr(mcp_canary_service, "_probe_discovery", _discovery)
    monkeypatch.setattr(mcp_canary_service, "_probe_data", _data)
    monkeypatch.setattr(trace_module, "emit_trace", lambda **event: events.append(event))
    monkeypatch.setattr(recorder_module, "TraceRecorder", _Recorder)

    outer = McpExecutionContext(tenant_id=7, uid="outer", run_id="outer-run")
    outer_token = set_mcp_execution_context(outer)
    try:
        summary = await mcp_canary_service.run_mcp_live_canary()
        assert get_mcp_execution_context() is outer
    finally:
        reset_mcp_execution_context(outer_token)

    assert summary == {
        "server_count": 7,
        "ok_count": 5,
        "success_rate": 0.7143,
        "p95_latency_ms": 25,
        "empty_result_count": 1,
    }
    assert len(events) == 8
    assert all(event["visibility"] == "ADMIN" for event in events)
    probe_events = [event for event in events if event["event_type"] == "mcp.canary.probe"]
    assert {event["attributes"]["mcp_server"] for event in probe_events} == {
        "bio-mcp",
        "data-aggregator",
        "gene-authority",
        "gramene",
        "plant-genomics",
        "ricekb",
        "ricekb-profile",
    }
    ricekb = next(event for event in probe_events if event["attributes"]["mcp_server"] == "ricekb")
    assert ricekb["attributes"]["ok"] is False
    assert ricekb["attributes"]["provider_status"] == "UNAVAILABLE"

    assert len(_Recorder.instances) == 1
    recorder = _Recorder.instances[0]
    assert recorder.activated is True
    assert recorder.finalized is True
    assert recorder.fields["run_id"].startswith("mcp-canary-")
    assert recorder.fields["run_type"] == "mcp_canary"

    assert len(session.added) == 1
    system_run = session.added[0]
    assert system_run.id == recorder.fields["run_id"]
    assert system_run.status == "completed"
    assert system_run.input_payload["canary_summary"] == summary


def test_p95_uses_bounded_nearest_rank():
    assert mcp_canary_service._p95([]) == 0
    assert mcp_canary_service._p95([7]) == 7
    assert mcp_canary_service._p95([1, 2, 3, 100]) == 100


@pytest.mark.asyncio
async def test_data_probe_requires_expected_golden_markers(monkeypatch):
    from yuxi.agents.mcp import host as host_module

    class _Host:
        async def call_tool(self, *_args, **_kwargs):
            return SimpleNamespace(text="successful but unrelated payload", is_error=False, provenance={})

    monkeypatch.setattr(host_module, "get_host", lambda: _Host())
    ok, status, _elapsed = await mcp_canary_service._probe_data("gene-authority", {})
    assert ok is False
    assert status == "CONTRACT_MISMATCH"


def test_contract_probes_cover_sequence_and_all_authority_paths():
    rice_tools = {probe.tool_name for probe in mcp_canary_service.CANARY_CONTRACT_PROBES["ricekb"]}
    authority_tools = {
        probe.tool_name for probe in mcp_canary_service.CANARY_CONTRACT_PROBES["gene-authority"]
    }
    assert rice_tools == {"ricekb_sequence"}
    assert authority_tools == {
        "ncbi_datasets_gene_report_rest",
        "ncbi_datasets_gene_summary_cli",
        "uniprot_search_rest",
    }
