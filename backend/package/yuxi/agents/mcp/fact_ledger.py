"""Bounded, deterministic fact references for trusted MCP tool results."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any

FACT_LEDGER_SCHEMA_VERSION = "mcp-fact-ledger.v1"
MAX_FACTS = 256
MAX_STRING_LENGTH = 240
_NUMBER_TOKEN = re.compile(r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?(?![\w.])")


@dataclass(frozen=True)
class ExtractedFact:
    fact_id: str
    path: str
    value: str | int | float | bool | None
    value_digest: str

    def model_dict(self) -> dict[str, Any]:
        return {"id": self.fact_id, "path": self.path, "value": self.value}

    def audit_dict(self, *, public_values: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.fact_id,
            "path": self.path,
            "value_digest": self.value_digest,
        }
        if public_values and isinstance(self.value, (int, float)) and not isinstance(self.value, bool):
            payload["numeric_value"] = self.value
        elif public_values and isinstance(self.value, str):
            numeric_tokens = [token.replace(",", "") for token in _NUMBER_TOKEN.findall(self.value)]
            if numeric_tokens:
                payload["numeric_tokens"] = numeric_tokens[:16]
        return payload


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))


def _fact(path: str, value: Any) -> ExtractedFact:
    canonical = _canonical(value)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    fact_id = "f_" + hashlib.sha256(f"{path}\0{canonical}".encode("utf-8")).hexdigest()[:16]
    return ExtractedFact(fact_id=fact_id, path=path, value=value, value_digest=f"sha256:{digest}")


def _parse_text_json(text: str) -> Any:
    candidate = str(text or "").strip()
    if not candidate or candidate[0] not in "[{":
        return None
    try:
        return json.loads(candidate)
    except (json.JSONDecodeError, TypeError):
        return None


def _result_payload(result: Any) -> Any:
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, (dict, list)):
        return structured
    parsed = _parse_text_json(getattr(result, "text", ""))
    if isinstance(parsed, (dict, list)):
        return parsed
    blocks = getattr(result, "content_blocks", None)
    return blocks if isinstance(blocks, (dict, list)) else None


def extract_facts(result: Any, *, maximum: int = MAX_FACTS) -> tuple[list[ExtractedFact], bool]:
    """Extract scalar JSON leaves without ever serializing large blobs."""
    payload = _result_payload(result)
    facts: list[ExtractedFact] = []
    truncated = False

    def walk(value: Any, path: str) -> None:
        nonlocal truncated
        if len(facts) >= maximum:
            truncated = True
            return
        if isinstance(value, dict):
            for key in sorted(value, key=lambda item: str(item)):
                walk(value[key], f"{path}/{str(key).replace('~', '~0').replace('/', '~1')}")
                if truncated:
                    return
            return
        if isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}/{index}")
                if truncated:
                    return
            return
        if value is None or isinstance(value, bool):
            facts.append(_fact(path or "/", value))
            return
        if isinstance(value, int):
            facts.append(_fact(path or "/", value))
            return
        if isinstance(value, float):
            if math.isfinite(value):
                facts.append(_fact(path or "/", value))
            return
        if isinstance(value, str) and len(value) <= MAX_STRING_LENGTH:
            facts.append(_fact(path or "/", value))

    if payload is not None:
        walk(payload, "")
    return facts, truncated


def build_audit_manifest(facts: list[ExtractedFact], *, truncated: bool, public_values: bool) -> dict[str, Any]:
    return {
        "schema_version": FACT_LEDGER_SCHEMA_VERSION,
        "fact_count": len(facts),
        "truncated": truncated,
        "facts": [fact.audit_dict(public_values=public_values) for fact in facts],
    }


def append_model_ledger(text: str, *, audit_id: int, facts: list[ExtractedFact], truncated: bool) -> str:
    """Append the compact citation catalog visible to the model.

    The block is machine-oriented and intentionally explicit: the answer must
    cite ``[MCP-F:<audit-id>:<fact-id>]``. It is part of the tool observation,
    not the upstream result digest.
    """
    ledger = {
        "schema_version": FACT_LEDGER_SCHEMA_VERSION,
        "audit_id": audit_id,
        "truncated": truncated,
        "citation_format": f"[MCP-F:{audit_id}:<fact-id>]",
        "facts": [fact.model_dict() for fact in facts],
    }
    return f"{str(text or '').rstrip()}\n\n<YUXI_MCP_FACT_LEDGER>{_canonical(ledger)}</YUXI_MCP_FACT_LEDGER>"


__all__ = [
    "FACT_LEDGER_SCHEMA_VERSION",
    "ExtractedFact",
    "append_model_ledger",
    "build_audit_manifest",
    "extract_facts",
]
