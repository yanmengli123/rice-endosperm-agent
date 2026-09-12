from __future__ import annotations

import json
from typing import Any

from yuxi.knowledge.products.authority_gate import AuthorityGate
from yuxi.knowledge.products.registry import is_derived_product
from yuxi.knowledge.rendering.citation_channel import public_citations
from yuxi.knowledge.validation.citation_validator import redact_narrative_citation_identifiers


def _locator_payload(contract: dict[str, Any]) -> dict[str, Any] | None:
    """确定性定位结果投影（复合意图时模型可见；页码只读，由后端渲染）。"""
    locator = contract.get("locator_resolution")
    if not isinstance(locator, dict) or not locator.get("page"):
        return None
    return {
        "status": locator.get("status"),
        "page": locator.get("page"),
        "zone": locator.get("zone"),
        "quote_head": locator.get("quote_head"),
        "backlinks": [
            {"page": item.get("page"), "zone": item.get("zone"), "quote_head": item.get("quote_head")}
            for item in (locator.get("backlinks") or [])[:3]
            if isinstance(item, dict)
        ],
    }


def _drop_derived_product_rows(rows: list[dict[str, Any]] | None) -> tuple[list[dict[str, Any]], int]:
    """答案上下文绝不接收派生知识产品（llmwiki）的行。

    返回 ``(kept_rows, dropped_count)``。行内 kb_type 缺失时视为权威源
    （向后兼容既有数据），命中派生产品的行被丢弃并计数，便于观测。
    """
    kept: list[dict[str, Any]] = []
    dropped = 0
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        kb_type = str(row.get("kb_type") or "").strip().casefold()
        if kb_type and is_derived_product(kb_type):
            dropped += 1
            continue
        kept.append(row)
    return kept, dropped


def build_answer_context(contract: dict[str, Any], *, narrative_evidence_limit: int = 10) -> str:
    """Compress the complete backend Contract into the context permitted for LLM narration."""
    # Authority Gate 第一层：WikiNavigationHit 等导航对象混入证据通道立即抛错。
    AuthorityGate.reject_navigation_as_evidence(contract.get("evidence"))
    # Authority Gate 第二层：按产品注册中心丢弃派生产品检索行。
    evidence_rows, derived_rows_dropped = _drop_derived_product_rows(contract.get("evidence"))
    scope = contract.get("knowledge_scope_snapshot") or {}
    claims = contract.get("claims") or []
    unique_subjects: set[str] = set()
    relation_group_counts: dict[str, dict[str, Any]] = {}
    for claim in claims:
        subject = claim.get("subject") or {}
        subject_key = str(subject.get("canonical_identity") or subject.get("id") or subject.get("name") or "")
        if subject_key:
            unique_subjects.add(subject_key)
        group = str(claim.get("relation_group") or "UNCLASSIFIED")
        group_counts = relation_group_counts.setdefault(group, {"claim_count": 0, "subjects": set()})
        group_counts["claim_count"] += 1
        if subject_key:
            group_counts["subjects"].add(subject_key)
    claim_summaries = [
        {
            "claim_id": claim.get("claim_id"),
            "subject": (claim.get("subject") or {}).get("name"),
            "predicate": claim.get("predicate"),
            "object": (claim.get("object") or {}).get("name"),
            "relation_group": claim.get("relation_group"),
            "evidence_count": len(claim.get("evidence") or []),
        }
        for claim in claims
    ]
    narrative_evidence = []
    citation_refs_by_evidence: dict[str, list[str]] = {}
    for citation in contract.get("citations") or []:
        evidence_id = str(citation.get("evidence_id") or "")
        ref = str(citation.get("ref") or "")
        if evidence_id and ref:
            citation_refs_by_evidence.setdefault(evidence_id, []).append(ref)
    for evidence in evidence_rows:
        if len(narrative_evidence) >= max(0, int(narrative_evidence_limit)):
            break
        item = {
            key: (
                redact_narrative_citation_identifiers(evidence.get(key))
                if key == "evidence_quote"
                else evidence.get(key)
            )
            for key in (
                "subject",
                "predicate",
                "object",
                "relation_group",
                "evidence_level",
                "condition",
                "experimental_subject_type",
                "observed_effect",
                "evidence_quote",
            )
            if evidence.get(key) not in (None, "", [], {})
        }
        refs = citation_refs_by_evidence.get(str(evidence.get("evidence_id") or ""), [])
        if refs:
            item["citation_refs"] = refs
        narrative_evidence.append(item)
    graph_expansion = contract.get("graph_expansion") or {}
    graph_nodes = [
        {
            "id": node.get("id"),
            "type": node.get("type"),
            "name": (node.get("properties") or {}).get("name"),
            "label": (node.get("properties") or {}).get("label"),
            "kb_id": node.get("kb_id"),
        }
        for node in (graph_expansion.get("nodes") or [])[:30]
    ]
    graph_edges = [
        {
            "source_id": edge.get("source_id"),
            "target_id": edge.get("target_id"),
            "type": edge.get("type") or (edge.get("properties") or {}).get("type"),
            "kb_id": edge.get("kb_id"),
        }
        for edge in (graph_expansion.get("edges") or [])[:40]
    ]
    payload = {
        "intent": (contract.get("retrieval_plan") or {}).get("intent"),
        "query_mode": (contract.get("retrieval_plan") or {}).get("query_mode"),
        "answer_mode": (contract.get("retrieval_plan") or {}).get("answer_mode"),
        "scope_id": scope.get("scope_id"),
        "scope_version": scope.get("scope_version"),
        "result_counts": {
            "claim_count": len(claims),
            "unique_subject_count": len(unique_subjects),
            "relation_groups": {
                group: {
                    "claim_count": counts["claim_count"],
                    "unique_subject_count": len(counts["subjects"]),
                }
                for group, counts in relation_group_counts.items()
            },
        },
        "completeness": contract.get("completeness") or {},
        "authority_gate": {
            "derived_rows_dropped": derived_rows_dropped,
            "wiki_navigation_in_evidence": False,
        },
        "claims": claim_summaries,
        "selected_evidence": narrative_evidence,
        "citations": [
            {
                "ref": citation.get("ref"),
                "file": citation.get("filename"),
                "zone": citation.get("zone"),
                "quote_head": citation.get("quote_head"),
                "toc_line": bool(citation.get("toc_line")),
                "locatable": bool(citation.get("locatable")),
                "secondary_of": citation.get("secondary_of"),
            }
            for citation in public_citations((contract.get("citations") or [])[:12])
        ],
        "locator": _locator_payload(contract),
        "sub_intents": ((contract.get("locator_intent") or {}).get("sub_intents") or []),
        "graph_expansion": {
            "seeds": graph_expansion.get("seeds") or [],
            "nodes": graph_nodes,
            "edges": graph_edges,
            "authority": "NEO4J_PROJECTION_CONTEXT_ONLY",
        },
    }
    return (
        "<AUTHORITATIVE_KNOWLEDGE_CONTRACT>\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n</AUTHORITATIVE_KNOWLEDGE_CONTRACT>\n"
        "规则：仅依据上面的 Claim 组织科研解释。PMID、DOI、evidence_id 和完整结构化表由后端工具卡呈现，"
        "不要自行生成、补全或改写这些引文标识符；plant_gene_lookup 等工具返回的官方基因、转录本、蛋白和"
        "RAP/MSU 位点 ID 不属于引文标识，可以在正文保留。即使存在引文标识格式问题，也必须完成基于 Claim 的"
        "科研解读，不得改为拒答或只返回计数。FUNCTIONAL_REGULATION 可表述为功能调控；"
        "PERTURBATION_EVIDENCE 只能表述为遗传/实验扰动证据；ASSOCIATION_OR_CONTEXT 不得升级为因果。"
        "Neo4j graph_expansion 只用于机制和路径上下文，不能替代 PostgreSQL canonical Claim。"
        "统计时必须区分 result_counts.claim_count 与 unique_subject_count：回答基因数量只能使用 "
        "unique_subject_count；关系分组数量只能使用各组的 unique_subject_count，并明确同一基因可跨组重复。"
        "正文只用自然语言表达统计，不得暴露 result_counts、unique_subject_count 等内部字段名，也不得把 Claim 称为预测。"
        "completeness.status=PASS 只表示完整返回了当前证据策略允许的 Claim；"
        "可表述为‘全部可引用结果’。只有 all_exact_relations_citable=true 时才可进一步称为‘全部调控基因’；"
        "否则数量必须表述为‘当前证据策略下返回的可引用基因’，不得称为知识库收录总数。"
        "页码与位置引用：提及某证据所在页码/分区时，只在句末标注与 citations.ref 对应的 [E1]、[E2] 占位符，"
        "由后端确定性渲染为「文件·分区·页码」引用；严禁自行书写页码、p.N、第N页或 ea_/ev 锚点 ID——"
        "此类内容会被后端剥离。citations 中 locatable=false 的条目只能表述为‘无法定位页码’，不得猜测。"
        "zone=MAIN_TEXT 是正文，zone=SUPPORTING_INFO 是补充材料；用户问‘正文哪一页’时只能引用 MAIN_TEXT 条目。"
        "selected_evidence.citation_refs 是证据行与本轮引用编号的唯一映射；不得在两个列表之间自行猜配。"
        "判断普通回答中的某句话由哪条证据支持时，只能使用该证据行自身的 citation_refs；"
        "toc_line=true 的条目是图表目录/图注清单行，只说明图表位置，"
        "严禁作为正文句子的位置引用；语义相近不等于同页。"
        "复合意图：当 sub_intents 同时包含 LOCATOR 与 EXPLANATION 时，必须同时回答全部子意图——"
        "定位部分不要复述页码（后端已确定性渲染定位行），直接继续回答解释部分；"
        "解释部分每个关键结论句末必须带 [E#] 占位符，含数字/基因符号/缩写的句子尤其必须标注，"
        "未标注的硬约束句会被后端自动核验，核验不过会在答案中明示‘未定位到依据’。"
        "图注类引文（quote_head 以 Figure/Table 开头）的含义解释必须优先依据 locator.backlinks 中"
        "正文分区（MAIN_TEXT）的讨论段，并引用其 [E#]；backlinks 为空时明示‘原文未在正文展开讨论该图’，"
        "只解释图注字面内容，不得编造实验结论。"
    )
