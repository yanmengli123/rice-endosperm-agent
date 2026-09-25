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
async def test_chinese_adjacent_cds_does_not_fall_back_to_transcript(monkeypatch):
    seen: list[tuple[str, dict]] = []

    async def fake_call(tool_name: str, arguments: dict):
        seen.append((tool_name, arguments))
        if tool_name == "ricekb_resolve":
            return _result({"status": "FOUND", "entity": {"canonical_rap_id": "Os06g0133000"}, "data": []})
        if tool_name == "ricekb_entity":
            return _result(
                {
                    "status": "FOUND",
                    "data": {"source_records": {"rapdb_annotations": [{"transcript_id": "Os06t0133000-01"}]}},
                }
            )
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
    result = await execute_rice_sequence_query("Wx的CDS序列给我")

    assert result.succeeded is True
    assert seen[-1] == (
        "ricekb_sequence",
        {"source": "RAP_DB", "sequence_type": "cds", "sequence_id": "Os06t0133000-01"},
    )


@pytest.mark.asyncio
async def test_requested_cds_never_accepts_transcript_records(monkeypatch):
    """红线：请求 cds 时上游返回 transcript 必须判契约漂移，不得当 CDS 发布。"""

    async def fake_call(tool_name: str, arguments: dict):
        sequence = "ATGC"
        return _result(
            {
                "status": "FOUND",
                "data": {
                    "sequence_id": arguments["sequence_id"],
                    "sequence_type": "transcript",
                    "sequence": sequence,
                    "sequence_length": len(sequence),
                    "sequence_sha256": hashlib.sha256(sequence.encode()).hexdigest(),
                },
            }
        )

    monkeypatch.setattr("yuxi.services.rice_sequence_service._call_ricekb_tool", fake_call)
    result = await execute_rice_sequence_query("Os06t0133000-01 的 CDS 序列")

    assert result.status == "CONTRACT_DRIFT"
    assert result.succeeded is False
    assert result.records == []
    assert result.expected_count == 1
    assert result.failed_count == 1
    assert result.failures[0]["reason"] == "sequence_type_mismatch"
    assert "契约校验" in (result.error_message or "")


@pytest.mark.asyncio
async def test_missing_transcript_is_partial_with_success_and_failure_counts(monkeypatch):
    """红线：两个转录本只回一个 → PARTIAL 且必须记录成功数/失败数，绝不静默当完整结果。"""

    async def fake_call(tool_name: str, arguments: dict):
        if tool_name == "ricekb_resolve":
            return _result({"status": "FOUND", "entity": {"canonical_rap_id": "Os06g0133000"}, "data": []})
        if tool_name == "ricekb_entity":
            return _result(
                {
                    "status": "FOUND",
                    "data": {
                        "source_records": {
                            "rapdb_annotations": [
                                {"transcript_id": "Os06t0133000-01"},
                                {"transcript_id": "Os06t0133000-02"},
                            ]
                        }
                    },
                }
            )
        if arguments["sequence_id"].endswith("02"):
            return _result({"status": "NOT_FOUND", "data": []})
        sequence = "ATGC"
        return _result(
            {
                "status": "FOUND",
                "data": {
                    "sequence_id": arguments["sequence_id"],
                    "sequence_type": "cds",
                    "sequence": sequence,
                    "sequence_length": len(sequence),
                    "sequence_sha256": hashlib.sha256(sequence.encode()).hexdigest(),
                },
            }
        )

    monkeypatch.setattr("yuxi.services.rice_sequence_service._call_ricekb_tool", fake_call)
    result = await execute_rice_sequence_query("Wx的CDS序列给我")

    assert result.status == "PARTIAL"
    assert result.succeeded is False
    assert result.expected_count == 2
    assert result.succeeded_count == 1
    assert result.failed_count == 1
    assert [record["sequence_id"] for record in result.records] == ["Os06t0133000-01"]
    assert "1/2" in (result.error_message or "")


@pytest.mark.asyncio
async def test_mismatched_sequence_id_is_contract_drift(monkeypatch):
    """绑定红线：上游把 -02 的序列挂在 -01 请求上时拒绝发布。"""

    async def fake_call(tool_name: str, arguments: dict):
        sequence = "ATGC"
        return _result(
            {
                "status": "FOUND",
                "data": {
                    "sequence_id": "Os06t0133000-02",
                    "sequence_type": "cds",
                    "sequence": sequence,
                    "sequence_length": len(sequence),
                    "sequence_sha256": hashlib.sha256(sequence.encode()).hexdigest(),
                },
            }
        )

    monkeypatch.setattr("yuxi.services.rice_sequence_service._call_ricekb_tool", fake_call)
    result = await execute_rice_sequence_query("Os06t0133000-01 的 CDS 序列")

    assert result.status == "CONTRACT_DRIFT"
    assert result.failures[0]["reason"] == "sequence_id_mismatch"
    assert result.records == []


@pytest.mark.asyncio
async def test_tampered_sequence_bytes_are_rejected_before_publishing(monkeypatch):
    """完整性红线：长度/摘要与字节不一致的记录不得进入答案（与 FASTA 落盘同一门）。"""

    async def fake_call(tool_name: str, arguments: dict):
        return _result(
            {
                "status": "FOUND",
                "data": {
                    "sequence_id": arguments["sequence_id"],
                    "sequence_type": "cds",
                    "sequence": "ATGC",
                    "sequence_length": 4,
                    "sequence_sha256": hashlib.sha256(b"ATGG").hexdigest(),
                },
            }
        )

    monkeypatch.setattr("yuxi.services.rice_sequence_service._call_ricekb_tool", fake_call)
    result = await execute_rice_sequence_query("Os06t0133000-01 的 CDS 序列")

    assert result.status == "CONTRACT_DRIFT"
    assert result.failures[0]["reason"] == "sequence_integrity_mismatch"


@pytest.mark.asyncio
async def test_each_record_binds_its_own_call_audit_id(monkeypatch):
    """多转录本轮：每条记录绑定本次调用自己的 audit ID，禁止跨调用绑定。"""

    audit_ids = iter((390, 391))

    async def fake_call(tool_name: str, arguments: dict):
        if tool_name == "ricekb_resolve":
            return _result({"status": "FOUND", "entity": {"canonical_rap_id": "Os06g0133000"}, "data": []})
        if tool_name == "ricekb_entity":
            return _result(
                {
                    "status": "FOUND",
                    "data": {
                        "source_records": {
                            "rapdb_annotations": [
                                {"transcript_id": "Os06t0133000-01"},
                                {"transcript_id": "Os06t0133000-02"},
                            ]
                        }
                    },
                }
            )
        sequence = "ATGC"
        return McpToolResult(
            text=json.dumps(
                {
                    "status": "FOUND",
                    "data": {
                        "sequence_id": arguments["sequence_id"],
                        "sequence_type": "cds",
                        "sequence": sequence,
                        "sequence_length": len(sequence),
                        "sequence_sha256": hashlib.sha256(sequence.encode()).hexdigest(),
                    },
                }
            ),
            provenance={"mcp_call_audit_id": next(audit_ids)},
        )

    monkeypatch.setattr("yuxi.services.rice_sequence_service._call_ricekb_tool", fake_call)
    result = await execute_rice_sequence_query("Wx的CDS序列给我")

    assert result.succeeded is True
    assert [record["mcp_call_audit_id"] for record in result.records] == [390, 391]


@pytest.mark.asyncio
async def test_sequence_fetch_unavailable_is_not_reported_as_not_found(monkeypatch):
    """单条序列取回失败：UNAVAILABLE 不得被 PARTIAL/NOT_FOUND 稀释。"""

    async def fake_call(tool_name: str, arguments: dict):
        return _result({"status": "UNAVAILABLE", "data": []})

    monkeypatch.setattr("yuxi.services.rice_sequence_service._call_ricekb_tool", fake_call)
    result = await execute_rice_sequence_query("Os06t0133000-01 的 CDS 序列")

    assert result.status == "UNAVAILABLE"
    assert result.succeeded is False
    assert "暂不可用" in (result.error_message or "")
    assert "未找到" not in (result.error_message or "")


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
