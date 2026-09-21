"""四平面类型契约：EvidenceEnvelope 与 WikiNavigationHit 的硬隔离。

不变量 ``WikiNavigationHit ∉ EvidenceEnvelope`` 在这里由类型实现：

- ``EvidenceEnvelope``：唯一允许进入答案生成上下文的证据载体。
  由 AuthorityGate 从 Authority Plane 的原始检索结果构造。
- ``WikiNavigationHit``：Navigation Plane 的输出。只包含实体、别名、
  扩展词、建议关系与页面路径，**没有可被当作证据引用的文本字段**，
  且类型上不可转换为 ``EvidenceEnvelope``（``to_evidence_envelope``
  拒绝该输入并抛出 AuthorityGateError）。

数据结构刻意使用 frozen dataclass：无 ``text/content/quote`` 类字段、
无 ``__dict__`` 写入，防止下游代码把导航结果"顺手"塞进证据列表。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

EvidenceOrigin = Literal["DOCUMENT", "GRAPH", "STRUCTURED", "CSV_ROW", "PDF_ANCHOR", "CANONICAL_CLAIM"]
NavigationChannel = Literal["ENTITY", "ALIAS", "EXPANSION", "RELATION_SUGGESTION", "PAGE_PATH"]


@dataclass(frozen=True, slots=True)
class EvidenceEnvelope:
    """答案层唯一合法的证据单位（Authority Plane 产物）。"""

    evidence_id: str
    origin: EvidenceOrigin
    kb_id: str
    file_id: str = ""
    chunk_id: str = ""
    content: str = ""
    score: float = 0.0
    claim_eligible: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "origin": self.origin,
            "kb_id": self.kb_id,
            "file_id": self.file_id,
            "chunk_id": self.chunk_id,
            "content": self.content,
            "score": self.score,
            "claim_eligible": self.claim_eligible,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class WikiNavigationHit:
    """Navigation Plane 输出：仅用于扩展召回与路径建议，不是证据。"""

    wiki_id: str
    publication_id: str
    channel: NavigationChannel
    entity_id: str = ""
    entity_name: str = ""
    aliases: tuple[str, ...] = ()
    expansion_terms: tuple[str, ...] = ()
    suggested_relations: tuple[dict[str, Any], ...] = ()
    page_path: tuple[str, ...] = ()
    score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "wiki_id": self.wiki_id,
            "publication_id": self.publication_id,
            "channel": self.channel,
            "entity_id": self.entity_id,
            "entity_name": self.entity_name,
            "aliases": list(self.aliases),
            "expansion_terms": list(self.expansion_terms),
            "suggested_relations": [dict(item) for item in self.suggested_relations],
            "page_path": list(self.page_path),
            "score": self.score,
        }


def to_evidence_envelope(row: dict[str, Any]) -> EvidenceEnvelope:
    """把 Authority Plane 的原始检索行归一化为 EvidenceEnvelope。

    派生产品（llmwiki）的行永远不能通过这里—— 即使有人把 Wiki 页面
    正文伪装成检索行，``is_derived_product`` 也会在类型边界处拒绝。
    """
    from yuxi.knowledge.products.registry import get_product_spec

    kb_type = str(row.get("kb_type") or "")
    spec = get_product_spec(kb_type)
    if spec.category != "authority_source" or not spec.capabilities.supports_raw_evidence:
        from yuxi.knowledge.products.authority_gate import AuthorityGateError

        if spec.category == "derived_product":
            raise AuthorityGateError(f"derived product kb_type={spec.kb_type!r} cannot enter the evidence channel")
        raise AuthorityGateError(f"kb_type={spec.kb_type!r} is not registered as an evidence authority")
    evidence_id = str(row.get("evidence_id") or "").strip()
    if not evidence_id:
        raise ValueError("evidence row requires evidence_id")
    content = str(row.get("content") or "")
    if not content.strip():
        raise ValueError("evidence row requires non-empty content")
    return EvidenceEnvelope(
        evidence_id=evidence_id,
        origin=row.get("source_type") or "DOCUMENT",
        kb_id=str(row.get("kb_id") or ""),
        file_id=str(row.get("file_id") or ""),
        chunk_id=str(row.get("chunk_id") or ""),
        content=content,
        score=float(row.get("raw_score") or row.get("score") or 0.0),
        claim_eligible=bool(row.get("claim_eligible")),
        metadata=dict(row.get("metadata") or {}),
    )
