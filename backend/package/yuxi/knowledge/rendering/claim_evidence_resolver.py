"""Claim–Evidence 绑定解析器（ClaimEvidenceResolver）。

职责边界（企业级不变量的核心）：

- **LLM 负责语言与科学推理；本模块负责「这句话属于哪条证据」。**
- 模型写的 ``[E#]`` / 伪造芯片只是 *proposal*（提议），不是真相；绑定必须经过
  本解析器的确定性验证后才能渲染页码。
- 页码永远不在这里决定——解析器只产出「哪条证据」；页码由已绑定证据的
  ``EvidenceAnchor`` 派生（见 citation_channel）。

四层验证（确定性优先，不先调 LLM）：

1. **EXACT_QUOTE_CONTAINMENT**：回答中的引文句（归一化后 ≥40 字符）被某条
   证据的完整引文包含 → 唯一命中即 VERIFIED；多个不同页命中 → MULTIPLE_MATCHES
   （失败关闭，除非分区意图能消歧）。
2. **IDENTIFIER_NUMERIC_EXACT**：科研硬约束（数字、区间端点、基因符号如
   OsMYB73、大写缩写如 SANT）必须逐个出现在候选证据引文中；约束集非空且
   唯一满足 → VERIFIED。E1 只有 OsMYB73 而没有结构域数字时被 REJECT。
3. **NORMALIZED_LEXICAL_OVERLAP**：归一化词面重叠唯一显著领先（≥0.55 且
   领先次名 ≥0.1）→ VERIFIED（带置信度）。解决 PDF 连字（ﬁ→fi）、
   全半角、断行、破折号变体等（与 align_texts_to_anchors 同一哲学）。
4. **语义 verifier（SUPPORTED/PARTIAL/UNSUPPORTED/CONTRADICTED）**：预留
   扩展点——即使未来接入，它也只判定「是否支持」，*永远不能决定页码*。
   当前未接入：前三层无法判定 → UNSUPPORTED（失败关闭，不显示页码）。

归一化处理：NFKC（连字 ﬁ/ﬂ → fi/fl）、破折号变体统一、全半角、大小写、
换行断词与多余空白。
"""

from __future__ import annotations

import html
import re
import unicodedata
from typing import Any

BINDING_VERIFIED = "VERIFIED"
BINDING_MULTIPLE_MATCHES = "MULTIPLE_MATCHES"
BINDING_UNSUPPORTED = "UNSUPPORTED"

METHOD_EXACT_QUOTE = "EXACT_QUOTE_CONTAINMENT"
METHOD_HARD_CONSTRAINTS = "IDENTIFIER_NUMERIC_EXACT"
METHOD_LEXICAL_OVERLAP = "NORMALIZED_LEXICAL_OVERLAP"

RESOLVER_VERSION = "claim_evidence_resolver_v2"

# 引文句判定阈值：归一化后 ≥40 字符的连续文本才足以做包含判定
EXACT_QUOTE_MIN_CHARS = 40
# 词面重叠：唯一领先阈值与领先幅度
LEXICAL_VERIFIED_THRESHOLD = 0.55
LEXICAL_LEAD_MARGIN = 0.10

_DASH_CLASS = "‐‑‒–—―−"
_NON_ALNUM_TAIL = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")
# 小数空格修复：数字 空格* 句点 空格* 数字 → 数字.数字（"1 . 0"→"1.0"、"0. 05"→"0.05"）
_DECIMAL_SPACE_PATTERN = re.compile(r"(\d)\s*\.\s*(?=\d)")
# 基因/转录本样式：OsMYB73、ZmMYB14、T1 代（字母+数字混合，≥3 字符）
_GENE_LIKE = re.compile(r"\b(?=[A-Za-z]{2,}\d)[A-Za-z][A-Za-z0-9]{2,}\b")
# 大写缩写：SANT、CRISPR、GUS、GFP（≥3 个连续大写字母）
_ACRONYM = re.compile(r"\b[A-Z]{3,}\b")
# 图表编号：Figure S1 / Fig. 3 / Table S2
_FIGURE_LABEL = re.compile(r"\b(?:fig(?:ure)?|table)\s*S?\d+[a-z]?\b", flags=re.IGNORECASE)
# Experimental/platform acronyms are common across an entire paper. They can
# constrain an already-discriminative claim, but cannot combine with one gene
# symbol to make layer 2 unique (CRISPR + OsMYB73 previously authorized any
# CRISPR/OsMYB73 caption, including a later multi-mutant comparison figure).
_NON_DISCRIMINATIVE_IDENTIFIERS = {
    "crispr",
    "cas9",
    "rna",
    "rna-seq",
    "pcr",
    "qrt-pcr",
    "rt-pcr",
    "gus",
    "gfp",
    "sem",
    "tem",
    "kegg",
}


def normalize_for_match(text: str) -> str:
    """科研文本匹配归一化（v2）：HTML 实体、连字/破折号/全半角/大小写/空白/小数空格。

    v2 起的处理顺序（2026-09 Figure 5 事故：用户粘贴文本含 ``&#x20;`` 实体与
    ``1 . 0 cm`` 小数空格，旧归一化把实体残骸折叠成额外 token 破坏精确匹配）：

    1. ``html.unescape``——HTML 实体（``&#x20;`` → 空格、``&amp;`` → &）先于
       一切处理，否则 NFKC/字符折叠会把实体残骸变成假 token；
    2. NFKC——连字 ﬁ/ﬂ → fi/fl、全半角、上标数字；
    3. 破折号族统一为 ``-``；
    4. 小数空格修复——``1 . 0`` → ``1.0``、``0. 05`` → ``0.05``（仅数字两侧
       的句点，不影响句边界与缩写）；
    5. casefold + 非字母数字折叠 + 空白归一。
    """
    value = html.unescape(str(text or ""))
    value = unicodedata.normalize("NFKC", value)
    for dash in _DASH_CLASS:
        value = value.replace(dash, "-")
    value = _DECIMAL_SPACE_PATTERN.sub(r"\1.", value)
    value = value.casefold()
    value = _NON_ALNUM_TAIL.sub(" ", value)
    return re.sub(r"\s+", " ", value).strip()


def extract_hard_constraints(text: str) -> dict[str, list[str]]:
    """抽取科研硬约束：数字、基因样式 token、大写缩写、图表编号（保留原始大小写）。"""
    source = str(text or "")
    # Measurements/standalone numbers only. Digits embedded in identifiers
    # (OsMYB73, T1, S21) are represented by the identifier layer and must not
    # independently activate numeric exact matching.
    numbers = sorted({match.group(0) for match in re.finditer(r"(?<![A-Za-z])\d+(?:\.\d+)?(?![A-Za-z0-9])", source)})
    identifiers: set[str] = set()
    identifiers.update(match.group(0) for match in _GENE_LIKE.finditer(source))
    identifiers.update(match.group(0) for match in _ACRONYM.finditer(source))
    # Preserve the separator: normalize_for_match("Figure 2") is "figure 2".
    # Removing it produced "figure2", which could never match normalized
    # caption text and caused valid caption facts to fail closed after F#
    # signing.
    identifiers.update(match.group(0) for match in _FIGURE_LABEL.finditer(source))
    return {"numbers": numbers, "identifiers": sorted(identifiers)}


def _constraints_satisfied(hard: dict[str, list[str]], quote_norm: str) -> bool:
    """硬约束必须全部出现在候选引文中（数字原样、标识符归一化后包含）。

    数字按归一化形态比对（``0.05`` 与载体归一化后的 ``0 05`` 等价），
    否则 ``P < 0.05`` 类约束永远判失败。
    """
    if not hard["numbers"] and not hard["identifiers"]:
        return False  # 没有约束时不启用该层（由词面重叠层裁决）
    for number in hard["numbers"]:
        if normalize_for_match(number) not in quote_norm:
            return False
    for identifier in hard["identifiers"]:
        if normalize_for_match(identifier) not in quote_norm:
            return False
    return True


def _hard_constraints_discriminative(hard: dict[str, list[str]]) -> bool:
    """Whether layer-2 constraints are strong enough to identify evidence.

    A single ubiquitous gene symbol (for example ``OsMYB73``) identifies the
    paper topic, not the asserted relation.  Treating it as sufficient allowed
    any OsMYB73 paragraph to authorize an unrelated mechanism sentence.  A
    numeric constraint or at least two independent identifiers is required;
    weaker claims continue to the lexical/semantic layers and fail closed when
    their relation words are unsupported.

    Numbers that are substrings of identifiers (the ``73`` inside ``OsMYB73``)
    do not count — they are gene-symbol suffixes, not independent measurements.
    """
    identifiers_lower = {str(identifier).lower() for identifier in hard["identifiers"]}
    discriminative_identifiers = identifiers_lower - _NON_DISCRIMINATIVE_IDENTIFIERS
    has_figure_label = any(re.fullmatch(r"(?:fig(?:ure)?|table)\s*s?\d+[a-z]?", value) for value in identifiers_lower)
    independent_numbers = [
        number for number in hard["numbers"] if not any(number in identifier for identifier in identifiers_lower)
    ]
    return bool(has_figure_label or independent_numbers or len(discriminative_identifiers) >= 2)


def _sentences(text_norm: str) -> list[str]:
    return [s.strip() for s in re.split(r"[.!?。！？;；\n]", text_norm) if len(s.strip()) >= EXACT_QUOTE_MIN_CHARS]


def _lexical_overlap(claim_norm: str, quote_norm: str) -> float:
    claim_tokens = set(claim_norm.split()) - {""}
    if not claim_tokens:
        return 0.0
    quote_tokens = set(quote_norm.split())
    return len(claim_tokens & quote_tokens) / len(claim_tokens)


def _eligible(citation: dict[str, Any], partition_intent: str | None) -> bool:
    if not citation.get("locatable"):
        return False
    if citation.get("toc_line"):
        return False
    if len(citation.get("page_numbers") or []) != 1:
        return False
    zone = str(citation.get("zone") or "")
    if partition_intent and zone != partition_intent:
        return False
    return True


def _physical_location(citation: dict[str, Any]) -> tuple[str, str, int] | None:
    pages = citation.get("page_numbers") or []
    if len(pages) != 1:
        return None
    try:
        page = int(pages[0])
    except (TypeError, ValueError):
        return None
    if page < 1:
        return None
    return (
        str(citation.get("_parse_revision_id") or citation.get("parse_revision_id") or ""),
        str(citation.get("file_id") or ""),
        page,
    )


def _ref_sort_key(citation: dict[str, Any]) -> tuple[int, str]:
    ref = str(citation.get("ref") or "")
    match = re.fullmatch(r"E(\d+)", ref)
    return (int(match.group(1)) if match else 10_000, ref)


def _verified_payload(
    matches: list[dict[str, Any]],
    *,
    method: str,
    confidence: float,
    proposed_ref: str | None,
) -> dict[str, Any]:
    locations = {_physical_location(citation) for citation in matches}
    locations.discard(None)
    if len(locations) != 1:
        return {
            "status": BINDING_MULTIPLE_MATCHES,
            "reason": f"{method.casefold()}_hits_multiple_physical_locations",
            "refs": [str(citation.get("ref")) for citation in sorted(matches, key=_ref_sort_key)],
        }
    selected = (
        next(
            (citation for citation in matches if str(citation.get("ref")) == str(proposed_ref or "")),
            None,
        )
        or sorted(matches, key=_ref_sort_key)[0]
    )
    return {
        "status": BINDING_VERIFIED,
        "ref": str(selected["ref"]),
        "evidence_id": selected.get("evidence_id"),
        "anchor_id": selected.get("_anchor_id") or (selected.get("anchor_ids") or [None])[0],
        "method": method,
        "confidence": confidence,
        "corrected_from": proposed_ref if proposed_ref and proposed_ref != str(selected["ref"]) else None,
    }


def resolve_binding(
    *,
    claim_context: str,
    proposed_ref: str | None = None,
    citations: list[dict[str, Any]],
    partition_intent: str | None = None,
) -> dict[str, Any]:
    """把「回答中的一段话」绑定到唯一证据条目（确定性，四层验证）。

    citations 条目需要：ref / zone / locatable / page_numbers / _quote_norm（内部键，
    归一化后的完整引文，永不进入模型 payload）。
    返回 {status, ref?, method?, confidence?, reason}；status 为 VERIFIED 才允许
    渲染页码，其余一律失败关闭。
    """
    claim_norm = normalize_for_match(claim_context)
    if not claim_norm:
        return {"status": BINDING_UNSUPPORTED, "reason": "empty_claim_context"}

    pool = [citation for citation in citations or [] if _eligible(citation, partition_intent)]
    if not pool:
        return {"status": BINDING_UNSUPPORTED, "reason": "no_eligible_candidates"}
    hard = extract_hard_constraints(claim_context)

    # 层 1：引文句包含判定（最强信号）。
    sentences = _sentences(claim_norm)
    if sentences:
        matched: list[dict[str, Any]] = [
            citation
            for citation in pool
            if any(sentence in str(citation.get("_quote_norm") or "") for sentence in sentences)
        ]
        if matched:
            return _verified_payload(
                matched,
                method=METHOD_EXACT_QUOTE,
                confidence=1.0,
                proposed_ref=proposed_ref,
            )

    # 层 2：科研硬约束（数字/基因/缩写必须逐个出现在引文中）。
    # 非区分性约束（单基因符号，且数字只是符号后缀如 OsMYB73 的 73）跳过
    # 本层——单基因名只标识论文主题不验证关系，落入层 3 词面重叠判定。
    if _hard_constraints_discriminative(hard):
        satisfied = [
            citation for citation in pool if _constraints_satisfied(hard, str(citation.get("_quote_norm") or ""))
        ]
        if satisfied:
            return _verified_payload(
                satisfied,
                method=METHOD_HARD_CONSTRAINTS,
                confidence=0.95,
                proposed_ref=proposed_ref,
            )

    # 层 3：归一化词面重叠（唯一显著领先才通过；解决 paraphrase 误绑必须从严）。
    scored = sorted(
        ((_lexical_overlap(claim_norm, str(citation.get("_quote_norm") or "")), citation) for citation in pool),
        key=lambda pair: pair[0],
        reverse=True,
    )
    if scored and scored[0][0] >= LEXICAL_VERIFIED_THRESHOLD:
        best_score, best = scored[0]
        best_location = _physical_location(best)
        same_location = [
            citation
            for score, citation in scored
            if _physical_location(citation) == best_location and score == best_score
        ]
        second_location_score = max(
            (score for score, citation in scored if _physical_location(citation) != best_location),
            default=0.0,
        )
        if best_score - second_location_score >= LEXICAL_LEAD_MARGIN:
            return _verified_payload(
                same_location,
                method=METHOD_LEXICAL_OVERLAP,
                confidence=round(best_score, 3),
                proposed_ref=proposed_ref,
            )

    # 层 4（语义 verifier）为预留扩展点：即使接入也只判「是否支持」，不判页码。
    return {"status": BINDING_UNSUPPORTED, "reason": "all_layers_unresolved"}
