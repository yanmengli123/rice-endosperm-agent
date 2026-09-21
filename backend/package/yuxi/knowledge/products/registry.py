"""知识产品注册中心（Product Registry）。

把"知识库"拆成两类产品，从类型层阻止派生知识伪装成权威知识源：

- ``AUTHORITY_SOURCE``：PDF/CSV/图谱/数据库快照。可建立事实权威，
  由现有 ``KnowledgeBaseManager`` 与 ``KnowledgeBase`` 适配器管理。
- ``DERIVED_PRODUCT``：动态 LLM-Wiki 等编译产物。只增强召回与导航，
  永不作为回答证据；由 Wiki 控制面/编译面/发布面管理，
  绝不注册为 ``KnowledgeBase`` 存储适配器（无上传、无 aquery）。

不变量（由类型、注册中心与 AuthorityGate 共同保证）::

    WikiNavigationHit ∉ EvidenceEnvelope
    kb_type=llmwiki  ⇒ capabilities.supports_upload = False
    kb_type=llmwiki  ⇒ 不能进入 EvidenceEnvelope 通道
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ProductCategory = Literal["authority_source", "derived_product", "unregistered"]
TrustClass = Literal["PRIMARY", "SECONDARY", "DERIVED"]
AuthorityClass = Literal["FACT_AUTHORITY", "NAVIGATION_ONLY"]


@dataclass(frozen=True)
class KnowledgeProductCapabilities:
    """一个知识产品类型能做什么、永远不能做什么。"""

    supports_upload: bool = True
    supports_source_binding: bool = False
    supports_dynamic_sync: bool = False
    supports_navigation: bool = False
    supports_raw_evidence: bool = True
    supports_publication: bool = False
    supports_rollback: bool = False


@dataclass(frozen=True)
class KnowledgeProductSpec:
    """一种知识产品类型的静态声明（注册即契约）。"""

    kb_type: str
    category: ProductCategory
    trust_class: TrustClass
    authority_class: AuthorityClass
    capabilities: KnowledgeProductCapabilities = field(default_factory=KnowledgeProductCapabilities)


_LLMWIKI_CAPABILITIES = KnowledgeProductCapabilities(
    supports_upload=False,
    supports_source_binding=True,
    supports_dynamic_sync=True,
    supports_navigation=True,
    supports_raw_evidence=False,
    supports_publication=True,
    supports_rollback=True,
)

# 权威知识源：普通文件型知识库（milvus 等）保持默认能力。
_AUTHORITY_SOURCE = KnowledgeProductSpec(
    kb_type="milvus",
    category="authority_source",
    trust_class="PRIMARY",
    authority_class="FACT_AUTHORITY",
)

DIFY = KnowledgeProductSpec(
    kb_type="dify",
    category="authority_source",
    trust_class="SECONDARY",
    authority_class="FACT_AUTHORITY",
    capabilities=KnowledgeProductCapabilities(supports_upload=False),
)

NOTION = KnowledgeProductSpec(
    kb_type="notion",
    category="authority_source",
    trust_class="SECONDARY",
    authority_class="FACT_AUTHORITY",
    capabilities=KnowledgeProductCapabilities(supports_upload=False),
)

# 派生知识产品：动态 LLM-Wiki。控制面实体，不是 KnowledgeBase 适配器。
LLMWIKI = KnowledgeProductSpec(
    kb_type="llmwiki",
    category="derived_product",
    trust_class="DERIVED",
    authority_class="NAVIGATION_ONLY",
    capabilities=_LLMWIKI_CAPABILITIES,
)

_REGISTRY: dict[str, KnowledgeProductSpec] = {
    _AUTHORITY_SOURCE.kb_type: _AUTHORITY_SOURCE,
    DIFY.kb_type: DIFY,
    NOTION.kb_type: NOTION,
    LLMWIKI.kb_type: LLMWIKI,
}

def get_product_spec(kb_type: str) -> KnowledgeProductSpec:
    """返回产品声明；非空未知类型 fail-closed，不获得证据权限。"""
    normalized = str(kb_type or "").strip().casefold()
    if not normalized:
        # 历史检索行未携带 kb_type；其成员已在冻结 scope 中经过类型校验。
        return _AUTHORITY_SOURCE
    return _REGISTRY.get(
        normalized,
        KnowledgeProductSpec(
            kb_type=normalized,
            category="unregistered",
            trust_class="DERIVED",
            authority_class="NAVIGATION_ONLY",
            capabilities=KnowledgeProductCapabilities(
                supports_upload=False,
                supports_raw_evidence=False,
            ),
        ),
    )


def is_derived_product(kb_type: str) -> bool:
    """该类型是否为派生知识产品（永远不能进入证据通道）。"""
    return get_product_spec(kb_type).category == "derived_product"


def is_evidence_authority(kb_type: str) -> bool:
    """Only explicitly registered authority products may emit answer evidence."""
    spec = get_product_spec(kb_type)
    return spec.category == "authority_source" and spec.capabilities.supports_raw_evidence


def require_capability(kb_type: str, capability: str) -> None:
    """断言某类型具备某能力，否则抛出 TypeError。

    用于把"Wiki 不能上传/不能当证据源"从约定升级为运行时契约。
    """
    spec = get_product_spec(kb_type)
    if not getattr(spec.capabilities, str(capability), False):
        raise TypeError(
            f"kb_type={spec.kb_type!r} (category={spec.category}) does not support capability {capability!r}"
        )


def registry_snapshot() -> list[dict]:
    """给前端/路由使用的注册中心只读快照。"""
    return [
        {
            "kb_type": spec.kb_type,
            "category": spec.category,
            "trust_class": spec.trust_class,
            "authority_class": spec.authority_class,
            "capabilities": {
                "supports_upload": spec.capabilities.supports_upload,
                "supports_source_binding": spec.capabilities.supports_source_binding,
                "supports_dynamic_sync": spec.capabilities.supports_dynamic_sync,
                "supports_navigation": spec.capabilities.supports_navigation,
                "supports_raw_evidence": spec.capabilities.supports_raw_evidence,
                "supports_publication": spec.capabilities.supports_publication,
                "supports_rollback": spec.capabilities.supports_rollback,
            },
        }
        for spec in _REGISTRY.values()
    ]
