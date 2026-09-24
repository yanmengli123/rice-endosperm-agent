from __future__ import annotations

import hashlib
import json

import pytest

from yuxi.agents.mcp.host import McpToolResult
from yuxi.services.rice_sequence_service import execute_rice_sequence_query


def _result(payload: dict) -> McpToolResult:
    return McpToolResult(text=json.dumps(payload, ensure_ascii=False))


@pytest.mark.asyncio
async def test_symbol_transcript_query_resolves_and_fetches_all_isoforms(monkeypatch):
    seen: list[tuple[str, dict]] = []

    async def fake_call(tool_name: str, arguments: dict):
        seen.append((tool_name, arguments))
        if tool_name == "ricekb_resolve":
            return _result({"status": "FOUND", "entity": {"canonical_rap_id": "Os06g0133000"}, "data": []})
        if tool_name == "ricekb_entity":
            return _result(
                {
                    "status": "FOUND",
                    "data": {
                        "identifiers": [],
                        "source_records": {
                            "rapdb_annotations": [
                                {"transcript_id": "Os06t0133000-02"},
                                {"transcript_id": "Os06t0133000-01"},
                            ]
                        },
                    },
                }
            )
        sequence_id = arguments["sequence_id"]
        sequence = "ATGC" if sequence_id.endswith("01") else "ATGCAA"
        return _result(
            {
                "status": "FOUND",
                "data": {
                    "source_database": "RAP_DB",
                    "sequence_type": "transcript",
                    "sequence_id": sequence_id,
                    "sequence": sequence,
                    "sequence_length": len(sequence),
                    "sequence_sha256": hashlib.sha256(sequence.encode()).hexdigest(),
                },
            }
        )

    monkeypatch.setattr("yuxi.services.rice_sequence_service._call_ricekb_tool", fake_call)
    result = await execute_rice_sequence_query("Wx的转录本序列给我")

    assert result.succeeded is True
    assert result.identifier == "Wx"
    assert [record["sequence_id"] for record in result.records] == [
        "Os06t0133000-01",
        "Os06t0133000-02",
    ]
    assert [name for name, _ in seen] == [
        "ricekb_resolve",
        "ricekb_entity",
        "ricekb_sequence",
        "ricekb_sequence",
    ]


@pytest.mark.asyncio
async def test_exact_transcript_skips_resolution(monkeypatch):
    seen: list[tuple[str, dict]] = []

    async def fake_call(tool_name: str, arguments: dict):
        seen.append((tool_name, arguments))
        return _result(
            {
                "status": "FOUND",
                "data": {
                    "sequence_id": arguments["sequence_id"],
                    "sequence_type": arguments["sequence_type"],
                    "sequence": "ATGC",
                    "sequence_length": 4,
                    "sequence_sha256": hashlib.sha256(b"ATGC").hexdigest(),
                },
            }
        )

    monkeypatch.setattr("yuxi.services.rice_sequence_service._call_ricekb_tool", fake_call)
    result = await execute_rice_sequence_query("Os06t0133000-01 的 CDS 序列")

    assert result.succeeded is True
    assert seen == [
        (
            "ricekb_sequence",
            {"source": "RAP_DB", "sequence_type": "cds", "sequence_id": "Os06t0133000-01"},
        )
    ]


@pytest.mark.asyncio
async def test_not_found_is_not_reported_as_unavailable(monkeypatch):
    async def fake_call(tool_name: str, arguments: dict):
        return _result({"status": "NOT_FOUND", "data": []})

    monkeypatch.setattr("yuxi.services.rice_sequence_service._call_ricekb_tool", fake_call)
    result = await execute_rice_sequence_query("UnknownGene的转录本序列给我")

    assert result.status == "NOT_FOUND"
    assert result.succeeded is False
    assert result.error_message == "RiceKB 未找到唯一匹配的基因记录。"


@pytest.mark.asyncio
async def test_internal_resolution_keeps_audit_context_but_limits_artifacts(monkeypatch):
    from yuxi.agents.mcp.execution import (
        McpExecutionContext,
        get_mcp_execution_context,
        reset_mcp_execution_context,
        set_mcp_execution_context,
    )

    observed_policies = []

    async def fake_call(tool_name: str, arguments: dict):
        observed_policies.append(get_mcp_execution_context().artifact_policy)
        return _result(
            {
                "status": "FOUND",
                "data": {
                    "sequence_id": arguments["sequence_id"],
                    "sequence_type": arguments["sequence_type"],
                    "sequence": "ATGC",
                    "sequence_length": 4,
                    "sequence_sha256": hashlib.sha256(b"ATGC").hexdigest(),
                },
            }
        )

    monkeypatch.setattr("yuxi.services.rice_sequence_service._call_ricekb_tool", fake_call)
    outer = McpExecutionContext(tenant_id=1, uid="u1", thread_id="t1", run_id="r1")
    token = set_mcp_execution_context(outer)
    try:
        result = await execute_rice_sequence_query("Os06t0133000-01 的 CDS 序列")
        assert result.succeeded is True
        assert observed_policies == ["sequence_only"]
        assert get_mcp_execution_context() is outer
    finally:
        reset_mcp_execution_context(token)
