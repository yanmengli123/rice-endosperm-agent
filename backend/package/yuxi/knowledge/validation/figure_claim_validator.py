"""Figure/Table narrative claim release gate.

This module validates *what an answer says about a named figure or table* before
``citation_channel`` signs the mention as an authoritative ``F#`` chip.  It is
deliberately separate from rendering:

* the figure-reference channel proves identity/provenance;
* this gate proves that an assertive description is compatible with the frozen
  caption evidence;
* asset projection proves whether an image/table can actually be shown.

The gate is deterministic and conservative.  It does not try to invent a
better scientific explanation.  Unsupported assertive sentences are removed;
navigation-only mentions ("see Figure 2") remain eligible for ordinary
reference signing.  This is the fail-closed boundary used when a semantic
repair model is unavailable or times out.
"""

from __future__ import annotations

import re
from typing import Any

from yuxi.knowledge.evidence.caption_locator import iter_figure_labels
from yuxi.knowledge.evidence.sentence_splitter import split_sentences

FIGURE_CLAIM_VALIDATOR_VERSION = "figure_claim_validator_v1"

VERDICT_SUPPORTED = "SUPPORTED"
VERDICT_UNSUPPORTED = "UNSUPPORTED"

REASON_NO_REGISTRY = "NO_REGISTRY"
REASON_AMBIGUOUS_SCOPE = "AMBIGUOUS_SCOPE"
REASON_CAPTION_SEMANTIC_MISMATCH = "CAPTION_SEMANTIC_MISMATCH"
REASON_EXPERIMENT_CONDITION_MISMATCH = "EXPERIMENT_CONDITION_MISMATCH"
REASON_INTERNAL_COMPARISON_CONTRADICTION = "INTERNAL_COMPARISON_CONTRADICTION"
REASON_PANEL_SEMANTIC_MISMATCH = "PANEL_SEMANTIC_MISMATCH"

# A sentence that merely navigates to a figure is not a semantic description.
# Assertive descriptions must pass the gate.  The patterns intentionally cover
# Chinese and English forms used by the built-in scientific-answer prompts.
# Navigation phrases ("详细结果见 Figure 99" / "see Figure 2 for details") are
# exempt: they point to a figure without asserting its content.  The exemption
# must be checked BEFORE the assertion pattern to avoid "结果" in "详细结果见"
# being misread as an assertion verb.
_NAVIGATION_PATTERN = re.compile(
    r"(?:详细结果|更多|相关|具体)?(?:见|参见|详见|参考|查阅|refer to|see)\s*(?:Figure|Fig\.?|Table|图|表)",
    flags=re.IGNORECASE,
)
_ASSERTION_PATTERN = re.compile(
    r"(?:显示|展示|表明|描述|给出|比较|用于|支持|证明|说明|反映|对应|位于|"
    r"归纳为|证据|结论|结果|shows?|displays?|describes?|compares?|"
    r"supports?|demonstrates?|indicates?|evidence|result)",
    flags=re.IGNORECASE,
)
_REFERENCE_LIST_PATTERN = re.compile(r"^\s*(?:依据来源|参考来源|sources?\s*:)", re.IGNORECASE)
_MARKDOWN_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_BULLET_PREFIX = re.compile(r"^(\s*(?:(?:[-*+]\s+)|(?:\d+[.)]\s+))?)(.*)$", re.DOTALL)
_BARE_SUPPLEMENTARY = re.compile(r"(?<![A-Za-z0-9])S(\d{1,3})(?![A-Za-z0-9])", re.IGNORECASE)
_EVIDENCE_ONLY_SENTENCE = re.compile(r"\s*(?:(?:\[E\d{1,3}\]|证据E\d{1,3})\s*)+", re.IGNORECASE)
_OPPOSITE_DIRECTION = re.compile(r"方向相反|相反(?:的)?表型|opposite\s+(?:direction|phenotype|effect)", re.IGNORECASE)
_CHALKINESS_MENTION = re.compile(r"垩白|腹白|chalk(?:y|iness)|white[- ]belly", re.IGNORECASE)
_UNIVERSAL_DIFFERENCE = re.compile(
    r"(?:所有|全部|各项|各指标).{0,16}(?:均|都)?(?:存在|具有|呈现)?(?:显著)?差异|"
    r"all\s+(?:traits?|indices|indicators?|measurements?).{0,24}(?:differ|significant)",
    re.IGNORECASE,
)
_CAPTION_UNIVERSAL_DIFFERENCE = re.compile(
    r"all\s+(?:traits?|indices|indicators?|measurements?).{0,24}(?:differ|significant)|"
    r"(?:所有|全部|各项|各指标).{0,16}(?:均|都).{0,8}(?:显著)?差异",
    re.IGNORECASE,
)
_MULTI_FIGURE_SUMMARY = re.compile(r"综上|汇总|总体|整体|共同|主要从|collectively|overall", re.IGNORECASE)

_PANEL_TERM_PATTERNS: dict[str, re.Pattern[str]] = {
    "grain_length": re.compile(r"粒长|grain\s+length", re.IGNORECASE),
    "filling_rate": re.compile(r"灌浆速率|grain\s+filling\s+rate", re.IGNORECASE),
    "grain_width": re.compile(r"粒宽|grain\s+width", re.IGNORECASE),
    "thousand_grain_weight": re.compile(r"千粒重|1000[- ]?grain\s+weight", re.IGNORECASE),
    "tiller": re.compile(r"分蘖|tillers?", re.IGNORECASE),
    "plant_height": re.compile(r"株高|plant\s+height", re.IGNORECASE),
    "chalkiness": re.compile(r"垩白|腹白|chalk", re.IGNORECASE),
}
_CAPTION_PANEL = re.compile(r"\(([a-z](?:\s*[-–—,，、]\s*[a-z])*)\)", re.IGNORECASE)
_CLAIM_PANEL = re.compile(
    r"(?P<context>.{0,28}?)(?:\(|（)?\s*(?:panel|分图)\s*(?P<panels>[a-z](?:\s*[-–—,，、]\s*[a-z])*)\s*(?:\)|）)?",
    re.IGNORECASE,
)


def _patterns(*items: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(item, re.IGNORECASE) for item in items)


# Only high-information concepts participate.  Generic words such as "analysis"
# or "phenotype" alone are too weak and would create false rejections.
_CONCEPT_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "overexpression": _patterns(r"过表达", r"\boverexpress(?:ion|ed|ing)?\b", r"\bOE[-_ ]?\w*"),
    "double_mutant": _patterns(
        r"双突变",
        r"\bdouble\s+mutants?\b",
        # Captions commonly encode a double mutant only as a genotype pair
        # (for example ``OsMYB73 + OsNF-YB1``) without spelling out the words
        # "double mutant".  The pair itself is an exact experimental identity.
        r"\bOs[A-Za-z0-9-]+\s*\+\s*Os[A-Za-z0-9-]+\b",
        r"\bmyb\w*\s*\+\s*(?:nf-|isa|ltpl)\w*\b",
    ),
    "heat_stress": _patterns(r"热胁迫", r"高温(?:处理|条件|胁迫)", r"\bheat\s+stress\b"),
    "starch_particle": _patterns(r"淀粉(?:颗粒|粒|体)(?:形态|排列)?", r"\bstarch\s+(?:granules?|particles?)\b"),
    "physicochemical": _patterns(
        r"理化(?:性质|性状)", r"糊化温度", r"直链淀粉", r"\bphysicochemical\b", r"\bamylose\b", r"\bgelatinization\b"
    ),
    "microscopy": _patterns(r"扫描电镜", r"透射电镜", r"电镜", r"\bSEM\b", r"\bTEM\b", r"microscop"),
    "grain_phenotype": _patterns(
        r"籽粒表型", r"粒长", r"粒宽", r"垩白", r"腹白", r"\bgrain\s+(?:phenotype|length|width|size|chalkiness)\b"
    ),
    "plant_morphology": _patterns(r"植株形态", r"株高", r"\bplant\s+morphology\b"),
    "rna_seq": _patterns(r"转录组", r"\bRNA[- ]?seq(?:uencing)?\b", r"transcriptom"),
    "venn": _patterns(r"韦恩", r"\bVenn\b"),
    "kegg": _patterns(r"\bKEGG\b"),
    "go": _patterns(r"(?:^|[^A-Za-z])GO(?:[^A-Za-z]|$)", r"Gene Ontology"),
    "heatmap": _patterns(r"热图", r"\bheat\s*map\b"),
    "pca": _patterns(r"主成分", r"\bPCA\b"),
    "metabolite": _patterns(r"代谢物", r"代谢组", r"\bmetabolites?\b", r"metabolom"),
    "binding_assay": _patterns(r"结合(?:实验|活性)", r"启动子", r"\bbinding\b", r"\bpromoter\b"),
    "auxin": _patterns(r"生长素", r"\bauxin\b", r"\bIAA\b"),
}

# Concepts for which absence from the caption is a hard experimental-identity
# conflict.  Other concepts are used to catch cross-class descriptions when the
# caption clearly belongs to a different class.
_STRICT_CONCEPTS = {"overexpression", "double_mutant", "heat_stress", "auxin"}
_CARRIER_CLASS_CONCEPTS = {
    "starch_particle",
    "physicochemical",
    "microscopy",
    "grain_phenotype",
    "plant_morphology",
    "rna_seq",
    "venn",
    "kegg",
    "go",
    "heatmap",
    "pca",
    "metabolite",
    "binding_assay",
}


def _concepts(text: str) -> set[str]:
    source = str(text or "")
    return {
        concept
        for concept, patterns in _CONCEPT_PATTERNS.items()
        if any(pattern.search(source) for pattern in patterns)
    }


def _panel_letters(value: str) -> list[str]:
    source = re.sub(r"\s+", "", str(value or "").lower()).replace("，", ",").replace("、", ",")
    if re.fullmatch(r"[a-z][-–—][a-z]", source):
        start, end = source[0], source[-1]
        if ord(start) <= ord(end) and ord(end) - ord(start) <= 12:
            return [chr(code) for code in range(ord(start), ord(end) + 1)]
    return [part for part in re.split(r"[,\-–—]", source) if re.fullmatch(r"[a-z]", part)]


def _term_names(text: str) -> set[str]:
    return {name for name, pattern in _PANEL_TERM_PATTERNS.items() if pattern.search(str(text or ""))}


def _caption_panel_terms(caption: str) -> dict[str, set[str]]:
    matches = list(_CAPTION_PANEL.finditer(str(caption or "")))
    panel_map: dict[str, set[str]] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(caption)
        terms = _term_names(caption[match.end() : end])
        for panel in _panel_letters(match.group(1)):
            panel_map[panel] = terms
    return panel_map


def _panel_claim_supported(sentence: str, caption: str) -> bool:
    claims = list(_CLAIM_PANEL.finditer(sentence))
    if not claims:
        return True
    panel_map = _caption_panel_terms(caption)
    if not panel_map:
        return False
    for claim in claims:
        terms = _term_names(claim.group("context"))
        if not terms:
            continue
        for panel in _panel_letters(claim.group("panels")):
            if panel not in panel_map or not (terms & panel_map[panel]):
                return False
    return True


def _sentence_mentions(sentence: str) -> list[dict[str, Any]]:
    mentions = iter_figure_labels(sentence)
    # Summary sentences often use compact "S4/S6/S8" notation.  Accept it only
    # when the line is visibly about supplementary figures, avoiding biological
    # identifiers that merely contain an S-number.
    if re.search(r"(?:补充|Supporting|归纳为|依据来源|S\d+\s*[/、])", sentence, re.IGNORECASE):
        existing = {item["base_key"] for item in mentions}
        for match in _BARE_SUPPLEMENTARY.finditer(sentence):
            key = f"figure s{match.group(1)}"
            if key in existing:
                continue
            mentions.append(
                {
                    "raw": match.group(0),
                    "start": match.start(),
                    "end": match.end(),
                    "canonical": key,
                    "base_key": key,
                    "kind": "figure",
                    "panel": "",
                }
            )
            existing.add(key)
    return sorted(mentions, key=lambda item: int(item["start"]))


def _validate_sentence(
    sentence: str,
    *,
    registry: dict[str, dict[str, Any]],
    inherited_mentions: list[dict[str, Any]] | None = None,
) -> tuple[bool, list[dict[str, Any]]]:
    mentions = _sentence_mentions(sentence) or list(inherited_mentions or [])
    if not mentions or _REFERENCE_LIST_PATTERN.match(sentence):
        return True, []
    if _NAVIGATION_PATTERN.search(sentence) and not re.search(
        r"显示|展示|表明|证明|说明|描述|比较|supports?|demonstrates?", sentence, re.IGNORECASE
    ):
        return True, []  # 导航提及（"见 Figure N"）不是断言
    if not _ASSERTION_PATTERN.search(sentence) and not _UNIVERSAL_DIFFERENCE.search(sentence):
        return True, []

    # A frequent scientific overclaim is to call two whole phenotypes
    # "opposite" even though the sentence itself says both are chalky (only
    # grain size is opposite). This is an internal contradiction and needs no
    # external semantic judge to reject.
    if _OPPOSITE_DIRECTION.search(sentence) and len(_CHALKINESS_MENTION.findall(sentence)) >= 2:
        return False, [
            {
                "label": mention["raw"],
                "key": str(mention["base_key"]),
                "verdict": VERDICT_UNSUPPORTED,
                "reason_code": REASON_INTERNAL_COMPARISON_CONTRADICTION,
            }
            for mention in mentions
        ]

    claim_concepts = _concepts(sentence)
    # A compact range summary (for example S17–S20 collectively supporting
    # metabolomics *and* annotation) describes the union of several captions.
    # Validate its carrier class against that frozen union; ordinary sentences
    # still bind each figure independently, so cross-figure laundering remains
    # blocked for panel-specific claims.
    aggregate_summary = len(mentions) >= 3 and bool(_MULTI_FIGURE_SUMMARY.search(sentence))
    aggregate_evidence_concepts: set[str] = set()
    if aggregate_summary:
        for item in mentions:
            aggregate_entry = registry.get(str(item["base_key"]))
            if aggregate_entry and not aggregate_entry.get("ambiguous"):
                aggregate_evidence_concepts.update(
                    _concepts(str(aggregate_entry.get("_caption_quote") or aggregate_entry.get("quote_head") or ""))
                )
    reports: list[dict[str, Any]] = []
    supported = True
    for mention in mentions:
        key = str(mention["base_key"])
        entry = registry.get(key)
        if entry is None:
            supported = False
            reports.append(
                {
                    "label": mention["raw"],
                    "key": key,
                    "verdict": VERDICT_UNSUPPORTED,
                    "reason_code": REASON_NO_REGISTRY,
                }
            )
            continue
        if entry.get("ambiguous"):
            supported = False
            reports.append(
                {
                    "label": mention["raw"],
                    "key": key,
                    "verdict": VERDICT_UNSUPPORTED,
                    "reason_code": REASON_AMBIGUOUS_SCOPE,
                }
            )
            continue

        caption_text = str(entry.get("_caption_quote") or entry.get("quote_head") or "")
        if not _panel_claim_supported(sentence, caption_text):
            supported = False
            reports.append(
                {
                    "label": str(entry.get("label") or mention["raw"]),
                    "key": key,
                    "verdict": VERDICT_UNSUPPORTED,
                    "reason_code": REASON_PANEL_SEMANTIC_MISMATCH,
                    "citation_ref": str(entry.get("citation_ref") or ""),
                }
            )
            continue
        if _UNIVERSAL_DIFFERENCE.search(sentence) and not _CAPTION_UNIVERSAL_DIFFERENCE.search(caption_text):
            supported = False
            reports.append(
                {
                    "label": str(entry.get("label") or mention["raw"]),
                    "key": key,
                    "verdict": VERDICT_UNSUPPORTED,
                    "reason_code": REASON_CAPTION_SEMANTIC_MISMATCH,
                    "missing_concepts": ["universal_difference"],
                    "citation_ref": str(entry.get("citation_ref") or ""),
                }
            )
            continue
        # A named figure is authorized by its own frozen caption only. Another
        # [E#] in the sentence may support surrounding interpretation, but it
        # must never lend its experimental identity to this F# (cross-figure
        # evidence laundering).
        evidence_concepts = _concepts(caption_text)
        strict_missing = sorted(_STRICT_CONCEPTS & claim_concepts - evidence_concepts)
        class_claims = _CARRIER_CLASS_CONCEPTS & claim_concepts
        class_evidence = _CARRIER_CLASS_CONCEPTS & evidence_concepts
        if aggregate_summary:
            class_mismatch = bool(
                class_claims and aggregate_evidence_concepts and not class_claims <= aggregate_evidence_concepts
            )
        else:
            class_mismatch = bool(class_claims and class_evidence and not (class_claims & class_evidence))
        if strict_missing:
            supported = False
            reason = (
                REASON_EXPERIMENT_CONDITION_MISMATCH
                if "heat_stress" in strict_missing
                else REASON_CAPTION_SEMANTIC_MISMATCH
            )
            reports.append(
                {
                    "label": str(entry.get("label") or mention["raw"]),
                    "key": key,
                    "verdict": VERDICT_UNSUPPORTED,
                    "reason_code": reason,
                    "missing_concepts": strict_missing,
                    "citation_ref": str(entry.get("citation_ref") or ""),
                }
            )
        elif class_mismatch:
            supported = False
            reports.append(
                {
                    "label": str(entry.get("label") or mention["raw"]),
                    "key": key,
                    "verdict": VERDICT_UNSUPPORTED,
                    "reason_code": REASON_CAPTION_SEMANTIC_MISMATCH,
                    "claim_concepts": sorted(class_claims),
                    "evidence_concepts": sorted(class_evidence),
                    "citation_ref": str(entry.get("citation_ref") or ""),
                }
            )
        else:
            reports.append(
                {
                    "label": str(entry.get("label") or mention["raw"]),
                    "key": key,
                    "verdict": VERDICT_SUPPORTED,
                    "reason_code": None,
                    "citation_ref": str(entry.get("citation_ref") or ""),
                }
            )
    return supported, reports


def validate_figure_claims(
    text: str,
    *,
    registry: dict[str, dict[str, Any]],
    citations: list[dict[str, Any]] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Remove assertive Figure/Table descriptions that do not match evidence.

    The returned report is safe for audit metadata: it contains short claim
    heads and closed reason codes, never full evidence bodies or internal
    registry state.
    """
    source = str(text or "")
    reports: list[dict[str, Any]] = []
    output: list[str] = []
    in_fence = False
    removed = 0
    for line in source.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            output.append(line)
            continue
        if in_fence or _MARKDOWN_TABLE_ROW.match(line) or line.lstrip().startswith("#"):
            output.append(line)
            continue
        prefix_match = _BULLET_PREFIX.match(line)
        prefix = prefix_match.group(1) if prefix_match else ""
        body = prefix_match.group(2) if prefix_match else line
        kept: list[str] = []
        previous_claim_removed = False
        inherited_mentions: list[dict[str, Any]] = []
        for sentence in split_sentences(body):
            direct_mentions = _sentence_mentions(sentence)
            if direct_mentions:
                inherited_mentions = direct_mentions
            ok, sentence_reports = _validate_sentence(
                sentence,
                registry=registry,
                inherited_mentions=inherited_mentions if _UNIVERSAL_DIFFERENCE.search(sentence) else None,
            )
            for report in sentence_reports:
                reports.append({**report, "claim_head": re.sub(r"\s+", " ", sentence).strip()[:120]})
            if ok:
                if previous_claim_removed and _EVIDENCE_ONLY_SENTENCE.fullmatch(sentence):
                    # The citation proposal belonged to the rejected claim.
                    # Keeping it would publish an orphan E# chip that appears
                    # to support an empty/deleted sentence.
                    continue
                kept.append(sentence)
                previous_claim_removed = False
            else:
                removed += 1
                previous_claim_removed = True
        rebuilt = "".join(kept).strip()
        if rebuilt:
            output.append(prefix + rebuilt)
        elif not body.strip():
            output.append(line)
    cleaned = "\n".join(output)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, {
        "version": FIGURE_CLAIM_VALIDATOR_VERSION,
        "status": "PASS" if removed == 0 else "DEGRADED",
        "checked_claim_count": len(reports),
        "unsupported_count": sum(1 for report in reports if report["verdict"] == VERDICT_UNSUPPORTED),
        "removed_sentence_count": removed,
        "claims": reports[:32],
    }


__all__ = [
    "FIGURE_CLAIM_VALIDATOR_VERSION",
    "REASON_AMBIGUOUS_SCOPE",
    "REASON_CAPTION_SEMANTIC_MISMATCH",
    "REASON_EXPERIMENT_CONDITION_MISMATCH",
    "REASON_INTERNAL_COMPARISON_CONTRADICTION",
    "REASON_NO_REGISTRY",
    "REASON_PANEL_SEMANTIC_MISMATCH",
    "VERDICT_SUPPORTED",
    "VERDICT_UNSUPPORTED",
    "validate_figure_claims",
]
