from __future__ import annotations

import re
from typing import Any

from yuxi.knowledge.planning.task_classifier import TASK_CLASSIFIER_VERSION, classify_task

PLANNER_VERSION = "1.0"

_SOCIAL_PATTERNS = (
    r"^(?:hi|hello|hey|你好|您好|嗨|早上好|下午好|晚上好)[!！。.，,\s]*$",
    r"^(?:谢谢|感谢|thanks|thank you|再见|bye)[!！。.，,\s]*$",
)
_IDENTITY_PATTERNS = (r"你是谁", r"你叫什么", r"who are you", r"what(?:'s| is) your name")
_TRANSLATION_PATTERNS = (r"^(?:请)?(?:翻译|改写|润色|校对)", r"^(?:translate|rewrite|polish|proofread)\b")


def _matches(patterns: tuple[str, ...], text: str) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _clean_target(value: str | None) -> str | None:
    if not value:
        return None
    target = re.sub(r"\s+", " ", value).strip(" 的?？。.!！,，:：\"'“”")
    target = re.sub(r"^(?:the|rice)\s+", "", target, flags=re.IGNORECASE)
    return target[:160] or None


def _enumeration_target(question: str) -> str | None:
    patterns = (
        r"对\s*(.+?)\s*(?:进行)?(?:调控|影响|控制)(?:的)?(?:基因|gene)",
        r"(?:调控|影响|控制|参与)\s*(.+?)\s*(?:的)?(?:基因|gene)",
        r"(?:哪些|所有|全部|列出).{0,20}(?:基因|genes?).{0,30}(?:调控|影响|控制)\s*(.+?)(?:[?？。]|$)",
        r"(?:genes?)(?:\s+that)?\s+(?:regulate|control|affect|influence)\s+(.+?)(?:[?？.]|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, question, flags=re.IGNORECASE)
        if match:
            return _clean_target(match.group(1))
    return None


def plan_knowledge_query(question: str, *, strategy: str, scope_nonempty: bool) -> dict[str, Any]:
    """确定性优先的查询规划器；复杂语义后续可在 UNKNOWN 上接模型兜底。"""
    text = re.sub(r"\s+", " ", str(question or "")).strip()
    lowered = text.casefold()
    base = {
        "planner_version": PLANNER_VERSION,
        "task_classifier_version": TASK_CLASSIFIER_VERSION,
        "question": text,
        "intent": "GENERAL_KNOWLEDGE_QUERY",
        "query_mode": "BOUNDED",
        "retrieval_required": False,
        "target_mention": None,
        "answer_mode": "LLM_GROUNDED_NARRATIVE",
        "reason": "",
    }

    effective_strategy = str(strategy or "MODEL_DECIDES").upper()
    if effective_strategy == "DISABLED" or not scope_nonempty:
        return {**base, "intent": "NO_RETRIEVAL", "reason": "SCOPE_OR_STRATEGY_DISABLED"}
    if not text or _matches(_SOCIAL_PATTERNS, text):
        return {**base, "intent": "SOCIAL", "reason": "SOCIAL_TURN"}
    if _matches(_IDENTITY_PATTERNS, lowered):
        return {**base, "intent": "IDENTITY", "reason": "IDENTITY_TURN"}
    if _matches(_TRANSLATION_PATTERNS, lowered):
        return {**base, "intent": "TRANSFORMATION", "reason": "PURE_TRANSFORMATION"}

    target = _enumeration_target(text)
    enumeration_marker = bool(
        re.search(r"(?:哪些|所有|全部|列出|有什么|有哪些|which|what|list|all)", text, flags=re.IGNORECASE)
        and re.search(r"(?:基因|genes?)", text, flags=re.IGNORECASE)
    )
    if target and enumeration_marker:
        return {
            **base,
            "intent": "PHENOTYPE_REGULATOR_ENUMERATION",
            "query_mode": "EXHAUSTIVE",
            "retrieval_required": effective_strategy == "KNOWLEDGE_FIRST",
            "target_mention": target,
            "answer_mode": "HYBRID",
            "reason": "DETERMINISTIC_ENUMERATION_RULE",
        }

    intent = classify_task(text)

    return {
        **base,
        "intent": intent,
        "retrieval_required": effective_strategy == "KNOWLEDGE_FIRST",
        "answer_mode": "DETERMINISTIC_STRUCTURED" if intent == "ENTITY_LOOKUP" else base["answer_mode"],
        "reason": "KNOWLEDGE_FIRST_DEFAULT" if effective_strategy == "KNOWLEDGE_FIRST" else "MODEL_DECIDES",
    }
