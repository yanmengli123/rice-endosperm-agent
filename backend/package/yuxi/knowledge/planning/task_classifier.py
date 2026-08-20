from __future__ import annotations

import re

TASK_CLASSIFIER_VERSION = "1.0"


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
    return "GENERAL_KNOWLEDGE_QUERY"
