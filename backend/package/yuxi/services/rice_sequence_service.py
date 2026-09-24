"""Deterministic RiceKB sequence retrieval for sequence-export turns.

The model is not part of this path.  A symbol/locus is resolved, transcript
identifiers are enumerated from the authoritative entity record, and every
requested source sequence is fetched through the audited MCP host.  The host
also creates the downloadable FASTA artifacts.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Any

from yuxi.agents.mcp.host import McpToolResult, get_host
from yuxi.agents.mcp.service import build_runtime_config, get_enabled_mcp_server_config

_RAP_TRANSCRIPT = re.compile(r"\bOs(?:0[1-9]|1[0-2])t\d{5,7}-\d{2}\b", re.I)
_RAP_LOCUS = re.compile(r"\bOs(?:0[1-9]|1[0-2])g\d{5,7}\b", re.I)
_MSU_TRANSCRIPT = re.compile(r"\bLOC_Os(?:0[1-9]|1[0-2])g\d{5,7}\.\d+\b", re.I)
_MSU_LOCUS = re.compile(r"\bLOC_Os(?:0[1-9]|1[0-2])g\d{5,7}\b", re.I)
_QUERY_IDENTIFIER = re.compile(
    r"(?<![A-Za-z0-9_.-])([A-Za-z][A-Za-z0-9_.-]{0,39})(?![A-Za-z0-9_.-])"
    r"\s*的\s*(?:CDS|cDNA|mRNA|转录本|蛋白|基因组|FASTA|序列)",
    re.I,
)
_AFTER_LOOKUP = re.compile(
    r"(?:查|查询|检索|获取)\s*([A-Za-z][A-Za-z0-9_.-]{0,39})(?![A-Za-z0-9_.-])",
    re.I,
)
_RESERVED = {"MCP", "CDS", "CDNA", "MRNA", "FASTA", "RICEKB"}


@dataclass(frozen=True)
class RiceSequenceResult:
    status: str
    identifier: str
    sequence_type: str
    records: list[dict[str, Any]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    error_message: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == "FOUND" and bool(self.records)


async def execute_rice_sequence_query(question: str) -> RiceSequenceResult:
    """Execute the fixed resolve → entity → sequence chain through RiceKB MCP."""

    from yuxi.agents.mcp.execution import (
        get_mcp_execution_context,
        reset_mcp_execution_context,
        set_mcp_execution_context,
    )

    context = get_mcp_execution_context()
    context_token = (
        set_mcp_execution_context(replace(context, artifact_policy="sequence_only")) if context is not None else None
    )
    try:
        return await _execute_rice_sequence_query(question)
    finally:
        reset_mcp_execution_context(context_token)


async def _execute_rice_sequence_query(question: str) -> RiceSequenceResult:
    """Implementation separated so the scoped artifact policy always resets."""

    identifier = _extract_identifier(question)
    sequence_type = _extract_sequence_type(question)
    if not identifier:
        return RiceSequenceResult(
            status="INVALID_IDENTIFIER",
            identifier="",
            sequence_type=sequence_type,
            error_message="未能从问题中识别基因符号或数据库标识符。",
        )

    calls: list[dict[str, Any]] = []

    async def call(tool_name: str, arguments: dict[str, Any]) -> tuple[McpToolResult, dict[str, Any] | None]:
        result = await _call_ricekb_tool(tool_name, arguments)
        calls.append({"tool": tool_name, "arguments": arguments, "is_error": result.is_error})
        return result, _json_envelope(result.text)

    direct = _direct_sequence_request(identifier, sequence_type)
    if direct is not None:
        requests = [direct]
    else:
        resolved_result, resolved = await call("ricekb_resolve", {"query": identifier, "limit": 20})
        failure = _failed_result(resolved_result, resolved, identifier, sequence_type, calls)
        if failure is not None:
            return failure
        canonical = _canonical_rap_id(resolved)
        if not canonical:
            return RiceSequenceResult(
                status="INVALID_IDENTIFIER",
                identifier=identifier,
                sequence_type=sequence_type,
                calls=calls,
                error_message="RiceKB 未返回可用于精确取序列的规范 RAP 标识符。",
            )
        if sequence_type == "gene":
            requests = [{"source": "RAP_DB", "sequence_type": "gene", "sequence_id": canonical}]
        else:
            entity_result, entity = await call("ricekb_entity", {"identifier": canonical})
            failure = _failed_result(entity_result, entity, identifier, sequence_type, calls)
            if failure is not None:
                return failure
            transcript_ids = _rap_transcript_ids(entity)
            if not transcript_ids:
                return RiceSequenceResult(
                    status="NOT_FOUND",
                    identifier=identifier,
                    sequence_type=sequence_type,
                    calls=calls,
                    error_message="RiceKB 实体记录中没有可用于取序列的 RAP-DB 转录本标识符。",
                )
            requests = [
                {"source": "RAP_DB", "sequence_type": sequence_type, "sequence_id": transcript_id}
                for transcript_id in transcript_ids
            ]

    records: list[dict[str, Any]] = []
    final_status = "NOT_FOUND"
    for arguments in requests:
        sequence_result, envelope = await call("ricekb_sequence", arguments)
        if sequence_result.is_error:
            return RiceSequenceResult(
                status="UNAVAILABLE",
                identifier=identifier,
                sequence_type=sequence_type,
                records=records,
                calls=calls,
                error_message="RiceKB 序列工具调用失败。",
            )
        status = str((envelope or {}).get("status") or "UNAVAILABLE").upper()
        if status == "FOUND" and isinstance((envelope or {}).get("data"), dict):
            records.append(dict(envelope["data"]))
            final_status = "FOUND"
        elif final_status != "FOUND":
            final_status = status

    return RiceSequenceResult(
        status=final_status,
        identifier=identifier,
        sequence_type=sequence_type,
        records=records,
        calls=calls,
        error_message=None if records else "RiceKB 未找到匹配的序列记录。",
    )


async def _call_ricekb_tool(tool_name: str, arguments: dict[str, Any]) -> McpToolResult:
    config = await get_enabled_mcp_server_config("ricekb")
    if config is None:
        return McpToolResult(text="RiceKB MCP 未启用", is_error=True)
    runtime_config = build_runtime_config("ricekb", config)
    return await get_host().call_tool("ricekb", runtime_config, tool_name, arguments)


def _extract_identifier(question: str) -> str:
    text = str(question or "")
    for pattern in (_RAP_TRANSCRIPT, _MSU_TRANSCRIPT, _RAP_LOCUS, _MSU_LOCUS):
        match = pattern.search(text)
        if match:
            return match.group(0)
    for pattern in (_QUERY_IDENTIFIER, _AFTER_LOOKUP):
        match = pattern.search(text)
        if match and match.group(1).upper() not in _RESERVED:
            return match.group(1)
    return ""


def _extract_sequence_type(question: str) -> str:
    text = str(question or "")
    if re.search(r"\bCDS\b", text, re.I):
        return "cds"
    if re.search(r"蛋白|protein", text, re.I):
        return "protein"
    if re.search(r"基因组|genomic|gene\s+sequence", text, re.I):
        return "gene"
    return "transcript"


def _direct_sequence_request(identifier: str, sequence_type: str) -> dict[str, str] | None:
    if _RAP_TRANSCRIPT.fullmatch(identifier):
        return {"source": "RAP_DB", "sequence_type": sequence_type, "sequence_id": identifier}
    if _MSU_TRANSCRIPT.fullmatch(identifier):
        msu_type = "cdna" if sequence_type == "transcript" else sequence_type
        if msu_type in {"cds", "cdna"}:
            return {"source": "MSU", "sequence_type": msu_type, "sequence_id": identifier}
    return None


def _json_envelope(text: str) -> dict[str, Any] | None:
    candidate = str(text or "").strip()
    if not candidate.startswith("{"):
        return None
    try:
        payload, _ = json.JSONDecoder().raw_decode(candidate)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _failed_result(
    result: McpToolResult,
    envelope: dict[str, Any] | None,
    identifier: str,
    sequence_type: str,
    calls: list[dict[str, Any]],
) -> RiceSequenceResult | None:
    if result.is_error:
        return RiceSequenceResult(
            status="UNAVAILABLE",
            identifier=identifier,
            sequence_type=sequence_type,
            calls=calls,
            error_message="RiceKB MCP 调用失败。",
        )
    status = str((envelope or {}).get("status") or "UNAVAILABLE").upper()
    if status in {"FOUND", "PARTIAL"}:
        return None
    return RiceSequenceResult(
        status=status,
        identifier=identifier,
        sequence_type=sequence_type,
        calls=calls,
        error_message="RiceKB 未找到唯一匹配的基因记录。" if status == "NOT_FOUND" else "RiceKB 无法唯一解析该标识符。",
    )


def _canonical_rap_id(envelope: dict[str, Any] | None) -> str:
    entity = (envelope or {}).get("entity")
    if isinstance(entity, dict) and entity.get("canonical_rap_id"):
        return str(entity["canonical_rap_id"])
    for row in (envelope or {}).get("data") or []:
        if isinstance(row, dict) and row.get("canonical_rap_id"):
            return str(row["canonical_rap_id"])
    return ""


def _rap_transcript_ids(envelope: dict[str, Any] | None) -> list[str]:
    data = (envelope or {}).get("data")
    if not isinstance(data, dict):
        return []
    candidates: list[str] = []
    source_records = data.get("source_records")
    if isinstance(source_records, dict):
        for row in source_records.get("rapdb_annotations") or []:
            if isinstance(row, dict):
                candidates.append(str(row.get("transcript_id") or ""))
    for row in data.get("identifiers") or []:
        if isinstance(row, dict):
            candidates.append(str(row.get("identifier") or ""))
    return sorted({candidate for candidate in candidates if _RAP_TRANSCRIPT.fullmatch(candidate)})


__all__ = ["RiceSequenceResult", "execute_rice_sequence_query"]
