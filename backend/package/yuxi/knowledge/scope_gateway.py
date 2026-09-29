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
from yuxi.knowledge.products.registry import is_derived_product, is_evidence_authority
from yuxi.knowledge.research_evidence import (
    build_evidence_semantics,
    candidate_explicitly_requested,
    evidence_category_rank,
    is_yield_gene_query,
    sanitize_scientific_identifier_notation,
    validate_doi,
    validate_pmid,
)
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

# A scope can fan out to many independently managed knowledge bases. One slow
# embedding/reranker/provider must never hold the whole conversation open.
KNOWLEDGE_DOCUMENT_SOURCE_TIMEOUT_SECONDS = 45.0
KNOWLEDGE_GRAPH_SOURCE_TIMEOUT_SECONDS = 20.0
# VERBATIM 通道是本地 PG 查询（无嵌入/外部服务），超时预算远小于文档通道。
KNOWLEDGE_VERBATIM_SOURCE_TIMEOUT_SECONDS = 8.0
# 精确字面命中加成，量级对齐 Milvus 侧 scientific identifier boost（+0.04）：
# 只保证确定性信号进入候选池，不与语义分争夺排序。
VERBATIM_EXACT_BONUS = 0.04


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


def classify_evidence_status(
    assertion_status: str | None,
    evidence_level: str | None,
    alignment: str | None,
    entity_status: str | None = None,
) -> str:
    values = " ".join(
        str(value or "").casefold() for value in (assertion_status, evidence_level, alignment, entity_status)
    )
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


def _member_kb_type(member: dict[str, Any], target: dict[str, Any] | None = None) -> str:
    """Best-effort kb_type for a scope member (member dict wins, retriever metadata fallback)."""
    kb_type = str(member.get("kb_type") or "").strip().casefold()
    if kb_type:
        return kb_type
    metadata = target.get("metadata") if isinstance(target, dict) else None
    return str((metadata or {}).get("kb_type") or "").strip().casefold()


def _member_is_derived(member: dict[str, Any], target: dict[str, Any] | None = None) -> bool:
    """派生知识产品（如 llmwiki）永远不能进入证据通道（Authority Gate）。"""
    return is_derived_product(_member_kb_type(member, target))


def _member_evidence_denied(member: dict[str, Any], target: dict[str, Any] | None = None) -> bool:
    kb_type = _member_kb_type(member, target)
    return bool(kb_type) and not is_evidence_authority(kb_type)


def _member_authority_denial_code(member: dict[str, Any], target: dict[str, Any] | None = None) -> str:
    return "DERIVED_PRODUCT_CHANNEL_DENIED" if _member_is_derived(member, target) else "PRODUCT_AUTHORITY_DENIED"


def _normalize_document_results(
    kb_id: str,
    kb_name: str,
    result: Any,
    priority: int,
    *,
    query_text: str | None = None,
) -> list[dict[str, Any]]:
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
        scientific_provenance = (
            metadata.get("scientific_provenance") if isinstance(metadata.get("scientific_provenance"), dict) else {}
        )
        raw_score = metadata.get("rerank_score")
        if raw_score is None:
            raw_score = row.get("rerank_score")
        if raw_score is None:
            raw_score = metadata.get("score")
        if raw_score is None:
            raw_score = row.get("score")
        try:
            score = float(raw_score) if raw_score is not None else 1.0 / (rank + 1)
        except (TypeError, ValueError):
            score = 1.0 / (rank + 1)
        if query_text:
            # Milvus hybrid scores and graph lexical scores use different scales.
            # Calibrate document hits with the same lexical function used by the
            # graph channel so exact scientific identifiers cannot be crowded out
            # solely because their backend score has a smaller numeric range.
            score = max(
                score,
                _lexical_score(
                    _query_tokens(query_text),
                    row.get("content"),
                    metadata.get("source"),
                ),
            )
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
                "parse_revision_id": scientific_provenance.get("parse_revision_id"),
                "index_revision_id": scientific_provenance.get("index_revision_id"),
                "anchor_ids": scientific_provenance.get("evidence_anchor_ids")
                or scientific_provenance.get("anchor_ids")
                or [],
                "content": sanitize_scientific_identifier_notation(row.get("content")),
                "metadata": metadata,
                "claim_eligible": False,
                "identifier_status": "UNSTRUCTURED_DOCUMENT",
                "outcome_class": "OTHER",
                "raw_score": score,
                "priority": priority,
                "provenance": [{"kb_id": kb_id, "source_type": "DOCUMENT", "file_id": file_id, "chunk_id": chunk_id}],
            }
        )
    return normalized


async def _query_document_source(
    member: dict[str, Any],
    query_text: str,
    *,
    file_ids: list[str] | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    if not member.get("document_enabled"):
        return [], None
    from yuxi.knowledge.runtime import knowledge_base

    kb_id = member["kb_id"]
    if _member_evidence_denied(member):
        # Authority Gate：派生产品无论资源是否可用都先被门禁拒绝，不属于可用性问题。
        logger.warning(f"Scope member {kb_id} is a derived product; document channel denied")
        return [], _member_authority_denial_code(member)
    target = knowledge_base.get_retrievers().get(kb_id)
    if not target:
        return [], "DOCUMENT_RETRIEVER_UNAVAILABLE"
    try:
        retriever = target["retriever"]
        kb_type = _member_kb_type(member, target)
        retrieval_kwargs: dict[str, Any] = {}
        if file_ids:
            # mention.v2 文献硬约束：不支持按 file_id 收窄的通道必须失败关闭，
            # 绝不能忽略过滤条件后返回全库命中（那等于静默扩大范围）。
            if kb_type != "milvus":
                return [], "DOCUMENT_SCOPE_NARROWING_UNSUPPORTED"
            retrieval_kwargs["file_ids"] = [str(value) for value in file_ids]
        if kb_type == "milvus":
            # The answer plane needs lexical recall for exact scientific symbols
            # (for example SANT/OsMYB73) and vector recall for natural-language
            # paraphrases. Raw PDF chunks remain the authoritative evidence source.
            retrieval_kwargs.setdefault("search_mode", "hybrid")
            retrieval_kwargs.setdefault("scientific_pdf_diversity", True)
        result = retriever(query_text, **retrieval_kwargs)
        if inspect.isawaitable(result):
            result = await result
        rows = _normalize_document_results(
            kb_id,
            member.get("kb_name") or target.get("name") or kb_id,
            result,
            int(member.get("priority") or 100),
            query_text=query_text,
        )
        return [row for row in rows if _policy_allows(member, row["evidence_status"])], None
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Scope document retrieval failed for {kb_id}: {exc}")
        return [], f"DOCUMENT_ERROR: {exc}"


async def _query_source_with_timeout(
    source_coro,
    *,
    kb_id: str,
    source_type: str,
    timeout_seconds: float,
) -> tuple[list[dict[str, Any]], str | None]:
    """Bound one retrieval channel while preserving successful sibling results."""

    try:
        return await asyncio.wait_for(source_coro, timeout=timeout_seconds)
    except TimeoutError:
        logger.warning(f"Scope source timed out: kb={kb_id}, source={source_type}, timeout={timeout_seconds:.1f}s")
        return [], f"{source_type}_TIMEOUT"
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - one source must not abort the full scope
        logger.exception(f"Scope source failed unexpectedly: kb={kb_id}, source={source_type}")
        return [], f"{source_type}_ERROR: {exc}"


async def _query_managed_graph_source(
    member: dict[str, Any], query_text: str, *, limit: int
) -> tuple[list[dict[str, Any]], str | None]:
    if _member_evidence_denied(member):
        # Authority Gate：派生知识产品没有 graph/structured 证据通道，
        # 门禁优先于通道开关判断——策略拒绝不依赖成员策略字段。
        logger.warning(f"Scope member {member['kb_id']} is a derived product; graph channel denied")
        return [], _member_authority_denial_code(member)
    if not member.get("graph_enabled") and not member.get("structured_enabled"):
        return [], None
    tokens = _query_tokens(query_text)
    if not tokens:
        return [], None

    source_entity = aliased(KnowledgeGraphEntity)
    target_entity = aliased(KnowledgeGraphEntity)
    yield_query = is_yield_gene_query(query_text)
    include_candidates = candidate_explicitly_requested(query_text)
    if yield_query:
        outcome_filters = [
            KnowledgeGraphRelationEvidence.outcome_class.in_(
                [
                    "DIRECT_YIELD",
                    "CONDITION_SPECIFIC_YIELD",
                    "YIELD_COMPONENT",
                    "GRAIN_FILLING",
                    "GRAIN_MORPHOLOGY",
                    "QUALITY",
                ]
            )
        ]
        for alias in (
            "grain yield",
            "yield per plant",
            "grain weight",
            "1000-grain weight",
            "grain number",
            "panicle number",
            "seed-setting rate",
            "grain filling",
            "grain size",
            "grain length",
            "grain width",
        ):
            outcome_filters.append(target_entity.normalized_name.ilike(f"%{alias}%"))
        filters = outcome_filters
    else:
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
        # Yield questions are stratified after retrieval.  Read a bounded but
        # sufficiently broad candidate pool first so insertion order cannot
        # starve condition-specific or component outcomes before reranking.
        .limit(min(max(limit * 50, 2000), 5000) if yield_query else max(limit * 3, 20))
    )
    try:
        async with pg_manager.get_async_session_context() as db:
            rows = (await db.execute(stmt)).all()
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Scope graph retrieval failed for {member['kb_id']}: {exc}")
        return [], f"GRAPH_ERROR: {exc}"

    evidence_rows = []
    for triple, source, target, evidence in rows:
        source_attributes = source.attributes if isinstance(source.attributes, dict) else {}
        entity_status = str(source_attributes.get("gene_status") or "")
        if evidence is not None and member.get("structured_enabled"):
            status = classify_evidence_status(
                evidence.assertion_status,
                evidence.evidence_level,
                evidence.evidence_alignment_status,
                entity_status,
            )
            evidence_id = evidence.evidence_id
            source_type = "STRUCTURED"
            content = evidence.evidence_quote or triple.content
            pmid, pmid_status = validate_pmid(evidence.pmid)
            doi, doi_status = validate_doi(evidence.doi)
            if pmid_status == "VALID" or doi_status == "VALID":
                identifier_status = "VALID"
            elif pmid_status == "MISSING" and doi_status == "MISSING":
                identifier_status = "MISSING"
            else:
                identifier_status = (
                    "INVALID_SCIENTIFIC_NOTATION"
                    if ("SCIENTIFIC_NOTATION" in pmid_status or "SCIENTIFIC_NOTATION" in doi_status)
                    else "INVALID_FORMAT"
                )
            fallback_semantics = build_evidence_semantics(
                source_name=source.name,
                source_label=source.label,
                relation_type=triple.relation_type,
                target_name=target.name,
                quote=content,
                direction=evidence.direction,
            )
            outcome_class = evidence.outcome_class or fallback_semantics["outcome_class"]
            if outcome_class == "OTHER":
                outcome_class = fallback_semantics["outcome_class"]
            condition = evidence.condition or fallback_semantics["condition"]
            persisted_subject_type = str(evidence.experimental_subject_type or "").upper()
            fallback_subject_type = str(fallback_semantics["experimental_subject_type"] or "").upper()
            if persisted_subject_type in {"", "GENE", "UNKNOWN"} and fallback_subject_type not in {
                "",
                "GENE",
                "UNKNOWN",
            }:
                experimental_subject_type = fallback_subject_type
                subject_material = fallback_semantics["subject_material"]
            else:
                experimental_subject_type = persisted_subject_type or fallback_subject_type
                subject_material = evidence.subject_material or fallback_semantics["subject_material"]
            claim_eligible = bool(
                evidence.claim_eligible
                and identifier_status == "VALID"
                and evidence.evidence_alignment_status == "ALIGNED"
                and str(content or "").strip()
            )
        elif member.get("graph_enabled"):
            status = classify_evidence_status("asserted", triple.best_evidence_level, "ALIGNED", entity_status)
            evidence_id = _stable_id("evgraph", member["kb_id"], triple.triple_id)
            source_type = "GRAPH"
            content = triple.content
            pmid = None
            doi = None
            identifier_status = "GRAPH_WITHOUT_BOUND_EVIDENCE"
            fallback_semantics = build_evidence_semantics(
                source_name=source.name,
                source_label=source.label,
                relation_type=triple.relation_type,
                target_name=target.name,
                quote=content,
                direction=triple.consensus_direction,
            )
            outcome_class = fallback_semantics["outcome_class"]
            condition = fallback_semantics["condition"]
            claim_eligible = False
        else:
            continue
        if status == "CANDIDATE" and not include_candidates:
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
                "content": sanitize_scientific_identifier_notation(content),
                "pmid": pmid,
                "doi": doi,
                "identifier_status": identifier_status,
                "evidence_level": evidence.evidence_level if evidence is not None else triple.best_evidence_level,
                "alignment_status": evidence.evidence_alignment_status if evidence is not None else "ALIGNED",
                "assertion_status": evidence.assertion_status if evidence is not None else "ASSERTED",
                "claim_eligible": claim_eligible,
                "outcome_class": outcome_class,
                "yield_measure_type": (
                    evidence.yield_measure_type if evidence is not None else fallback_semantics["yield_measure_type"]
                ),
                "experimental_subject_type": (
                    experimental_subject_type
                    if evidence is not None
                    else fallback_semantics["experimental_subject_type"]
                ),
                "subject_material": (
                    subject_material if evidence is not None else fallback_semantics["subject_material"]
                ),
                "perturbs": evidence.perturbs if evidence is not None else fallback_semantics["perturbs"],
                "perturbation_direction": (
                    evidence.perturbation_direction
                    if evidence is not None
                    else fallback_semantics["perturbation_direction"]
                ),
                "condition": condition,
                "cultivar": evidence.cultivar if evidence is not None else None,
                "genetic_background": evidence.genetic_background if evidence is not None else None,
                "development_stage": evidence.development_stage if evidence is not None else None,
                "observed_effect": (
                    evidence.observed_effect if evidence is not None else fallback_semantics["observed_effect"]
                ),
                "observed_relation": (
                    evidence.observed_relation if evidence is not None else fallback_semantics["observed_relation"]
                ),
                "inferred_gene_function": evidence.inferred_gene_function if evidence is not None else None,
                "evidence_quote": sanitize_scientific_identifier_notation(
                    evidence.evidence_quote if evidence is not None else None
                ),
                "raw_score": score,
                "priority": int(member.get("priority") or 100),
                "provenance": [
                    {
                        "kb_id": member["kb_id"],
                        "source_type": source_type,
                        "triple_id": triple.triple_id,
                        "evidence_id": evidence_id,
                    }
                ],
            }
        )
    return evidence_rows, None


def _normalize_verbatim_results(
    member: dict[str, Any],
    spans: list[dict[str, Any]],
    *,
    query_text: str,
) -> list[dict[str, Any]]:
    """把 evidence span 命中归一成与 DOCUMENT 通道同构的证据行。

    分数纪律：确定性命中只给 ``TIER_BASE_SCORE`` 级基础分 + ``+0.04`` 精确
    加成（与 Milvus 标识符 boost 同量级），跨通道 lexical 校准取 max，
    不与语义分争夺排序；``claim_eligible`` 恒为 False（Claim 只出自 canonical 图）。
    """
    from yuxi.knowledge.evidence.verbatim import TIER_BASE_SCORE

    kb_id = member["kb_id"]
    kb_name = member.get("kb_name") or kb_id
    tokens = _query_tokens(query_text)
    rows = []
    for span in spans:
        quote = str(span.get("quote") or "")
        if not quote.strip():
            continue
        base = TIER_BASE_SCORE.get(str(span.get("match_tier")), 0.68)
        score = min(1.0, max(_lexical_score(tokens, quote), base) + VERBATIM_EXACT_BONUS)
        rows.append(
            {
                "evidence_id": span.get("evidence_id") or _stable_id("evs", span.get("span_id"), quote),
                "source_type": "VERBATIM",
                "evidence_status": "SUPPORTING",
                "kb_id": kb_id,
                "kb_name": kb_name,
                "found_in_kbs": [kb_id],
                "file_id": span.get("file_id"),
                "chunk_id": span.get("span_id"),
                "span_id": span.get("span_id"),
                "anchor_id": span.get("anchor_id"),
                "page_number": span.get("page_number"),
                "parse_revision_id": span.get("parse_revision_id"),
                "document_partition": span.get("document_partition"),
                "partition_confidence": span.get("partition_confidence"),
                "evidence_type": span.get("evidence_type"),
                "container_label": span.get("container_label"),
                "row_key": span.get("row_key"),
                "content": sanitize_scientific_identifier_notation(quote),
                "metadata": {"match_tier": span.get("match_tier"), "matched_value": span.get("matched_value")},
                "claim_eligible": False,
                "identifier_status": "UNSTRUCTURED_DOCUMENT",
                "outcome_class": "OTHER",
                "raw_score": score,
                "priority": int(member.get("priority") or 100),
                "provenance": [
                    {
                        "kb_id": kb_id,
                        "source_type": "VERBATIM",
                        "file_id": span.get("file_id"),
                        "span_id": span.get("span_id"),
                        "anchor_id": span.get("anchor_id"),
                        "parse_revision_id": span.get("parse_revision_id"),
                    }
                ],
                "retrieval_channel": "VERBATIM",
                "match_tier": span.get("match_tier"),
                "matched_value": span.get("matched_value"),
            }
        )
    return rows


async def _query_verbatim_scope_source(
    members: list[dict[str, Any]],
    query_text: str,
    *,
    verbatim: dict[str, Any],
    limit: int,
    file_ids: list[str] | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """对冻结成员库执行一次跨库 VERBATIM 查询（单次 PG 往返，不分成员 fan-out）。"""
    from yuxi.knowledge.evidence.verbatim import MAX_VERBATIM_HITS, query_verbatim_evidence

    tenant_id = verbatim.get("tenant_id")
    if tenant_id is None:
        return [], "VERBATIM_DISABLED: missing tenant"
    kb_ids = [str(member["kb_id"]) for member in members]
    try:
        async with pg_manager.get_async_session_context() as db:
            result = await query_verbatim_evidence(
                db,
                tenant_id=int(tenant_id),
                kb_ids=kb_ids,
                question=query_text,
                patterns=verbatim.get("patterns") or None,
                file_ids=[str(value) for value in (file_ids or [])] or None,
                limit=max(1, min(int(limit or MAX_VERBATIM_HITS), MAX_VERBATIM_HITS)),
            )
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Scope verbatim retrieval failed: {exc}")
        return [], f"VERBATIM_ERROR: {exc}"
    member_by_kb = {str(member["kb_id"]): member for member in members}
    rows: list[dict[str, Any]] = []
    for span in result.get("spans") or []:
        member = member_by_kb.get(str(span.get("kb_id")))
        if member is None:
            continue
        rows.extend(_normalize_verbatim_results(member, [span], query_text=query_text))
    return [row for row in rows if _policy_allows(member_by_kb[row["kb_id"]], row["evidence_status"])], None


def _merge_verbatim_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """VERBATIM 命中若已被某个 DOCUMENT chunk 覆盖（quote 为其子串），合并进
    该 chunk 行并继承 span 的稳定 evidence_id / 页码；独立命中保留为独立
    VERBATIM 证据行。在去重/截断之前执行，保证强信号不被 top_k 挤掉。"""
    verbatim_rows = [row for row in rows if row.get("retrieval_channel") == "VERBATIM"]
    if not verbatim_rows:
        return rows
    document_rows = [row for row in rows if row.get("source_type") == "DOCUMENT"]
    folded_contents = [re.sub(r"\s+", " ", str(row.get("content") or "")).casefold() for row in document_rows]
    merged_rows = [row for row in rows if row.get("retrieval_channel") != "VERBATIM"]
    for verbatim_row in verbatim_rows:
        quote_folded = re.sub(r"\s+", " ", str(verbatim_row.get("content") or "")).casefold()
        target = next(
            (
                document_row
                for document_row, folded in zip(document_rows, folded_contents)
                if quote_folded and quote_folded in folded
            ),
            None,
        )
        if target is not None:
            target["verbatim_hit"] = True
            target["verbatim_evidence_id"] = verbatim_row.get("evidence_id")
            target.setdefault("page_number", verbatim_row.get("page_number"))
            target.setdefault("span_id", verbatim_row.get("span_id"))
            target.setdefault("parse_revision_id", verbatim_row.get("parse_revision_id"))
            target.setdefault("document_partition", verbatim_row.get("document_partition"))
            target.setdefault("partition_confidence", verbatim_row.get("partition_confidence"))
            target.setdefault("match_tier", verbatim_row.get("match_tier"))
            target["provenance"] = [*(target.get("provenance") or []), *(verbatim_row.get("provenance") or [])]
        else:
            merged_rows.append(verbatim_row)
    return merged_rows


async def query_verbatim_for_scope(
    *,
    query_text: str,
    scope_snapshot: dict[str, Any],
    patterns: list[str] | None = None,
    question_types: list[str] | None = None,
    top_k: int = 12,
    file_ids: list[str] | None = None,
) -> dict[str, Any]:
    """直接在冻结范围内执行 VERBATIM 通道（不跑 Milvus/图谱）。

    供编排器低置信兜底与 ``grep_evidence`` 工具复用；租户缺失时失败关闭。
    """
    members = [
        member
        for member in scope_snapshot.get("members") or []
        if isinstance(member, dict) and member.get("document_enabled") and not _member_evidence_denied(member)
    ]
    tenant_id = scope_snapshot.get("tenant_id")
    if not members or tenant_id is None or not str(query_text or "").strip():
        return {
            "evidence": [],
            "warnings": ["VERBATIM 检索未执行：缺少租户标识、成员库或查询文本。"],
            "retrieval_summary": {"verbatim_hit_count": 0},
        }
    verbatim = {"tenant_id": int(tenant_id), "patterns": patterns or [], "question_types": question_types or []}
    rows, error = await _query_verbatim_scope_source(
        members,
        query_text,
        verbatim=verbatim,
        limit=top_k,
        file_ids=[str(value).strip() for value in (file_ids or []) if str(value).strip()] or None,
    )
    if error:
        return {
            "evidence": [],
            "warnings": [f"VERBATIM 检索失败（失败关闭，不影响其他通道）：{error}"],
            "retrieval_summary": {"verbatim_hit_count": 0},
        }
    ranked, _ = _deduplicate_and_rerank(rows, top_k=top_k)
    evidence = [_compact_evidence(item, query_text=query_text) for item in ranked]
    return {
        "evidence": evidence,
        "warnings": [],
        "retrieval_summary": {
            "verbatim_hit_count": sum(1 for item in evidence if item.get("retrieval_channel") == "VERBATIM")
        },
    }


def _canonical_key(row: dict[str, Any]) -> str:
    subject = str((row.get("subject") or {}).get("name") or "").strip().casefold()
    predicate = str(row.get("predicate") or "").strip().casefold()
    obj = str((row.get("object") or {}).get("name") or "").strip().casefold()
    if subject or predicate or obj:
        return f"claim:{subject}|{predicate}|{obj}"
    doi = str(row.get("doi") or "").strip().casefold()
    pmid = str(row.get("pmid") or "").strip().casefold()
    content = re.sub(r"\s+", " ", str(row.get("content") or "").strip().casefold())
    return f"text:{doi or pmid}|{hashlib.sha256(content.encode('utf-8')).hexdigest()[:24]}"


def _deduplicate_and_rerank(
    rows: list[dict[str, Any]], *, top_k: int, stratify_yield: bool = False
) -> tuple[list[dict[str, Any]], list[str]]:
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
        merged["evidence_records"] = [
            {
                field: row.get(field)
                for field in (
                    "evidence_id",
                    "source_type",
                    "kb_id",
                    "file_id",
                    "chunk_id",
                    "pmid",
                    "doi",
                    "evidence_level",
                    "condition",
                )
                if row.get(field) not in (None, "", [], {})
            }
            for row in group
        ]
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

    def evidence_level_rank(value: Any) -> int:
        match = re.search(r"\d+", str(value or ""))
        return int(match.group(0)) if match else 99

    if stratify_yield:
        merged_rows.sort(
            key=lambda row: (
                bool(row.get("conflict")),
                evidence_category_rank(row.get("outcome_class")),
                status_rank.get(str(row.get("evidence_status")), 9),
                evidence_level_rank(row.get("evidence_level")),
                -float(row.get("score") or 0.0),
                int(row.get("priority") or 100),
            )
        )
    else:
        # Outcome strata are meaningful only for yield questions. Applying that
        # ordering globally lets unrelated graph relations outrank an exact raw
        # PDF passage, which both hides the authoritative quote and invites the
        # answer model to invent details.
        merged_rows.sort(
            key=lambda row: (
                bool(row.get("conflict")),
                -float(row.get("score") or 0.0),
                status_rank.get(str(row.get("evidence_status")), 9),
                evidence_level_rank(row.get("evidence_level")),
                int(row.get("priority") or 100),
            )
        )
    if not stratify_yield:
        return merged_rows[:top_k], warnings

    # Preserve the semantic layers in a bounded answer package.  A plain
    # global sort puts DIRECT_YIELD first and can consume the entire top-k,
    # falsely making present condition/component evidence look absent.
    layer_schedule = [
        "DIRECT_YIELD",
        "CONDITION_SPECIFIC_YIELD",
        "YIELD_COMPONENT",
        "DIRECT_YIELD",
        "CONDITION_SPECIFIC_YIELD",
        "YIELD_COMPONENT",
        "GRAIN_FILLING",
        "GRAIN_MORPHOLOGY",
        "QUALITY",
        "OTHER",
        "DIRECT_YIELD",
        "YIELD_COMPONENT",
    ]
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in merged_rows:
        groups[str(row.get("outcome_class") or "OTHER").upper()].append(row)

    selected: list[dict[str, Any]] = []
    selected_ids: set[int] = set()
    for category in layer_schedule:
        if len(selected) >= top_k or not groups[category]:
            continue
        row = groups[category].pop(0)
        selected.append(row)
        selected_ids.add(id(row))
    for row in merged_rows:
        if len(selected) >= top_k:
            break
        if id(row) in selected_ids:
            continue
        selected.append(row)
        selected_ids.add(id(row))
    return selected, warnings


def _compact_evidence(row: dict[str, Any], *, query_text: str | None = None) -> dict[str, Any]:
    """Return the claim/renderer contract without internal ranking baggage."""

    def clipped(value: Any, limit: int = 420) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        if len(text) <= limit:
            return text

        if query_text:
            folded = text.casefold()
            anchors = []
            for token in _query_tokens(query_text):
                normalized_token = token.casefold()
                index = folded.find(normalized_token)
                if index >= 0:
                    anchors.append((folded.count(normalized_token), -index, -len(normalized_token)))
            if anchors:
                _, negated_anchor, _ = min(anchors)
                anchor = -negated_anchor
                content_limit = max(limit - 2, 1)
                start = max(0, anchor - content_limit // 3)
                end = min(len(text), start + content_limit)
                if end == len(text):
                    start = max(0, end - content_limit)
                excerpt = text[start:end].strip()
                return f"{'…' if start else ''}{excerpt}{'…' if end < len(text) else ''}"

        return f"{text[: limit - 1].rstrip()}…"

    observed_effect = row.get("observed_effect")
    if not observed_effect or str(observed_effect).upper() in {"UNKNOWN", "NONE"}:
        observed_effect = row.get("direction")
    observed_relation = row.get("observed_relation") or row.get("predicate")
    compact = {
        "evidence_id": row.get("evidence_id"),
        "source_type": row.get("source_type"),
        "evidence_status": row.get("evidence_status"),
        "kb_id": row.get("kb_id"),
        "kb_name": row.get("kb_name"),
        "file_id": row.get("file_id"),
        "chunk_id": row.get("chunk_id"),
        "parse_revision_id": row.get("parse_revision_id"),
        "index_revision_id": row.get("index_revision_id"),
        "anchor_ids": row.get("anchor_ids") or [],
        "subject": {"name": (row.get("subject") or {}).get("name")},
        "object": {"name": (row.get("object") or {}).get("name")},
        "pmid": row.get("pmid"),
        "doi": row.get("doi"),
        "evidence_level": row.get("evidence_level"),
        "claim_eligible": bool(row.get("claim_eligible")),
        "outcome_class": row.get("outcome_class"),
        "yield_measure_type": row.get("yield_measure_type"),
        "experimental_subject_type": row.get("experimental_subject_type"),
        "subject_material": row.get("subject_material"),
        "perturbs": row.get("perturbs"),
        "perturbation_direction": row.get("perturbation_direction"),
        "condition": row.get("condition"),
        "cultivar": row.get("cultivar"),
        "genetic_background": row.get("genetic_background"),
        "development_stage": row.get("development_stage"),
        "observed_effect": observed_effect,
        "observed_relation": observed_relation,
        "inferred_gene_function": row.get("inferred_gene_function"),
        "evidence_quote": clipped(row.get("evidence_quote") or row.get("content"), limit=300),
        "conflict": bool(row.get("conflict")),
        "evidence_records": row.get("evidence_records") or [],
        # contract 1.1：通道归属（DOCUMENT/GRAPH/STRUCTURED/VERBATIM）与
        # VERBATIM 命中细节（span/anchor/页码/match_tier）为增量字段。
        "retrieval_channel": row.get("retrieval_channel") or row.get("source_type"),
        "span_id": row.get("span_id"),
        "anchor_id": row.get("anchor_id"),
        "page_number": row.get("page_number"),
        "document_partition": row.get("document_partition"),
        "partition_confidence": row.get("partition_confidence"),
        "span_evidence_type": row.get("evidence_type"),
        "container_label": row.get("container_label"),
        "row_key": row.get("row_key"),
        "match_tier": row.get("match_tier"),
        "matched_value": row.get("matched_value"),
        "verbatim_hit": row.get("verbatim_hit"),
        "verbatim_evidence_id": row.get("verbatim_evidence_id"),
    }
    return {key: value for key, value in compact.items() if value not in (None, "", [], {}) or isinstance(value, bool)}


async def _partition_document_ids_by_kb(file_ids: list[str] | None) -> dict[str, list[str]]:
    """把文献硬约束按 kb 归属拆分（单次 PK 查询；未知 file_id 不会命中任何库）。"""
    from sqlalchemy import select

    from yuxi.storage.postgres.manager import pg_manager
    from yuxi.storage.postgres.models_knowledge import KnowledgeFile

    ids = [str(value).strip() for value in (file_ids or []) if str(value).strip()]
    if not ids:
        return {}
    async with pg_manager.get_async_session_context() as db:
        rows = (
            await db.execute(select(KnowledgeFile.file_id, KnowledgeFile.kb_id).where(KnowledgeFile.file_id.in_(ids)))
        ).all()
    partition: dict[str, list[str]] = {}
    for file_id, kb_id in rows:
        partition.setdefault(str(kb_id or ""), []).append(str(file_id))
    return partition


async def query_knowledge_scope_gateway(
    *,
    query_text: str,
    scope_snapshot: dict[str, Any],
    top_k: int = 12,
    verbatim: dict[str, Any] | None = None,
    file_ids: list[str] | None = None,
) -> dict[str, Any]:
    members = [member for member in scope_snapshot.get("members") or [] if isinstance(member, dict)]
    top_k = min(max(int(top_k), 1), 12)
    tasks = []
    task_labels = []
    per_source_limit = max(top_k, 8)
    verbatim_members = [
        member for member in members if member.get("document_enabled") and not _member_evidence_denied(member)
    ]
    # mention.v2 文献硬约束（@doc）：对所有证据通道生效——文档通道按 file_id 收窄，
    # 图谱/结构化通道（无文献粒度）直接排除并记录状态，绝不静默忽略过滤条件。
    document_scope_files = [str(value).strip() for value in (file_ids or []) if str(value).strip()]
    files_by_kb = await _partition_document_ids_by_kb(document_scope_files)
    effective_document_ids: dict[str, list[str]] = {}
    document_filter_active: dict[str, bool] = {}
    for member in members:
        kb_id = str(member["kb_id"])
        mentioned = files_by_kb.get(kb_id) or []
        folder_restricted = bool(member.get("folder_scope_restricted"))
        folder_files = {
            str(value).strip() for value in (member.get("folder_file_ids") or []) if str(value).strip()
        }
        document_filter_active[kb_id] = bool(document_scope_files or folder_restricted)
        if document_scope_files:
            effective_document_ids[kb_id] = [
                file_id for file_id in mentioned if not folder_restricted or file_id in folder_files
            ]
        elif folder_restricted:
            effective_document_ids[kb_id] = sorted(folder_files)
        else:
            effective_document_ids[kb_id] = []
    verbatim_members = [
        member
        for member in verbatim_members
        if not document_filter_active[str(member["kb_id"])]
        or effective_document_ids[str(member["kb_id"])]
    ]
    verbatim_member_kb_ids = {str(member["kb_id"]) for member in verbatim_members}
    # VERBATIM 通道只在调用方携带字面信号（patterns/题型）且租户可解析时启用；
    # 单次跨库任务（本地 PG 查询），不随成员数放大往返。
    verbatim_active = bool(
        verbatim
        and verbatim.get("tenant_id") is not None
        and (verbatim.get("patterns") or verbatim.get("question_types"))
        and verbatim_members
    )
    scope_excluded_kbs: list[str] = []
    for member in members:
        kb_id = member["kb_id"]
        if _member_evidence_denied(member):
            # Authority Gate：派生知识产品不产生任何证据任务；未来由
            # Wiki Navigator 单独供给 navigation_hits（P4）。
            logger.warning(f"Scope member {kb_id} is a derived product; evidence channels skipped")
            continue
        member_document_ids = effective_document_ids.get(str(kb_id)) or []
        member_filter_active = document_filter_active.get(str(kb_id), False)
        if member_filter_active and not member_document_ids:
            # 该库在文献硬约束下不可能有命中：文档任务不开，图谱任务排除。
            scope_excluded_kbs.append(str(kb_id))
            continue
        tasks.append(
            _query_source_with_timeout(
                _query_document_source(member, query_text, file_ids=member_document_ids or None),
                kb_id=kb_id,
                source_type="DOCUMENT",
                timeout_seconds=KNOWLEDGE_DOCUMENT_SOURCE_TIMEOUT_SECONDS,
            )
        )
        task_labels.append((kb_id, "DOCUMENT"))
        if member_filter_active:
            # 图谱/结构化证据不带文献粒度：文献硬约束生效时整通道排除（失败关闭）。
            scope_excluded_kbs.append(str(kb_id))
            continue
        tasks.append(
            _query_source_with_timeout(
                _query_managed_graph_source(member, query_text, limit=per_source_limit),
                kb_id=kb_id,
                source_type="GRAPH_STRUCTURED",
                timeout_seconds=KNOWLEDGE_GRAPH_SOURCE_TIMEOUT_SECONDS,
            )
        )
        task_labels.append((kb_id, "GRAPH_STRUCTURED"))
    if verbatim_active:
        # Run per member so a folder-restricted member can never widen another
        # member's verbatim query (or be widened by an unrestricted member).
        for member in verbatim_members:
            kb_id = str(member["kb_id"])
            member_file_ids = effective_document_ids.get(kb_id) or []
            tasks.append(
                _query_source_with_timeout(
                    _query_verbatim_scope_source(
                        [member],
                        query_text,
                        verbatim=verbatim,
                        limit=per_source_limit,
                        file_ids=member_file_ids if document_filter_active.get(kb_id) else None,
                    ),
                    kb_id=kb_id,
                    source_type="VERBATIM",
                    timeout_seconds=KNOWLEDGE_VERBATIM_SOURCE_TIMEOUT_SECONDS,
                )
            )
            task_labels.append((kb_id, "VERBATIM"))

    results = await asyncio.gather(*tasks, return_exceptions=True) if tasks else []
    all_rows: list[dict[str, Any]] = []
    source_errors: list[str] = []
    verbatim_errors: dict[str, str] = {}
    for (kb_id, source_type), result in zip(task_labels, results):
        if isinstance(result, BaseException):
            if isinstance(result, asyncio.CancelledError):
                raise result
            logger.error(f"Scope source task failed: kb={kb_id}, source={source_type}, error={result}")
            rows, error = [], f"{source_type}_ERROR: {result}"
        else:
            rows, error = result
        if source_type == "VERBATIM":
            # VERBATIM 任务跨成员；错误只记一次，命中行在 merge 阶段回填各 kb telemetry。
            if error:
                verbatim_errors[str(kb_id)] = error
        all_rows.extend(rows)
        if error:
            logger.warning(f"Scope source unavailable: kb={kb_id}, source={source_type}, error={error}")
            source_errors.append(f"{kb_id}/{source_type}: {error}")
    all_rows = _merge_verbatim_rows(all_rows)

    yield_query = is_yield_gene_query(query_text)
    selection_rows = all_rows
    if yield_query:
        eligible_rows = [row for row in all_rows if row.get("claim_eligible") is True]
        if eligible_rows:
            # Document and Graph-only hits remain visible through source/channel
            # telemetry, but must not compete with citable structured evidence
            # in a scientific yield answer.
            selection_rows = eligible_rows
    ranked_evidence, warnings = _deduplicate_and_rerank(
        selection_rows,
        top_k=top_k,
        stratify_yield=yield_query,
    )
    warnings.extend(source_errors)
    if not members:
        warnings.append("当前运行的有效知识范围为空。")
    if not ranked_evidence and members:
        warnings.append("范围内未检索到足以回答该问题的证据。")

    source_usage: dict[str, dict[str, Any]] = {}
    for item in ranked_evidence:
        provenance = item.get("provenance") or []
        if not provenance:
            provenance = [
                {"kb_id": kb_id, "source_type": item.get("source_type")}
                for kb_id in item.get("found_in_kbs") or [item.get("kb_id")]
            ]
        for origin in provenance:
            kb_id = str(origin.get("kb_id") or "").strip()
            if not kb_id:
                continue
            usage = source_usage.setdefault(kb_id, {"kb_id": kb_id, "source_types": set(), "hits": 0})
            source_type = str(origin.get("source_type") or item.get("source_type") or "").strip()
            if source_type:
                usage["source_types"].add(source_type)
            usage["hits"] += 1
    sources_used = [
        {**usage, "source_types": sorted(usage["source_types"])}
        for usage in sorted(source_usage.values(), key=lambda value: value["kb_id"])
    ]

    evidence = [_compact_evidence(item, query_text=query_text) for item in ranked_evidence]
    package: dict[str, list[str]] = {
        "direct_yield": [],
        "condition_specific_yield": [],
        "yield_components": [],
        "grain_filling": [],
        "grain_morphology": [],
        "quality": [],
        "supporting_context": [],
        "candidate": [],
    }
    category_keys = {
        "DIRECT_YIELD": "direct_yield",
        "CONDITION_SPECIFIC_YIELD": "condition_specific_yield",
        "YIELD_COMPONENT": "yield_components",
        "GRAIN_FILLING": "grain_filling",
        "GRAIN_MORPHOLOGY": "grain_morphology",
        "QUALITY": "quality",
    }
    for item in evidence:
        package_key = (
            "candidate"
            if item.get("evidence_status") == "CANDIDATE"
            else category_keys.get(str(item.get("outcome_class") or ""), "supporting_context")
        )
        package[package_key].append(str(item.get("evidence_id")))

    def channel_status(member: dict[str, Any], channel: str) -> str:
        if not member.get(f"{channel}_enabled"):
            return "DISABLED"
        details = member.get("health_details") if isinstance(member.get("health_details"), dict) else {}
        channels = details.get("channels") if isinstance(details.get("channels"), dict) else {}
        channel_details = channels.get(channel) if isinstance(channels.get(channel), dict) else {}
        if channel_details.get("ready"):
            return "AVAILABLE"
        return {
            "document": "NO_DOCUMENTS",
            "graph": "NO_GRAPH",
            "structured": "NO_STRUCTURED_EVIDENCE",
        }[channel]

    verbatim_member_kb_ids = {member["kb_id"] for member in verbatim_members}
    knowledge_source_status = []
    for member in members:
        status_row = {
            "kb_id": member["kb_id"],
            "kb_name": member.get("kb_name") or member["kb_id"],
            "document_status": channel_status(member, "document"),
            "graph_status": channel_status(member, "graph"),
            "structured_status": channel_status(member, "structured"),
            "health_status": member.get("health_status"),
        }
        if verbatim_active and member["kb_id"] in verbatim_member_kb_ids:
            # VERBATIM 状态只在通道实际运行的 Run 里出现（contract 1.1 增量字段）。
            status_row["verbatim_status"] = (
                "UNAVAILABLE" if str(member["kb_id"]) in verbatim_errors else "AVAILABLE"
            )
        member_filter_active = document_filter_active.get(str(member["kb_id"]), False)
        if member_filter_active:
            # 文献硬约束审计：图谱/结构化通道被排除；本库无指定文献时文档通道也不执行。
            status_row["graph_status"] = "DOCUMENT_SCOPE_EXCLUDED"
            status_row["structured_status"] = "DOCUMENT_SCOPE_EXCLUDED"
            if not (effective_document_ids.get(str(member["kb_id"])) or []):
                status_row["document_status"] = "NO_MATCHING_DOCUMENT"
        knowledge_source_status.append(status_row)
    verbatim_hit_count = sum(
        1 for item in evidence if item.get("retrieval_channel") == "VERBATIM" or item.get("verbatim_hit")
    )
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
        "evidence_package": package,
        "retrieval_plan": {
            "intent": "YIELD_GENE_QUERY" if yield_query else "GENERAL_KNOWLEDGE_QUERY",
            "candidate_requested": candidate_explicitly_requested(query_text),
            "layers": [
                "DIRECT_YIELD",
                "CONDITION_SPECIFIC_YIELD",
                "YIELD_COMPONENT",
                "GRAIN_FILLING_OR_MORPHOLOGY",
                "SUPPORTING",
                "CANDIDATE_IF_EXPLICIT",
            ],
        },
        "sources_used": sources_used,
        "knowledge_source_status": knowledge_source_status,
        "document_scope_files": document_scope_files,
        "document_scope_excluded_kbs": sorted(set(scope_excluded_kbs)),
        "retrieval_summary": {
            "query": query_text,
            "raw_hits": len(all_rows),
            "deduplicated_hits": len(evidence),
            "verbatim_hit_count": verbatim_hit_count,
            "web_tool_available": bool(scope_snapshot.get("allow_web", False)),
            "web_call_count": 0,
        },
        "warnings": warnings,
        "answer_instruction": (
            "仅引用 claim_eligible=true 的 evidence_id；按 evidence_package 分层并保留条件、材料和冲突。"
        ),
    }


async def query_single_kb_unified(
    *,
    kb_id: str,
    query_text: str,
    retrieval_params: dict[str, Any] | None = None,
    contract_key: str | None = None,
    top_k: int = 10,
) -> list[dict[str, Any]]:
    """单库统一检索入口：检索测试与评估必须和正式问答走同一套通道实现。

    - 文档契约库：文档通道（kb aquery，检索参数全量透传），行为与直连一致；
    - managed_graph 契约库：复用 scope gateway 的规范图谱通道
      （PostgreSQL 事实源 + 结构化证据），命中以文档 chunk 同构形状返回，
      triple_id/evidence_id 保留在 metadata 与 raw_graph_hit 中，转换过程
      不丢失规范身份。

    返回 list[dict]，文档命中与图谱命中同构：content/score/metadata。
    """
    from yuxi.knowledge.runtime import knowledge_base
    from yuxi.knowledge.source_contracts.gate import load_kb_contract

    if contract_key is None:
        contract_key = (await load_kb_contract(kb_id)).contract_key
    results: list[dict[str, Any]] = []

    if contract_key != "managed_graph":
        doc_hits = await knowledge_base.aquery(query_text, kb_id=kb_id, **(retrieval_params or {}))
        if isinstance(doc_hits, dict):
            doc_hits = doc_hits.get("retrieved_chunks") or []
        for hit in doc_hits or []:
            metadata = dict(hit.get("metadata") or {})
            metadata.setdefault("retrieval_channel", "DOCUMENT")
            results.append({**hit, "metadata": metadata})
        return results

    member = {
        "kb_id": kb_id,
        "kb_name": kb_id,
        "graph_enabled": True,
        "structured_enabled": True,
        "document_enabled": False,
        # 与 knowledge_scope_service 的默认成员策略一致
        "evidence_supporting": True,
        "evidence_candidate": False,
        "priority": 100,
    }
    graph_hits, error = await _query_managed_graph_source(member, query_text, limit=max(int(top_k), 8))
    if error:
        logger.warning(f"Unified graph channel unavailable: kb={kb_id}, error={error}")
    for row in graph_hits:
        provenance = (row.get("provenance") or [{}])[0]
        triple_id = provenance.get("triple_id")
        subject = row.get("subject") or {}
        target = row.get("object") or {}
        headline = f"{subject.get('name')} —{row.get('predicate')}→ {target.get('name')}"
        quote = str(row.get("evidence_quote") or row.get("content") or "").strip()
        content = f"{headline}\n{quote}" if quote else headline
        results.append(
            {
                "content": content,
                "score": float(row.get("raw_score") or 0.0),
                "metadata": {
                    "chunk_id": triple_id or row.get("evidence_id"),
                    "file_id": None,
                    "chunk_index": None,
                    "source": f"规范图谱 · {row.get('source_type')}",
                    "retrieval_channel": row.get("source_type") or "GRAPH_STRUCTURED",
                    "triple_id": triple_id,
                    "evidence_id": row.get("evidence_id"),
                    "entity_source_id": subject.get("id"),
                    "entity_target_id": target.get("id"),
                    "evidence_status": row.get("evidence_status"),
                    "pmid": row.get("pmid"),
                    "doi": row.get("doi"),
                },
                "raw_graph_hit": row,
            }
        )
    results.sort(key=lambda item: float(item.get("score") or 0.0), reverse=True)
    return results[: max(int(top_k), 1)]
