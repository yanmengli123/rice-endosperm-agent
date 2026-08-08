from __future__ import annotations

import json
from http import HTTPStatus
from typing import Any


def format_provider_http_error(
    operation: str,
    *,
    status_code: int,
    reason: str | None = None,
    response_text: str = "",
    trace_id: str | None = None,
) -> str:
    """Return a safe, actionable provider error without leaking credentials."""
    provider_code: Any = None
    provider_message = ""

    if response_text:
        try:
            body = json.loads(response_text)
        except (TypeError, ValueError):
            body = None

        if isinstance(body, dict):
            error = body.get("error")
            provider_code = body.get("code")
            provider_message = str(body.get("message") or body.get("detail") or "").strip()
            if isinstance(error, dict):
                provider_code = provider_code or error.get("code")
                provider_message = provider_message or str(error.get("message") or "").strip()
            elif not provider_message and error:
                provider_message = str(error).strip()
        else:
            provider_message = response_text.strip()[:500]

    status = f"HTTP {status_code}"
    if not reason:
        try:
            reason = HTTPStatus(status_code).phrase
        except ValueError:
            reason = None
    if reason:
        status += f" {reason}"

    details = [f"{operation}失败：{status}"]
    if provider_message:
        details.append(provider_message)
    if provider_code not in (None, ""):
        details.append(f"code={provider_code}")
    if trace_id:
        details.append(f"trace_id={trace_id}")
    return "；".join(details)
