from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.knowledge.contracts.schemas import (
    CLAIM_VALIDATOR_VERSION,
    CONTRACT_SCHEMA_VERSION,
    claim_id,
    relation_group,
)
from yuxi.knowledge.evidence.verbatim import extract_verbatim_patterns
from yuxi.knowledge.planning.entity_resolver import ENTITY_RESOLVER_VERSION, resolve_entities
from yuxi.knowledge.planning.query_planner import PLANNER_VERSION, plan_knowledge_query
from yuxi.knowledge.products.registry import is_derived_product
from yuxi.knowledge.rendering.structured_renderer import render_structured_rows
from yuxi.knowledge.retrieval.canonical_graph_retriever import (
    extract_gene_identifiers,
    retrieve_entities_by_identifiers,
    retrieve_exact_regulator_enumeration,
)
from yuxi.knowledge.retrieval.neo4j_path_retriever import retrieve_neo4j_paths
from yuxi.knowledge.validation.citation_validator import validate_structured_citations
from yuxi.knowledge.validation.claim_validator import validate_deterministic_claims
from yuxi.knowledge.validation.completeness_validator import validate_completeness
from yuxi.storage.postgres.models_knowledge import KnowledgeRetrievalRun
from yuxi.utils.datetime_utils import utc_now_naive
from yuxi.utils.logging_config import logger

ORCHESTRATOR_VERSION = "1.0"


def _merge_gateway_results(
    baseline: dict[str, Any],
    guided: dict[str, Any] | None,
    *,
    limit: int,
) -> dict[str, Any]:
    """Merge raw retrieval paths while keeping Wiki hits outside evidence."""
    if not guided:
        return baseline
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path, result in (("BASELINE", baseline), ("WIKI_GUIDED", guided)):
        for row in result.get("evidence") or []:
            key = str(row.get("evidence_id") or row.get("chunk_id") or "") or _hash_contract(row)
            if key in seen:
                continue
            seen.add(key)
            copied = dict(row)
            copied["retrieval_path"] = path
            merged.append(copied)
    merged = merged[: max(1, min(int(limit), 24))]
    warnings = list(dict.fromkeys([*(baseline.get("warnings") or []), *(guided.get("warnings") or [])]))
    sources: dict[str, dict[str, Any]] = {}
    for result in (baseline, guided):
        for item in result.get("sources_used") or []:
            kb_id = str(item.get("kb_id") or "")
            if not kb_id:
                continue
            aggregate = sources.setdefault(kb_id, {"kb_id": kb_id, "source_types": set(), "hits": 0})
            aggregate["source_types"].update(item.get("source_types") or [])
            aggregate["hits"] += int(item.get("hits") or 0)
    return {
        **baseline,
        "evidence": merged,
        "warnings": warnings,
        "sources_used": [
            {**item, "source_types": sorted(item["source_types"])}
            for item in sorted(sources.values(), key=lambda row: row["kb_id"])
        ],
        "retrieval_summary": {
            **(baseline.get("retrieval_summary") or {}),
            "baseline_hits": len(baseline.get("evidence") or []),
            "wiki_guided_hits": len(guided.get("evidence") or []),
            "deduplicated_hits": len(merged),
        },
    }


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
            "VERBATIM": int(item.get("hits") or 0) if "VERBATIM" in source_types else 0,
        }
    rows = []
    for status in result.get("knowledge_source_status") or []:
        kb_id = status.get("kb_id")
        for channel, legacy_key in (
            ("DOCUMENT", "document_status"),
            ("GRAPH", "graph_status"),
            ("STRUCTURED", "structured_status"),
            ("VERBATIM", "verbatim_status"),
        ):
            legacy_status = status.get(legacy_key)
            if legacy_status is None:
                # VERBATIM 只在通道实际运行的 Run 里携带该键，缺席即不产审计行。
                continue
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


def _emit_knowledge_trace(
    retrieval_id: str,
    contract: dict[str, Any],
    started_at,
    *,
    skipped: bool = False,
) -> None:
    """把一次知识检索的事实投影成 trace 事件（无活跃 recorder 时 no-op）。

    详情（chunk/claim/evidence/DOI/页码）永远留在 KnowledgeRetrievalRun 审计表，
    trace 只保存摘要计数与 resource_ref 引用。
    """
    from yuxi.trace import emit_trace

    summary_obj = contract.get("retrieval_summary") or {}
    completeness = contract.get("completeness") or {}
    status = str(contract.get("status") or "COMPLETED")
    suffix = "skipped" if skipped else ("failed" if status == "FAILED" else "completed")
    duration_ms = int((utc_now_naive() - started_at).total_seconds() * 1000)
    attributes = {
        "claim_count": summary_obj.get("claim_count"),
        "evidence_count": summary_obj.get("evidence_count"),
        "verbatim_hit_count": summary_obj.get("verbatim_hit_count"),
        "wiki_navigation_hit_count": summary_obj.get("wiki_navigation_hit_count"),
        "intent": (contract.get("retrieval_plan") or {}).get("intent"),
        "contract_status": status,
        "completeness_status": completeness.get("status"),
        "warning_count": len(contract.get("warnings") or []),
    }
    error_code = contract.get("error_code")
    if error_code:
        attributes["error_code"] = str(error_code)
    scope_snapshot = contract.get("knowledge_scope_snapshot") or {}
    if scope_snapshot.get("scope_version") is not None:
        attributes["knowledge_scope_version"] = scope_snapshot.get("scope_version")
    if skipped:
        summary_text = "判定无需知识检索（闲聊/通用问题），已跳过并留痕"
    elif status == "FAILED":
        summary_text = f"知识检索失败：{error_code or 'UNKNOWN'}"
    else:
        summary_text = (
            f"召回 {summary_obj.get('evidence_count') or 0} 条证据、"
            f"{summary_obj.get('claim_count') or 0} 条 Claim，"
            f"Wiki 导航命中 {summary_obj.get('wiki_navigation_hit_count') or 0} 次"
        )
        verbatim_hits = summary_obj.get("verbatim_hit_count") or 0
        if verbatim_hits:
            summary_text += f"，VERBATIM 精确命中 {verbatim_hits} 次"
    emit_trace(
        category="KNOWLEDGE",
        operation="search",
        event_type=f"knowledge.search.{suffix}",
        span_id=None if skipped else retrieval_id,
        title="知识检索",
        summary=summary_text,
        duration_ms=duration_ms,
        attributes=attributes,
        resource_refs=[{"type": "knowledge_retrieval", "id": retrieval_id}],
        visibility="ADMIN" if skipped else "USER",
    )


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
            locator_resolution_json=contract.get("locator_resolution"),
            contract_hash=contract.get("contract_hash"),
            status=str(contract.get("status") or "COMPLETED"),
            warnings_json=contract.get("warnings") or [],
            error_code=contract.get("error_code"),
            started_at=started_at,
            finished_at=utc_now_naive(),
        )
    )
    await db.flush()


def _locator_verbatim_windows(quote_text: str) -> list[str]:
    """把定位引文切成 ≤128 字符的 ILIKE 窗口（取标识符密集的句段，上限 2 个）。

    长引文不能整段做 ILIKE（上限 128），也不能只取开头（方法句开头套路化）；
    按句切分后优先含数字/连字符标识符的句段，归一化空白后交给 VERBATIM 通道。
    """
    import re as _re

    from yuxi.knowledge.evidence.verbatim import MAX_VERBATIM_PATTERN_CHARS

    sentences = [s.strip() for s in _re.split(r"[.。]", str(quote_text or "")) if s.strip()]
    windows: list[str] = []
    for sentence in sentences:
        if len(sentence) <= MAX_VERBATIM_PATTERN_CHARS:
            candidate = _re.sub(r"\s+", " ", sentence)
        else:
            # 长句取中段 120 字符窗口
            start = (len(sentence) - 120) // 2
            candidate = _re.sub(r"\s+", " ", sentence[start : start + 120].strip())
        if len(candidate) >= 16 and candidate not in windows:
            windows.append(candidate)
        if len(windows) >= 2:
            break
    return windows


def _freeze_locator_evidence(contract: dict[str, Any], row: dict[str, Any] | None) -> None:
    """Prepend one verified locator row to both answer and validation evidence."""
    if not row or not row.get("evidence_id"):
        return
    for key in ("evidence", "context_evidence"):
        existing = [item for item in contract.get(key) or [] if isinstance(item, dict)]
        if not any(item.get("evidence_id") == row["evidence_id"] for item in existing):
            contract[key] = [row, *existing]
    contract["warnings"] = [
        warning
        for warning in contract.get("warnings") or []
        if warning != "范围内未检索到足以回答该问题的证据。"
    ]


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
    raw_members = [member for member in members if not is_derived_product(str(member.get("kb_type") or ""))]
    wiki_members = [
        member
        for member in members
        if is_derived_product(str(member.get("kb_type") or "")) and member.get("wiki_navigation_enabled")
    ]
    strategy = str(scope_snapshot.get("knowledge_strategy") or "MODEL_DECIDES").upper()
    plan = plan_knowledge_query(question, strategy=strategy, scope_nonempty=bool(raw_members))
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
        "wiki_navigation_hits": [],
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
    # 原句定位（单一证据集原则）：常规召回负责解释上下文，确定性定位器负责
    # 把精确 ``span + anchor`` 补成一条正式 evidence row；随后统一冻结引用池，
    # 页码裁决和状态模块投影都只读取该集合。定位器不能绕开集合直接回答。
    from yuxi.knowledge.evidence.quote_locator import (
        LOCATOR_KIND_QUOTE,
        detect_locator_intent,
    )

    locator_intent = detect_locator_intent(question)
    contract["locator_intent"] = locator_intent
    locator_pending = locator_intent.get("kind") == LOCATOR_KIND_QUOTE and bool(raw_members)
    direct_locator: dict[str, Any] | None = None
    locator_evidence: dict[str, Any] | None = None
    if locator_pending:
        # 定位/复合解释都需要检索证据集：无论知识策略如何都必须执行检索
        # （MODEL_DECIDES 下本可不检索，定位问题不能跟着 SKIPPED）。
        plan = {**plan, "retrieval_required": True}
        from yuxi.knowledge.evidence.quote_locator import resolve_quote_locator

        direct_locator = await resolve_quote_locator(
            db,
            question=question,
            kb_ids=[str(member["kb_id"]) for member in raw_members],
        )
        if direct_locator.get("status") == "VERIFIED":
            locator_evidence = {
                "evidence_id": direct_locator.get("evidence_id"),
                "span_evidence_id": direct_locator.get("span_evidence_id"),
                "source_type": "DOCUMENT",
                "evidence_status": "SUPPORTING",
                "retrieval_channel": "QUOTE_LOCATOR",
                "kb_id": direct_locator.get("kb_id"),
                "file_id": direct_locator.get("file_id"),
                "parse_revision_id": direct_locator.get("parse_revision_id"),
                "index_revision_id": direct_locator.get("index_revision_id"),
                "source_sha256": direct_locator.get("source_sha256"),
                "span_id": direct_locator.get("span_id"),
                "evidence_type": direct_locator.get("evidence_type"),
                "anchor_id": direct_locator.get("anchor_id"),
                "page_number": direct_locator.get("page"),
                "document_partition": direct_locator.get("zone"),
                "evidence_quote": direct_locator.get("quote") or direct_locator.get("quote_head"),
                "claim_eligible": False,
                "match_tier": "DETERMINISTIC_QUOTE_LOCATOR",
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
        _emit_knowledge_trace(retrieval_id, contract, started_at, skipped=True)
        return contract

    from yuxi.trace import emit_trace

    emit_trace(
        category="KNOWLEDGE",
        operation="search",
        event_type="knowledge.search.started",
        span_id=retrieval_id,
        title="知识检索",
        attributes={
            "intent": plan.get("intent"),
            "knowledge_scope_version": scope_snapshot.get("scope_version"),
        },
    )

    try:
        navigation_status: list[dict[str, Any]] = []
        scope_tenant_id = scope_snapshot.get("tenant_id")
        if wiki_members and scope_tenant_id is not None:
            from yuxi.services.wiki_service import navigate_scope_wikis

            navigation_hits, navigation_status = await navigate_scope_wikis(
                db,
                tenant_id=int(scope_tenant_id),
                question=question,
                wiki_members=wiki_members,
                raw_members=raw_members,
            )
            contract["wiki_navigation_hits"] = [item.to_dict() for item in navigation_hits]
        elif wiki_members:
            contract["warnings"].append("当前运行快照缺少租户标识，Wiki 导航已失败关闭；原始证据检索不受影响。")
        # 定位意图优先于枚举/实体查询分支：引文里出现基因名是常态（2026-09
        # BiFC 事故：含 OsMYB73 的定位问句被 ENTITY_LOOKUP 劫持，检索证据集
        # 为空导致定位失败关闭）；定位必须走 gateway 检索并注入 VERBATIM 窗口。
        if locator_pending:
            plan = {**plan, "intent": "GENERAL_KNOWLEDGE_QUERY"}
        if plan.get("intent") == "PHENOTYPE_REGULATOR_ENUMERATION" and plan.get("target_mention"):
            async with db.begin_nested():
                resolution = await resolve_entities(
                    db,
                    mention=str(plan["target_mention"]),
                    kb_ids=[str(member["kb_id"]) for member in raw_members],
                )
                contract["resolved_entities"] = resolution.get("entities") or []
                if resolution.get("match_tier") not in {"EXACT_CANONICAL", "EXACT_ALIAS"}:
                    raise LookupError("EXACT_ENTITY_NOT_FOUND")
                if resolution.get("ambiguity"):
                    raise LookupError("AMBIGUOUS_EXACT_ENTITY")
                exact = await retrieve_exact_regulator_enumeration(
                    db,
                    resolved_entities=contract["resolved_entities"],
                    members=raw_members,
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
        elif plan.get("intent") == "ENTITY_LOOKUP":
            identifiers = extract_gene_identifiers(question)
            if not identifiers:
                raise LookupError("GENE_IDENTIFIER_NOT_FOUND")
            async with db.begin_nested():
                lookup = await retrieve_entities_by_identifiers(
                    db,
                    identifiers=identifiers,
                    members=raw_members,
                )
            if not lookup["matched_entities"]:
                raise LookupError("EXACT_ENTITY_NOT_FOUND")
            contract["resolved_entities"] = lookup["matched_entities"]
            contract.update(
                {
                    "status": "COMPLETED",
                    "claims": lookup["claims"],
                    "evidence": lookup["evidence"],
                    "knowledge_source_status": lookup["source_status"],
                    "completeness": lookup["completeness"],
                    "retriever_version": lookup["retriever_version"],
                }
            )
            completeness_status, completeness_warnings = validate_completeness(contract["completeness"])
            contract["completeness"]["status"] = completeness_status
            contract["warnings"].extend(completeness_warnings)
            if not lookup["claims"]:
                contract["warnings"].append(
                    f"标识符 {', '.join(identifiers)} 已精确匹配到规范实体，但当前策略下没有可引用的一跳关系证据。"
                )
        else:
            from yuxi.knowledge.scope_gateway import query_knowledge_scope_gateway, query_verbatim_for_scope

            raw_scope_snapshot = {**scope_snapshot, "members": raw_members}
            top_k = int((scope_snapshot.get("retrieval_policy") or {}).get("bounded_top_k") or 12)
            # 题型自适应召回（P2-12）：MULTI_HOP 需要跨章节/跨实体的证据并集，
            # 扩召回后仍由 per-file/per-section 多样性与 rerank 收敛。
            if "MULTI_HOP" in (plan.get("question_types") or []):
                top_k = min(top_k + 8, 24)
            # VERBATIM 通道（P0-P2）：携带字面信号（模式/题型）且租户可解析时，
            # 随 baseline 一次执行；guided 复跑会重复 PG 扫描，跳过（evidence_id 去重）。
            verbatim_question_types = list(plan.get("question_types") or [])
            verbatim_patterns = extract_verbatim_patterns(question)
            # 定位意图：引文本身就是最强字面信号，注入 VERBATIM 通道保证目标句
            # 进入检索证据集（单一证据集原则下，池里没有就答不了页码）。
            # ILIKE 模式上限 128 字符，取引文的稳定中段窗口（避开句首套路化开头）。
            locator_quote = (locator_intent or {}).get("quote_text")
            if locator_pending and locator_quote:
                for pattern in _locator_verbatim_windows(locator_quote):
                    if pattern not in verbatim_patterns:
                        verbatim_patterns.append(pattern)
            verbatim_config = None
            if scope_tenant_id is not None and (
                verbatim_patterns
                or {"NUMERIC", "CITATION", "FIGURE", "TABLE", "ENTITY", "VERBATIM"} & set(verbatim_question_types)
            ):
                verbatim_config = {
                    "tenant_id": int(scope_tenant_id),
                    "question_types": verbatim_question_types,
                    "patterns": verbatim_patterns,
                }
            baseline = await query_knowledge_scope_gateway(
                query_text=question,
                scope_snapshot=raw_scope_snapshot,
                top_k=top_k,
                verbatim=verbatim_config,
            )
            expansion_terms = list(
                dict.fromkeys(
                    str(term).strip()
                    for hit in contract.get("wiki_navigation_hits") or []
                    for term in hit.get("expansion_terms") or []
                    if str(term).strip() and str(term).casefold() not in question.casefold()
                )
            )[:12]
            guided = None
            if expansion_terms:
                guided = await query_knowledge_scope_gateway(
                    query_text=f"{question}\n导航扩展词：{' '.join(expansion_terms)}",
                    scope_snapshot=raw_scope_snapshot,
                    top_k=top_k,
                )
            result = _merge_gateway_results(baseline, guided, limit=min(top_k * 2, 24))
            # P2 低置信兜底：语义召回完全为空、且 VERBATIM 尚未随 baseline 执行时，
            # 用问题里的字面信号补发一次精确检索（确定性、不扩范围、不产生 Claim）。
            if not (result.get("evidence") or []) and verbatim_config is None and verbatim_patterns:
                fallback = await query_verbatim_for_scope(
                    query_text=question,
                    scope_snapshot=raw_scope_snapshot,
                    patterns=verbatim_patterns,
                    question_types=verbatim_question_types,
                    top_k=top_k,
                )
                if fallback.get("evidence"):
                    result = {
                        **result,
                        "evidence": fallback["evidence"],
                        "warnings": [
                            *(result.get("warnings") or []),
                            "语义召回为空，已启用 VERBATIM 字面精确检索兜底。",
                            *(fallback.get("warnings") or []),
                        ],
                    }
            claims, context_evidence = _gateway_contract(result)
            contract.update(
                {
                    "status": "COMPLETED",
                    "claims": claims,
                    "evidence": result.get("evidence") or [],
                    "context_evidence": context_evidence,
                    "knowledge_source_status": [*_gateway_source_status(result), *navigation_status],
                    "completeness": {"status": "NOT_APPLICABLE"},
                    "warnings": result.get("warnings") or [],
                }
            )
            if plan.get("intent") == "MECHANISM_EXPLANATION":
                graph_expansion = await retrieve_neo4j_paths(question=question, members=raw_members)
                contract["graph_expansion"] = {
                    "retriever_version": graph_expansion["retriever_version"],
                    "seeds": graph_expansion["seeds"],
                    "nodes": graph_expansion["nodes"],
                    "edges": graph_expansion["edges"],
                }
                contract["knowledge_source_status"].extend(graph_expansion["source_status"])
        # The exact physical row is part of the frozen evidence set before
        # context validation, so the model sees the same evidence later replayed
        # by the run status panel.
        _freeze_locator_evidence(contract, locator_evidence)
        existing_status_keys = {
            (str(item.get("kb_id") or ""), str(item.get("source") or ""))
            for item in contract.get("knowledge_source_status") or []
        }
        contract["knowledge_source_status"].extend(
            item
            for item in navigation_status
            if (str(item.get("kb_id") or ""), str(item.get("source") or "")) not in existing_status_keys
        )
        contract["structured_result"] = render_structured_rows(contract["claims"])
        if plan.get("intent") in {"PHENOTYPE_REGULATOR_ENUMERATION", "ENTITY_LOOKUP"}:
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
        else:
            # 通用分支（P2-14）：context_evidence 同样过确定性验证——
            # 唯一性/非空/非派生来源/问题标识符覆盖，结果进 contract 供审计与答案校验。
            from yuxi.knowledge.validation.context_evidence_validator import validate_context_evidence

            derived_kb_ids = {
                str(member["kb_id"]) for member in members if is_derived_product(str(member.get("kb_type") or ""))
            }
            required_identifiers = [
                token
                for token in re.findall(r"(?u)\b[\w.-]{3,}\b", question)
                if any(char.isdigit() or char.isupper() for char in token)
            ]
            context_validation, context_warnings = validate_context_evidence(
                contract["context_evidence"],
                derived_kb_ids=derived_kb_ids,
                required_identifiers=required_identifiers[:8],
            )
            contract["validation"] = {"context_evidence": context_validation}
            contract["warnings"].extend(context_warnings)
            if context_validation["status"] == "FAIL":
                contract["status"] = "DEGRADED"
                contract["error_code"] = "CONTEXT_EVIDENCE_VALIDATION_FAILED"
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
                    for member in raw_members
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

    # Catastrophic failure in a secondary source must not discard an already
    # verified physical row. Its answer/status projection remains auditable,
    # while the overall contract keeps the retrieval failure status.
    _freeze_locator_evidence(contract, locator_evidence)

    # 引用通道（citation channel）：页码/锚点的唯一权威来源。构建失败时返回空列表
    # 走失败关闭——模型没有可引用页码，输出门禁仍会剥离其自写的裸页码。
    from yuxi.knowledge.rendering.citation_channel import build_citations_for_contract

    contract["citations"] = await build_citations_for_contract(db, contract.get("evidence") or [])
    logger.info(
        f"CITATION_CHANNEL_PROBE retrieval={retrieval_id} evidence={len(contract.get('evidence') or [])} "
        f"citations={len(contract.get('citations') or [])}"
    )

    # 单一证据集定位：在与状态模块同一行的引用池上解析（含图注反链由
    # 命中锚点是否 caption 触发的审计兜底跳过——引用池定位不反查正文，
    # 反链解释依据仍来自检索证据本身）。
    if locator_pending and (locator_intent.get("quote_text") or not locator_intent.get("compound")):
        from yuxi.knowledge.evidence.quote_locator import resolve_quote_locator_from_citations

        if direct_locator and direct_locator.get("status") == "MULTIPLE_MATCHES":
            # 全冻结范围已确认跨物理位置重复；Top-K 即使只召回其中一条也不得
            # 把歧义错误收缩成唯一页码。
            locator_resolution = direct_locator
        else:
            locator_resolution = resolve_quote_locator_from_citations(
                quote_text=locator_intent.get("quote_text") or "",
                citations=contract.get("citations") or [],
                partition_intent=locator_intent.get("partition_intent"),
            )
            if (
                locator_resolution.get("status") == "VERIFIED"
                and direct_locator
                and direct_locator.get("status") == "VERIFIED"
                and (
                    locator_resolution.get("evidence_id") == direct_locator.get("evidence_id")
                    or (
                        locator_resolution.get("parse_revision_id") == direct_locator.get("parse_revision_id")
                        and locator_resolution.get("file_id") == direct_locator.get("file_id")
                        and locator_resolution.get("anchor_id") == direct_locator.get("anchor_id")
                        and locator_resolution.get("page") == direct_locator.get("page")
                    )
                )
            ):
                locator_resolution["backlinks"] = direct_locator.get("backlinks") or []
        contract["locator_resolution"] = locator_resolution
        if locator_intent.get("compound"):
            contract["retrieval_plan"] = {
                **contract["retrieval_plan"],
                "intent": "QUOTE_LOCATOR",
                "answer_mode": (
                    "LOCATOR_GROUNDED_ANSWER"
                    if locator_resolution.get("status") == "VERIFIED"
                    else "LOCATOR_UNRESOLVED_ANSWER"
                ),
            }
            contract["warnings"] = [
                *(contract.get("warnings") or []),
                "复合意图：页码部分由确定性定位器在检索证据集内解析（"
                + (
                    "已验证"
                    if locator_resolution.get("status") == "VERIFIED"
                    else "未通过验证，失败关闭不展示页码"
                )
                + "），其余子意图基于检索证据回答。",
            ]
            if locator_resolution.get("status") != "VERIFIED":
                contract["status"] = "DEGRADED"
                contract["error_code"] = (
                    "QUOTE_LOCATOR_MULTIPLE_MATCHES"
                    if locator_resolution.get("status") == "MULTIPLE_MATCHES"
                    else "QUOTE_LOCATOR_NOT_FOUND"
                )
        else:
            contract["retrieval_plan"] = {
                **contract["retrieval_plan"],
                "intent": "QUOTE_LOCATOR",
                "answer_mode": "DETERMINISTIC_LOCATOR",
            }
            if locator_resolution.get("status") != "VERIFIED":
                contract["status"] = "DEGRADED"
                contract["error_code"] = (
                    "QUOTE_LOCATOR_MULTIPLE_MATCHES"
                    if locator_resolution.get("status") == "MULTIPLE_MATCHES"
                    else "QUOTE_LOCATOR_NOT_FOUND"
                )
                contract["warnings"] = [
                    *(contract.get("warnings") or []),
                    "当前无法可靠定位原文页码；系统未展示任何候选页码。",
                ]
            else:
                contract["status"] = "COMPLETED"
                contract["answer_instruction"] = "本题由后端确定性引文定位器直接回答，模型不得生成或修改页码。"

    # 复合意图不短路生成。定位锚点已是首位冻结证据；这里只补充可能存在的
    # 图注正文反链引用，解释部分的 [E#] 因而仍可绑定到权威锚点。
    _tail_locator = contract.get("locator_resolution") or {}
    if (
        _tail_locator.get("status") == "VERIFIED"
        and (contract.get("locator_intent") or {}).get("compound")
    ):
        from yuxi.knowledge.rendering.citation_channel import append_locator_citations

        contract["citations"] = append_locator_citations(contract.get("citations") or [], _tail_locator)

    contract["retrieval_summary"] = {
        "query": question,
        "claim_count": len(contract.get("claims") or []),
        "evidence_count": len(contract.get("evidence") or []),
        "verbatim_hit_count": sum(
            1
            for row in contract.get("evidence") or []
            if row.get("retrieval_channel") == "VERBATIM" or row.get("verbatim_hit")
        ),
        "wiki_navigation_hit_count": len(contract.get("wiki_navigation_hits") or []),
        "web_call_count": 0,
    }
    contract["answer_instruction"] = (
        "结构化结果和引用由后端生成；模型只负责解释。completeness.status=PASS 仅允许称为"
        "‘全部可引用结果’；all_exact_relations_citable=true 时才可称为‘全部调控基因’。"
        "页码与锚点引用只写 [E1]/[E2] 占位符（对应 citations.ref），由后端渲染为权威"
        "「文件·分区·页码」引用；不要自行书写页码。"
    )
    if locator_intent.get("compound"):
        contract["answer_instruction"] += (
            " 定位行由后端确定性渲染；解释部分每个关键结论必须带 [E#] 引用，"
            "图注类问题优先引用正文讨论段（citations 中 zone=MAIN_TEXT 的反链条目）。"
        )
    # NUMERIC 题型：数字/区间/单位必须逐字来自证据原文，禁止换算或近似改写（P2-12）
    if "NUMERIC" in (plan.get("question_types") or []):
        contract["answer_instruction"] += (
            "本题含数值事实：数字、区间与单位必须与证据原文逐字一致，不得换算、"
            "四舍五入或改写表述；证据未给出的数值一律回答未提供。"
        )
    contract["contract_hash"] = _hash_contract(contract)
    await _persist_audit(
        db,
        retrieval_id=retrieval_id,
        run_id=run_id,
        request_id=request_id,
        snapshot=scope_snapshot,
        plan=contract.get("retrieval_plan") or plan,
        contract=contract,
        started_at=started_at,
    )
    _emit_knowledge_trace(retrieval_id, contract, started_at)
    return contract
