from __future__ import annotations

import json
from typing import Any

from yuxi.knowledge.validation.citation_validator import redact_narrative_citation_identifiers


def build_answer_context(contract: dict[str, Any], *, narrative_evidence_limit: int = 10) -> str:
    """Compress the complete backend Contract into the context permitted for LLM narration."""
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
    for evidence in contract.get("evidence") or []:
        if len(narrative_evidence) >= max(0, int(narrative_evidence_limit)):
            break
        narrative_evidence.append(
            {
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
        )
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
        "claims": claim_summaries,
        "selected_evidence": narrative_evidence,
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
    )
