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
                for field in ("evidence_id", "pmid", "doi", "kb_id", "evidence_level", "condition")
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


def _compact_evidence(row: dict[str, Any]) -> dict[str, Any]:
    """Return the claim/renderer contract without internal ranking baggage."""

    def clipped(value: Any, limit: int = 420) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        return text if len(text) <= limit else f"{text[: limit - 1].rstrip()}…"

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
    }
    return {key: value for key, value in compact.items() if value not in (None, "", [], {}) or isinstance(value, bool)}


async def query_knowledge_scope_gateway(
    *, query_text: str, scope_snapshot: dict[str, Any], top_k: int = 12
) -> dict[str, Any]:
    members = [member for member in scope_snapshot.get("members") or [] if isinstance(member, dict)]
    top_k = min(max(int(top_k), 1), 12)
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
    source_errors: list[str] = []
    for (kb_id, source_type), (rows, error) in zip(task_labels, results):
        all_rows.extend(rows)
        if error:
            logger.warning(f"Scope source unavailable: kb={kb_id}, source={source_type}, error={error}")
            source_errors.append(f"{kb_id}/{source_type}: {error}")

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

    evidence = [_compact_evidence(item) for item in ranked_evidence]
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

    knowledge_source_status = [
        {
            "kb_id": member["kb_id"],
            "kb_name": member.get("kb_name") or member["kb_id"],
            "document_status": channel_status(member, "document"),
            "graph_status": channel_status(member, "graph"),
            "structured_status": channel_status(member, "structured"),
            "health_status": member.get("health_status"),
        }
        for member in members
    ]
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
        "retrieval_summary": {
            "query": query_text,
            "raw_hits": len(all_rows),
            "deduplicated_hits": len(evidence),
            "web_tool_available": bool(scope_snapshot.get("allow_web", False)),
            "web_call_count": 0,
        },
        "warnings": warnings,
        "answer_instruction": (
            "仅引用 claim_eligible=true 的 evidence_id；按 evidence_package 分层并保留条件、材料和冲突。"
        ),
    }
