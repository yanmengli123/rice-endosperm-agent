"""Stage B 纯函数层：括号分诊（B1）、时间区间定义（B3）、指代/缩写词典（B2）。"""

from __future__ import annotations

from yuxi.knowledge.graphs.doclex.bracket_triage import (
    KIND_ABBREV_DEFINITION,
    KIND_ALIAS_TAXON,
    KIND_CONDITION_PARAMETER,
    KIND_EQUIPMENT_ATTRIBUTE,
    KIND_LOCAL_RULE,
    KIND_UNCERTAIN,
    classify_brackets,
    taxon_alias_target,
)
from yuxi.knowledge.graphs.doclex.coref_dictionary import build_coreference_dictionary
from yuxi.knowledge.graphs.doclex.temporal_definitions import parse_temporal_definitions
from yuxi.knowledge.graphs.lexicon import LEXICON_VERSION, pre_annotate


def _kinds(text):
    return {annotation.inner: annotation for annotation in classify_brackets(text)}


# ── B1 括号分诊：先分类，再归一 ──────────────────────────────────


def test_taxon_bracket_is_alias_not_attribute():
    text = "以秋田小町（O. sativa L. cv. Akitakomachi）为材料。"
    annotations = classify_brackets(text)
    assert len(annotations) == 1
    assert annotations[0].kind == KIND_ALIAS_TAXON
    assert taxon_alias_target(text, annotations[0]) == "秋田小町"


def test_english_abbreviation_definition_point():
    annotations = _kinds("plants were grown under high-temperature treatment (HT) for 5 days.")
    annotation = annotations["HT"]
    assert annotation.kind == KIND_ABBREV_DEFINITION
    assert annotation.expansion is not None
    assert "high-temperature treatment" in annotation.expansion


def test_chinese_abbreviation_definition_point():
    annotations = _kinds("幼苗经高温处理（HT）后取样。")
    annotation = annotations["HT"]
    assert annotation.kind == KIND_ABBREV_DEFINITION
    assert annotation.expansion == "高温处理"


def test_equipment_model_is_attribute_not_synonym():
    annotations = _kinds("Sections were cut on a microtome (Leica RM2265).")
    assert annotations["Leica RM2265"].kind == KIND_EQUIPMENT_ATTRIBUTE


def test_numeric_with_unit_is_condition_parameter():
    kinds = _kinds("Growth chamber set to (28 °C) day/(22 °C) night.")
    assert kinds["28 °C"].kind == KIND_CONDITION_PARAMETER
    assert kinds["22 °C"].kind == KIND_CONDITION_PARAMETER


def test_local_rule_stays_document_scoped():
    annotations = _kinds("取样时间（即抽穗后第10天）记录。")
    assert annotations["即抽穗后第10天"].kind == KIND_LOCAL_RULE


def test_unclassifiable_content_is_uncertain_not_alias():
    annotations = _kinds("Three independent lines were analyzed (three biological replicates).")
    assert annotations["three biological replicates"].kind == KIND_UNCERTAIN


# ── B3 时间区间定义：定义是断言，不是 sameAs ─────────────────────


def test_chinese_stage_interval_definition():
    text = "本文将灌浆中期定义为抽穗后10-25 dDAH。"
    definitions = parse_temporal_definitions(text)
    assert len(definitions) == 1
    definition = definitions[0]
    assert definition.entity_name == "灌浆中期"
    assert (definition.interval_start, definition.interval_end) == (10, 25)
    assert definition.interval_unit == "DAH"
    assert definition.ref_event == "heading"


def test_english_stage_interval_definition():
    text = "The middle grain filling stage was defined as 12–28 DAF in this study."
    definitions = parse_temporal_definitions(text)
    assert len(definitions) == 1
    assert definitions[0].entity_name == "middle grain filling stage"
    assert (definitions[0].interval_start, definitions[0].interval_end) == (12, 28)
    assert definitions[0].interval_unit == "DAF"


def test_interval_without_nearby_stage_is_not_a_definition():
    definitions = parse_temporal_definitions("Samples were collected at 10-25 DAP across all lines.")
    assert definitions == []


def test_conflicting_definitions_produce_distinct_intervals():
    # 文献 A 与文献 B 的口径差异必须可机器比较（DEFINITION 冲突检测的输入）
    doc_a = parse_temporal_definitions("灌浆中期为抽穗后10-25 dDAH。")
    doc_b = parse_temporal_definitions("灌浆中期为抽穗后12-28 dDAH。")
    intervals_a = {(d.interval_start, d.interval_end) for d in doc_a}
    intervals_b = {(d.interval_start, d.interval_end) for d in doc_b}
    assert intervals_a == {(10, 25)}
    assert intervals_b == {(12, 28)}
    assert intervals_a != intervals_b


# ── B2 指代与缩写词典 ───────────────────────────────────────────


def test_abbreviation_entry_carries_lexicon_label():
    entries = build_coreference_dictionary("幼苗经高温处理（HT）后取样。")
    abbreviations = [entry for entry in entries if entry.surface == "HT"]
    assert abbreviations
    assert abbreviations[0].resolved_name == "高温处理"
    assert abbreviations[0].resolved_label == "Condition"


def test_demonstrative_resolves_to_taxon_paired_cultivar():
    text = "以秋田小町（O. sativa L. cv. Akitakomachi）为材料。灌浆中期取样。该品种在高温下结实率显著下降。"
    entries = build_coreference_dictionary(text)
    demonstratives = [entry for entry in entries if entry.surface == "该品种"]
    assert demonstratives
    assert demonstratives[0].resolved_name == "秋田小町"
    assert demonstratives[0].resolved_label == "Cultivar"


def test_wt_resolves_to_nearest_cultivar_via_lexicon():
    text = "Nipponbare seeds were surface-sterilized. WT and mutant lines were grown together."
    entries = build_coreference_dictionary(text)
    wild_type = [entry for entry in entries if entry.surface == "WT"]
    assert wild_type
    assert wild_type[0].resolved_name == "Nipponbare"


def test_unresolvable_demonstrative_is_omitted():
    # 前文无任何候选先行词 → 不产出条目（宁缺勿错，不造「该品种」垃圾实体）
    entries = build_coreference_dictionary("处理后测定淀粉含量。该处理重复三次。")
    demonstratives = [entry for entry in entries if entry.surface == "该处理"]
    assert demonstratives == []


# ── 词典 v2：缩写作为领域词条（DAH/CT/HT/FW/DW）─────────────────


def test_lexicon_v2_recognizes_domain_abbreviations():
    labels = {match.surface: match.label for match in pre_annotate("DAH DAF CT HT FW DW")}
    assert labels["DAH"] == "DevelopmentStage"
    assert labels["DAF"] == "DevelopmentStage"
    assert labels["CT"] == "Condition"
    assert labels["HT"] == "Condition"
    assert labels["FW"] == "Phenotype"
    assert labels["DW"] == "Phenotype"


def test_lexicon_version_bumped_for_fingerprint_invalidation():
    assert LEXICON_VERSION == "rice-scientific-v2"
