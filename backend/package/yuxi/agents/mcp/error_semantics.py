"""Stable, value-free failure semantics for MCP calls.

Raw adapter error text is deliberately not persisted in diagnostic columns: it
may echo tool arguments or credentials.  The classifier stores a bounded code,
stage and fixed public-safe summary while the existing result digest preserves
tamper evidence without exposing values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class McpFailureClassification:
    error_class: str
    error_stage: str
    provider_status: str
    error_excerpt: str
    http_status: int | None = None


_HTTP_STATUS_RE = re.compile(r"(?:status(?:\s+code)?|http)[^0-9]{0,8}([1-5][0-9]{2})", re.IGNORECASE)


def _failure_text(value: Any) -> str:
    if isinstance(value, BaseException):
        return f"{type(value).__name__}: {value}"
    return str(value or "")


def classify_mcp_failure(value: Any, *, stage_hint: str | None = None) -> McpFailureClassification:
    """Map adapter/provider failures onto a closed, UI-safe taxonomy."""

    text = _failure_text(value)
    lowered = text.casefold()
    status_match = _HTTP_STATUS_RE.search(text)
    http_status = int(status_match.group(1)) if status_match else None

    if (
        any(
            marker in lowered
            for marker in (
                "validationerror",
                "validation error",
                "field required",
                "missing required",
                "extra inputs are not permitted",
                "unexpected keyword argument",
                "invalid arguments",
                "invalid tool arguments",
            )
        )
        or http_status == 422
    ):
        return McpFailureClassification(
            "MCP_TOOL_ARGS_INVALID",
            "validation",
            "ARGUMENT_INVALID",
            "工具参数未通过服务端契约校验。",
            http_status,
        )
    if http_status == 429 or "rate limit" in lowered or "too many requests" in lowered:
        return McpFailureClassification(
            "MCP_RATE_LIMITED", "provider", "UNAVAILABLE", "数据源请求频率受限。", http_status or 429
        )
    if http_status in {401, 403} or any(marker in lowered for marker in ("unauthorized", "forbidden")):
        return McpFailureClassification(
            "MCP_AUTH_FAILED", "authentication", "UNAVAILABLE", "数据源认证或授权失败。", http_status
        )
    if any(marker in lowered for marker in ("timeout", "timed out", "deadline exceeded")):
        return McpFailureClassification("MCP_TIMEOUT", "transport", "UNAVAILABLE", "数据源请求超时。", http_status)
    if http_status is not None and 500 <= http_status <= 599:
        return McpFailureClassification(
            "MCP_PROVIDER_5XX", "provider", "UNAVAILABLE", "数据源服务端暂时不可用。", http_status
        )
    if any(
        marker in lowered
        for marker in (
            "connection refused",
            "connection reset",
            "name or service not known",
            "temporary failure in name resolution",
            "getaddrinfo",
            "network is unreachable",
            "connecterror",
        )
    ):
        return McpFailureClassification(
            "MCP_TRANSPORT_FAILED", "transport", "UNAVAILABLE", "无法连接数据源。", http_status
        )
    return McpFailureClassification(
        "MCP_TOOL_ERROR",
        stage_hint or "execution",
        "ERROR",
        "MCP 工具执行失败。",
        http_status,
    )


def apply_failure_provenance(
    provenance: dict[str, Any],
    value: Any,
    *,
    stage_hint: str | None = None,
) -> McpFailureClassification:
    classification = classify_mcp_failure(value, stage_hint=stage_hint)
    provenance.update(
        {
            "error_class": classification.error_class,
            "error_stage": classification.error_stage,
            "provider_status": classification.provider_status,
            "error_excerpt": classification.error_excerpt,
        }
    )
    if classification.http_status is not None:
        provenance["http_status"] = classification.http_status
    return classification


__all__ = ["McpFailureClassification", "apply_failure_provenance", "classify_mcp_failure"]
