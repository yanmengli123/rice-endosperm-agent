from __future__ import annotations

import pytest

from yuxi.agents.mcp.error_semantics import classify_mcp_failure
from yuxi.agents.mcp.execution import argument_shape


@pytest.mark.parametrize(
    ("message", "error_class", "provider_status", "stage"),
    [
        (
            "ValidationError: taxon Field required [type=missing]",
            "MCP_TOOL_ARGS_INVALID",
            "ARGUMENT_INVALID",
            "validation",
        ),
        ("request timed out after 30 seconds", "MCP_TIMEOUT", "UNAVAILABLE", "transport"),
        ("HTTP status code 429 Too Many Requests", "MCP_RATE_LIMITED", "UNAVAILABLE", "provider"),
        ("HTTP status 503", "MCP_PROVIDER_5XX", "UNAVAILABLE", "provider"),
        ("connection refused", "MCP_TRANSPORT_FAILED", "UNAVAILABLE", "transport"),
        ("TypeError: fetch failed", "MCP_TRANSPORT_FAILED", "UNAVAILABLE", "transport"),
        ("Tunnel connection failed: 502 Bad Gateway", "MCP_TRANSPORT_FAILED", "UNAVAILABLE", "transport"),
    ],
)
def test_failure_classifier_is_closed_and_structured(message, error_class, provider_status, stage):
    result = classify_mcp_failure(message)
    assert result.error_class == error_class
    assert result.provider_status == provider_status
    assert result.error_stage == stage
    assert result.error_excerpt


def test_argument_shape_never_contains_values():
    shape = argument_shape(
        {
            "identifiers": ["Wx"],
            "taxon": "Oryza sativa",
            "page_size": 20,
            "credentials": {"api_key": "secret-value"},
        }
    )
    rendered = repr(shape)
    assert "Wx" not in rendered
    assert "Oryza sativa" not in rendered
    assert "secret-value" not in rendered
    assert shape["fields"]["taxon"] == {"type": "string"}
    assert shape["fields"]["identifiers"] == {"type": "array", "item_types": ["string"]}
