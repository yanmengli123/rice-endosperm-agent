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
from yuxi.agents.mcp.sequence_deliverable import SequenceDeliverable, verify_sequence_integrity
from yuxi.agents.mcp.service import build_runtime_config, get_enabled_mcp_server_config
from yuxi.knowledge.planning.scientific_intent import parse_scientific_intent

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

#: 请求类型 → 可接受的返回类型（仅同分子族内的上游命名差异，不做跨类型放宽：
#: 请求 CDS 时 transcript 必须判为契约漂移，绝不静默当 CDS 发布）。
_TYPE_EQUIVALENTS: dict[str, frozenset[str]] = {
    "cds": frozenset({"cds"}),
    "transcript": frozenset({"transcript", "cdna", "mrna"}),
    "protein": frozenset({"protein", "peptide", "aa"}),
    "gene": frozenset({"gene", "genomic"}),
}
_DRIFT_REASONS = frozenset({"sequence_id_mismatch", "sequence_type_mismatch", "sequence_integrity_mismatch"})
_EMPTY_REASONS = frozenset({"not_found", "no_evidence", "empty_payload"})


@dataclass(frozen=True)
class RiceSequenceResult:
    status: str
    identifier: str
    sequence_type: str
    records: list[dict[str, Any]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    error_message: str | None = None
    expected_count: int = 0
    failures: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.status == "FOUND" and bool(self.records)

    @property
    def succeeded_count(self) -> int:
        return len(self.records)

    @property
    def failed_count(self) -> int:
        return len(self.failures)


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
    failures: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for arguments in requests:
        requested_id = str(arguments["sequence_id"])
        requested_type = str(arguments["sequence_type"])
        sequence_result, envelope = await call("ricekb_sequence", arguments)
        if sequence_result.is_error:
            return RiceSequenceResult(
                status="UNAVAILABLE",
                identifier=identifier,
                sequence_type=sequence_type,
                records=records,
                calls=calls,
                error_message="RiceKB 序列工具调用失败。",
                expected_count=len(requests),
                failures=failures,
                warnings=warnings,
            )
        status = str((envelope or {}).get("status") or "UNAVAILABLE").upper()
        data = (envelope or {}).get("data")
        if status != "FOUND" or not isinstance(data, dict):
            failures.append(
                {"sequence_id": requested_id, "reason": "empty_payload" if status == "FOUND" else status.casefold()}
            )
            continue
        record = dict(data)
        violation = _verify_sequence_record(record, requested_type=requested_type, requested_id=requested_id)
        if violation is not None:
            # 类型/标识符/字节不一致：绝不把这条记录当成本次请求的答案发布。
            failures.append({"sequence_id": requested_id, "reason": violation})
            continue
        audit_id = (sequence_result.provenance or {}).get("mcp_call_audit_id")
        if audit_id is not None:
            # 每条记录绑定本次调用自己的 audit ID（多转录本轮禁止跨调用绑定）。
            record["mcp_call_audit_id"] = audit_id
        if _sequence_bytes_consistent(record) is None:
            warnings.append({"sequence_id": requested_id, "reason": "sequence_bytes_unverifiable"})
        records.append(record)

    final_status, error_message = _final_status(requests, records, failures)
    return RiceSequenceResult(
        status=final_status,
        identifier=identifier,
        sequence_type=sequence_type,
        records=records,
        calls=calls,
        error_message=error_message,
        expected_count=len(requests),
        failures=failures,
        warnings=warnings,
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
    return parse_scientific_intent(question).sequence_type or "transcript"


def _direct_sequence_request(identifier: str, sequence_type: str) -> dict[str, str] | None:
    if _RAP_TRANSCRIPT.fullmatch(identifier):
        return {"source": "RAP_DB", "sequence_type": sequence_type, "sequence_id": identifier}
    if _MSU_TRANSCRIPT.fullmatch(identifier):
        msu_type = "cdna" if sequence_type == "transcript" else sequence_type
        if msu_type in {"cds", "cdna"}:
            return {"source": "MSU", "sequence_type": msu_type, "sequence_id": identifier}
    return None


def _sequence_bytes_consistent(record: dict[str, Any]) -> bool | None:
    """序列字节自洽门：与 FASTA 落盘同一实现（``verify_sequence_integrity``）。

    返回 ``None`` 表示记录是摘要形态（未携带可校验字节），此时不阻塞摘要发布，
    但该记录不会产出 FASTA；返回 ``False`` 表示字节与声明的长度/字母表/摘要
    不一致——上游损坏或被篡改的数据不得进入答案。
    """

    sequence = str(record.get("sequence") or "")
    declared_sha = str(record.get("sequence_sha256") or "").strip().lower()
    if not sequence and not declared_sha:
        return None
    if not sequence or not declared_sha:
        return False
    try:
        declared_length = int(record.get("sequence_length") or 0)
    except (TypeError, ValueError):
        return False
    spec = SequenceDeliverable(
        sequence_id=str(record.get("sequence_id") or ""),
        sequence_type=str(record.get("sequence_type") or ""),
        sequence=sequence,
        sequence_length=declared_length or len(sequence),
        sequence_sha256=declared_sha,
        description="",
        source_table="",
    )
    return verify_sequence_integrity(spec)


def _verify_sequence_record(
    record: dict[str, Any],
    *,
    requested_type: str,
    requested_id: str,
) -> str | None:
    """后置条件：返回记录必须与本次请求同标识符、同类型且字节自洽。"""

    returned_id = str(record.get("sequence_id") or "").strip()
    if returned_id.casefold() != requested_id.casefold():
        return "sequence_id_mismatch"
    returned_type = str(record.get("sequence_type") or "").strip().casefold()
    accepted = _TYPE_EQUIVALENTS.get(requested_type.casefold(), frozenset({requested_type.casefold()}))
    if returned_type not in accepted:
        return "sequence_type_mismatch"
    if _sequence_bytes_consistent(record) is False:
        return "sequence_integrity_mismatch"
    return None


def _final_status(requests: list[dict[str, Any]], records: list[dict], failures: list[dict]) -> tuple[str, str | None]:
    """成功数/失败数记账 → (状态, 用户面失败文案)。少一条就是 PARTIAL，不补齐。"""

    expected = len(requests)
    drift = [failure for failure in failures if failure.get("reason") in _DRIFT_REASONS]
    if drift:
        return (
            "CONTRACT_DRIFT",
            f"数据源返回的 {len(drift)}/{expected} 条记录未通过本次请求的契约校验"
            "（标识符、序列类型或字节摘要不一致），已拒绝发布错类型或不可校验的结果。",
        )
    if expected and len(records) >= expected:
        return "FOUND", None
    if records:
        return (
            "PARTIAL",
            f"数据源只返回了 {len(records)}/{expected} 条请求的序列记录，"
            "缺失记录不会以推断值补齐，已按部分结果处理；请重试或改用完整标识符点名查询。",
        )
    # 一条都没拿到：按首个失败原因给终态——NOT_FOUND 绝不吞掉 UNAVAILABLE。
    reason = str((failures[0].get("reason") if failures else "not_found") or "not_found").lower()
    if reason in _EMPTY_REASONS:
        return "NOT_FOUND", "RiceKB 未找到匹配的序列记录。"
    if reason in {"unavailable", "error", "timeout", "rate_limited", "unauthenticated"}:
        return "UNAVAILABLE", "RiceKB 序列查询暂不可用，请稍后重试。"
    if reason in {"ambiguous", "conflict"}:
        return reason.upper(), "RiceKB 无法唯一解析该标识符，请携带完整标识符（如 RAP ID / MSU ID）重新查询。"
    return reason.upper(), "RiceKB 未返回可用于发布的序列结果。"


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
