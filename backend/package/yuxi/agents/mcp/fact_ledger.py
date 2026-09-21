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

# 数字抽取的统一口径：事实侧（numeric_tokens）与答案侧（门禁数字核验）必须
# 逐字符一致，否则"逐字复制工具结果"不再自洽。边界只认 ASCII 词字符、点号与
# 冒号（"GO:0004373"、"Os06t0101600-01" 一类标识符内嵌数字不构成数值主张）；
# CJK 字符视为合法边界（"相差3个"里的 3 必须核验）；数字体不得以逗号结尾
# （列表分隔的 "0004373," 只取数字部分）；连字符同样视为标识符边界
# （"Os06t0101600-01" 的后缀不构成数值主张，负数字面量按罕见场景放弃核验）；
# 下划线不视为边界——"SOURCE_ONLY_34_TABLES" 里的 34 仍是可核验数值，
# 标识符保护由紧邻字母完成（LOC_Os06g01210 的数字段前恒为字母）；
# 紧贴单位的数字（"3bp"）由单位式单独捕获。
_NUMBER_TOKEN = re.compile(r"(?<![A-Za-z0-9_.:-])\d(?:[\d,]*\d)?(?:\.\d+)?(?![A-Za-z0-9_])")
_NUMBER_WITH_UNIT = re.compile(
    r"(?<![A-Za-z0-9_.:-])(\d(?:[\d,]*\d)?(?:\.\d+)?)(?:bp|kb|mb|gb|aa|nt|kda|da)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
# 非主张性结构片段：日期时间与 JSON 路径。答案侧核验前先掩蔽，避免
# "2026-09-21" 或 "/rows/0/start" 里的数字被当成待核验的数值主张。
_DATETIME_SPAN = re.compile(
    r"\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:[T ]\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?"
    r"|\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
    r"|\d{4}年\s*\d{1,2}月\s*\d{1,2}日"
)
_JSON_PATH_SPAN = re.compile(r"/[A-Za-z~][\w.-]*(?:/~?[\w][\w.-]*)*")


def extract_number_tokens(text: str) -> list[str]:
    tokens = [match.group(0) for match in _NUMBER_TOKEN.finditer(str(text or ""))]
    tokens.extend(match.group(1) for match in _NUMBER_WITH_UNIT.finditer(str(text or "")))
    return tokens


def mask_structural_number_spans(text: str) -> str:
    """Mask datetime and JSON-path spans so their digits are not treated as claims."""
    masked = _DATETIME_SPAN.sub("▁", str(text or ""))
    return _JSON_PATH_SPAN.sub("▁", masked)


@dataclass(frozen=True)
class ExtractedFact:
    fact_id: str
    path: str
    value: str | int | float | bool | None
    value_digest: str

    def model_dict(self) -> dict[str, Any]:
        return {"id": self.fact_id, "path": self.path, "value": self.value}

    def audit_dict(self, *, public_values: bool) -> dict[str, Any]:
        """Persist-side fact record.

        ``public_values=False``（受限数据级）只落摘要：数值与字符串值都不入库，
        因此该轮次的答案不承担数字级核验义务（门禁按"无账本事实"处理）；
        ``public_values=True``（PUBLIC 数据级）记录数值与字符串值，既支撑终态
        数字核验，也供有界修复与降级渲染使用。
        """
        payload: dict[str, Any] = {
            "id": self.fact_id,
            "path": self.path,
            "value_digest": self.value_digest,
        }
        if public_values and isinstance(self.value, (int, float)) and not isinstance(self.value, bool):
            payload["numeric_value"] = self.value
        elif public_values and isinstance(self.value, str):
            payload["string_value"] = self.value
            numeric_tokens = [token.replace(",", "") for token in extract_number_tokens(self.value)]
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
    "extract_number_tokens",
    "mask_structural_number_spans",
]
