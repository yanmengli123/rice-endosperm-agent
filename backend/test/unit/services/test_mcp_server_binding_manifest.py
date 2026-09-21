"""点名服务器后验（P1-A）：required_server 存在时，capability 命中还必须来自
该服务器的成功调用；等价能力服务器（ricekb 之于 bio-mcp）不得静默顶替。
"""

from __future__ import annotations

from types import SimpleNamespace

from yuxi.knowledge.planning.turn_execution_plan import plan_turn
from yuxi.services.chat_service import _finalize_mcp_manifest, _initial_source_manifest


class _FakeScalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _FakeScalars(self._rows)


class _FakeDB:
    def __init__(self, audits):
        self._audits = audits

    async def execute(self, *_args, **_kwargs):
        return _FakeResult(self._audits)


def _audit(audit_id: int, *, server: str, tool: str = "ricekb_resolve", status: str = "success"):
    return SimpleNamespace(
        id=audit_id,
        status=status,
        capability_name=tool,
        server_slug=server,
        arguments_digest=f"sha256:req-{audit_id}",
        result_digest=f"sha256:res-{audit_id}",
        provenance={},
    )


async def test_named_server_rejects_equivalent_capability_substitution():
    plan = plan_turn(
        "通过 BioMCP 查 Wx 基因信息",
        has_knowledge_scope=True,
        configured_mcps=["bio-mcp", "ricekb"],
        known_mcps=["bio-mcp", "ricekb"],
    )
    assert plan.required_server == "bio-mcp"

    manifest = _initial_source_manifest(plan)
    # ricekb 成功调用了等价能力工具，但用户点名的是 bio-mcp
    valid = await _finalize_mcp_manifest(
        _FakeDB([_audit(1, server="ricekb")]), run_id="r1", plan=plan, manifest=manifest
    )

    assert valid is False
    assert manifest.error_code == "MCP_SERVER_NOT_INVOKED"
    assert manifest.status == "SOURCE_UNAVAILABLE"
    reason = {item.reason_code for item in manifest.authority_outcomes}
    assert "MCP_SERVER_NOT_INVOKED" in reason


async def test_named_server_passes_when_that_server_succeeded():
    plan = plan_turn(
        "通过 BioMCP 查 Wx 基因信息",
        has_knowledge_scope=True,
        configured_mcps=["bio-mcp", "ricekb"],
        known_mcps=["bio-mcp", "ricekb"],
    )
    manifest = _initial_source_manifest(plan)
    valid = await _finalize_mcp_manifest(
        _FakeDB([_audit(2, server="bio-mcp")]), run_id="r1", plan=plan, manifest=manifest
    )

    assert valid is True
    assert manifest.successful_mcp_call_count == 1
    assert manifest.mcp_servers == ["bio-mcp"]


async def test_unnamed_plan_keeps_capability_level_semantics():
    plan = plan_turn(
        "通过 MCP 查 Wx 基因信息",
        has_knowledge_scope=True,
        configured_mcps=["ricekb"],
        known_mcps=["bio-mcp", "ricekb"],
    )
    assert plan.required_server is None

    manifest = _initial_source_manifest(plan)
    valid = await _finalize_mcp_manifest(
        _FakeDB([_audit(3, server="ricekb")]), run_id="r1", plan=plan, manifest=manifest
    )

    assert valid is True
