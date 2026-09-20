"""Source Contract 注册中心：代码内不可变注册项 + fail-closed 解析。

与 Product Registry 的关键差异：这里**没有**默认回落。未知 contract_key /
contract_version 一律抛 UnknownSourceContractError——旧产品注册中心
"未知类型回落成权威知识源"的兼容策略只适用于 kb_type，不适用于权威契约。
"""

from __future__ import annotations

from yuxi.knowledge.source_contracts.definitions import (
    CSV_QA,
    CSV_RECORD,
    GENERIC_DOCUMENT,
    LEGACY_GENERIC,
    LEGACY_MIXED,
    MANAGED_GRAPH,
    MANAGED_GRAPH_V1_1,
    PDF_EVIDENCE,
)
from yuxi.knowledge.source_contracts.specs import SourceContractSpec, spec_to_api_dict


class SourceContractError(Exception):
    """契约违例基类；路由层负责映射为 HTTP 状态码。"""

    http_status = 422
    error_code = "SOURCE_CONTRACT_VIOLATION"


class UnknownSourceContractError(SourceContractError):
    """未注册的契约 key/version——fail closed，绝不回落。"""

    http_status = 422
    error_code = "SOURCE_CONTRACT_VIOLATION"


_REGISTRY: dict[tuple[str, str], SourceContractSpec] = {
    (spec.contract_key, spec.version): spec
    for spec in (
        PDF_EVIDENCE,
        CSV_RECORD,
        CSV_QA,
        MANAGED_GRAPH,
        MANAGED_GRAPH_V1_1,
        GENERIC_DOCUMENT,
        LEGACY_GENERIC,
        LEGACY_MIXED,
    )
}


def resolve_contract(contract_key: str | None, contract_version: str | None = None) -> SourceContractSpec:
    """解析契约；version 缺省取该 key 的最新注册版本。未知即抛错。"""
    key = str(contract_key or "").strip()
    if not key:
        raise UnknownSourceContractError("source contract key is required")
    if contract_version is None:
        versions = [version for (reg_key, version) in _REGISTRY if reg_key == key]
        if not versions:
            raise UnknownSourceContractError(f"unknown source contract: {key}")
        # legacy 契约固定 "0"；语义化版本按字符串排序取最大
        contract_version = max(versions, key=lambda v: tuple(int(p) if p.isdigit() else p for p in v.split(".")))
    version = str(contract_version).strip()
    spec = _REGISTRY.get((key, version))
    if spec is None:
        known = sorted({f"{k}@{v}" for (k, v) in _REGISTRY})
        raise UnknownSourceContractError(f"unknown source contract: {key}@{version} (known: {', '.join(known)})")
    return spec


def contract_registry_snapshot(include_hidden: bool = False) -> list[dict]:
    """给前端/路由使用的注册中心只读快照。"""
    return [spec_to_api_dict(spec) for spec in _REGISTRY.values() if include_hidden or not spec.hidden]


def registered_contracts() -> tuple[SourceContractSpec, ...]:
    return tuple(_REGISTRY.values())
