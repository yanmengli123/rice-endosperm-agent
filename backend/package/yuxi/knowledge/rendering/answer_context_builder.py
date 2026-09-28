from __future__ import annotations

import json
import re
from typing import Any

from yuxi.knowledge.graphs.graph_utils import TIER_C, predicate_tier
from yuxi.knowledge.products.authority_gate import AuthorityGate
from yuxi.knowledge.products.registry import is_evidence_authority
from yuxi.knowledge.rendering.citation_channel import public_citations
from yuxi.knowledge.validation.citation_validator import redact_narrative_citation_identifiers


_FIGURE_QUESTION = re.compile(
    r"(?:\b(?:Figure|Fig\.)\s*S?\d+|图\s*S?\d+|图注|补充图|哪几张图|哪些图|图片)",
    re.IGNORECASE,
)
_TABLE_QUESTION = re.compile(
    r"(?:\bTable\s*S?\d+|表\s*S?\d+|表格|表注|哪几张表|哪些表)",
    re.IGNORECASE,
)


def build_answer_output_profile(contract: dict[str, Any]) -> dict[str, Any]:
    """Return the user-facing shape contract for this answer.

    This is intentionally derived from the original question, not from
    retrieved citations: an incidental Figure caption must not turn an
    ordinary biology question into a figure-report template.
    """

    summary = contract.get("retrieval_summary") or {}
    question = str(summary.get("query") if isinstance(summary, dict) else "")
    asks_figure = bool(_FIGURE_QUESTION.search(question))
    asks_table = bool(_TABLE_QUESTION.search(question))
    if asks_figure and asks_table:
        mode = "FIGURE_TABLE_PARALLEL"
        sections = ["结论", "逐图依据", "逐表数据", "综合解释", "证据边界"]
    elif asks_figure:
        mode = "FIGURE_PARALLEL"
        sections = ["结论", "逐图依据", "正文解释", "证据边界"]
    elif asks_table:
        mode = "TABLE_PARALLEL"
        sections = ["结论", "逐表数据", "数据含义", "证据边界"]
    else:
        return {"schema": "answer-output-profile.v1", "mode": "STANDARD", "required_sections": []}

    return {
        "schema": "answer-output-profile.v1",
        "mode": mode,
        "required_sections": sections,
        "figure_item": (
            "每张被采用的图单独一个 bullet，以原始 Figure N/Figure SN 标签开头；"
            "先写图注或图面直接事实，再写它对问题的支持作用。不得把正文机制冒充图中所示。"
        )
        if asks_figure
        else "",
        "table_item": (
            "每张被采用的表单独一个 bullet，以原始 Table N/Table SN 标签开头；"
            "数值陈述必须同时写对象、条件、指标、值和单位。差值、排名或倍数仅在成对操作数齐全时输出。"
        )
        if asks_table
        else "",
        "evidence_boundary": (
            "只说明影响本题结论的缺失证据；证据不足时直接写无法比较或无法归因，"
            "不得输出候选值、二选一数值、零差值或过程性占位符。"
        ),
        "style": "先回答结论，再列证据；不重复问题，不描述后端、检索轮次、渲染过程或内部协议。",
    }


def _output_profile_rules(profile: dict[str, Any]) -> str:
    if profile.get("mode") == "STANDARD":
        return ""
    sections = " → ".join(str(item) for item in profile.get("required_sections") or [])
    return (
        "图表并联回答格式是硬约束：按顺序输出「"
        + sections
        + "」。结论必须直接回答用户问题；逐图/逐表部分只列本轮实际采用且有证据绑定的项目。"
        + str(profile.get("figure_item") or "")
        + str(profile.get("table_item") or "")
        + str(profile.get("evidence_boundary") or "")
        + str(profile.get("style") or "")
    )


def _locator_payload(contract: dict[str, Any]) -> dict[str, Any] | None:
    """确定性定位结果投影；物理页码仅留在后端渲染通道。"""
    locator = contract.get("locator_resolution")
    if not isinstance(locator, dict) or not locator.get("page"):
        return None
    return {
        "status": locator.get("status"),
        "zone": locator.get("zone"),
        "quote_head": locator.get("quote_head"),
        "backlinks": [
            {"zone": item.get("zone"), "quote_head": item.get("quote_head")}
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
        if kb_type and not is_evidence_authority(kb_type):
            dropped += 1
            continue
        kept.append(row)
    return kept, dropped


def _answer_policy_payload(contract: dict[str, Any]) -> dict[str, Any] | None:
    """结构化 answer_policy 投影（D2）：策略必须真正进入模型请求。

    自由文本 answer_instruction 只进审计 contract，从未投递模型；图片定位
    类问题的输出授权由本策略与输出守卫双重执行。
    """
    policy = contract.get("answer_policy")
    return policy if isinstance(policy, dict) and policy.get("mode") else None


def build_answer_context(contract: dict[str, Any], *, narrative_evidence_limit: int = 10) -> str:
    """Compress the complete backend Contract into the context permitted for LLM narration."""
    # Authority Gate 第一层：WikiNavigationHit 等导航对象混入证据通道立即抛错。
    AuthorityGate.reject_navigation_as_evidence(contract.get("evidence"))
    # Authority Gate 第二层：按产品注册中心丢弃派生产品检索行。
    evidence_rows, _derived_rows_dropped = _drop_derived_product_rows(contract.get("evidence"))
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
    # R3 TIER_C 守卫：结构边（OBSERVED_BY/UNDER_CONDITION）降级为方法/条件附注，
    # 永不进入主边列表——主边（edges）只承载 TIER_A/B 的事实性关系。
    # 旧契约数据未带 tier 标注时按谓词分级实时判定（防御性过滤）。
    _all_graph_edges = (graph_expansion.get("edges") or [])[:50]
    graph_edges = []
    structural_edges = []
    for edge in _all_graph_edges:
        relation_type = str(edge.get("type") or (edge.get("properties") or {}).get("type") or "")
        tier = edge.get("predicate_tier") or predicate_tier(relation_type)
        item = {
            "source_id": edge.get("source_id"),
            "target_id": edge.get("target_id"),
            "type": relation_type,
            "kb_id": edge.get("kb_id"),
        }
        if tier == TIER_C:
            if len(structural_edges) < 10:
                structural_edges.append(item)
            continue
        if len(graph_edges) < 40:
            graph_edges.append(item)
    frozen_output = contract.get("answer_output")
    answer_output = (
        frozen_output
        if isinstance(frozen_output, dict) and frozen_output.get("schema") == "answer-output-profile.v1"
        else build_answer_output_profile(contract)
    )
    payload = {
        "intent": (contract.get("retrieval_plan") or {}).get("intent"),
        "query_mode": (contract.get("retrieval_plan") or {}).get("query_mode"),
        "answer_mode": (contract.get("retrieval_plan") or {}).get("answer_mode"),
        "answer_policy": _answer_policy_payload(contract),
        "answer_output": answer_output,
        "count_facts": {
            "citable_claims": len(claims),
            "distinct_subjects": len(unique_subjects),
            "relation_groups": {
                group: {
                    "citable_claims": counts["claim_count"],
                    "distinct_subjects": len(counts["subjects"]),
                }
                for group, counts in relation_group_counts.items()
            },
        },
        "completeness": contract.get("completeness") or {},
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
            # citation_channel admits at most 16 rows. Do not silently hide
            # the last four from the model: an omitted row may be the direct
            # single-mutant caption while a comparative figure stays visible.
            for citation in public_citations((contract.get("citations") or [])[:16])
        ],
        "locator": _locator_payload(contract),
        "sub_intents": ((contract.get("locator_intent") or {}).get("sub_intents") or []),
        "graph_expansion": {
            "seeds": graph_expansion.get("seeds") or [],
            "nodes": graph_nodes,
            "edges": graph_edges,
            # 方法桥/条件挂载：只作多跳中继的方法与条件附注，不得当作事实关系陈述
            "structural_edges": structural_edges,
            "authority": "NEO4J_PROJECTION_CONTEXT_ONLY",
        },
    }
    policy_rules = ""
    policy = _answer_policy_payload(contract)
    if policy and policy.get("page_claim_allowed") is False:
        policy_rules = (
            "answer_policy 为本轮输出授权的硬约束（与上述规则冲突时以 answer_policy 为准）："
            "本回答禁止出现文献名、Figure/Fig/图+编号、任何页码（第N页/p.N/pages N）与任何引用占位符；"
            "不得宣称该图片出自某篇论文。"
            + (
                "只能基于图片可见内容作答（panel、图表类型、文字标签、生物学对象），并附上"
                f"required_disclosure 原句：「{policy.get('required_disclosure') or ''}」。"
                if policy.get("visual_explanation_allowed")
                else (
                    "只能保守说明无法定位，并附上 required_disclosure 原句："
                    f"「{policy.get('required_disclosure') or ''}」。"
                )
            )
        )
    output_profile_rules = _output_profile_rules(answer_output)
    return (
        "<AUTHORITATIVE_KNOWLEDGE_CONTRACT>\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n</AUTHORITATIVE_KNOWLEDGE_CONTRACT>\n"
        + policy_rules
        + ("\n" if policy_rules else "")
        + output_profile_rules
        + ("\n" if output_profile_rules else "")
        + "规则：仅依据上面的 Claim 组织科研解释。PMID、DOI、evidence_id 和完整结构化表由后端工具卡呈现，"
        "不要自行生成、补全或改写这些引文标识符；plant_gene_lookup 等工具返回的官方基因、转录本、蛋白和"
        "RAP/MSU 位点 ID 不属于引文标识，可以在正文保留。即使存在引文标识格式问题，也必须完成基于 Claim 的"
        "科研解读，不得改为拒答或只返回计数。FUNCTIONAL_REGULATION 可表述为功能调控；"
        "PERTURBATION_EVIDENCE 只能表述为遗传/实验扰动证据；ASSOCIATION_OR_CONTEXT 不得升级为因果。"
        "Neo4j graph_expansion 只用于机制和路径上下文，不能替代 PostgreSQL canonical Claim。"
        "统计时必须区分 count_facts.citable_claims 与 distinct_subjects：回答基因数量只能使用 "
        "distinct_subjects；关系分组数量只能使用各组的 distinct_subjects，并明确同一基因可跨组重复。"
        "正文只用自然语言表达统计，不得暴露 JSON 字段名、scope/version/contract 等内部运行信息，"
        "也不得把 Claim 称为预测。"
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
        "同一表型有多张图时，主依据必须选直接回答该材料与表型的题注；单突变体问题优先单突变体图，"
        "不得用双突变体比较图、过表达图或工作模型图冒充主证据。后几类只能作为明确标注的补充证据。"
        "输出协议：只输出 <YUXI_ANSWER_DRAFT> 与 </YUXI_ANSWER_DRAFT> 包裹的严格 JSON。"
        'JSON 形状为 {"schema_version":"answer-draft.v2","blocks":[{"type":'
        '"heading|paragraph|bullet","text":"自然语言","evidence_refs":["E1"]}]}。'
        "不要自建‘参考文献/证据引用/资料来源’章节；后端会根据 evidence_refs 完成验证、渲染 Markdown 和"
        "【证据引用】区块。不要在 text 中复制 [E#]、[citation omitted] 或任何〔…〕引用标记。"
    )
