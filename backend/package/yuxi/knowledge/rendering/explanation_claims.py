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
    locator_head = str((locator or {}).get("quote") or (locator or {}).get("quote_head") or "")
    locator_is_caption = bool(
        (locator or {}).get("evidence_type") == "caption"
        or (locator or {}).get("locator_kind") == "FIGURE_CAPTION"
        or _CAPTION_LABEL_HEAD.match(locator_head)
    )
    for citation in citations or []:
        if not citation.get("locatable"):
            continue
        quote = str(citation.get("_quote") or citation.get("quote_head") or "")
        if (
            locator_is_caption and locator_anchor and str(citation.get("_anchor_id") or "") == locator_anchor
        ) or _CAPTION_LABEL_HEAD.match(quote):
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


# 机制归因句标记（H2b enforce）：只有 explanation_grounding=VERIFIED（正文回链）
# 才允许「某基因调控某通路/据此认为」类机制结论；UNRESOLVED/PARTIAL 时整句删除
# 机制归因句标记（I4）：识别「归因科研机制」的句法形态。注意这**不是**安全
# 边界——安全边界是逐 Claim 的 resolve_binding 证据验证；本模式只负责圈出
# 「需要验证的候选句」（宽召回），有 VERIFIED 正文证据绑定的候选句会被保留。
_MECHANISM_CLAIM_PATTERN = re.compile(
    r"(?:这说明|这表明|这证明|据此认为|由此可知|由此可见|结果支持|"
    r"提示.{0,80}(?:调控|调节|参与|介导|影响|决定)|"
    r".{0,80}(?:调控|调节|介导|抑制|促进|激活).{0,80}(?:通路|过程|表达|发育|活性|基因|因子)|"
    r"(?:负|正)调控(?:因子|作用)|上游(?:负|正)?(?:调控|调节).{0,40}|"
    r"与.{0,80}(?:模型|机制).{0,20}一致|对.{0,60}具有(?:负向|正向|抑制|促进)作用|"
    r"(?:results?|data|findings?).{0,30}(?:support|suggest|indicate|demonstrate|are\s+consistent\s+with)|"
    r".{0,60}\bas\s+an?\s+.{0,30}(?:negative|positive|upstream|downstream)?\s*regulator|"
    r"(?:demonstrates|indicates|suggests|implies)\s+that.{0,100}"
    r"(?:regulates|mediates|controls|inhibits|promotes)|"
    r"\b(?:regulates?|regulation|mediates?|controls?|inhibits?|promotes?|activates?)\b|"
    r"consistent\s+with\s+the\s+model\s+that)",
    flags=re.IGNORECASE,
)
# 题注事实句标记（I4 caption_fact_allowed 执行）：声称转述题注内容
_CAPTION_FACT_CLAIM_PATTERN = re.compile(
    r"(?:题注(?:中|里|说|描述|表明|记载)|(?:figure|table)\s+caption\s+(?:states|describes|indicates)|"
    r"图注(?:中|说|描述|表明)|(?:figure|图|表)\s*S?\d{1,3}.{0,30}(?:shows|describes|展示|描述))",
    re.IGNORECASE,
)


def enforce_explanation_grounding(
    answer_text: str,
    *,
    policy: dict[str, Any] | None,
    citations: list[dict[str, Any]] | None = None,
    locator: dict[str, Any] | None = None,
) -> tuple[str, int]:
    """逐 Claim 证据授权执行（I4）：机制/题注句必须绑定 VERIFIED 证据才保留。

    升级自 H2b 的关键词删除——现在的判定顺序：

    1. 句子不匹配机制/题注模式 → 保留（普通描述不强制绑定）；
    2. 匹配机制或题注模式 → 无论全局能力位真假，均尝试逐 Claim 验证：
       ``resolve_binding`` 对正文/题注引用池四层验证，VERIFIED 则保留；
       验证不过即删除（全局能力位不是单条 Claim 的通行证）；
    3. 引用池为空（无法逐 Claim 验证）→ 直接删除（无法验证即无据）。

    与审计用的 :func:`classify_explanation_claims` 互补——分类器记录「哪句
    无依据」，本函数把无依据句从用户可见输出中删除。纯函数、幂等。
    """
    if not isinstance(policy, dict):
        return str(answer_text or ""), 0
    mechanism_governed = "mechanism_attribution_allowed" in policy
    caption_governed = "caption_fact_allowed" in policy
    if not mechanism_governed and not caption_governed:
        return str(answer_text or ""), 0
    result = str(answer_text or "")
    if not result.strip():
        return result, 0
    pool = [citation for citation in (citations or []) if citation.get("locatable")]
    caption_pool = _caption_pool(pool, locator)
    caption_refs = {str(citation.get("ref") or "") for citation in caption_pool}
    mechanism_pool = [
        citation
        for citation in pool
        if str(citation.get("zone") or "") == "MAIN_TEXT" and str(citation.get("ref") or "") not in caption_refs
    ]
    removed = 0
    kept_lines: list[str] = []
    for line in result.split("\n"):
        sentences = _SENTENCE_SPLIT_PATTERN.findall(line)
        if not sentences:
            kept_lines.append(line)
            continue
        kept = []
        for sentence in sentences:
            is_caption = caption_governed and bool(_CAPTION_FACT_CLAIM_PATTERN.search(sentence))
            is_mechanism = mechanism_governed and bool(_MECHANISM_CLAIM_PATTERN.search(sentence))
            # 纯图面观察由 VisualObservationEnvelope 负责；若同句同时作出机制
            # 归因或题注转述，则仍必须进入证据验证，不能用“图中可见”前缀绕过。
            if _VISUAL_MARKER.match(sentence.strip()) and not (is_mechanism or is_caption):
                kept.append(sentence)
                continue
            if not is_mechanism and not is_caption:
                kept.append(sentence)
                continue
            # 同句同时包含题注事实与机制归因时按更严格的正文机制证据验证，
            # 题注不能单独授权论文机制结论。
            claim_pool = mechanism_pool if is_mechanism else caption_pool
            if not claim_pool:
                removed += 1  # 无法逐 Claim 验证 → 无据 → 删除
                continue
            chip_ref = _CHIP_REF_PATTERN.search(sentence)
            proposed_ref = chip_ref.group(1) if chip_ref else None
            binding = resolve_binding(
                claim_context=sentence,
                proposed_ref=proposed_ref,
                citations=claim_pool,
            )
            if binding.get("status") == BINDING_VERIFIED:
                kept.append(sentence)  # 有 VERIFIED 证据绑定：保留（宁可少答不误删有据句）
            else:
                removed += 1
        rebuilt = "".join(kept)
        kept_lines.append(rebuilt)
    cleaned = "\n".join(kept_lines)
    cleaned = re.sub(r"^[，。；,.;]+", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned, removed


__all__.append("enforce_explanation_grounding")
