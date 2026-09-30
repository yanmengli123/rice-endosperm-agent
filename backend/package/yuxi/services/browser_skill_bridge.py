"""Client for the host-side BrowserSkill bridge used by local Docker services."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx

from yuxi.services.browser_gateway_service import BrowserGatewayError
from yuxi.utils.logging_config import logger

BROWSER_SKILL_BRIDGE_URL = os.getenv("BROWSER_SKILL_BRIDGE_URL", "").strip().rstrip("/")
BROWSER_SKILL_BRIDGE_SECRET_FILE = os.getenv("BROWSER_SKILL_BRIDGE_SECRET_FILE", "").strip()


def is_browser_skill_bridge_enabled() -> bool:
    return bool(BROWSER_SKILL_BRIDGE_URL and BROWSER_SKILL_BRIDGE_SECRET_FILE)


def _read_secret() -> str:
    if not BROWSER_SKILL_BRIDGE_SECRET_FILE:
        raise BrowserGatewayError(
            "BROWSER_SKILL_DISABLED", "BrowserSkill local bridge is not configured", http_status=503
        )
    try:
        value = Path(BROWSER_SKILL_BRIDGE_SECRET_FILE).read_text(encoding="utf-8").strip()
    except OSError as error:
        raise BrowserGatewayError(
            "BROWSER_SKILL_DISABLED", "BrowserSkill bridge secret is unavailable", http_status=503
        ) from error
    if len(value) < 32:
        raise BrowserGatewayError("BROWSER_SKILL_DISABLED", "BrowserSkill bridge secret is invalid", http_status=503)
    return value


async def browser_skill_health() -> dict[str, Any]:
    return await _request("GET", "/health", timeout=15)


async def dispatch_browser_skill(
    *, uid: str, run_id: str | None, op: str, payload: dict[str, Any], timeout_s: float
) -> dict[str, Any]:
    data = await _request(
        "POST",
        "/dispatch",
        json_body={"uid": uid, "run_id": run_id, "op": op, "payload": payload},
        timeout=timeout_s + 15,
    )
    result = data.get("result")
    return result if isinstance(result, dict) else {}


async def _request(
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    timeout: float,
) -> dict[str, Any]:
    if not is_browser_skill_bridge_enabled():
        raise BrowserGatewayError("BROWSER_SKILL_DISABLED", "BrowserSkill local bridge is not enabled", http_status=503)
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.request(
                method,
                f"{BROWSER_SKILL_BRIDGE_URL}{path}",
                json=json_body,
                headers={"X-Yuxi-Browser-Secret": _read_secret()},
            )
    except httpx.HTTPError as error:
        logger.warning(f"browser skill bridge unreachable: {type(error).__name__}")
        raise BrowserGatewayError(
            "BROWSER_SKILL_OFFLINE",
            "BrowserSkill local bridge is unreachable; verify the bsk daemon and Yuxi bridge are running",
            http_status=503,
        ) from error
    data = _safe_json(response.text)
    if response.status_code != 200 or not isinstance(data, dict) or not data.get("ok"):
        error = data.get("error") if isinstance(data, dict) else {}
        raise BrowserGatewayError(
            str((error or {}).get("code") or "BROWSER_SKILL_ERROR"),
            str((error or {}).get("message") or f"BrowserSkill bridge error (HTTP {response.status_code})"),
            http_status=response.status_code,
        )
    return data


def _safe_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None
