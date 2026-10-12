"""文献会话追问轮的指代消解（批次 2）。

多轮追问里，用户常用指代词开头（「它的方法呢？」「那结果呢？」「这篇的结论？」），
直接拿去检索会丢失被指代的实体（基因名、方法名、@doc、DOI），导致一次检索定生死。

本模块做**确定性、规则式、fail-open** 的检索查询增强：

- 只在「当前轮以指代词开头/含指代结构」且「历史里有可锚定实体」时改写；
- 只产出**检索查询**（`resolve_retrieval_query` 的返回值），**不改**用户可见
  `query`、不改模型对话 `model_query`；
- 只影响「拿什么去检索」，**不参与**证据资格判定——事实仍须绑原文 `evidence`，
  证据资格仍走确定性 validator 链，LLM 不决定检索完整性。

设计约束：

- **纯函数、无 IO、可单测**：给定 query 与历史用户问题即可复现。
- **fail-open**：无指代、无历史、抽取不到锚点、任何异常 → 原样返回原 query。
- **保守抽取**：只抽 @doc / DOI / 英文术语（文献场景的实体多为拉丁标识），
  中文实体不强行抽取（避免过度工程化与误抽）；锚点数量与总长都有上限。
- **LITE/非文献会话**：调用方（chat_service）已由 ``literature_binding`` 闸门收窄，
  本模块不感知会话类型。
"""

from __future__ import annotations

import re

#: 追问指代模式（保守）：以指代词开头，或含指代结构（「它的…」「该文的…」/「its method」）。
_COREFERENCE_PATTERNS: tuple[str, ...] = (
    r"^(?:它|该|此|这|那|其中|上述|前面|上文|刚才|这篇|该文|本研究|本文)",
    r"^(?:it|its|that|this|those|these)\b",
    # 英文物主/指示代词 + 文献名词（任意位置）：its method / that result / these findings
    r"\b(?:its|his|her|their|this|that|these|those)\s+(?:method|methods|result|results|conclusion|conclusions|discussion|background|data|model|models|mechanism|function|expression|pathway|analysis|experiment|experiments|sample|samples|indicator|indicators|dataset|datasets|finding|findings|paper|study)\b",
)

_COREFERENCE_RE = re.compile("|".join(_COREFERENCE_PATTERNS), re.IGNORECASE)

#: 锚点抽取：@doc token、DOI、英文术语/基因名（≥3 字符，含数字与连字符）
_DOC_TOKEN = re.compile(r"@\S+")
_DOI = re.compile(r"10\.\d{4,9}/[^\s,;]+")
_ENGLISH_TERM = re.compile(r"\b[A-Za-z][A-Za-z0-9_\-]{2,}\b")

#: 英文停用词/常见噪声（避免把 "the"/"how"/"methods" 当成实体锚点）
_ENGLISH_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "are",
        "was",
        "were",
        "how",
        "why",
        "what",
        "when",
        "where",
        "who",
        "does",
        "did",
        "can",
        "could",
        "should",
        "would",
        "about",
        "from",
        "into",
        "that",
        "this",
        "these",
        "those",
        "with",
        "have",
        "has",
        "had",
        "not",
        "you",
        "your",
        "please",
        "tell",
        "explain",
        "summarize",
        "method",
        "methods",
        "result",
        "results",
        "conclusion",
        "conclusions",
        "discussion",
        "background",
        "it",
        "its",
        "mechanism",
        "function",
        "expression",
        "pathway",
        "analysis",
        "experiment",
        "sample",
        "indicator",
        "dataset",
        "finding",
        "paper",
        "study",
    }
)

#: Up to two candidates are sufficient to detect ambiguity; only a single antecedent is used.
_MAX_ANCHORS_PER_HISTORY = 2


def detect_followup_coreference(query: str) -> bool:
    """判断当前轮是否为含指代的追问（保守正则；空串返回 False）。"""
    text = str(query or "").strip()
    if not text:
        return False
    return bool(_COREFERENCE_RE.search(text))


def _extract_anchors(text: str) -> list[str]:
    """从一条历史用户问题里抽取实体锚点候选（@doc / DOI / 英文术语，有序去重）。"""
    if not text:
        return []
    anchors: list[str] = []
    seen: set[str] = set()

    for match in _DOC_TOKEN.finditer(text):
        token = match.group(0)
        key = token.lower()
        if key not in seen:
            seen.add(key)
            anchors.append(token)

    for match in _DOI.finditer(text):
        token = match.group(0).rstrip(".,;:)")
        key = token.lower()
        if key not in seen:
            seen.add(key)
            anchors.append(token)

    for match in _ENGLISH_TERM.finditer(text):
        token = match.group(0)
        if token.lower() in _ENGLISH_STOPWORDS:
            continue
        key = token.lower()
        if key not in seen:
            seen.add(key)
            anchors.append(token)

    return anchors[:_MAX_ANCHORS_PER_HISTORY]


def resolve_retrieval_query(query: str, history_user_queries: list[str] | None) -> str:
    """把含指代的追问折叠成可独立检索的查询（fail-open）。

    - 非指代追问 / 无历史 / 抽不到锚点 → 原样返回 ``query``；
    - 历史必须按最近优先传入；仅使用最近的单一实体，明确实体问题或歧义时不改写。

    返回值仅供检索（``prepare_knowledge_context`` 的 ``question``），不改用户可见
    文本、不参与证据资格判定。
    """
    original = str(query or "").strip()
    if not original or not detect_followup_coreference(original) or _extract_anchors(original):
        return original
    history = [str(item).strip() for item in (history_user_queries or []) if str(item or "").strip()]
    if not history:
        return original

    # A single nearest antecedent is safer than blending entities from unrelated turns.
    # Multiple entities require clarification; do not silently guess which one "it" refers to.
    for past_query in history:
        anchors = _extract_anchors(past_query)
        if not anchors:
            continue
        if len(anchors) != 1:
            return original
        return f"{anchors[0][:160]} {original}"
    return original
