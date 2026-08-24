from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from yuxi.knowledge.contracts.schemas import claim_id, evidence_key, relation_group
from yuxi.knowledge.research_evidence import (
    extract_gene_identifiers,
    sanitize_scientific_identifier_notation,
    validate_doi,
    validate_pmid,
)
from yuxi.knowledge.scope_gateway import classify_evidence_status
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeGraphEntity,
    KnowledgeGraphRelationEvidence,
    KnowledgeGraphTriple,
)

CANONICAL_RETRIEVER_VERSION = "1.1"


def _policy_allows(member: dict[str, Any], status: str) -> bool:
    return bool(member.get(f"evidence_{status.casefold()}", False))


def _valid_identifier(evidence: KnowledgeGraphRelationEvidence) -> tuple[str | None, str | None, str]:
    pmid, pmid_status = validate_pmid(evidence.pmid)
    doi, doi_status = validate_doi(evidence.doi)
    if pmid_status == "VALID" or doi_status == "VALID":
        return pmid, doi, "VALID"
    if pmid_status == "MISSING" and doi_status == "MISSING":
        return pmid, doi, "MISSING"
    return pmid, doi, "INVALID_FORMAT"


def _is_gene(entity: KnowledgeGraphEntity) -> bool:
    return "gene" in str(entity.label or "").casefold()


def _public_evidence(
    evidence: KnowledgeGraphRelationEvidence,
    *,
    kb_name: str,
    subject: KnowledgeGraphEntity,
    target: KnowledgeGraphEntity,
    relation_type: str,
    evidence_status: str,
    pmid: str | None,
    doi: str | None,
) -> dict[str, Any]:
    quote = sanitize_scientific_identifier_notation(evidence.evidence_quote)
    return {
        "evidence_id": evidence.evidence_id,
        "kb_id": evidence.kb_id,
        "kb_name": kb_name,
        "source_type": "STRUCTURED",
        "evidence_status": evidence_status,
        "claim_eligible": True,
        "subject": {"id": subject.entity_id, "name": subject.name, "label": subject.label},
        "predicate": relation_type,
        "object": {"id": target.entity_id, "name": target.name, "label": target.label},
        "relation_group": relation_group(relation_type),
        "pmid": pmid,
        "doi": doi,
        "evidence_level": evidence.evidence_level,
        "direction": evidence.direction,
        "directness": evidence.directness,
        "condition": evidence.condition,
        "cultivar": evidence.cultivar,
        "genetic_background": evidence.genetic_background,
        "development_stage": evidence.development_stage,
        "experimental_subject_type": evidence.experimental_subject_type,
        "subject_material": evidence.subject_material,
        "perturbs": evidence.perturbs,
        "perturbation_direction": evidence.perturbation_direction,
        "observed_effect": evidence.observed_effect or evidence.direction,
        "observed_relation": evidence.observed_relation or relation_type,
        "evidence_quote": quote[:600] if quote else None,
    }


async def retrieve_exact_regulator_enumeration(
    db: AsyncSession,
    *,
    resolved_entities: list[dict[str, Any]],
    members: list[dict[str, Any]],
) -> dict[str, Any]:
    """从 PostgreSQL canonical graph 完整枚举一跳 Gene -> target 关系。"""
    member_by_kb = {str(member["kb_id"]): member for member in members}
    target_ids = [str(entity["entity_id"]) for entity in resolved_entities]
    kb_ids = list(member_by_kb)
    source_entity = aliased(KnowledgeGraphEntity)
    target_entity = aliased(KnowledgeGraphEntity)
    stmt = (
        select(KnowledgeGraphTriple, source_entity, target_entity, KnowledgeGraphRelationEvidence)
        .join(source_entity, source_entity.entity_id == KnowledgeGraphTriple.source_entity_id)
        .join(target_entity, target_entity.entity_id == KnowledgeGraphTriple.target_entity_id)
        .outerjoin(
            KnowledgeGraphRelationEvidence,
            KnowledgeGraphRelationEvidence.triple_id == KnowledgeGraphTriple.triple_id,
        )
        .where(
            KnowledgeGraphTriple.kb_id.in_(kb_ids),
            KnowledgeGraphTriple.target_entity_id.in_(target_ids),
        )
        .order_by(
            source_entity.normalized_name,
            KnowledgeGraphTriple.relation_type,
            KnowledgeGraphTriple.triple_id,
            KnowledgeGraphRelationEvidence.evidence_id,
        )
    )
    rows = list((await db.execute(stmt)).all())

    exact_relation_keys: set[str] = set()
    exact_source_identities: set[str] = set()
    relation_triples: dict[str, set[str]] = defaultdict(set)
    relation_kbs: dict[str, set[str]] = defaultdict(set)
    relation_hits_by_kb: dict[str, set[str]] = defaultdict(set)
    eligible_evidence_keys: set[str] = set()
    claim_groups: dict[str, dict[str, Any]] = {}
    evidence_hits_by_kb: dict[str, int] = defaultdict(int)

    for triple, source, target, evidence in rows:
        if not _is_gene(source):
            continue
        source_identity = source.canonical_identity or f"{source.label}:{source.normalized_name}"
        # EntityResolver already established exact normalized-name equivalence. Historical label variants
        # (Phenotype/PHENOTYPE/表型) therefore share one relation universe instead of inflating global counts.
        key = claim_id(source_identity, triple.relation_type, f"exact-target:{target.normalized_name}")
        exact_relation_keys.add(key)
        exact_source_identities.add(source_identity)
        relation_triples[key].add(triple.triple_id)
        relation_kbs[key].add(str(triple.kb_id))
        relation_hits_by_kb[str(triple.kb_id)].add(key)
        if evidence is None:
            continue
        member = member_by_kb.get(str(triple.kb_id))
        if not member or not member.get("structured_enabled"):
            continue
        status = classify_evidence_status(
            evidence.assertion_status,
            evidence.evidence_level,
            evidence.evidence_alignment_status,
            str((source.attributes or {}).get("gene_status") if isinstance(source.attributes, dict) else ""),
        )
        pmid, doi, identifier_status = _valid_identifier(evidence)
        eligible = bool(
            evidence.claim_eligible
            and identifier_status == "VALID"
            and str(evidence.evidence_alignment_status or "").upper() == "ALIGNED"
            and str(evidence.evidence_quote or "").strip()
            and _policy_allows(member, status)
        )
        if not eligible:
            continue

        evidence_hits_by_kb[str(triple.kb_id)] += 1
        claim = claim_groups.setdefault(
            key,
            {
                "claim_id": key,
                "subject": {
                    "id": source.entity_id,
                    "name": source.name,
                    "label": source.label,
                    "canonical_identity": source.canonical_identity,
                },
                "predicate": triple.relation_type,
                "object": {
                    "id": target.entity_id,
                    "name": target.name,
                    "label": target.label,
                    "canonical_identity": target.canonical_identity,
                },
                "relation_group": relation_group(triple.relation_type),
                "triple_ids": [],
                "found_in_kbs": [],
                "evidence": [],
                "claim_eligible": True,
            },
        )
        if triple.triple_id not in claim["triple_ids"]:
            claim["triple_ids"].append(triple.triple_id)
        if triple.kb_id not in claim["found_in_kbs"]:
            claim["found_in_kbs"].append(triple.kb_id)
        public_evidence = _public_evidence(
            evidence,
            kb_name=str(member.get("kb_name") or triple.kb_id),
            subject=source,
            target=target,
            relation_type=triple.relation_type,
            evidence_status=status,
            pmid=pmid,
            doi=doi,
        )
        dedup_key = evidence_key(key, public_evidence)
        eligible_evidence_keys.add(dedup_key)
        if all(item.get("evidence_key") != dedup_key for item in claim["evidence"]):
            public_evidence["evidence_key"] = dedup_key
            claim["evidence"].append(public_evidence)

    for key, claim in claim_groups.items():
        claim["triple_ids"] = sorted(relation_triples[key])
        claim["found_in_kbs"] = sorted(relation_kbs[key])

    relation_order = {
        "FUNCTIONAL_REGULATION": 0,
        "PERTURBATION_EVIDENCE": 1,
        "ASSOCIATION_OR_CONTEXT": 2,
    }
    claims = list(claim_groups.values())
    claims.sort(
        key=lambda claim: (
            relation_order.get(claim["relation_group"], 9),
            str(claim["subject"]["name"]).casefold(),
            str(claim["predicate"]).casefold(),
        )
    )
    evidence = [item for claim in claims for item in claim["evidence"]]
    completeness = {
        "exact_relation_count": len(exact_relation_keys),
        "eligible_claim_count": len(claim_groups),
        "eligible_evidence_count": len(eligible_evidence_keys),
        "unique_source_entity_count": len(exact_source_identities),
        "returned_relation_count": len(exact_relation_keys),
        "returned_claim_count": len(claims),
        "returned_evidence_count": len(evidence),
        "uncited_exact_relation_count": len(exact_relation_keys - set(claim_groups)),
        "assertion_scope": "EVIDENCE_ELIGIBLE_CLAIMS",
    }
    completeness["all_exact_relations_citable"] = completeness["uncited_exact_relation_count"] == 0
    completeness["status"] = (
        "PASS"
        if completeness["eligible_claim_count"] == completeness["returned_claim_count"]
        and completeness["eligible_evidence_count"] == completeness["returned_evidence_count"]
        else "FAIL"
    )

    source_status = []
    for member in members:
        enabled = bool(member.get("graph_enabled") or member.get("structured_enabled"))
        source_status.append(
            {
                "kb_id": member["kb_id"],
                "kb_name": member.get("kb_name") or member["kb_id"],
                "source": "POSTGRES_CANONICAL",
                "capability_status": "AVAILABLE" if enabled else "DISABLED",
                "query_status": "SUCCESS" if enabled else "NOT_QUERIED",
                "hit_count": len(relation_hits_by_kb.get(str(member["kb_id"]), set())) if enabled else None,
                "eligible_evidence_count": (
                    evidence_hits_by_kb.get(str(member["kb_id"]), 0) if member.get("structured_enabled") else None
                ),
            }
        )
    return {
        "claims": claims,
        "evidence": evidence,
        "source_status": source_status,
        "completeness": completeness,
        "retriever_version": CANONICAL_RETRIEVER_VERSION,
    }


async def retrieve_entities_by_identifiers(
    db: AsyncSession,
    *,
    identifiers: list[str],
    members: list[dict[str, Any]],
) -> dict[str, Any]:
    """按 RAP/MSU 标识符精确解析实体，并枚举其全部一跳关系（任意方向、任意实体类型）。

    与 retrieve_exact_regulator_enumeration 的区别：不做 Gene→target 方向限制，
    也不要求 source 必须是基因；用于 ENTITY_LOOKUP 意图的确定性标识符问答。
    """
    member_by_kb = {str(member["kb_id"]): member for member in members}
    kb_ids = list(member_by_kb)

    matched_entities: list[dict[str, Any]] = []
    entity_ids: list[str] = []
    if kb_ids and identifiers:
        stmt = select(KnowledgeGraphEntity).where(
            KnowledgeGraphEntity.kb_id.in_(kb_ids),
            func.lower(KnowledgeGraphEntity.canonical_identity).in_([item.casefold() for item in identifiers]),
        )
        entities = list((await db.execute(stmt)).scalars().all())
        for entity in entities:
            matched_entities.append(
                {
                    "entity_id": entity.entity_id,
                    "name": entity.name,
                    "label": entity.label,
                    "canonical_identity": entity.canonical_identity,
                }
            )
            entity_ids.append(entity.entity_id)

    relation_triples: dict[str, set[str]] = defaultdict(set)
    relation_kbs: dict[str, set[str]] = defaultdict(set)
    relation_hits_by_kb: dict[str, set[str]] = defaultdict(set)
    eligible_evidence_keys: set[str] = set()
    claim_groups: dict[str, dict[str, Any]] = {}
    evidence_hits_by_kb: dict[str, int] = defaultdict(int)
    claims: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []

    if entity_ids:
        source_entity = aliased(KnowledgeGraphEntity)
        target_entity = aliased(KnowledgeGraphEntity)
        stmt = (
            select(KnowledgeGraphTriple, source_entity, target_entity, KnowledgeGraphRelationEvidence)
            .join(source_entity, source_entity.entity_id == KnowledgeGraphTriple.source_entity_id)
            .join(target_entity, target_entity.entity_id == KnowledgeGraphTriple.target_entity_id)
            .outerjoin(
                KnowledgeGraphRelationEvidence,
                KnowledgeGraphRelationEvidence.triple_id == KnowledgeGraphTriple.triple_id,
            )
            .where(
                KnowledgeGraphTriple.kb_id.in_(kb_ids),
                or_(
                    KnowledgeGraphTriple.source_entity_id.in_(entity_ids),
                    KnowledgeGraphTriple.target_entity_id.in_(entity_ids),
                ),
            )
            .order_by(
                source_entity.normalized_name,
                KnowledgeGraphTriple.relation_type,
                KnowledgeGraphTriple.triple_id,
                KnowledgeGraphRelationEvidence.evidence_id,
            )
        )
        rows = list((await db.execute(stmt)).all())

        for triple, source, target, evidence_row in rows:
            source_identity = source.canonical_identity or f"{source.label}:{source.normalized_name}"
            key = claim_id(source_identity, triple.relation_type, f"{target.label}:{target.normalized_name}")
            relation_triples[key].add(triple.triple_id)
            relation_kbs[key].add(str(triple.kb_id))
            relation_hits_by_kb[str(triple.kb_id)].add(key)
            if evidence_row is None:
                continue
            member = member_by_kb.get(str(triple.kb_id))
            if not member or not member.get("structured_enabled"):
                continue
            status = classify_evidence_status(
                evidence_row.assertion_status,
                evidence_row.evidence_level,
                evidence_row.evidence_alignment_status,
                str((source.attributes or {}).get("gene_status") if isinstance(source.attributes, dict) else ""),
            )
            pmid, doi, identifier_status = _valid_identifier(evidence_row)
            eligible = bool(
                evidence_row.claim_eligible
                and identifier_status == "VALID"
                and str(evidence_row.evidence_alignment_status or "").upper() == "ALIGNED"
                and str(evidence_row.evidence_quote or "").strip()
                and _policy_allows(member, status)
            )
            if not eligible:
                continue

            evidence_hits_by_kb[str(triple.kb_id)] += 1
            claim = claim_groups.setdefault(
                key,
                {
                    "claim_id": key,
                    "subject": {
                        "id": source.entity_id,
                        "name": source.name,
                        "label": source.label,
                        "canonical_identity": source.canonical_identity,
                    },
                    "predicate": triple.relation_type,
                    "object": {
                        "id": target.entity_id,
                        "name": target.name,
                        "label": target.label,
                        "canonical_identity": target.canonical_identity,
                    },
                    "relation_group": relation_group(triple.relation_type),
                    "triple_ids": [],
                    "found_in_kbs": [],
                    "evidence": [],
                    "claim_eligible": True,
                },
            )
            if triple.triple_id not in claim["triple_ids"]:
                claim["triple_ids"].append(triple.triple_id)
            if triple.kb_id not in claim["found_in_kbs"]:
                claim["found_in_kbs"].append(triple.kb_id)
            public_evidence = _public_evidence(
                evidence_row,
                kb_name=str(member.get("kb_name") or triple.kb_id),
                subject=source,
                target=target,
                relation_type=triple.relation_type,
                evidence_status=status,
                pmid=pmid,
                doi=doi,
            )
            dedup_key = evidence_key(key, public_evidence)
            eligible_evidence_keys.add(dedup_key)
            if all(item.get("evidence_key") != dedup_key for item in claim["evidence"]):
                public_evidence["evidence_key"] = dedup_key
                claim["evidence"].append(public_evidence)

        claims = list(claim_groups.values())
        claims.sort(
            key=lambda claim: (
                str(claim["subject"]["name"]).casefold(),
                str(claim["predicate"]).casefold(),
            )
        )
        evidence = [item for claim in claims for item in claim["evidence"]]

    completeness = {
        "identifier_count": len(identifiers),
        "matched_entity_count": len(matched_entities),
        "eligible_claim_count": len(claim_groups),
        "eligible_evidence_count": len(eligible_evidence_keys),
        "returned_claim_count": len(claims),
        "returned_evidence_count": len(evidence),
        "assertion_scope": "IDENTIFIER_ENTITY_LOOKUP",
    }
    completeness["status"] = (
        "PASS"
        if completeness["eligible_claim_count"] == completeness["returned_claim_count"]
        and completeness["eligible_evidence_count"] == completeness["returned_evidence_count"]
        else "FAIL"
    )

    source_status = []
    for member in members:
        enabled = bool(member.get("graph_enabled") or member.get("structured_enabled"))
        source_status.append(
            {
                "kb_id": member["kb_id"],
                "kb_name": member.get("kb_name") or member["kb_id"],
                "source": "POSTGRES_CANONICAL",
                "capability_status": "AVAILABLE" if enabled else "DISABLED",
                "query_status": "SUCCESS" if enabled else "NOT_QUERIED",
                "hit_count": len(relation_hits_by_kb.get(str(member["kb_id"]), set())) if enabled else None,
                "eligible_evidence_count": (
                    evidence_hits_by_kb.get(str(member["kb_id"]), 0) if member.get("structured_enabled") else None
                ),
            }
        )
    return {
        "claims": claims,
        "evidence": evidence,
        "matched_entities": matched_entities,
        "source_status": source_status,
        "completeness": completeness,
        "retriever_version": CANONICAL_RETRIEVER_VERSION,
    }


__all__ = [
    "CANONICAL_RETRIEVER_VERSION",
    "extract_gene_identifiers",
    "retrieve_entities_by_identifiers",
    "retrieve_exact_regulator_enumeration",
]
