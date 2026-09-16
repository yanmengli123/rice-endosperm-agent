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


def _emit_figure_projection_trace(retrieval_id: str, contract: dict[str, Any]) -> None:
    """图卡投影留痕（P0-4 SLA 采集点）：attached/suppressed + 抑制原因枚举。

    只读 locator_resolution["figure_projection"]（在 contract_hash 之前写入），
    不修改 contract；无投影信封（无定位裁决）时 no-op。
    """
    resolution = contract.get("locator_resolution") or {}
    projection = resolution.get("figure_projection") if isinstance(resolution, dict) else None
    if not isinstance(projection, dict):
        return
    from yuxi.trace import emit_trace

    status = str(projection.get("status") or "suppressed")
    emit_trace(
        category="KNOWLEDGE",
        operation="figure_projection",
        event_type=f"knowledge.figure_projection.{status}",
        span_id=retrieval_id,
        title="图卡资产投影",
        summary="论文原图投影已附着" if status == "attached" else f"论文原图投影被抑制：{projection.get('reason')}",
        attributes={
            "reason": projection.get("reason"),
            "figure_count": len(projection.get("figures") or []),
            "locator_kind": resolution.get("locator_kind") if isinstance(resolution, dict) else None,
        },
        resource_refs=[{"type": "knowledge_retrieval", "id": retrieval_id}],
        visibility="ADMIN",
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


def _build_answer_policy(
    *,
    status: str,
    observation_available: bool,
    vision_status: str,
    locator_kind: str = "",
    binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """按定位状态机构造结构化 answer_policy（进入模型上下文 + 输出守卫双执行）。

    输出规则表（2026-09 收敛，G2 扩展到全部定位意图）：只有 VERIFIED 允许
    文献名/页码/文献引用；Figure 编号需要 figure_identity_binding 独立
    VERIFIED（页码已验证但编号未确认时只发页码不发编号）；MULTIPLE/
    NOT_FOUND 只保留视觉解释或保守说明，永不发布候选页码。任何定位入口
    （QUOTE/FIGURE/PAGE/IMAGE）未验证时普通语义召回不得包装成来源定位证据。
    """
    conservative_only = vision_status in {"NOT_CONFIGURED", "PROVIDER_FAILED", "SCHEMA_INVALID", "IMAGE_UNREADABLE"}
    decision_status = status
    # I1 严格唯一授权源：Binding 存在 → 所有权限只读 Binding，UNRESOLVED 一律
    # 拒绝（顶层 status=VERIFIED + Binding.page_binding=UNRESOLVED 的矛盾构造
    # 以 Binding 为准，不再回退顶层）；Binding 缺失 + 顶层 VERIFIED → 失败关闭
    # （出口门禁保证新 Run 的 VERIFIED 必有 Binding，缺失即链路异常，宁可拒答）。
    # 顶层 status/container_label 此后只用于审计投影，不参与授权。
    if isinstance(binding, dict):
        from yuxi.knowledge.contracts.locator_binding import authoritative_locator_projection

        authoritative_locator = authoritative_locator_projection(binding)
        if authoritative_locator is not None:
            authoritative_binding = authoritative_locator["binding"]
            grounding = str(authoritative_binding.get("explanation_grounding") or "UNRESOLVED")
            return {
                "mode": "LOCATOR_VERIFIED",
                "locator_kind": locator_kind,
                "document_identity_allowed": True,
                "figure_label_allowed": authoritative_binding.get("figure_identity_binding") == "VERIFIED",
                "page_claim_allowed": True,
                "document_citations_allowed": True,
                "visual_explanation_allowed": True,
                # 图卡发布授权（figure card projection）：与视觉解释授权分立——
                # 只有冻结 Binding 的 VERIFIED 定位才允许把论文原图投进对话流；
                # 发布开关（figure_card_enabled）在 chat 层读取，与此位双闸
                "figure_image_publish_allowed": True,
                # 解释依据分级：机制归因需要正文回链（VERIFIED）；题注字面
                # 解释在 PARTIAL 即可；UNRESOLVED 只能描述可见内容
                "explanation_grounding": grounding,
                "mechanism_attribution_allowed": grounding == "VERIFIED",
                "caption_fact_allowed": grounding in {"VERIFIED", "PARTIAL"},
                "required_disclosure": None,
            }
        # Binding 明确否决或结构不完整：即使顶层 status=VERIFIED 也不授予
        # 任何定位权限。顶层字段不能把一个无效 Binding 重新升级为 VERIFIED。
        if status == "VERIFIED" or binding.get("status") == "VERIFIED":
            return {
                "mode": "LOCATOR_BINDING_UNRESOLVED",
                "locator_kind": locator_kind,
                "document_identity_allowed": False,
                "figure_label_allowed": False,
                "page_claim_allowed": False,
                "document_citations_allowed": False,
                "visual_explanation_allowed": observation_available,
                "figure_image_publish_allowed": False,
                "explanation_grounding": "UNRESOLVED",
                "mechanism_attribution_allowed": False,
                "caption_fact_allowed": False,
                "required_disclosure": "定位结论未形成可审计的页码绑定，系统不展示任何文献、编号或页码信息。",
            }
        binding_status = str(binding.get("status") or "NOT_FOUND")
        decision_status = (
            binding_status if binding_status in {"MULTIPLE_MATCHES", "NOT_FOUND", "NOT_APPLICABLE"} else "NOT_FOUND"
        )
    elif status == "VERIFIED":
        # Binding 缺失 + 顶层 VERIFIED：失败关闭（不允许顶层回退授权）
        return {
            "mode": "LOCATOR_DEGRADED_NO_BINDING",
            "locator_kind": locator_kind,
            "document_identity_allowed": False,
            "figure_label_allowed": False,
            "page_claim_allowed": False,
            "document_citations_allowed": False,
            "visual_explanation_allowed": False,
            "figure_image_publish_allowed": False,
            "explanation_grounding": "UNRESOLVED",
            "mechanism_attribution_allowed": False,
            "caption_fact_allowed": False,
            "required_disclosure": "定位结论缺少可审计的权威绑定，系统不展示任何文献、编号或页码信息。",
        }
    if decision_status == "MULTIPLE_MATCHES":
        return {
            "mode": "LOCATOR_AMBIGUOUS",
            "locator_kind": locator_kind,
            "document_identity_allowed": False,
            "figure_label_allowed": False,
            "page_claim_allowed": False,
            "document_citations_allowed": False,
            # 图片内容解释只在存在可信视觉观察时允许；无观察的文本歧义
            # 无法核验模型解释 → 守卫整体替换保守文案（G3）
            "visual_explanation_allowed": observation_available,
            "figure_image_publish_allowed": False,
            "explanation_grounding": "UNRESOLVED",
            "mechanism_attribution_allowed": False,
            "caption_fact_allowed": False,
            "required_disclosure": (
                "该内容在当前知识范围内存在多个可能的匹配，无法唯一确定来源文献与页码；"
                "可补充原文语句、题注文字或限定知识库后重试。"
            ),
        }
    if observation_available:
        return {
            "mode": "VISUAL_ONLY_UNLOCATED",
            "locator_kind": locator_kind,
            "document_identity_allowed": False,
            "figure_label_allowed": False,
            "page_claim_allowed": False,
            "document_citations_allowed": False,
            "visual_explanation_allowed": True,
            "figure_image_publish_allowed": False,
            "explanation_grounding": "UNRESOLVED",
            "mechanism_attribution_allowed": False,
            "caption_fact_allowed": False,
            "required_disclosure": "可以解释图片可见内容，但无法可靠确定来源文献和页码。",
        }
    return {
        "mode": "UNLOCATED",
        "locator_kind": locator_kind,
        "document_identity_allowed": False,
        "figure_label_allowed": False,
        "page_claim_allowed": False,
        "document_citations_allowed": False,
        # 未定位且无可信视觉观察：解释不可核验 → 守卫整体替换保守文案（G3）；
        # vision_status 仅决定披露文案口径
        "visual_explanation_allowed": False,
        "figure_image_publish_allowed": False,
        "explanation_grounding": "UNRESOLVED",
        "mechanism_attribution_allowed": False,
        "caption_fact_allowed": False,
        "required_disclosure": (
            "当前无法进行可靠定位（视觉定位通道不可用且指纹未命中），因此不能确定其来源文献与页码。"
            if conservative_only
            else "当前知识库范围内无法可靠确定该内容的来源文献与页码。"
        ),
    }


def _ledger_status(resolution: dict[str, Any] | None) -> str:
    """resolution → ledger stage 状态（VERIFIED/MULTIPLE_MATCHES/NO_MATCH/...）。"""
    if not isinstance(resolution, dict):
        return "NOT_APPLICABLE"
    status = str(resolution.get("status") or "")
    if status == "VERIFIED":
        return "VERIFIED"
    if status == "MULTIPLE_MATCHES":
        return "MULTIPLE_MATCHES"
    if status == "NOT_APPLICABLE":
        return "NOT_APPLICABLE"
    return "NO_MATCH"


def _ledger_entry(stage: str, resolution: dict[str, Any] | None) -> dict[str, str]:
    entry = {"stage": stage, "status": _ledger_status(resolution)}
    if isinstance(resolution, dict) and resolution.get("reason"):
        entry["reason"] = str(resolution["reason"])
    return entry


def _freeze_locator_evidence(contract: dict[str, Any], row: dict[str, Any] | None) -> None:
    """Prepend one verified locator row to both answer and validation evidence."""
    if not row or not row.get("evidence_id"):
        return
    for key in ("evidence", "context_evidence"):
        existing = [item for item in contract.get(key) or [] if isinstance(item, dict)]
        if not any(item.get("evidence_id") == row["evidence_id"] for item in existing):
            contract[key] = [row, *existing]
    contract["warnings"] = [
        warning for warning in contract.get("warnings") or [] if warning != "范围内未检索到足以回答该问题的证据。"
    ]


async def prepare_knowledge_context(
    db: AsyncSession,
    *,
    question: str,
    scope_snapshot: dict[str, Any],
    run_id: str | None,
    request_id: str | None,
    retrieval_id: str | None = None,
    image_bytes: bytes | None = None,
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
        LOCATOR_KIND_FIGURE,
        LOCATOR_KIND_QUOTE,
        detect_locator_intent,
    )

    locator_intent = detect_locator_intent(question)
    contract["locator_intent"] = locator_intent
    # FIGURE_LOCATOR（图表编号问题）与 QUOTE_LOCATOR 同为精确定位意图：
    # 编号走 caption 通道 + label 硬约束（caption_locator v3）。图片附件在场时
    # 任何定位意图（含 PAGE_LOCATOR——「这个图在哪一页」无引文无编号）都
    # 先走 FIGURE_IMAGE 通道。
    locator_pending = (
        locator_intent.get("kind") in {LOCATOR_KIND_QUOTE, LOCATOR_KIND_FIGURE}
        or (image_bytes and locator_intent.get("kind"))
    ) and bool(raw_members)
    if locator_pending:
        # 定位/复合解释都需要检索证据集：无论知识策略如何都必须执行检索
        # （MODEL_DECIDES 下本可不检索，定位问题不能跟着 SKIPPED）。
        plan = {**plan, "retrieval_required": True}
    direct_locator: dict[str, Any] | None = None
    locator_evidence: dict[str, Any] | None = None
    # LocatorAttemptLedger（P0-D）：定位链路每个 stage 的尝试结论随审计行
    # 持久化——失败原因保真，NOT_APPLICABLE 永不覆盖真实失败。
    attempt_ledger: list[dict[str, str]] = []
    if locator_pending and image_bytes:
        # 图片附件入口（FIGURE_IMAGE）权威链（P0 收敛版）：
        #   指纹（V0 SHA/V1 pHash/V1G ORB）→ 视觉观察（VLM 最后）→ Caption
        #   Bridge（观察编号 + 逐字文本经真实题注 T0/T1 验证）→ 文本回退。
        # 每 stage 记账；V0/V1/V1G 或观察层 VERIFIED 即终局，后续 stage 跳过。
        from yuxi.knowledge.vision.figure_image_locator import resolve_figure_image_locator

        scope_kb_ids = [str(member["kb_id"]) for member in raw_members]
        image_locator = await resolve_figure_image_locator(
            db,
            kb_ids=scope_kb_ids,
            image_bytes=image_bytes,
        )
        attempt_ledger.append(_ledger_entry("ASSET_FINGERPRINT", image_locator))
        observation = None
        if image_locator.get("reason") == "VISION_PROVIDER_UNAVAILABLE":
            from yuxi.knowledge.vision.provider import get_vision_provider, mark_observation_schema_invalid

            provider = get_vision_provider()
            # G5 运行门禁：ready = canary 实测 READY（spec 非空 ≠ 能力可用）；
            # canary 判 FAILED/SCHEMA_INVALID 后本进程不再调用 provider
            if getattr(provider, "ready", provider.available):
                observation = await provider.describe(image_bytes)
                if observation is None:
                    mark_observation_schema_invalid(str(getattr(provider, "model_spec", "") or ""))
                attempt_ledger.append(
                    {
                        "stage": "VISION_OBSERVATION",
                        "status": "OK" if observation is not None else "SCHEMA_INVALID_OR_FAILED",
                    }
                )
            else:
                attempt_ledger.append(
                    {
                        "stage": "VISION_OBSERVATION",
                        "status": "PROVIDER_NOT_READY",
                        "reason": "canary 未通过或未配置",
                    }
                )
            contract["figure_image_observation"] = (
                observation.model_dump(mode="json") if observation is not None else {"available": False}
            )
            if observation is not None:
                retried = await resolve_figure_image_locator(
                    db,
                    kb_ids=scope_kb_ids,
                    image_bytes=image_bytes,
                    observation=observation,
                )
                if retried.get("reason") != "VISION_PROVIDER_UNAVAILABLE":
                    image_locator = retried
                    attempt_ledger.append({"stage": "VISUAL_CONSTRAINTS", "status": _ledger_status(retried)})
        # Caption Bridge（P0-C）：指纹与观察约束都未决，但观察给出了图表编号 →
        # 把观察的编号/逐字文本结构化交题注通道裁决（不构造假自然语言问句）。
        if (
            observation is not None
            and observation.figure_label
            and image_locator.get("status") not in {"VERIFIED", "MULTIPLE_MATCHES"}
        ):
            from yuxi.knowledge.evidence.caption_locator import FigureCaptionQuery, resolve_caption_bridge

            bridge = await resolve_caption_bridge(
                db,
                query=FigureCaptionQuery(
                    canonical_label=str(observation.figure_label),
                    verbatim_segments=tuple(str(item) for item in observation.visible_text or []),
                    visible_entities=tuple(str(item) for item in observation.visible_entities or []),
                ),
                kb_ids=scope_kb_ids,
            )
            attempt_ledger.append(
                {
                    "stage": "CAPTION_BRIDGE",
                    "status": "OK" if bridge is not None else "NO_MATCH",
                    "reason": str(bridge.get("reason") or "") if bridge else "no_caption_candidate",
                }
            )
            if bridge is not None:
                # 桥接命中：定位身份仍是 FIGURE_IMAGE 入口，裁决层是题注权威
                bridge["locator_kind"] = "FIGURE_IMAGE"
                bridge["match_tier"] = f"CAPTION_BRIDGE_{bridge.get('match_tier') or 'LABEL'}"
                image_locator = bridge
        elif image_locator.get("status") not in {"VERIFIED", "MULTIPLE_MATCHES"}:
            attempt_ledger.append(
                {
                    "stage": "CAPTION_BRIDGE",
                    "status": "SKIPPED",
                    "reason": "no_observation_label" if observation is not None else "observation_unavailable",
                }
            )
        if image_locator.get("status") in {"VERIFIED", "MULTIPLE_MATCHES"}:
            # 图片裁决优先且终局：MULTIPLE_MATCHES 不允许文本路径收缩成唯一页码；
            # 观察不可用等失败关闭结论同样终局。
            direct_locator = image_locator
        elif image_locator.get("reason") in {
            "VISION_PROVIDER_UNAVAILABLE",
            "figure_adjudication_error",
            "figure_index_empty_in_scope",
        }:
            direct_locator = image_locator
        if direct_locator and direct_locator.get("status") == "VERIFIED":
            # P4 解释绑定：Figure → Caption → mentioned_by（正文反链），
            # 解释阶段的 [E#] 优先绑定正文讨论段
            from yuxi.knowledge.evidence.quote_locator import attach_caption_backlinks

            await attach_caption_backlinks(db, kb_ids=scope_kb_ids, resolution=direct_locator)
    # 图片流裁决结果单独保存（D1）：无论是否落在「终局原因」清单内，图片通道
    # 的结论都不允许被文本回退的 NOT_APPLICABLE 覆盖。
    image_flow_terminal: dict[str, Any] | None = image_locator if (locator_pending and image_bytes) else None
    if locator_pending and (direct_locator is None or direct_locator.get("status") == "NOT_FOUND"):
        # 文本回退资格：图片定位已产生结果时，只有用户确实提供了引文文本才允许
        # 文本回退；纯图片问句没有可提取引句，回退只会把图片 NOT_FOUND
        # （如 VISION_PROVIDER_UNAVAILABLE）覆盖成 NOT_APPLICABLE
        # （no_extractable_quote），让下游误以为不存在必须失败关闭的定位任务。
        if image_flow_terminal is not None and not locator_intent.get("quote_text"):
            attempt_ledger.append(
                {"stage": "TEXT_FALLBACK", "status": "NOT_APPLICABLE", "reason": "no_user_quote_text"}
            )
            direct_locator = image_flow_terminal
        else:
            from yuxi.knowledge.evidence.quote_locator import resolve_quote_locator

            text_result = await resolve_quote_locator(
                db,
                question=question,
                kb_ids=[str(member["kb_id"]) for member in raw_members],
            )
            attempt_ledger.append(_ledger_entry("TEXT_FALLBACK", text_result))
            # 优先归并：文本 VERIFIED 才有资格覆盖图片失败结论；否则图片终局保留
            direct_locator = (
                text_result
                if text_result.get("status") == "VERIFIED" and (image_flow_terminal or {}).get("status") != "VERIFIED"
                else (image_flow_terminal or text_result)
            )
    if direct_locator is not None and attempt_ledger:
        # 账本随 locator_resolution 持久化（primary_reason 由最终状态承载；
        # NOT_APPLICABLE 只是路由不适用，无资格覆盖真实失败）
        direct_locator.setdefault("attempt_ledger", attempt_ledger)
    if image_flow_terminal is not None and direct_locator is not None:
        from yuxi.knowledge.vision.provider import current_vision_status

        direct_locator.setdefault("vision_status", current_vision_status().get("status") or "UNKNOWN")
    if direct_locator and direct_locator.get("status") == "VERIFIED":
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
            "match_tier": direct_locator.get("match_tier") or "DETERMINISTIC_QUOTE_LOCATOR",
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
    # 反链解释依据仍来自检索证据本身）。图片附件流的裁决已在 FIGURE_IMAGE
    # 通道完成且终局，同样经此写回 locator_resolution 供门禁与渲染消费。
    if locator_pending and (
        locator_intent.get("quote_text") or not locator_intent.get("compound") or (image_bytes and direct_locator)
    ):
        from yuxi.knowledge.evidence.quote_locator import resolve_quote_locator_from_citations

        if image_bytes and direct_locator and direct_locator.get("status") in {"VERIFIED", "MULTIPLE_MATCHES"}:
            # 图片物理裁决是终局：不得再用文本引用池覆盖图片本体页。
            locator_resolution = direct_locator
        elif locator_intent.get("figure_label") and direct_locator:
            # Figure 编号/题注必须由 caption span + physical anchor 通道裁决。
            # 普通正文可能复述 “Figure N showed ...”，引用池不得重新计算并
            # 冒充题注所在页；旧 revision 无题注锚点时宁可失败关闭。
            locator_resolution = direct_locator
        elif direct_locator and direct_locator.get("status") == "MULTIPLE_MATCHES":
            # 全冻结范围已确认跨物理位置重复；Top-K 即使只召回其中一条也不得
            # 把歧义错误收缩成唯一页码。
            locator_resolution = direct_locator
        elif not locator_intent.get("quote_text"):
            # 编号定位（无引文片段）：caption 通道裁决即终局——label 硬约束
            # 已在通道内完成，引用池无引文可比对。
            locator_resolution = direct_locator or {
                "status": "NOT_FOUND",
                "locator_version": "quote_locator_v2",
                "reason": "no_extractable_quote",
            }
        else:
            locator_resolution = resolve_quote_locator_from_citations(
                quote_text=locator_intent.get("quote_text") or "",
                citations=contract.get("citations") or [],
                partition_intent=locator_intent.get("partition_intent"),
                figure_label=locator_intent.get("figure_label"),
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
        # Locator Authority 出口门禁：VERIFIED ⇒ 物理证据必须已在冻结证据契约
        # 中（binding 随 locator_resolution_json 持久化，状态投影/渲染器只消费
        # 该对象）。违例 → ANSWER_VALIDATION_FAILED，失败关闭不展示页码。
        from yuxi.knowledge.contracts.locator_binding import enforce_locator_authority

        enforce_locator_authority(
            contract,
            retrieval_id=retrieval_id,
            locator_kind=str(locator_resolution.get("locator_kind") or "QUOTE_LOCATOR"),
            hard_constraints=["figure_label"] if locator_intent.get("figure_label") else None,
        )
        locator_resolution = contract["locator_resolution"]
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
                + ("已验证" if locator_resolution.get("status") == "VERIFIED" else "未通过验证，失败关闭不展示页码")
                + "），其余子意图基于检索证据回答。",
            ]
            if locator_resolution.get("status") != "VERIFIED":
                contract["status"] = "DEGRADED"
                # 权威门禁结论（ANSWER_VALIDATION_FAILED）不被常规失败码覆盖
                if contract.get("error_code") != "ANSWER_VALIDATION_FAILED":
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
                # 权威门禁结论（ANSWER_VALIDATION_FAILED）不被常规失败码覆盖
                if contract.get("error_code") != "ANSWER_VALIDATION_FAILED":
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
    if _tail_locator.get("status") == "VERIFIED" and (contract.get("locator_intent") or {}).get("compound"):
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
            " 解释纪律：对图面的观察（panel/坐标轴/染色）可直接描述；题注载明的"
            "事实（实验内容）必须引用题注条目；作者的推断结论（功能/机制）必须"
            "引用正文讨论段——找不到对应 [E#] 时明示「原文未提供该结论的依据」，"
            "不得给出无依据推断。"
        )
    # AC15 描述权 ≠ 定位权 + 结构化 answer_policy（D2/D3/D4）：
    # G2：answer_policy 扩展到**全部定位意图**（QUOTE/FIGURE/PAGE/IMAGE）——
    # 任何入口只要定位任务成立且未 VERIFIED，都不得把普通语义召回包装成
    # 来源定位证据（生产事故：Figure 4 题注 NOT_FOUND 后回答仍宣称 Liu 2024
    # 并渲染第 8/13 页普通证据）。
    _locator_required = bool(locator_intent.get("kind"))
    if _locator_required:
        _final_resolution = contract.get("locator_resolution") or {}
        _final_status = str(_final_resolution.get("status") or "NOT_FOUND")
        _observation_available = isinstance(contract.get("figure_image_observation"), dict) and "figure_label" in (
            contract.get("figure_image_observation") or {}
        )
        # H2 唯一授权源：策略消费出口门禁冻结的 Binding 对象（三元组），
        # 不再并行读取顶层字段推导权限
        contract["answer_policy"] = _build_answer_policy(
            status=_final_status,
            observation_available=_observation_available,
            vision_status=str(_final_resolution.get("vision_status") or "UNKNOWN"),
            locator_kind=str(locator_intent.get("kind") or ""),
            binding=_final_resolution.get("binding") if isinstance(_final_resolution, dict) else None,
        )
        if isinstance(_final_resolution, dict) and "answer_policy" not in _final_resolution:
            _final_resolution["answer_policy"] = contract["answer_policy"]
        _locator_authorized = bool(
            contract["answer_policy"].get("page_claim_allowed") is True
            and contract["answer_policy"].get("document_citations_allowed") is True
        )
        if not _locator_authorized:
            # 关闭普通引用池：定位未验证时，普通语义检索命中不得升级为来源
            # 定位证据——文献引用池必须为空（检索候选仍可在状态面板展示）。
            contract["citations"] = []
            contract["context_evidence"] = []
            contract["warnings"] = [
                *(contract.get("warnings") or []),
                "定位任务未通过验证：本轮不发布任何文献引用与页码；普通检索命中仅作为候选展示。",
            ]
            # 回答证据计数持久化为显式 0（不得为 NULL）
            contract["completeness"] = {
                **(contract.get("completeness") or {}),
                "status": str(contract["answer_policy"].get("mode") or f"LOCATOR_{_final_status}"),
                "returned_evidence_count": 0,
            }
        else:
            contract["completeness"] = {
                **(contract.get("completeness") or {}),
                "status": "LOCATOR_VERIFIED",
                "returned_evidence_count": 1,
            }
        contract["answer_instruction"] += " answer_policy（结构化，必须遵守）：" + json.dumps(
            contract["answer_policy"], ensure_ascii=False
        )
    # 图卡资产投影（P0-1 暗发布）：从冻结 Binding 派生可发布资产投影，写入
    # locator_resolution["figure_projection"]。必须在 _hash_contract 之前执行
    # （信封随 contract_hash 落库审计）；投影永远计算、与发布开关解耦，
    # 抑制只影响图卡，绝不影响文本回答/芯片/PDF 跳转。
    if isinstance((contract.get("locator_resolution") or {}).get("binding"), dict):
        from yuxi.knowledge.contracts.figure_asset_projection import attach_figure_projection

        await attach_figure_projection(
            db,
            contract["locator_resolution"],
            publish_allowed=bool((contract.get("answer_policy") or {}).get("figure_image_publish_allowed")),
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
    _emit_figure_projection_trace(retrieval_id, contract)
    return contract
