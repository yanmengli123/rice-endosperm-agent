"""Source Contract 的值类型、规范化序列化与 digest 计算。

一个 Source Contract 回答"这个知识库接受什么数据、如何处理、权威边界在哪"，
与 Product Registry（权威源 vs 派生产品）和投影适配器（Milvus/Neo4j/Dify）
是三个正交维度。契约定义只存在于后端代码中，前端通过只读 API 读取，
不允许拼装；digest 对契约的语义字段做规范化哈希，创建知识库时随
snapshot 一起冻结，用于检测"代码里的契约"与"库上冻结的契约"漂移。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field


@dataclass(frozen=True)
class SourceContractDisplay:
    """前端展示信息；不参与权威判定，只用于选型与说明。"""

    label: str
    card_description: str = ""
    operator_description: str = ""
    # primary = 主界面三张卡；advanced = 高级折叠区；hidden = 兼容契约，不展示
    entry_mode: str = "primary"


@dataclass(frozen=True)
class SourceContractMediaRule:
    """一类输入的媒体约束；role 区分文档/节点 CSV/关系 CSV 等槽位。"""

    role: str
    extensions: tuple[str, ...] = ()
    content_types: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceContractSpec:
    """一种知识源契约的不可变声明。"""

    contract_key: str
    version: str
    product_category: str
    display: SourceContractDisplay
    allowed_commands: tuple[str, ...] = ()
    forbidden_commands: tuple[str, ...] = ()
    accepted_media: tuple[SourceContractMediaRule, ...] = ()
    authority_policy: dict = field(default_factory=dict)
    required_provenance: tuple[str, ...] = ()
    base_capabilities: dict = field(default_factory=dict)
    # Step3「数据处理、权威与检索策略」的只读展示文本
    processing_policy: dict = field(default_factory=dict)
    # 旧契约的升级提示；新契约为 None
    upgrade_path: str | None = None

    @property
    def hidden(self) -> bool:
        return self.display.entry_mode == "hidden"

    @property
    def contract_ref(self) -> str:
        return f"{self.contract_key}@{self.version}"


def canonical_contract_payload(spec: SourceContractSpec) -> dict:
    """契约的规范化可序列化形式；digest 的输入，字段增减都视为契约变更。"""
    return {
        "contract_key": spec.contract_key,
        "version": spec.version,
        "product_category": spec.product_category,
        "allowed_commands": sorted(spec.allowed_commands),
        "forbidden_commands": sorted(spec.forbidden_commands),
        "accepted_media": [
            {
                "role": rule.role,
                "extensions": sorted(rule.extensions),
                "content_types": sorted(rule.content_types),
            }
            for rule in spec.accepted_media
        ],
        "authority_policy": spec.authority_policy,
        "required_provenance": sorted(spec.required_provenance),
        "base_capabilities": dict(sorted(spec.base_capabilities.items())),
    }


def contract_digest(spec: SourceContractSpec) -> str:
    payload = json.dumps(
        canonical_contract_payload(spec),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def spec_to_api_dict(spec: SourceContractSpec) -> dict:
    """只读 API / contract_snapshot 的输出形式（含展示信息与 digest）。"""
    return {
        "contract_key": spec.contract_key,
        "version": spec.version,
        "contract_ref": spec.contract_ref,
        "product_category": spec.product_category,
        "digest": contract_digest(spec),
        "display": {
            "label": spec.display.label,
            "card_description": spec.display.card_description,
            "operator_description": spec.display.operator_description,
            "entry_mode": spec.display.entry_mode,
        },
        "allowed_commands": sorted(spec.allowed_commands),
        "forbidden_commands": sorted(spec.forbidden_commands),
        "accepted_media": [
            {
                "role": rule.role,
                "extensions": sorted(rule.extensions),
                "content_types": sorted(rule.content_types),
            }
            for rule in spec.accepted_media
        ],
        "authority_policy": dict(spec.authority_policy),
        "required_provenance": sorted(spec.required_provenance),
        "base_capabilities": dict(sorted(spec.base_capabilities.items())),
        "processing_policy": dict(spec.processing_policy),
        "upgrade_path": spec.upgrade_path,
    }
