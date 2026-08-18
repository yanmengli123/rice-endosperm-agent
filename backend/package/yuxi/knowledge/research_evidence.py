"""Deterministic scientific evidence semantics used by import and Graph-RAG.

The helpers in this module deliberately do not repair lossy identifiers.  A
scientific-notation PMID has already lost information and must be rebuilt from
an authoritative string source instead of being rounded back into an integer.
"""

from __future__ import annotations

import re
from typing import Any

PMID_PATTERN = re.compile(r"^[0-9]{6,10}$")
SCIENTIFIC_NOTATION_PATTERN = re.compile(r"^[+-]?(?:\d+\.\d+|\d+)[eE][+-]?\d+$")
SCIENTIFIC_IDENTIFIER_IN_TEXT = re.compile(r"(?<![\w.])\d\.\d{4,}[eE]\+?0?[6-9](?!\w)")
DOI_PATTERN = re.compile(r"^10\.\d{4,9}/\S+$", re.IGNORECASE)

OUTCOME_CLASSES = (
    "DIRECT_YIELD",
    "YIELD_COMPONENT",
    "GRAIN_MORPHOLOGY",
    "GRAIN_FILLING",
    "QUALITY",
    "CONDITION_SPECIFIC_YIELD",
    "OTHER",
)

_DIRECT_YIELD = ("grain yield", "yield per plant", "field yield", "plot yield", "籽粒产量", "单株产量")
_YIELD_COMPONENTS: dict[str, tuple[str, ...]] = {
    "THOUSAND_GRAIN_WEIGHT": ("1000-grain weight", "1000 grain weight", "thousand-grain weight", "千粒重"),
    "GRAIN_WEIGHT": ("grain weight", "kernel weight", "seed weight", "粒重"),
    "GRAIN_NUMBER_PER_PANICLE": ("grain number per panicle", "grains per panicle", "每穗粒数"),
    "PANICLE_NUMBER": ("panicle number", "panicles per plant", "穗数"),
    "SEED_SETTING_RATE": ("seed-setting rate", "seed setting rate", "结实率"),
}
_MORPHOLOGY = ("grain size", "grain length", "grain width", "grain shape", "seed size", "粒型", "粒长", "粒宽")
_FILLING = ("grain filling", "grain-filling", "filling rate", "灌浆")
_QUALITY = (
    "grain quality",
    "chalkiness",
    "amylose",
    "starch quality",
    "eating quality",
    "protein content",
    "品质",
    "垩白",
)
_CONDITION_TERMS: dict[str, tuple[str, ...]] = {
    "HIGH_TEMPERATURE": ("high temperature", "high-temperature", "heat stress", "高温"),
    "DROUGHT": ("drought", "water deficit", "干旱"),
    "SALT_STRESS": ("salt stress", "salinity", "盐胁迫"),
    "LOW_NITROGEN": ("low nitrogen", "nitrogen deficiency", "低氮", "缺氮"),
}


def validate_pmid(value: Any) -> tuple[str | None, str]:
    """Return a lossless PMID string and status; never infer rounded digits."""
    text = str(value or "").strip()
    if not text:
        return None, "MISSING"
    if SCIENTIFIC_NOTATION_PATTERN.fullmatch(text):
        return None, "INVALID_SCIENTIFIC_NOTATION"
    if not PMID_PATTERN.fullmatch(text):
        return None, "INVALID_FORMAT"
    return text, "VALID"


def validate_doi(value: Any) -> tuple[str | None, str]:
    text = str(value or "").strip()
    if not text:
        return None, "MISSING"
    if SCIENTIFIC_NOTATION_PATTERN.fullmatch(text):
        return None, "INVALID_SCIENTIFIC_NOTATION"
    if not DOI_PATTERN.fullmatch(text):
        return None, "INVALID_FORMAT"
    return text, "VALID"


def identifier_has_scientific_notation(value: Any) -> bool:
    return bool(SCIENTIFIC_NOTATION_PATTERN.fullmatch(str(value or "").strip()))


def sanitize_scientific_identifier_notation(text: Any) -> str:
    """Prevent lossy identifier-shaped numbers from reaching the answer model."""
    return SCIENTIFIC_IDENTIFIER_IN_TEXT.sub("[INVALID_IDENTIFIER_SCIENTIFIC_NOTATION]", str(text or ""))


def detect_condition(*parts: Any) -> str | None:
    text = " ".join(str(part or "") for part in parts).casefold()
    for condition, aliases in _CONDITION_TERMS.items():
        if any(alias.casefold() in text for alias in aliases):
            return condition
    return None


def classify_outcome(target_name: Any, condition: str | None = None) -> tuple[str, str | None]:
    target = str(target_name or "").strip().casefold()
    if any(term.casefold() in target for term in _DIRECT_YIELD):
        measure = "YIELD_PER_PLANT" if "per plant" in target or "单株" in target else "FIELD_YIELD"
        return ("CONDITION_SPECIFIC_YIELD" if condition else "DIRECT_YIELD", measure)
    for measure, aliases in _YIELD_COMPONENTS.items():
        if any(alias.casefold() in target for alias in aliases):
            return "YIELD_COMPONENT", measure
    if any(term.casefold() in target for term in _MORPHOLOGY):
        return "GRAIN_MORPHOLOGY", None
    if any(term.casefold() in target for term in _FILLING):
        return "GRAIN_FILLING", None
    if any(term.casefold() in target for term in _QUALITY):
        return "QUALITY", None
    return "OTHER", None


def infer_experimental_subject_type(
    name: Any,
    label: Any,
    relation_type: Any = None,
    quote: Any = None,
) -> str:
    normalized_name = str(name or "").strip().casefold()
    normalized_label = str(label or "").strip().casefold()
    intervention = " ".join(str(value or "").strip().casefold() for value in (relation_type, quote))
    if normalized_name.startswith("sttm"):
        return "STTM_CONSTRUCT"
    if "rnai" in normalized_name or "rnai" in intervention:
        return "RNAI_CONSTRUCT"
    if "crispr" in normalized_name or "crispr" in intervention:
        return "CRISPR_LINE"
    if any(marker in normalized_name or marker in intervention for marker in ("knockout", "knock-out")):
        return "KNOCKOUT_LINE"
    if "loss-of-function" in intervention or "loss of function" in intervention:
        return "KNOCKOUT_LINE"
    if "knockdown" in normalized_name or "knockdown" in intervention or "knock-down" in intervention:
        return "KNOCKDOWN_LINE"
    if (
        normalized_name.startswith("oe-")
        or "overexpression" in normalized_name
        or "overexpression" in intervention
        or "over-expression" in intervention
    ):
        return "OVEREXPRESSION_LINE"
    if (
        "allele" in normalized_label
        or "mutant" in normalized_label
        or "mutant" in intervention
        or "allele" in intervention
    ):
        return "ALLELE_MUTANT"
    if "treatment" in normalized_label:
        return "TREATMENT"
    if "rna" in normalized_label or normalized_name.startswith(("mir", "osa-mir")):
        return "MIRNA"
    if "gene" in normalized_label:
        return "GENE"
    return normalized_label.upper() or "UNKNOWN"


def build_evidence_semantics(
    *, source_name: Any, source_label: Any, relation_type: Any, target_name: Any, quote: Any, direction: Any
) -> dict[str, Any]:
    condition = detect_condition(quote, target_name)
    outcome_class, yield_measure_type = classify_outcome(target_name, condition)
    subject_type = infer_experimental_subject_type(source_name, source_label, relation_type, quote)
    source_text = str(source_name or "").strip()
    material_suffixes = {
        "KNOCKOUT_LINE": "knockout line",
        "KNOCKDOWN_LINE": "knockdown line",
        "OVEREXPRESSION_LINE": "overexpression line",
        "RNAI_CONSTRUCT": "RNAi construct",
        "CRISPR_LINE": "CRISPR line",
        "ALLELE_MUTANT": "mutant/allele",
    }
    subject_material = source_text or None
    if source_text and subject_type in material_suffixes:
        subject_material = f"{source_text} {material_suffixes[subject_type]}"
    perturbs = None
    perturbation_direction = None
    if subject_type == "STTM_CONSTRUCT":
        suffix = source_text[4:].strip("-_ ")
        perturbs = f"miR{suffix}" if suffix and not suffix.casefold().startswith("mir") else suffix or None
        perturbation_direction = "INHIBITION"
    return {
        "outcome_class": outcome_class,
        "yield_measure_type": yield_measure_type,
        "experimental_subject_type": subject_type,
        "subject_material": subject_material,
        "perturbs": perturbs,
        "perturbation_direction": perturbation_direction,
        "condition": condition,
        "observed_effect": str(direction or "").strip().upper() or None,
        "observed_relation": str(relation_type or "").strip().upper() or None,
        # Gene function inferred from a construct or mutant is intentionally not
        # materialized as an observed assertion.
        "inferred_gene_function": None,
    }


def is_yield_gene_query(query: Any) -> bool:
    text = str(query or "").casefold()
    has_yield = "grain yield" in text or "产量" in text
    has_gene = any(term in text for term in ("gene", "genes", "基因", "related", "involve", "涉及"))
    return has_yield and has_gene


def candidate_explicitly_requested(query: Any) -> bool:
    text = str(query or "").casefold()
    return any(term in text for term in ("candidate", "putative", "候选", "预测", "可能"))


def evidence_category_rank(outcome_class: Any) -> int:
    return {
        "DIRECT_YIELD": 0,
        "CONDITION_SPECIFIC_YIELD": 1,
        "YIELD_COMPONENT": 2,
        "GRAIN_FILLING": 3,
        "GRAIN_MORPHOLOGY": 3,
        "QUALITY": 4,
        "OTHER": 5,
    }.get(str(outcome_class or "OTHER").upper(), 5)
