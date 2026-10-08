"""Single execution, tenant-scoped inputs, and truthful GenOffice delivery."""

from __future__ import annotations

import asyncio
import base64
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from mcp.types import TextContent

from yuxi.agents.mcp import genoffice, host as host_mod
from yuxi.agents.mcp.host import LegacyLangChainHost

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
CONFIG = {"transport": "streamable_http", "url": "http://genoffice:3093/mcp"}


@pytest.fixture
def harness(monkeypatch):
    seen = {"calls": 0, "active": False, "audit": []}
    host = LegacyLangChainHost()

    async def tools(*args, **kwargs):
        return [SimpleNamespace(name="create_docx"), SimpleNamespace(name="docs_read")]

    async def audit(**kwargs):
        seen["audit"].append(kwargs)
        return 123

    @asynccontextmanager
    async def slot(*args, **kwargs):
        yield

    @asynccontextmanager
    async def session(*args):
        seen["active"] = True

        async def call(name, args):
            seen["calls"] += 1
            data = b"PK-test-docx"
            return SimpleNamespace(
                isError=False,
                structuredContent=None,
                content=[
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {"status": "success", "output_url": "http://genoffice:3093/files/id/report.docx"}
                        ),
                    ),
                    SimpleNamespace(
                        type="resource",
                        resource=SimpleNamespace(
                            uri="http://genoffice:3093/files/id/report.docx",
                            mimeType=None,
                            blob=base64.b64encode(data).decode(),
                        ),
                    ),
                ],
            )

        yield SimpleNamespace(call_tool=call)
        seen["active"] = False

    async def bridge(config, args, **kwargs):
        return args

    async def persist(**kwargs):
        seen["blobs"] = kwargs["blobs"]
        return [
            SimpleNamespace(
                name="report.docx", virtual_path="/home/gem/user-data/outputs/report.docx", size_bytes=12, sha256="abc"
            )
        ]

    monkeypatch.setattr(host, "_ensure_raw_tools", tools)
    monkeypatch.setattr(host, "_official_session", session)
    monkeypatch.setattr(host, "_bridge_file_arguments", bridge)
    monkeypatch.setattr(host_mod, "record_mcp_call", audit)
    monkeypatch.setattr(host_mod, "persist_mcp_file_blobs", persist)
    monkeypatch.setattr(genoffice, "genoffice_slot", slot)
    return host, seen


async def test_file_is_delivered_with_one_tool_execution(harness):
    host, seen = harness
    result = await host.call_tool("genoffice", CONFIG, "create_docx", {"text": "test"})
    assert not result.is_error
    assert seen["calls"] == 1
    assert seen["blobs"][0][2] == b"PK-test-docx"
    assert seen["audit"][0]["status"] == "success"
    assert seen["audit"][0]["result"]["metadata"]["delivered_files"][0]["sha256"] == "abc"
    assert "mcp-file-deliverables" in result.text


async def test_ppt_directory_inputs_are_not_advertised_or_executed(harness):
    schema = {
        "properties": {"spec": {"type": "object"}, "spec_dir": {}, "outline": {}, "out": {}},
        "required": ["spec_dir"],
    }
    projected = genoffice.platform_tool_schema("create_pptx", schema)
    assert set(projected["properties"]) == {"spec"}
    assert not projected["required"]
    assert "spec_dir" in schema["properties"]
    host, seen = harness
    with pytest.raises(ValueError, match="inline spec"):
        await host._call_genoffice(CONFIG, "create_pptx", {"spec_dir": "/tmp/foreign"})
    assert seen["calls"] == 0


async def test_delivery_failure_is_audited_as_error_without_replay(harness, monkeypatch):
    host, seen = harness

    async def fail(**kwargs):
        raise RuntimeError("MCP_DELIVERY_FAILED: artifact registration failed")

    monkeypatch.setattr(host_mod, "persist_mcp_file_blobs", fail)
    result = await host.call_tool("genoffice", CONFIG, "create_docx", {})
    assert result.is_error
    assert seen["calls"] == 1
    assert seen["audit"][0]["status"] == "error"
    assert result.provenance["error_class"] == "MCP_DELIVERY_FAILED"
    assert "mcp-file-deliverables" not in result.text


@pytest.mark.parametrize(
    "value",
    [
        "http://genoffice:3093/files/other/report.docx",
        "https://example.com/x",
        "/etc/passwd",
        "C:/Users/other/report.docx",
        "/home/gem/user-data/outputs/../../../../etc/passwd",
    ],
)
async def test_foreign_file_arguments_never_reach_sidecar(value, monkeypatch):
    from yuxi.agents.mcp.execution import McpExecutionContext

    monkeypatch.setattr(
        "yuxi.agents.mcp.execution.get_mcp_execution_context",
        lambda: McpExecutionContext(tenant_id=2, uid="u2", thread_id="t2"),
    )
    host = LegacyLangChainHost()
    with pytest.raises(ValueError, match="forbidden"):
        await host._bridge_file_arguments(CONFIG, {"file": value}, strict=True)


async def test_download_happens_before_session_close(harness, monkeypatch):
    host, seen = harness

    @asynccontextmanager
    async def session(*args):
        seen["active"] = True

        async def call(name, args):
            seen["calls"] += 1
            return SimpleNamespace(
                isError=False,
                structuredContent=None,
                content=[
                    TextContent(type="text", text=json.dumps({"output_url": "http://genoffice:3093/files/id/big.docx"}))
                ],
            )

        yield SimpleNamespace(call_tool=call)
        seen["active"] = False

    async def download(*args):
        assert seen["active"]
        return b"big-file"

    monkeypatch.setattr(host, "_official_session", session)
    monkeypatch.setattr(host, "_download_within_session", download)
    result = await host.call_tool("genoffice", CONFIG, "create_docx", {})
    assert not result.is_error and seen["calls"] == 1


async def test_cancellation_propagates_and_is_audited(harness, monkeypatch):
    host, seen = harness

    async def cancel(*args):
        raise asyncio.CancelledError

    monkeypatch.setattr(host, "_call_genoffice", cancel)
    with pytest.raises(asyncio.CancelledError):
        await host.call_tool("genoffice", CONFIG, "create_docx", {})
    assert seen["audit"][0]["provenance"]["error_class"] == "MCP_CANCELLED"


async def test_thread_root_symlink_cannot_upload_another_users_file(tmp_path, monkeypatch):
    from yuxi.agents.backends.sandbox import paths
    from yuxi.agents.mcp.execution import McpExecutionContext

    monkeypatch.setattr(paths.conf, "save_dir", str(tmp_path))
    foreign = tmp_path / "threads" / "foreign" / "user-data" / "outputs"
    foreign.mkdir(parents=True)
    (foreign / "secret.md").write_text("private")
    own = tmp_path / "threads" / "own" / "user-data"
    own.mkdir(parents=True)
    (own / "outputs").symlink_to(foreign, target_is_directory=True)
    context = McpExecutionContext(tenant_id=1, uid="u1", thread_id="own")
    assert LegacyLangChainHost._resolve_workspace_file(context, "/home/gem/user-data/outputs/secret.md") is None


async def test_missing_audit_does_not_publish_a_success_notice(harness, monkeypatch):
    host, seen = harness

    async def unavailable(**kwargs):
        return None

    monkeypatch.setattr(host_mod, "record_mcp_call", unavailable)
    result = await host.call_tool("genoffice", CONFIG, "create_docx", {})
    assert result.is_error and "MCP_AUDIT_FAILED" in result.text
    assert seen["calls"] == 1
    assert "mcp-file-deliverables" not in result.text


async def test_resource_links_and_multiple_text_blocks_are_collected_once(harness, monkeypatch):
    host, _ = harness
    url = "http://genoffice:3093/files/id/report.pdf"
    response = SimpleNamespace(
        content=[
            TextContent(type="text", text="Generated report"),
            TextContent(type="text", text=json.dumps({"output_url": url})),
            SimpleNamespace(type="resource_link", name="report.pdf", uri=url),
        ]
    )
    requests = []

    async def download(config, value):
        requests.append(value)
        return b"%PDF-test"

    monkeypatch.setattr(host, "_download_within_session", download)
    blobs = await host._collect_file_blobs(response, "Generated report", CONFIG)
    assert len(blobs) == 1 and requests == [url]


async def test_many_outputs_are_rejected_before_unbounded_collection(harness):
    host, _ = harness
    response = SimpleNamespace(
        content=[
            SimpleNamespace(type="image", mimeType="image/png", data=base64.b64encode(bytes([i])).decode())
            for i in range(21)
        ]
    )
    with pytest.raises(ValueError, match="collection limit"):
        await host._collect_file_blobs(response, "", CONFIG)


@pytest.mark.parametrize("status", ["error", "partial"])
async def test_provider_failure_in_multiblock_response_is_not_success(harness, monkeypatch, status):
    host, seen = harness

    @asynccontextmanager
    async def session(*args):
        async def call(name, arguments):
            seen["calls"] += 1
            return SimpleNamespace(
                isError=False,
                structuredContent=None,
                content=[
                    TextContent(type="text", text="Operation result"),
                    TextContent(type="text", text=json.dumps({"status": status, "error": "failed operation"})),
                ],
            )

        yield SimpleNamespace(call_tool=call)

    monkeypatch.setattr(host, "_official_session", session)
    result = await host.call_tool("genoffice", CONFIG, "create_docx", {})
    assert result.is_error and seen["audit"][0]["status"] == "error"
    assert seen["calls"] == 1 and "blobs" not in seen


async def test_audit_timestamp_is_aware_utc_before_database_flush(monkeypatch):
    from datetime import timedelta

    from yuxi.agents.mcp import execution
    from yuxi.storage.postgres.manager import pg_manager
    from yuxi.storage.postgres.models_business import MCPCallAudit

    captured = []

    class Session:
        def add(self, audit):
            captured.append(audit)

        async def flush(self):
            captured[-1].id = 1

        async def commit(self):
            pass

    @asynccontextmanager
    async def session():
        yield Session()

    monkeypatch.setattr(pg_manager, "get_async_session_context", session)
    monkeypatch.setattr("yuxi.trace.emit_trace", lambda **kwargs: None)
    token = execution.set_mcp_execution_context(execution.McpExecutionContext(tenant_id=1, uid="u1"))
    try:
        assert (
            await execution.record_mcp_call(
                server_slug="genoffice",
                capability_type="tool",
                capability_name="guide",
                arguments={},
                result={},
                status="success",
                duration_ms=1,
            )
            == 1
        )
    finally:
        execution.reset_mcp_execution_context(token)
    assert captured[0].created_at.utcoffset() == timedelta(0)
    assert MCPCallAudit.created_at.default.arg(None).utcoffset() == timedelta(0)
