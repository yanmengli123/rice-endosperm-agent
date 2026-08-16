from __future__ import annotations

import asyncio
import hashlib
import inspect
import re
from collections import defaultdict
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import aliased

from yuxi.knowledge.base import KnowledgeBase
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeGraphEntity,
    KnowledgeGraphRelationEvidence,
    KnowledgeGraphTriple,
)
from yuxi.utils.logging_config import logger

_TOKEN_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{1,63}|[\u4e00-\u9fff]{2,8}")
_CANDIDATE_MARKERS = {"candidate", "hypothesis", "hypothetical", "predicted", "putative", "ambiguous"}
_REJECTED_MARKERS = {"rejected", "refuted", "false", "invalid", "disproved"}
_STRICT_MARKERS = {"strict", "high", "direct", "verified", "confirmed", "gold"}


def _stable_id(prefix: str, *parts: Any) -> str:
    raw = "\x1f".join(str(part or "").strip() for part in parts)
    return f"{prefix}_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


def _query_tokens(query: str) -> list[str]:
    seen: set[str] = set()
    tokens = []
    for token in _TOKEN_PATTERN.findall(query or ""):
        value = token.strip()
        key = value.casefold()
        if len(value) < 2 or key in seen:
            continue
        seen.add(key)
        tokens.append(value)
    return tokens[:12]


def classify_evidence_status(assertion_status: str | None, evidence_level: str | None, alignment: str | None) -> str:
    values = " ".join(str(value or "").casefold() for value in (assertion_status, evidence_level, alignment))
    if any(marker in values for marker in _REJECTED_MARKERS):
        return "REJECTED"
    if any(marker in values for marker in _CANDIDATE_MARKERS) or "ambiguous" in str(alignment or "").casefold():
        return "CANDIDATE"
    # CONFLICT describes disagreement between sources rather than invalid evidence.
    # Keep it retrievable as supporting evidence and expose the conflict marker to
    # the answer layer instead of silently filtering it with the rejected policy.
    if "conflict" in str(alignment or "").casefold():
        return "SUPPORTING"
    if any(marker in values for marker in _STRICT_MARKERS) and str(alignment or "ALIGNED").upper() == "ALIGNED":
        return "STRICT"
    return "SUPPORTING"


def _policy_allows(policy: dict[str, Any], status: str) -> bool:
    return bool(policy.get(f"evidence_{status.casefold()}", False))


def _lexical_score(query_tokens: list[str], *parts: Any) -> float:
    haystack = " ".join(str(part or "") for part in parts).casefold()
    if not query_tokens:
        return 0.2
    matched = sum(1 for token in query_tokens if token.casefold() in haystack)
    return min(1.0, 0.2 + (matched / len(query_tokens)) * 0.8)


def _normalize_document_results(kb_id: str, kb_name: str, result: Any, priority: int) -> list[dict[str, Any]]:
    if isinstance(result, list):
        result = KnowledgeBase.build_search_output(kb_id, result)
    rows = result.get("results") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        return []

    normalized = []
    for rank, row in enumerate(rows):
        if not isinstance(row, dict) or not str(row.get("content") or "").strip():
            continue
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        raw_score = metadata.get("rerank_score", metadata.get("score"))
        try:
            score = float(raw_score) if raw_score is not None else 1.0 / (rank + 1)
        except (TypeError, ValueError):
            score = 1.0 / (rank + 1)
        chunk_id = str(row.get("id") or metadata.get("chunk_id") or "")
        file_id = str(row.get("file_id") or metadata.get("file_id") or "")
        evidence_id = _stable_id("evdoc", kb_id, file_id, chunk_id, row.get("content"))
        normalized.append(
            {
                "evidence_id": evidence_id,
                "source_type": "DOCUMENT",
                "evidence_status": "SUPPORTING",
                "kb_id": kb_id,
                "kb_name": kb_name,
                "found_in_kbs": [kb_id],
                "file_id": file_id,
                "chunk_id": chunk_id,
                "content": str(row.get("content") or "").strip(),
                "metadata": metadata,
                "raw_score": score,
                "priority": priority,
                "provenance": [{"kb_id": kb_id, "file_id": file_id, "chunk_id": chunk_id}],
            }
        )
    return normalized


async def _query_document_source(member: dict[str, Any], query_text: str) -> tuple[list[dict[str, Any]], str | None]:
    if not member.get("document_enabled"):
        return [], None
    from yuxi.knowledge.runtime import knowledge_base

    kb_id = member["kb_id"]
    target = knowledge_base.get_retrievers().get(kb_id)
    if not target:
        return [], "DOCUMENT_RETRIEVER_UNAVAILABLE"
    try:
        retriever = target["retriever"]
        result = retriever(query_text)
        if inspect.isawaitable(result):
            result = await result
        rows = _normalize_document_results(
            kb_id,
            member.get("kb_name") or target.get("name") or kb_id,
            result,
            int(member.get("priority") or 100),
        )
        return [row for row in rows if _policy_allows(member, row["evidence_status"])], None
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Scope document retrieval failed for {kb_id}: {exc}")
        return [], f"DOCUMENT_ERROR: {exc}"


async def _query_managed_graph_source(
    member: dict[str, Any], query_text: str, *, limit: int
) -> tuple[list[dict[str, Any]], str | None]:
    if not member.get("graph_enabled") and not member.get("structured_enabled"):
        return [], None
    tokens = _query_tokens(query_text)
    if not tokens:
        return [], None

    source_entity = aliased(KnowledgeGraphEntity)
    target_entity = aliased(KnowledgeGraphEntity)
    filters = []
    for token in tokens:
        pattern = f"%{token}%"
        filters.extend(
            [
                source_entity.name.ilike(pattern),
                source_entity.normalized_name.ilike(pattern),
                target_entity.name.ilike(pattern),
                target_entity.normalized_name.ilike(pattern),
                KnowledgeGraphTriple.relation_type.ilike(pattern),
                KnowledgeGraphTriple.content.ilike(pattern),
                KnowledgeGraphRelationEvidence.pmid.ilike(pattern),
                KnowledgeGraphRelationEvidence.doi.ilike(pattern),
                KnowledgeGraphRelationEvidence.evidence_quote.ilike(pattern),
            ]
        )

    stmt = (
        select(KnowledgeGraphTriple, source_entity, target_entity, KnowledgeGraphRelationEvidence)
        .join(source_entity, source_entity.entity_id == KnowledgeGraphTriple.source_entity_id)
        .join(target_entity, target_entity.entity_id == KnowledgeGraphTriple.target_entity_id)
        .outerjoin(
            KnowledgeGraphRelationEvidence,
            KnowledgeGraphRelationEvidence.triple_id == KnowledgeGraphTriple.triple_id,
        )
        .where(KnowledgeGraphTriple.kb_id == member["kb_id"], or_(*filters))
        .limit(max(limit * 3, 20))
    )
    try:
        async with pg_manager.get_async_session_context() as db:
            rows = (await db.execute(stmt)).all()
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Scope graph retrieval failed for {member['kb_id']}: {exc}")
        return [], f"GRAPH_ERROR: {exc}"

    evidence_rows = []
    for triple, source, target, evidence in rows:
        if evidence is not None and member.get("structured_enabled"):
            status = classify_evidence_status(
                evidence.assertion_status,
                evidence.evidence_level,
                evidence.evidence_alignment_status,
            )
            evidence_id = evidence.evidence_id
            source_type = "STRUCTURED"
            content = evidence.evidence_quote or triple.content
        elif member.get("graph_enabled"):
            status = classify_evidence_status("asserted", triple.best_evidence_level, "ALIGNED")
            evidence_id = _stable_id("evgraph", member["kb_id"], triple.triple_id)
            source_type = "GRAPH"
            content = triple.content
        else:
            continue
        if not _policy_allows(member, status):
            continue
        score = _lexical_score(tokens, source.name, triple.relation_type, target.name, content)
        evidence_rows.append(
            {
                "evidence_id": evidence_id,
                "source_type": source_type,
                "evidence_status": status,
                "kb_id": member["kb_id"],
                "kb_name": member.get("kb_name") or member["kb_id"],
                "found_in_kbs": [member["kb_id"]],
                "subject": {"id": source.entity_id, "name": source.name, "label": source.label},
                "predicate": triple.relation_type,
                "object": {"id": target.entity_id, "name": target.name, "label": target.label},
                "direction": evidence.direction if evidence is not None else triple.consensus_direction,
                "content": content,
                "pmid": evidence.pmid if evidence is not None else None,
                "doi": evidence.doi if evidence is not None else None,
                "evidence_level": evidence.evidence_level if evidence is not None else triple.best_evidence_level,
                "alignment_status": evidence.evidence_alignment_status if evidence is not None else "ALIGNED",
                "raw_score": score,
                "priority": int(member.get("priority") or 100),
                "provenance": [
                    {
                        "kb_id": member["kb_id"],
                        "triple_id": triple.triple_id,
                        "evidence_id": evidence_id,
                    }
                ],
            }
        )
    return evidence_rows, None


def _canonical_key(row: dict[str, Any]) -> str:
    doi = str(row.get("doi") or "").strip().casefold()
    pmid = str(row.get("pmid") or "").strip().casefold()
    subject = str((row.get("subject") or {}).get("name") or "").strip().casefold()
    predicate = str(row.get("predicate") or "").strip().casefold()
    obj = str((row.get("object") or {}).get("name") or "").strip().casefold()
    if subject or predicate or obj:
        return f"claim:{subject}|{predicate}|{obj}|{doi or pmid}"
    content = re.sub(r"\s+", " ", str(row.get("content") or "").strip().casefold())
    return f"text:{doi or pmid}|{hashlib.sha256(content.encode('utf-8')).hexdigest()[:24]}"


def _deduplicate_and_rerank(rows: list[dict[str, Any]], *, top_k: int) -> tuple[list[dict[str, Any]], list[str]]:
    if not rows:
        return [], []
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[_canonical_key(row)].append(row)

    merged_rows = []
    warnings: list[str] = []
    for key, group in grouped.items():
        best = max(group, key=lambda row: (float(row.get("raw_score") or 0.0), -int(row.get("priority") or 100)))
        merged = dict(best)
        merged["found_in_kbs"] = sorted({kb for row in group for kb in row.get("found_in_kbs") or []})
        merged["provenance"] = [item for row in group for item in row.get("provenance") or []]
        directions = {str(row.get("direction") or "UNKNOWN").upper() for row in group} - {"", "UNKNOWN", "NONE"}
        alignments = {str(row.get("alignment_status") or "ALIGNED").upper() for row in group}
        conflict = len(directions) > 1 or any(value in {"CONFLICT", "CONFLICTED", "AMBIGUOUS"} for value in alignments)
        merged["conflict"] = conflict
        if conflict:
            warnings.append(f"证据冲突：{key}，必须在回答中保留分歧并降低结论强度。")
        corroboration = min(len(merged["found_in_kbs"]), 3) * 0.04
        priority_boost = max(0.0, (100 - int(merged.get("priority") or 100)) / 500.0)
        merged["score"] = min(1.0, float(merged.get("raw_score") or 0.0) + corroboration + priority_boost)
        merged_rows.append(merged)

    status_rank = {"STRICT": 0, "SUPPORTING": 1, "CANDIDATE": 2, "REJECTED": 3}
    merged_rows.sort(
        key=lambda row: (
            bool(row.get("conflict")),
            status_rank.get(str(row.get("evidence_status")), 9),
            -float(row.get("score") or 0.0),
            int(row.get("priority") or 100),
        )
    )
    return merged_rows[:top_k], warnings


async def query_knowledge_scope_gateway(
    *, query_text: str, scope_snapshot: dict[str, Any], top_k: int = 12
) -> dict[str, Any]:
    members = [member for member in scope_snapshot.get("members") or [] if isinstance(member, dict)]
    top_k = min(max(int(top_k), 1), 50)
    tasks = []
    task_labels = []
    per_source_limit = max(top_k, 8)
    for member in members:
        tasks.extend(
            [
                _query_document_source(member, query_text),
                _query_managed_graph_source(member, query_text, limit=per_source_limit),
            ]
        )
        task_labels.extend([(member["kb_id"], "DOCUMENT"), (member["kb_id"], "GRAPH_STRUCTURED")])

    results = await asyncio.gather(*tasks) if tasks else []
    all_rows: list[dict[str, Any]] = []
    source_summary = []
    for (kb_id, source_type), (rows, error) in zip(task_labels, results):
        all_rows.extend(rows)
        source_summary.append({"kb_id": kb_id, "source_type": source_type, "hits": len(rows), "error": error})

    evidence, warnings = _deduplicate_and_rerank(all_rows, top_k=top_k)
    if not members:
        warnings.append("当前运行的有效知识范围为空。")
    if not evidence and members:
        warnings.append("范围内未检索到足以回答该问题的证据。")
    return {
        "knowledge_scope_snapshot": {
            "scope_id": scope_snapshot.get("scope_id"),
            "scope_slug": scope_snapshot.get("scope_slug"),
            "scope_version": scope_snapshot.get("scope_version"),
            "scope_mode": scope_snapshot.get("scope_mode"),
            "kb_ids": scope_snapshot.get("effective_kb_ids") or [],
            "retrieval_mode": scope_snapshot.get("retrieval_mode"),
            "allow_web": bool(scope_snapshot.get("allow_web", False)),
        },
        "evidence": evidence,
        "retrieval_summary": {
            "query": query_text,
            "sources": source_summary,
            "raw_hits": len(all_rows),
            "deduplicated_hits": len(evidence),
            "web_tool_available": bool(scope_snapshot.get("allow_web", False)),
            "web_call_count": 0,
        },
        "warnings": warnings,
        "claim_validation_rules": [
            "关键科研论断必须引用本结果中的 evidence_id",
            "CANDIDATE 必须显式标为候选，不能表述为已证实",
            "binding/interaction 不等于 activation/repression",
            "Gene 与 Allele/Mutant 必须保持类型区分",
            "conflict=true 的证据必须呈现分歧",
        ],
    }
