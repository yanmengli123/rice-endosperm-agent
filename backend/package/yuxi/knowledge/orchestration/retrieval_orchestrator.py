from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.knowledge.contracts.schemas import (
    CLAIM_VALIDATOR_VERSION,
    CONTRACT_SCHEMA_VERSION,
    claim_id,
    relation_group,
)
from yuxi.knowledge.planning.entity_resolver import ENTITY_RESOLVER_VERSION, resolve_entities
from yuxi.knowledge.planning.query_planner import PLANNER_VERSION, plan_knowledge_query
from yuxi.knowledge.rendering.structured_renderer import render_structured_rows
from yuxi.knowledge.retrieval.canonical_graph_retriever import retrieve_exact_regulator_enumeration
from yuxi.knowledge.retrieval.neo4j_path_retriever import retrieve_neo4j_paths
from yuxi.knowledge.validation.citation_validator import validate_structured_citations
from yuxi.knowledge.validation.claim_validator import validate_deterministic_claims
from yuxi.knowledge.validation.completeness_validator import validate_completeness
from yuxi.storage.postgres.models_knowledge import KnowledgeRetrievalRun
from yuxi.utils.datetime_utils import utc_now_naive
from yuxi.utils.logging_config import logger

ORCHESTRATOR_VERSION = "1.0"


def _hash_contract(contract: dict[str, Any]) -> str:
    stable = {key: value for key, value in contract.items() if key not in {"retrieval_id", "contract_hash"}}
    raw = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _scope_public(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": "AGENT_RUN_SNAPSHOT",
        "authoritative_for_this_run": True,
        "scope_id": snapshot.get("scope_id"),
        "scope_slug": snapshot.get("scope_slug"),
        "scope_version": snapshot.get("scope_version"),
        "scope_mode": snapshot.get("scope_mode"),
        "knowledge_strategy": snapshot.get("knowledge_strategy"),
        "kb_ids": snapshot.get("effective_kb_ids") or [],
        "retrieval_mode": snapshot.get("retrieval_mode"),
        "allow_web": bool(snapshot.get("allow_web", False)),
    }


def _gateway_contract(result: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    claims: dict[str, dict[str, Any]] = {}
    context_evidence: list[dict[str, Any]] = []
    for evidence in result.get("evidence") or []:
        subject = evidence.get("subject") or {}
        target = evidence.get("object") or {}
        predicate = evidence.get("observed_relation") or evidence.get("predicate")
        if not subject.get("name") or not target.get("name") or not predicate:
            context_evidence.append(evidence)
            continue
        key = claim_id(subject.get("name"), predicate, target.get("name"))
        claim = claims.setdefault(
            key,
            {
                "claim_id": key,
                "subject": subject,
                "predicate": predicate,
                "object": target,
                "relation_group": relation_group(predicate),
                "triple_ids": [],
                "found_in_kbs": [],
                "evidence": [],
                "claim_eligible": bool(evidence.get("claim_eligible")),
            },
        )
        claim["evidence"].append(evidence)
        kb_id = evidence.get("kb_id")
        if kb_id and kb_id not in claim["found_in_kbs"]:
            claim["found_in_kbs"].append(kb_id)
    return list(claims.values()), context_evidence


def _gateway_source_status(result: dict[str, Any]) -> list[dict[str, Any]]:
    used: dict[str, dict[str, int]] = {}
    for item in result.get("sources_used") or []:
        kb_id = str(item.get("kb_id") or "")
        source_types = {str(value).upper() for value in item.get("source_types") or []}
        used[kb_id] = {
            "DOCUMENT": int(item.get("hits") or 0) if "DOCUMENT" in source_types else 0,
            "GRAPH": int(item.get("hits") or 0) if "GRAPH" in source_types else 0,
            "STRUCTURED": int(item.get("hits") or 0) if "STRUCTURED" in source_types else 0,
        }
    rows = []
    for status in result.get("knowledge_source_status") or []:
        kb_id = status.get("kb_id")
        for channel, legacy_key in (
            ("DOCUMENT", "document_status"),
            ("GRAPH", "graph_status"),
            ("STRUCTURED", "structured_status"),
        ):
            legacy_status = status.get(legacy_key)
            available = legacy_status == "AVAILABLE"
            capability_status = (
                "AVAILABLE" if available else ("DISABLED" if legacy_status == "DISABLED" else "UNAVAILABLE")
            )
            rows.append(
                {
                    "kb_id": kb_id,
                    "kb_name": status.get("kb_name") or kb_id,
                    "source": channel,
                    "capability_status": capability_status,
                    "query_status": "SUCCESS" if available else "NOT_QUERIED",
                    "hit_count": used.get(str(kb_id), {}).get(channel, 0) if available else None,
                }
            )
    return rows


async def _persist_audit(
    db: AsyncSession,
    *,
    retrieval_id: str,
    run_id: str | None,
    request_id: str | None,
    snapshot: dict[str, Any],
    plan: dict[str, Any],
    contract: dict[str, Any],
    started_at,
) -> None:
    completeness = contract.get("completeness") or {}
    claims = contract.get("claims") or []
    evidence = contract.get("evidence") or []
    db.add(
        KnowledgeRetrievalRun(
            retrieval_id=retrieval_id,
            run_id=run_id,
            request_id=request_id,
            scope_id=snapshot.get("scope_id"),
            scope_version=snapshot.get("scope_version"),
            knowledge_strategy=str(snapshot.get("knowledge_strategy") or "MODEL_DECIDES"),
            planner_version=PLANNER_VERSION,
            entity_resolver_version=ENTITY_RESOLVER_VERSION,
            retrieval_orchestrator_version=ORCHESTRATOR_VERSION,
            claim_validator_version=CLAIM_VALIDATOR_VERSION,
            contract_schema_version=CONTRACT_SCHEMA_VERSION,
            intent=str(plan.get("intent") or "GENERAL_KNOWLEDGE_QUERY"),
            query_mode=str(plan.get("query_mode") or "BOUNDED"),
            resolved_entity_ids=[item.get("entity_id") for item in contract.get("resolved_entities") or []],
            source_status_json=contract.get("knowledge_source_status") or [],
            expected_relation_count=completeness.get("exact_relation_count"),
            returned_relation_count=completeness.get("returned_relation_count"),
            expected_claim_count=completeness.get("eligible_claim_count"),
            returned_claim_count=completeness.get("returned_claim_count"),
            expected_evidence_count=completeness.get("eligible_evidence_count"),
            returned_evidence_count=completeness.get("returned_evidence_count"),
            claim_ids_json=[item.get("claim_id") for item in claims if item.get("claim_id")],
            evidence_ids_json=[item.get("evidence_id") for item in evidence if item.get("evidence_id")],
            chunk_ids_json=[item.get("chunk_id") for item in evidence if item.get("chunk_id")],
            contract_hash=contract.get("contract_hash"),
            status=str(contract.get("status") or "COMPLETED"),
            warnings_json=contract.get("warnings") or [],
            error_code=contract.get("error_code"),
            started_at=started_at,
            finished_at=utc_now_naive(),
        )
    )
    await db.flush()


async def prepare_knowledge_context(
    db: AsyncSession,
    *,
    question: str,
    scope_snapshot: dict[str, Any],
    run_id: str | None,
    request_id: str | None,
    retrieval_id: str | None = None,
) -> dict[str, Any]:
    retrieval_id = retrieval_id or f"kr_{uuid.uuid4().hex}"
    started_at = utc_now_naive()
    members = [member for member in scope_snapshot.get("members") or [] if isinstance(member, dict)]
    strategy = str(scope_snapshot.get("knowledge_strategy") or "MODEL_DECIDES").upper()
    plan = plan_knowledge_query(question, strategy=strategy, scope_nonempty=bool(members))
    contract: dict[str, Any] = {
        "contract_schema_version": CONTRACT_SCHEMA_VERSION,
        "retrieval_id": retrieval_id,
        "status": "SKIPPED",
        "knowledge_scope_snapshot": _scope_public(scope_snapshot),
        "retrieval_plan": plan,
        "resolved_entities": [],
        "claims": [],
        "evidence": [],
        "context_evidence": [],
        "graph_expansion": {"nodes": [], "edges": []},
        "structured_result": [],
        "knowledge_source_status": [],
        "completeness": {"status": "NOT_APPLICABLE"},
        "validation": {
            "claims": {"status": "NOT_APPLICABLE"},
            "citations": {"status": "NOT_APPLICABLE"},
        },
        "warnings": [],
    }
    if not plan.get("retrieval_required"):
        contract["contract_hash"] = _hash_contract(contract)
        await _persist_audit(
            db,
            retrieval_id=retrieval_id,
            run_id=run_id,
            request_id=request_id,
            snapshot=scope_snapshot,
            plan=plan,
            contract=contract,
            started_at=started_at,
        )
        return contract

    try:
        if plan.get("intent") == "PHENOTYPE_REGULATOR_ENUMERATION" and plan.get("target_mention"):
            async with db.begin_nested():
                resolution = await resolve_entities(
                    db,
                    mention=str(plan["target_mention"]),
                    kb_ids=[str(member["kb_id"]) for member in members],
                )
                contract["resolved_entities"] = resolution.get("entities") or []
                if resolution.get("match_tier") not in {"EXACT_CANONICAL", "EXACT_ALIAS"}:
                    raise LookupError("EXACT_ENTITY_NOT_FOUND")
                if resolution.get("ambiguity"):
                    raise LookupError("AMBIGUOUS_EXACT_ENTITY")
                exact = await retrieve_exact_regulator_enumeration(
                    db,
                    resolved_entities=contract["resolved_entities"],
                    members=members,
                )
            contract.update(
                {
                    "status": "COMPLETED",
                    "claims": exact["claims"],
                    "evidence": exact["evidence"],
                    "knowledge_source_status": exact["source_status"],
                    "completeness": exact["completeness"],
                    "retriever_version": exact["retriever_version"],
                }
            )
            completeness_status, completeness_warnings = validate_completeness(contract["completeness"])
            contract["completeness"]["status"] = completeness_status
            contract["warnings"].extend(completeness_warnings)
            uncited_count = int(contract["completeness"].get("uncited_exact_relation_count") or 0)
            if uncited_count:
                contract["warnings"].append(
                    f"另有 {uncited_count} 条精确图谱关系缺少当前策略允许的合格 Evidence；"
                    "它们已计入关系扫描，但未升级为可引用 Claim。"
                )
        else:
            from yuxi.knowledge.scope_gateway import query_knowledge_scope_gateway

            result = await query_knowledge_scope_gateway(
                query_text=question,
                scope_snapshot=scope_snapshot,
                top_k=int((scope_snapshot.get("retrieval_policy") or {}).get("bounded_top_k") or 12),
            )
            claims, context_evidence = _gateway_contract(result)
            contract.update(
                {
                    "status": "COMPLETED",
                    "claims": claims,
                    "evidence": result.get("evidence") or [],
                    "context_evidence": context_evidence,
                    "knowledge_source_status": _gateway_source_status(result),
                    "completeness": {"status": "NOT_APPLICABLE"},
                    "warnings": result.get("warnings") or [],
                }
            )
            if plan.get("intent") == "MECHANISM_EXPLANATION":
                graph_expansion = await retrieve_neo4j_paths(question=question, members=members)
                contract["graph_expansion"] = {
                    "retriever_version": graph_expansion["retriever_version"],
                    "seeds": graph_expansion["seeds"],
                    "nodes": graph_expansion["nodes"],
                    "edges": graph_expansion["edges"],
                }
                contract["knowledge_source_status"].extend(graph_expansion["source_status"])
        contract["structured_result"] = render_structured_rows(contract["claims"])
        if plan.get("intent") == "PHENOTYPE_REGULATOR_ENUMERATION":
            claim_validation, claim_warnings = validate_deterministic_claims(contract["claims"])
            citation_validation, citation_warnings = validate_structured_citations(
                contract["structured_result"],
                contract["evidence"],
            )
            contract["validation"] = {
                "claims": claim_validation,
                "citations": citation_validation,
            }
            contract["warnings"].extend(claim_warnings + citation_warnings)
            if claim_validation["status"] != "PASS" or citation_validation["status"] != "PASS":
                contract["status"] = "DEGRADED"
                contract["error_code"] = "SCIENTIFIC_CONTRACT_VALIDATION_FAILED"
    except LookupError as exc:
        contract.update(
            {
                "status": "DEGRADED",
                "error_code": str(exc),
                "warnings": [
                    "未找到无歧义的规范实体精确匹配；为避免把 grain weight/yield 等近义概念混入，本次没有宣称枚举完整。"
                ],
                "knowledge_source_status": [
                    {
                        "kb_id": member["kb_id"],
                        "kb_name": member.get("kb_name") or member["kb_id"],
                        "source": "POSTGRES_CANONICAL",
                        "capability_status": "AVAILABLE",
                        "query_status": "ERROR",
                        "hit_count": 0,
                    }
                    for member in members
                ],
                "completeness": {"status": "UNVERIFIED"},
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Knowledge-first retrieval failed: %s", exc)
        contract.update(
            {
                "status": "FAILED",
                "error_code": "CANONICAL_SOURCE_ERROR",
                "warnings": [
                    "PostgreSQL 规范图谱检索失败；系统没有静默改用投影并宣称结果完整。",
                    str(exc),
                ],
                "completeness": {"status": "UNVERIFIED"},
            }
        )

    contract["retrieval_summary"] = {
        "query": question,
        "claim_count": len(contract.get("claims") or []),
        "evidence_count": len(contract.get("evidence") or []),
        "web_call_count": 0,
    }
    contract["answer_instruction"] = (
        "结构化结果和引用由后端生成；模型只负责解释。completeness.status=PASS 仅允许称为"
        "‘全部可引用结果’；all_exact_relations_citable=true 时才可称为‘全部调控基因’。"
    )
    contract["contract_hash"] = _hash_contract(contract)
    await _persist_audit(
        db,
        retrieval_id=retrieval_id,
        run_id=run_id,
        request_id=request_id,
        snapshot=scope_snapshot,
        plan=plan,
        contract=contract,
        started_at=started_at,
    )
    return contract
