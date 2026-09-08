"""知识产品包：产品能力注册中心 + 四平面类型契约 + Authority Gate。"""

from yuxi.knowledge.products.authority_gate import AuthorityGate, AuthorityGateError, gate_evidence
from yuxi.knowledge.products.contracts import (
    EvidenceEnvelope,
    EvidenceOrigin,
    NavigationChannel,
    WikiNavigationHit,
    to_evidence_envelope,
)
from yuxi.knowledge.products.registry import (
    KnowledgeProductCapabilities,
    KnowledgeProductSpec,
    get_product_spec,
    is_derived_product,
    registry_snapshot,
    require_capability,
)

__all__ = [
    "AuthorityGate",
    "AuthorityGateError",
    "EvidenceEnvelope",
    "EvidenceOrigin",
    "KnowledgeProductCapabilities",
    "KnowledgeProductSpec",
    "NavigationChannel",
    "WikiNavigationHit",
    "gate_evidence",
    "get_product_spec",
    "is_derived_product",
    "registry_snapshot",
    "require_capability",
    "to_evidence_envelope",
]
