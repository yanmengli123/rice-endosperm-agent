from __future__ import annotations

import re

from yuxi.knowledge.research_evidence import extract_gene_identifiers

TASK_CLASSIFIER_VERSION = "1.3"

_NUMERIC_PATTERN = re.compile(
    r"\d+(?:[.,]\d+)?\s*(?:[-–~至到]\s*\d+(?:[.,]\d+)?\s*)?"
    r"(?:%|％|倍|mg/L|mM|µM|μM|kb|bp|aa|氨基酸|amino acids?|DAF|cM|MB)",
    flags=re.IGNORECASE,
)
_FIGURE_PATTERN = re.compile(r"(?:图\s*[SsFf]?\d|Figure\s*\w|图\d|Fig\.?\s*\w)", flags=re.IGNORECASE)
_TABLE_PATTERN = re.compile(r"(?:表\s*\d|Table\s*\w)", flags=re.IGNORECASE)
_CITATION_PATTERN = re.compile(r"(?:10\.\d{4,}/\w|PMID:?\s*\d|doi[:：]?\s*\S)", flags=re.IGNORECASE)
_MULTI_HOP_PATTERN = re.compile(
    r"(?:与.{0,12}(?:的)?(?:关系|区别|比较|异同)|both .{0,24}and)",
    flags=re.IGNORECASE,
)
# VERBATIM 信号：引号包裹的原文片段 / 逐字引用意图（驱动 scope_gateway 的
# VERBATIM 字面量通道；标识符样 token 不在此触发，避免扰动 ENTITY/CITATION 题型）
_VERBATIM_QUOTED_PATTERN = re.compile(r"[“\"]([^“”\"]{3,120})[”\"]")
_VERBATIM_INTENT_PATTERN = re.compile(r"(?:原文|原句|逐字|精确匹配|verbatim|exact\s+match)", flags=re.IGNORECASE)


def classify_task(question: str) -> str:
    """Classify non-enumeration knowledge questions with deterministic, auditable rules."""
    text = str(question or "")
    if re.search(r"(?:这篇|本文|文献|论文|文章|article|paper|document)", text, flags=re.IGNORECASE):
        return "DOCUMENT_EVIDENCE_SEARCH"
    if re.search(r"(?:机制|通路|如何|怎么|mechanism|pathway|how does)", text, flags=re.IGNORECASE):
        return "MECHANISM_EXPLANATION"
    if re.search(r"(?:是否|关系|关联|does .+ (?:regulate|affect)|relationship)", text, flags=re.IGNORECASE):
        return "RELATION_LOOKUP"
    if re.search(r"(?:RAP ID|MSU ID|基因编号|identifier|是什么基因)", text, flags=re.IGNORECASE):
        return "ENTITY_LOOKUP"
    # 问题中携带 RAP/MSU 标识符且未命中更高优先级意图时，走确定性标识符查询
    if extract_gene_identifiers(text):
        return "ENTITY_LOOKUP"
    return "GENERAL_KNOWLEDGE_QUERY"


_GENE_LIKE_PATTERN = re.compile(r"\b(?:[A-Z][A-Za-z]*[A-Z0-9][A-Za-z0-9]*|[A-Z]{3,}\d*)\b")


def detect_question_types(question: str) -> list[str]:
    """轻量规则题型检测（确定性、可审计），驱动检索策略与审计记录。

    返回有序去重类型；无任何信号时为 ["FACT"]（默认事实型）。该结果不改变
    主 intent（枚举/实体等确定性分支优先级更高），只作为补充信号：
    MULTI_HOP 扩召回、NUMERIC 触发数字保真提示；题型随 retrieval_plan
    进入 contract 持久化，供 benchmark 与答案校验消费。
    """
    text = str(question or "")
    types: list[str] = []
    identifiers = extract_gene_identifiers(text)
    gene_like = _GENE_LIKE_PATTERN.findall(text)
    if identifiers or gene_like:
        types.append("ENTITY")
    if _NUMERIC_PATTERN.search(text):
        types.append("NUMERIC")
    if _FIGURE_PATTERN.search(text):
        types.append("FIGURE")
    if _TABLE_PATTERN.search(text):
        types.append("TABLE")
    if _CITATION_PATTERN.search(text):
        types.append("CITATION")
    if _VERBATIM_QUOTED_PATTERN.search(text) or _VERBATIM_INTENT_PATTERN.search(text):
        types.append("VERBATIM")
    distinct_entities = {*(identifiers or []), *(gene_like or [])}
    if _MULTI_HOP_PATTERN.search(text) or len(distinct_entities) >= 2:
        types.append("MULTI_HOP")
    if not types:
        types.append("FACT")
    return types
