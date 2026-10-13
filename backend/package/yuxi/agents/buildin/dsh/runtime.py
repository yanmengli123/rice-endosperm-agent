"""Immutable runtime contract. A flag change affects new runs only."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

DSH_VERSION = "0.2.1-alpha.2"
ADAPTER_VERSION = 2
PROFILE_NAME = "yuxi-qa"


def positive_setting(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def profile_fingerprint() -> str:
    root = Path(__file__).parent
    digest = hashlib.sha256()
    for name in ("yuxi-qa.cordis.patch.yml", "mcp_bridge.py"):
        digest.update(name.encode())
        digest.update((root / name).read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()


def runtime_snapshot() -> dict:
    return {
        "name": "dsh",
        "adapter_version": ADAPTER_VERSION,
        "dsh_version": DSH_VERSION,
        "profile": PROFILE_NAME,
        "profile_sha256": profile_fingerprint(),
        "token_budget": positive_setting("YUXI_DSH_RUN_TOKEN_BUDGET", 32768),
        "max_output_tokens": positive_setting("YUXI_DSH_MAX_OUTPUT_TOKENS", 8192),
        "context_window": positive_setting("YUXI_DSH_MODEL_CONTEXT_WINDOW", 131072),
    }


def validate_snapshot(snapshot: dict) -> None:
    current = runtime_snapshot()
    for key in ("name", "adapter_version", "dsh_version", "profile", "profile_sha256"):
        if snapshot.get(key) != current[key]:
            raise ValueError(f"Frozen DSH runtime is unavailable ({key}); create a new run")
    for key in ("token_budget", "max_output_tokens", "context_window"):
        if type(snapshot.get(key)) is not int or snapshot[key] <= 0:
            raise ValueError(f"Invalid frozen DSH setting: {key}")
