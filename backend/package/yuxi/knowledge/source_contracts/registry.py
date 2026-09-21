"""Source Contract 注册中心：代码内不可变注册项 + fail-closed 解析。

与 Product Registry 的关键差异：这里**没有**默认回落。未知 contract_key /
contract_version 一律抛 UnknownSourceContractError——旧产品注册中心
"未知类型回落成权威知识源"的兼容策略只适用于 kb_type，不适用于权威契约。
"""

from __future__ import annotations

from yuxi.knowledge.source_contracts.definitions import (
    ALL_COMMANDS,
    CSV_QA,
    CSV_QA_V1_1,
    CSV_QA_V1_2,
    CSV_RECORD,
    CSV_RECORD_V1_1,
    CSV_RECORD_V1_2,
    GENERIC_DOCUMENT,
    GLOSSARY,
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
        CSV_RECORD_V1_1,
        CSV_RECORD_V1_2,
        CSV_QA,
        CSV_QA_V1_1,
        CSV_QA_V1_2,
        MANAGED_GRAPH,
        MANAGED_GRAPH_V1_1,
        GENERIC_DOCUMENT,
        GLOSSARY,
        LEGACY_GENERIC,
        LEGACY_MIXED,
    )
}


def _version_sort_key(version: str) -> tuple:
    return tuple(int(p) if p.isdigit() else p for p in version.split("."))


def latest_contract_version(contract_key: str) -> str | None:
    """该 key 的最新注册版本（语义化版本取最大）；未知 key 返回 None。"""
    versions = [version for (reg_key, version) in _REGISTRY if reg_key == contract_key]
    return max(versions, key=_version_sort_key) if versions else None


def _validate_registry_invariants() -> None:
    """注册中心启动期不变量校验（fail-fast， import 即执行）。

    企业契约治理底线：
    1. allowed ∩ forbidden = ∅（自相矛盾的契约会让 digest、展示与执行语义三脸不一致）；
    2. allowed ∪ forbidden ⊆ ALL_COMMANDS（命令闭集，防拼写漂移）；
    3. 同 key 多版本时各版本的 latest 可判定。
    违反即抛 RuntimeError 拒绝启动——契约是 digest 与一切门禁的基准，不能带病上线。
    """
    violations: list[str] = []
    for (key, version), spec in sorted(_REGISTRY.items()):
        overlap = sorted(set(spec.allowed_commands) & set(spec.forbidden_commands))
        if overlap:
            violations.append(f"{key}@{version}: allowed∩forbidden={overlap}")
        unknown = sorted((set(spec.allowed_commands) | set(spec.forbidden_commands)) - set(ALL_COMMANDS))
        if unknown:
            violations.append(f"{key}@{version}: 未注册命令={unknown}")
    if violations:
        raise RuntimeError("Source contract registry invariant violations: " + "; ".join(violations))


_validate_registry_invariants()


def resolve_contract(contract_key: str | None, contract_version: str | None = None) -> SourceContractSpec:
    """解析契约；version 缺省取该 key 的最新注册版本。未知即抛错。"""
    key = str(contract_key or "").strip()
    if not key:
        raise UnknownSourceContractError("source contract key is required")
    if contract_version is None:
        contract_version = latest_contract_version(key)
        if contract_version is None:
            raise UnknownSourceContractError(f"unknown source contract: {key}")
    version = str(contract_version).strip()
    spec = _REGISTRY.get((key, version))
    if spec is None:
        known = sorted({f"{k}@{v}" for (k, v) in _REGISTRY})
        raise UnknownSourceContractError(f"unknown source contract: {key}@{version} (known: {', '.join(known)})")
    return spec


def contract_registry_snapshot(include_hidden: bool = False) -> list[dict]:
    """给前端/路由使用的注册中心只读快照。

    每项附 latest_version（该 key 的最新版本）：前端按 key 选择契约时必须锚定
    latest_version，禁止依赖注册顺序 find() 取首条（曾导致 managed_graph 新库
    被冻结到 1.0.0，出现 1.1 才有的导图入口可见但被契约拒绝的断层）。
    """
    snapshot = []
    for spec in _REGISTRY.values():
        if not include_hidden and spec.hidden:
            continue
        item = dict(spec_to_api_dict(spec))
        item["latest_version"] = latest_contract_version(spec.contract_key)
        snapshot.append(item)
    return snapshot


def registered_contracts() -> tuple[SourceContractSpec, ...]:
    return tuple(_REGISTRY.values())
