"""图谱「点开即见原文」证据服务（不变式 I1/I2 的读侧）。

- I1（边）：每条 RELATION 边 ⇒ ≥1 条 triple_mention（逐字引文 ⊆ chunk 原文）；
- I2（节点）：每个实体 ⇒ ≥1 条 entity_mention（逐字主句 ⊆ chunk 原文）。

数据源是 PostgreSQL mention 表（Neo4j 边属性只是单条预览），因此多文献佐证的边会
返回全部原文。每条引文在显示时重新校验 ``quote ⊆ chunk.content``（OK / DEGRADED / MISSING），
防止 chunk 重建后的静默漂移；信任分级（CANDIDATE / VERIFIED_SINGLE / VERIFIED_CORROBORATED）
只是徽标，不隐藏任何已过 G2 的边——可显示性与可信性是两个正交维度。
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from yuxi.repositories.knowledge_graph_repository import KnowledgeGraphRepository

EVIDENCE_PROTOCOL_VERSION = "graph-evidence-v1"
CONTEXT_RADIUS = 150
TRUST_CANDIDATE = "CANDIDATE"
TRUST_VERIFIED_SINGLE = "VERIFIED_SINGLE"
TRUST_VERIFIED_CORROBORATED = "VERIFIED_CORROBORATED"
VERIFICATION_OK = "OK"
VERIFICATION_DEGRADED = "DEGRADED"
VERIFICATION_MISSING = "MISSING"

_PROVENANCE_LINE = re.compile(r"^\s*【([^】]{1,16})】\s*(.*)$")
_TRIPLE_MENTION_FIELDS = (
    "extractor_type",
    "confidence",
    "hedge",
    "context",
    "trigger_verified",
    "trigger_term",
    "verifier_confirmed",
)


class GraphEvidenceService:
    def __init__(
        self,
        graph_repo: KnowledgeGraphRepository | None = None,
        review_repo: Any = None,
        graph_service: Any = None,
    ):
        self.graph_repo = graph_repo or KnowledgeGraphRepository()
        self.review_repo = review_repo
        self.graph_service = graph_service

    async def triple_evidence(self, kb_id: str, triple_id: str) -> dict[str, Any]:
        source = await self.graph_repo.get_triple_evidence_source(kb_id, triple_id)
        if source is None:
            raise ValueError(f"三元组 {triple_id} 不存在")
        evidence = build_triple_evidence(source)
        evidence["decision"] = await self._decision(kb_id, "TRIPLE", triple_id)
        return evidence

    async def entity_evidence(self, kb_id: str, entity_id: str) -> dict[str, Any]:
        source = await self.graph_repo.get_entity_evidence_source(kb_id, entity_id)
        if source is None:
            raise ValueError(f"实体 {entity_id} 不存在")
        evidence = build_entity_evidence(source)
        evidence["decision"] = await self._decision(kb_id, "ENTITY", entity_id)
        return evidence

    async def integrity(self, kb_id: str, *, limit: int = 5000) -> dict[str, Any]:
        source = await self.graph_repo.list_integrity_source(kb_id, limit=limit)
        report = build_integrity_report(source)
        if self.review_repo is not None:
            review_counts = await self.review_repo.integrity_counts(kb_id)
            rejected_triple_ids = review_counts.pop("rejected_triple_ids")
            if self.graph_service is not None and rejected_triple_ids:
                projected = await asyncio.to_thread(
                    self.graph_service.count_projected_edges, kb_id, rejected_triple_ids
                )
                review_counts["I4_rejected_edges_projected"] = projected
            if self.graph_service is not None:
                review_counts["I7_edges_missing_status"] = await asyncio.to_thread(
                    self.graph_service.count_edges_missing_status, kb_id
                )
            for key, value in review_counts.items():
                report["counts"][key] = value
                if key.startswith("I"):
                    report["violations"][key] = value
            if any(report["violations"].values()):
                report["status"] = "VIOLATION"
        return report

    async def _decision(self, kb_id: str, kind: str, target_id: str) -> dict[str, Any] | None:
        if self.review_repo is None:
            return None
        decision = await self.review_repo.get_decision(kb_id, kind, target_id)
        if decision is None:
            return None
        return {key: decision.get(key) for key in ("action", "reason", "actor_uid", "version", "updated_at")}


def build_triple_evidence(source: dict[str, Any]) -> dict[str, Any]:
    triple = source["triple"]
    mentions = [_mention_view(mention) for mention in source.get("mentions") or []]
    mentions.sort(key=_mention_rank)
    literature_count = int(triple.get("literature_count") or 0) or len({m["file_id"] for m in mentions})
    return {
        "protocol_version": EVIDENCE_PROTOCOL_VERSION,
        "triple_id": triple["triple_id"],
        "relation_type": triple["relation_type"],
        "source": _endpoint_view(source.get("source")),
        "target": _endpoint_view(source.get("target")),
        "support_count": int(triple.get("support_count") or 0) or len(mentions),
        "literature_count": literature_count,
        "trust_tier": trust_tier(mentions, literature_count),
        "review_status": triple.get("review_status"),
        "review_version": triple.get("review_version"),
        "conflict_status": triple.get("conflict_status"),
        "risk_score": triple.get("risk_score"),
        "mentions": mentions,
        "verification_summary": _verification_summary(mentions),
    }


def build_entity_evidence(source: dict[str, Any]) -> dict[str, Any]:
    entity = source["entity"]
    mentions = [_mention_view(mention) for mention in source.get("mentions") or []]
    definition = derive_definition(mentions, source.get("relation_mentions") or [])
    return {
        "protocol_version": EVIDENCE_PROTOCOL_VERSION,
        "entity_id": entity["entity_id"],
        "name": entity["name"],
        "label": entity["label"],
        "canonical_identity": entity.get("canonical_identity"),
        "review_status": entity.get("review_status"),
        "review_version": entity.get("review_version"),
        "aliases": [alias["alias"] for alias in source.get("aliases") or []],
        "definition": definition,
        "mentions": mentions,
        "mention_count": len(mentions),
        "file_count": len({m["file_id"] for m in mentions}),
        "triple_count": int(source.get("triple_count") or 0),
        "verification_summary": _verification_summary(mentions),
    }


def build_integrity_report(source: dict[str, Any]) -> dict[str, Any]:
    """I1/I2 完整性报表：结构性违规计数 + 引文逐条重验；任一非零即 VIOLATION。"""
    counts = dict(source.get("counts") or {})
    degraded = {"triple": 0, "entity": 0}
    checked = {"triple": 0, "entity": 0}
    for item in source.get("quotes") or []:
        kind = item["kind"]
        checked[kind] += 1
        if verify_quote(item.get("quote") or "", item.get("content") or "")[0] != VERIFICATION_OK:
            degraded[kind] += 1
    violations = {
        "I1_triples_without_mention": counts.get("triples_without_mention", 0),
        "I1_triple_mentions_without_text": counts.get("triple_mentions_without_text", 0),
        "I1_triple_quotes_degraded": degraded["triple"],
        "I2_entities_without_mention": counts.get("entities_without_mention", 0),
        "I2_entity_mentions_without_text": counts.get("entity_mentions_without_text", 0),
        "I2_entity_quotes_degraded": degraded["entity"],
    }
    limit = int(source.get("limit") or 0)
    return {
        "protocol_version": EVIDENCE_PROTOCOL_VERSION,
        "status": "OK" if not any(violations.values()) else "VIOLATION",
        "counts": counts,
        "checked_quotes": checked,
        "violations": violations,
        "limit_reached": bool(limit) and (checked["triple"] >= limit or checked["entity"] >= limit),
    }


def verify_quote(quote: str, content: str) -> tuple[str, int | None]:
    """显示时校验：逐字命中 → OK（带偏移）；仅换行/空白差异 → OK（无偏移）；否则 DEGRADED。"""
    if not quote:
        return VERIFICATION_MISSING, None
    position = content.find(quote)
    if position >= 0:
        return VERIFICATION_OK, position
    if _collapse_whitespace(quote) in _collapse_whitespace(content):
        return VERIFICATION_OK, None
    return VERIFICATION_DEGRADED, None


def parse_chunk_provenance(content: str, source_provenance: dict[str, Any] | None) -> dict[str, str | None]:
    """从 chunk 前缀标记行（【章节】【文献】【标识符】【页码】）与 source_provenance 解析显示用来源信息。"""
    markers: dict[str, str] = {}
    for line in (content or "").splitlines():
        match = _PROVENANCE_LINE.match(line)
        if match:
            markers.setdefault(match.group(1), match.group(2).strip())
    provenance = source_provenance if isinstance(source_provenance, dict) else {}
    section_path = provenance.get("section_path") or []
    page_numbers = provenance.get("page_numbers") or []
    return {
        "section": markers.get("章节") or (" > ".join(str(part) for part in section_path) or None),
        "literature": markers.get("文献") or None,
        "identifiers": markers.get("标识符") or None,
        "page": markers.get("页码") or (", ".join(str(page) for page in page_numbers) or None),
    }


def build_context_snippet(content: str, quote: str, offset: int | None, *, radius: int = CONTEXT_RADIUS) -> str | None:
    """引文前后各 radius 字符的上下文片段（去掉前缀标记行），供面板高亮显示。"""
    if not quote or not content:
        return None
    if offset is None:
        offset = content.find(quote)
        if offset < 0:
            return None
    start = max(0, offset - radius)
    end = min(len(content), offset + len(quote) + radius)
    lines = [line for line in content[start:end].splitlines() if not _PROVENANCE_LINE.match(line)]
    snippet = "\n".join(lines).strip()
    if start > 0:
        snippet = "…" + snippet
    if end < len(content):
        snippet = snippet + "…"
    return snippet


def derive_definition(mentions: list[dict[str, Any]], relation_mentions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """定义语句派生（不存储、无悬空）：优先「作为关系端点且置信度最高」的句子，平局取文档序最前。"""
    score_by_chunk: dict[str, float] = {}
    for relation in relation_mentions:
        score = float(relation.get("confidence") or 0.0) + (0.5 if relation.get("trigger_verified") else 0.0)
        score_by_chunk[relation["chunk_id"]] = max(score_by_chunk.get(relation["chunk_id"], 0.0), score)
    best = None
    best_score = -1.0
    for mention in mentions:
        if mention["verification"] != VERIFICATION_OK:
            continue
        score = score_by_chunk.get(mention["chunk_id"], 0.0)
        if score > best_score:
            best, best_score = mention, score
    return best


def trust_tier(mentions: list[dict[str, Any]], literature_count: int) -> str:
    verified = any(m.get("trigger_verified") or m.get("verifier_confirmed") for m in mentions)
    if verified and literature_count >= 2:
        return TRUST_VERIFIED_CORROBORATED
    if verified:
        return TRUST_VERIFIED_SINGLE
    return TRUST_CANDIDATE


def _mention_view(mention: dict[str, Any]) -> dict[str, Any]:
    content = mention.get("chunk_content") or ""
    quote = mention.get("quote") or ""
    status, found_offset = verify_quote(quote, content)
    offset = mention.get("quote_start_char") if mention.get("quote_start_char") is not None else found_offset
    provenance = parse_chunk_provenance(content, mention.get("source_provenance"))
    view = {
        "quote": quote or None,
        "verification": status,
        "chunk_id": mention.get("chunk_id"),
        "file_id": mention.get("file_id"),
        "filename": mention.get("filename"),
        "chunk_index": mention.get("chunk_index"),
        "section": provenance["section"],
        "literature": provenance["literature"],
        "identifiers": provenance["identifiers"],
        "page": provenance["page"],
        "quote_start_char": offset,
        "pinned_by": mention.get("pinned_by"),
        "context": build_context_snippet(content, quote, offset) if quote else None,
        "chunk_content": content,
    }
    for field in _TRIPLE_MENTION_FIELDS:
        if field in mention:
            view[field] = mention[field]
    return view


def _mention_rank(mention: dict[str, Any]) -> tuple:
    """OK 引文优先、置信度高优先、推测性表述靠后；同分保持文档序。"""
    return (
        mention["verification"] != VERIFICATION_OK,
        -float(mention.get("confidence") or 0.0),
        bool(mention.get("hedge")),
    )


def _endpoint_view(entity: dict[str, Any] | None) -> dict[str, Any] | None:
    if not entity:
        return None
    return {"entity_id": entity["entity_id"], "name": entity["name"], "label": entity["label"]}


def _verification_summary(mentions: list[dict[str, Any]]) -> dict[str, int]:
    summary = {VERIFICATION_OK: 0, VERIFICATION_DEGRADED: 0, VERIFICATION_MISSING: 0}
    for mention in mentions:
        summary[mention["verification"]] = summary.get(mention["verification"], 0) + 1
    return summary


def _collapse_whitespace(text: str) -> str:
    return " ".join(text.split())
