"""Anchor Eligibility Policy：**可定位 ≠ 可作回答证据**（Invariant 5）。

2026-09 事故（D4）：期刊 running head
``Plant Biotechnology Journal (2025) 23, pp. 1021–1038`` 每页重复出现，MinerU 把它
分成 ``type="text"``，适配器无条件透传 ``anchor_type='text'`` /
``locator_quality='HIGH'`` / ``locatable=True``。结果这个「页码极其准确、但结构性
毫无区分度」的页眉锚点进入定位候选池，把「正文·第1页」当成位置证据渲染上屏。

两个**正交**谓词，任何调用点都不得混用：

- ``source_locatable``：源码位置是否可信（有页码、有 bbox）。页眉确实满足——
  它的位置是准的。因此本模块**不改写**该列的历史语义。
- ``answer_eligible``：能否作为科研回答的位置证据。页眉**不满足**——位置准
  不等于能支撑回答。

判定优先级（任一不满足即不 eligible）：

1. ``anchor_type ∈ {header, footer, page_number, ...}`` → 结构性无区分度；
2. running head 形态（期刊名 + (年份) + 卷(期) + ``pp.`` 页码范围）→ 即使入库时
   被误分类为 ``text`` 也必须排除（**存量数据无需迁移**即可止血：判定基于
   ``quote`` 形态而非列值）；
3. 既有 ``is_toc_like``（目录/清单行）判定。

治理顺序：本模块是**读侧统一策略**。生产侧修正走新 parser → 新 ParseRevision →
benchmark → promote，绝不对历史 anchor 做不可追踪的原地 UPDATE（科研证据链最怕
「昨天相同 anchor_id 是 HIGH，今天悄悄变 header」）。
"""

from __future__ import annotations

import re
from typing import Any

# MinerU 结构性块类型：位置准确但不可作回答证据（页眉/页脚/页码/边注）
ANSWER_INELIGIBLE_ANCHOR_TYPES = frozenset(
    {
        "header",
        "footer",
        "page_number",
        "page_num",
        "page_footnote",
        "aside_text",
    }
)

# running head：期刊名 + (年份) + 卷(期) + pp. 页码范围。
# 实测样本 "Plant Biotechnology Journal (2025) 23, pp. 1021–1038"。
# 刻意要求「括号年份 + 卷号 + pp. 区间」三者同时出现，避免误伤正文里的年份表述。
_RUNNING_HEAD_PATTERN = re.compile(
    r"[A-Z][A-Za-z&.\-'’]*"
    r"(?:\s+(?:[A-Za-z&.\-'’]+)){0,7}"
    r"\s*\(\s*(?:19|20)\d{2}\s*\)"
    r"\s*,?\s*(?:vol\.?\s*)?\d{1,4}(?:\s*\(\s*\d{1,4}\s*\))?\s*,"
    r"\s*(?:pp?\.\s*)?\d{1,5}\s*[-\u2013\u2014]\s*\d{1,5}"
)

# 只保留「刊名 + 年份 + 卷 + pp.」这种极短的独立行才算页眉；正文段落里的引用
# 说明（"as reported in Nature (2020) 12, pp. 1–9"）不应命中——用长度上限兜底。
_RUNNING_HEAD_MAX_CHARS = 160


def is_running_head(text: str | None) -> bool:
    """判断一段文本是否为期刊 running head（页眉）。纯函数，无依赖。"""
    normalized = " ".join(str(text or "").split())
    if not normalized or len(normalized) > _RUNNING_HEAD_MAX_CHARS:
        return False
    return bool(_RUNNING_HEAD_PATTERN.search(normalized))


def classify_anchor_type(block_type: str | None, quote: str | None) -> str:
    """入库门禁：形态是 running head 的 ``text`` 块强制重分类为 ``header``。

    MinerU 本就有独立的 header/footer/page_number 类型；把 running head 归成
    ``text`` 是适配器的形态缺口。此处只做**名称纠正**（不改变 locatable 语义），
    读侧排除由 :func:`is_answer_eligible` 统一执行。
    """
    normalized = str(block_type or "text").strip().casefold()
    if normalized == "text" and is_running_head(quote):
        return "header"
    return normalized


def answer_eligibility_reason(
    *,
    anchor_type: str | None = None,
    quote: str | None = "",
    evidence_type: str | None = None,
    partition: str | None = None,
) -> str | None:
    """不可作回答证据的原因（可审计）；eligible 时返回 None。"""
    if str(anchor_type or "").strip().casefold() in ANSWER_INELIGIBLE_ANCHOR_TYPES:
        return "structural_anchor_type"
    if is_running_head(quote):
        return "running_head"
    from yuxi.knowledge.evidence.quote_locator import is_toc_like

    if is_toc_like(str(quote or ""), evidence_type=evidence_type, partition=partition):
        return "toc_like"
    return None


def is_answer_eligible(
    *,
    anchor_type: str | None = None,
    quote: str | None = "",
    evidence_type: str | None = None,
    partition: str | None = None,
) -> bool:
    """统一资格判定：所有定位候选池（引文/题注/图片投影/引用池）唯一判据。"""
    return (
        answer_eligibility_reason(
            anchor_type=anchor_type,
            quote=quote,
            evidence_type=evidence_type,
            partition=partition,
        )
        is None
    )


def _field(source: Any, name: str) -> str:
    if isinstance(source, dict):
        return str(source.get(name) or "")
    return str(getattr(source, name, "") or "")


def anchor_answer_eligible(
    anchor: Any,
    *,
    evidence_type: str | None = None,
    partition: str | None = None,
) -> bool:
    """ORM 行 / dict 行的资格判定（quote + anchor_type 两个字段即可）。"""
    return is_answer_eligible(
        anchor_type=_field(anchor, "anchor_type"),
        quote=_field(anchor, "quote"),
        evidence_type=evidence_type,
        partition=partition,
    )


def caption_layout_exception(span: Any, anchor: Any) -> bool:
    """footer-caption 存量兼容放行（semantic_role ≠ layout_region）。

    2026-09 Figure 4 事故：题注位于页面底部被 MinerU 归为 ``anchor_type=
    footer``，但它的**语义角色**是 caption——布局区域在页脚不代表它是页脚。
    无条件资格收口把真实题注排成了 NOT_FOUND（正确应为正文第 8 页）。

    放行必须**全部**满足（任一不满足即维持排除）：

    1. span 语义角色为 caption（``evidence_type == "caption"``）；
    2. 有规范化 container_label（Figure/Table 编号）；
    3. span 页与 anchor 页一致（跨源页码一致性）；
    4. span 与 anchor 题注文本一致（quote_hash 相等或归一化文本相等）；
    5. anchor 与 span 文本都不是 running head / 页码行 / TOC（形态判定，
       与 anchor_type 列值无关——页码行/版权行即使伪装成 caption 也不放行）。

    生产侧长期修正仍走新 parser → 新 ParseRevision 重建（正确分类 caption
    块）；本函数只是存量数据的读侧止血，不给「允许所有 footer」开口子。
    """
    span_quote = _field(span, "quote")
    anchor_quote = _field(anchor, "quote")
    if str(_field(span, "evidence_type") or "").strip().casefold() != "caption":
        return False
    if not str(_field(span, "container_label") or "").strip():
        return False
    span_page = getattr(span, "page_number", None)
    anchor_page = getattr(anchor, "page", None)
    if span_page is not None and anchor_page is not None:
        try:
            if int(span_page) >= 1 and int(anchor_page) >= 1 and int(span_page) != int(anchor_page):
                return False
        except (TypeError, ValueError):
            return False
    if _field(span, "quote_hash") and _field(span, "quote_hash") == _field(anchor, "quote_hash"):
        text_consistent = True
    else:
        from yuxi.knowledge.rendering.claim_evidence_resolver import normalize_for_match

        text_consistent = bool(span_quote and anchor_quote) and normalize_for_match(span_quote) == normalize_for_match(
            anchor_quote
        )
    if not text_consistent:
        return False
    if is_running_head(anchor_quote) or is_running_head(span_quote):
        return False
    from yuxi.knowledge.evidence.quote_locator import is_toc_like

    if is_toc_like(anchor_quote, evidence_type="caption") or is_toc_like(span_quote, evidence_type="caption"):
        return False
    return True


__all__ = [
    "ANSWER_INELIGIBLE_ANCHOR_TYPES",
    "anchor_answer_eligible",
    "answer_eligibility_reason",
    "caption_layout_exception",
    "classify_anchor_type",
    "is_answer_eligible",
    "is_running_head",
]
