"""Deterministic executors for authoritative gene link requests."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any

from yuxi.agents.mcp.host import McpToolResult, get_host
from yuxi.agents.mcp.service import build_runtime_config, get_enabled_mcp_server_config
from yuxi.knowledge.planning.scientific_intent import parse_scientific_intent


@dataclass(frozen=True)
class OfficialLinkResult:
    status: str
    entity: str
    provider: str
    records: list[dict[str, Any]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    error_message: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == "FOUND" and bool(self.records)


async def execute_official_link_query(question: str) -> OfficialLinkResult:
    """Resolve an official provider record without model-generated arguments."""

    from yuxi.agents.mcp.execution import (
        get_mcp_execution_context,
        reset_mcp_execution_context,
        set_mcp_execution_context,
    )

    frame = parse_scientific_intent(question)
    entity = str(frame.entity or "").strip()
    if not entity:
        return OfficialLinkResult(
            status="INVALID_IDENTIFIER",
            entity="",
            provider=str(frame.provider or ""),
            error_message="未能从问题中识别要查询的基因标识符。",
        )
    if frame.provider != "NCBI":
        return OfficialLinkResult(
            status="UNSUPPORTED_PROVIDER",
            entity=entity,
            provider=str(frame.provider or ""),
            error_message="当前仅支持通过确定性链生成 NCBI 官方基因链接。",
        )
    if not frame.organism:
        return OfficialLinkResult(
            status="AMBIGUOUS",
            entity=entity,
            provider="NCBI",
            error_message="缺少物种信息，无法唯一解析 NCBI Gene 记录。",
        )

    context = get_mcp_execution_context()
    token = set_mcp_execution_context(replace(context, artifact_policy="value_only")) if context else None
    try:
        arguments = {
            "identifiers": [entity],
            "identifier_type": "symbol" if not entity.isdigit() else "gene-id",
            "taxon": frame.organism if not entity.isdigit() else None,
            "page_size": 20,
        }
        result = await _call_gene_authority("ncbi_datasets_gene_report_rest", arguments)
        calls = [{"tool": "ncbi_datasets_gene_report_rest", "arguments": arguments, "is_error": result.is_error}]
        if result.is_error:
            return OfficialLinkResult(
                status=str((result.provenance or {}).get("provider_status") or "UNAVAILABLE"),
                entity=entity,
                provider="NCBI",
                calls=calls,
                error_message="NCBI 权威记录查询失败。",
            )
        envelope = _json_envelope(result.text)
        status = str((envelope or {}).get("status") or "UNAVAILABLE").upper()
        reports = (envelope or {}).get("data", {}).get("reports", [])
        records = [item for item in reports if isinstance(item, dict) and isinstance(item.get("gene"), dict)]
        if status == "FOUND" and records:
            expected_taxon = frame.organism.casefold()
            records = [
                item
                for item in records
                if expected_taxon in str(item["gene"].get("taxname") or "").casefold()
                or str(item["gene"].get("tax_id") or "") in {"39947", "4530"}
            ]
            if not records:
                status = "CONFLICT"
        return OfficialLinkResult(
            status=status,
            entity=entity,
            provider="NCBI",
            records=records,
            calls=calls,
            error_message=None if status == "FOUND" and records else "NCBI 未返回与物种一致的权威基因记录。",
        )
    finally:
        reset_mcp_execution_context(token)


async def _call_gene_authority(tool_name: str, arguments: dict[str, Any]) -> McpToolResult:
    config = await get_enabled_mcp_server_config("gene-authority")
    if config is None:
        return McpToolResult(
            text="Gene Authority MCP 未启用",
            is_error=True,
            provenance={"provider_status": "UNAVAILABLE"},
        )
    runtime_config = build_runtime_config("gene-authority", config)
    return await get_host().call_tool("gene-authority", runtime_config, tool_name, arguments)


def _json_envelope(text: str) -> dict[str, Any] | None:
    candidate = str(text or "").strip()
    if not candidate.startswith("{"):
        return None
    try:
        payload, _ = json.JSONDecoder().raw_decode(candidate)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


__all__ = ["OfficialLinkResult", "execute_official_link_query"]
