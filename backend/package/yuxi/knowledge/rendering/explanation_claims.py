"""科研解释 Claim 分类与绑定验证（P4：Scientific Explanation Binding）。

对「是什么意思」类答案的每条实质句做确定性分类（零 LLM），从「事后免责」
升级为「事前控制」——事前由 answer_instruction 声明纪律（每类结论必须带
[E#] 或明示不可支持），事后由本模块验证实际兑现：

- ``VISUAL_OBSERVATION``：对图面本身的观察（图中 a/b/c panel）——由视觉
  观察契约（VisualObservationEnvelope）支持；
- ``CAPTION_FACT``：题注载明的事实（Figure 1 含 GUS 染色）——必须绑定
  题注载体（locator 锚点或 zone=题注的引用行）；
- ``TEXT_SUPPORTED_INTERPRETATION``：作者的推断（OsMYB73 发挥转录调控
  作用）——必须绑定正文（zone=MAIN_TEXT，即图注反链 mentioned_by）证据；
- ``UNSUPPORTED_INTERPRETATION``：有硬约束但绑定不到任何证据 → 失败明示，
  不得混在支持结论中静默输出。
"""

from __future__ import annotations

import re
from typing import Any

from yuxi.knowledge.rendering.citation_channel import _SENTENCE_SPLIT_PATTERN
from yuxi.knowledge.rendering.claim_evidence_resolver import (
    BINDING_VERIFIED,
    extract_hard_constraints,
    normalize_for_match,
    resolve_binding,
)

EXPLANATION_CLAIMS_VERSION = "explanation_claims_v1"

CLAIM_VISUAL_OBSERVATION = "VISUAL_OBSERVATION"
CLAIM_CAPTION_FACT = "CAPTION_FACT"
CLAIM_TEXT_SUPPORTED_INTERPRETATION = "TEXT_SUPPORTED_INTERPRETATION"
CLAIM_UNSUPPORTED_INTERPRETATION = "UNSUPPORTED_INTERPRETATION"

# 图面观察句标记（对图本身的描述，观察契约即可支持）
_VISUAL_MARKER = re.compile(
    r"^(?:图中|图上|面板|该图|这张图|柱状图|条形图|曲线图|显微图|染色图|"
    r"panels?\s|\(a\)|\(b\)|\(c\)|in\s+(?:the\s+)?(?:figure|image|panel)|the\s+figure\s+(?:shows|contains|displays))",
    flags=re.IGNORECASE,
)
_CHIP_REF_PATTERN = re.compile(r"〔证据(E\d{1,3})｜")
_CAPTION_LABEL_HEAD = re.compile(r"^\s*(?:fig(?:ure)?|table|图|表)\s*s?\s*\d+", flags=re.IGNORECASE)
_CLAIM_HEAD_CHARS = 60
_MAX_CLAIMS = 16


def _caption_pool(citations: list[dict[str, Any]], locator: dict[str, Any] | None) -> list[dict[str, Any]]:
    """CAPTION_FACT 的合法载体：locator 锚点行 + 题注句首编号的引用行。"""
    pool: list[dict[str, Any]] = []
    locator_anchor = str((locator or {}).get("anchor_id") or "")
    for citation in citations or []:
        if not citation.get("locatable"):
            continue
        quote = str(citation.get("_quote") or citation.get("quote_head") or "")
        if (locator_anchor and str(citation.get("_anchor_id") or "") == locator_anchor) or _CAPTION_LABEL_HEAD.match(
            quote
        ):
            pool.append(citation)
    return pool


def classify_explanation_claims(
    answer_text: str,
    *,
    citations: list[dict[str, Any]],
    locator: dict[str, Any] | None,
    observation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把解释答案的实质句按支持来源分类。纯函数（确定性，零 LLM）。

    返回 ``{version, claims: [{head, claim_type, ref, supported}], unsupported_count}``。
    只有 unsupported 句构成失败——它们已被输出门禁以「未定位依据」明示；
    本分类给审计/状态模块提供按类型的可见性。
    """
    locator = locator or {}
    caption_pool = _caption_pool(citations, locator)
    full_pool = [citation for citation in citations or [] if citation.get("locatable")]
    observation_available = isinstance(observation, dict) and "figure_label" in observation

    claims: list[dict[str, Any]] = []
    by_ref = {str(citation.get("ref")): citation for citation in citations or []}
    for match in _SENTENCE_SPLIT_PATTERN.finditer(str(answer_text or "")):
        sentence = match.group(0).strip()
        if not sentence or len(normalize_for_match(sentence)) < 12:
            continue
        # 后端产物行（定位行/引用区块/未依据注释）不参与分类
        if sentence.startswith(("已可靠定位", "【证据引用", "（注：", "##", "- E", "> ")):
            continue
        head = re.sub(r"\s+", " ", sentence)[:_CLAIM_HEAD_CHARS]
        entry: dict[str, Any] = {"head": head}

        # 已带后端芯片的句子：芯片经由输出门禁验证才会存在，直接按其引用行
        # 的分区分类（正文 → 作者推断；题注载体 → 题注事实）
        chip_ref = _CHIP_REF_PATTERN.search(sentence)
        if chip_ref:
            resolved = by_ref.get(chip_ref.group(1))
            if resolved is None:
                continue
            if str(resolved.get("zone") or "") == "MAIN_TEXT":
                entry["claim_type"] = CLAIM_TEXT_SUPPORTED_INTERPRETATION
            else:
                entry["claim_type"] = CLAIM_CAPTION_FACT
            entry["ref"] = str(resolved.get("ref"))
            entry["supported"] = True
            claims.append(entry)
            continue

        if _VISUAL_MARKER.match(sentence) and observation_available:
            entry["claim_type"] = CLAIM_VISUAL_OBSERVATION
            entry["supported"] = True
            claims.append(entry)
            continue

        hard = extract_hard_constraints(sentence)
        if not (hard["numbers"] or hard["identifiers"]):
            # 无硬约束的定性铺垫句不构成可证伪科研结论（无观察契约时也不
            # 冒充 VISUAL_OBSERVATION）
            continue
        binding = resolve_binding(claim_context=sentence, proposed_ref=None, citations=full_pool)
        if binding.get("status") == BINDING_VERIFIED:
            ref = str(binding.get("ref") or "")
            resolved = next((citation for citation in full_pool if str(citation.get("ref")) == ref), None)
            in_caption = any(str(item.get("ref")) == ref for item in caption_pool)
            in_main_text = resolved is not None and str(resolved.get("zone") or "") == "MAIN_TEXT"
            if in_caption and not in_main_text:
                entry["claim_type"] = CLAIM_CAPTION_FACT
            elif in_main_text:
                entry["claim_type"] = CLAIM_TEXT_SUPPORTED_INTERPRETATION
            else:
                entry["claim_type"] = CLAIM_CAPTION_FACT
            entry["ref"] = ref
            entry["supported"] = True
        else:
            entry["claim_type"] = CLAIM_UNSUPPORTED_INTERPRETATION
            entry["supported"] = False
        claims.append(entry)
        if len(claims) >= _MAX_CLAIMS:
            break
    return {
        "version": EXPLANATION_CLAIMS_VERSION,
        "claims": claims,
        "unsupported_count": sum(1 for claim in claims if not claim["supported"]),
    }


__all__ = [
    "CLAIM_CAPTION_FACT",
    "CLAIM_TEXT_SUPPORTED_INTERPRETATION",
    "CLAIM_UNSUPPORTED_INTERPRETATION",
    "CLAIM_VISUAL_OBSERVATION",
    "EXPLANATION_CLAIMS_VERSION",
    "classify_explanation_claims",
]
