"""文档级指代与缩写词典（B2，Pass 1）：「该品种」们必须解析成规范实体名。

两阶段抽取的第一阶段产物：确定性规则把文档内的指代词（该品种/WT/上述处理…）
与缩写（括号分诊的 ABBREV_DEFINITION）解析成规范名，Pass 2（句窗抽取）注入
prompt 强制实体链接。规则优先、零模型；解析不了的指代不产出条目（宁缺勿错），
后续 LLM 兜底位走 source=LLM + 人工审核。

指代解析只影响实体归一，本身不产生图谱边。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from yuxi.knowledge.graphs.doclex.bracket_triage import (
    KIND_ABBREV_DEFINITION,
    KIND_ALIAS_TAXON,
    classify_brackets,
    taxon_alias_target,
)
from yuxi.knowledge.graphs.lexicon import pre_annotate

COREF_DICTIONARY_VERSION = "coref_dictionary_v1"

KIND_COREFERENCE = "COREFERENCE"
KIND_ABBREV = "ABBREV"

# 指代词 → 先行词目标实体 label（lexicon 词法命中兜底）
_DEMONSTRATIVE_TARGETS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"(?:该|上述|供试)品种"), "Cultivar"),
    (re.compile(r"(?:该|上述)品系"), "Cultivar"),
    (re.compile(r"野生型|wild[- ]type(?:\s+plants?)?|\bWT\b"), "Cultivar"),
    (re.compile(r"(?:该|上述)(?:突变体|株系)"), "AlleleMutant"),
    (re.compile(r"(?:该|上述)基因"), "Gene"),
    (re.compile(r"(?:该|上述|此)(?:处理|胁迫|条件)"), "Condition"),
)

# 先行词回看窗口（字符）
_ANTECEDENT_WINDOW = 4000


@dataclass(frozen=True)
class CoreferenceEntry:
    """一条文档词典条目：surface 在本文档内等价于 resolved_name（label 类型）。"""

    entry_kind: str  # COREFERENCE | ABBREV
    surface: str
    resolved_name: str
    resolved_label: str | None
    quote: str
    source: str = "RULE"


def build_coreference_dictionary(text: str) -> list[CoreferenceEntry]:
    """构建文档级词典：缩写展开（来自括号定义点）+ 指代先行词解析（词法锚定）。"""
    annotations = classify_brackets(text)
    entries: list[CoreferenceEntry] = []
    seen: set[tuple[str, str]] = set()

    def push(entry: CoreferenceEntry) -> None:
        key = (entry.surface.lower(), entry.resolved_name.lower())
        if key in seen:
            return
        seen.add(key)
        entries.append(entry)

    # 1. 缩写定义点：HT = high-temperature treatment（surface 逐字来自括号，天然过 G2）
    for annotation in annotations:
        if annotation.kind == KIND_ABBREV_DEFINITION and annotation.expansion:
            push(
                CoreferenceEntry(
                    entry_kind=KIND_ABBREV,
                    surface=annotation.inner,
                    resolved_name=annotation.expansion,
                    resolved_label=_label_for_term(annotation.expansion),
                    quote=text[max(0, annotation.start - 80) : annotation.end].strip(),
                )
            )
        elif annotation.kind == KIND_ALIAS_TAXON:
            target = taxon_alias_target(text, annotation)
            if target:
                push(
                    CoreferenceEntry(
                        entry_kind=KIND_COREFERENCE,
                        surface=annotation.inner,
                        resolved_name=target,
                        resolved_label="Cultivar",
                        quote=annotation.surface,
                    )
                )

    # 2. 指代词 → 最近先行词（词法命中锚定；学名括号的配对品种名也算 Cultivar 候选）
    taxon_candidates = [
        (annotation.start, taxon_alias_target(text, annotation) or annotation.inner)
        for annotation in annotations
        if annotation.kind == KIND_ALIAS_TAXON
    ]
    for pattern, target_label in _DEMONSTRATIVE_TARGETS:
        for match in pattern.finditer(text):
            surface = match.group(0)
            resolved = _resolve_antecedent(text, match.start(), target_label, taxon_candidates)
            if not resolved:
                continue
            push(
                CoreferenceEntry(
                    entry_kind=KIND_COREFERENCE,
                    surface=surface,
                    resolved_name=resolved,
                    resolved_label=target_label,
                    quote=text[max(0, match.start() - 60) : match.end() + 60].strip(),
                )
            )
    return entries


def _resolve_antecedent(
    text: str,
    position: int,
    target_label: str,
    taxon_candidates: list[tuple[int, str]],
) -> str | None:
    """回看最近先行词：同 label 的词法命中，或最近的学名配对品种名（Cultivar）。"""
    window = text[max(0, position - _ANTECEDENT_WINDOW) : position]
    matches = [match for match in pre_annotate(window) if match.label == target_label]
    taxon = [resolved for start, resolved in taxon_candidates if start < position]
    lexicon_last = matches[-1].surface if matches else None
    taxon_last = taxon[-1] if taxon else None
    if lexicon_last and taxon_last:
        # 谁离指代词更近用谁：比较两者在回看窗口中的最后出现位置
        lexicon_pos = window.rfind(lexicon_last)
        taxon_pos = window.rfind(taxon_last)
        return lexicon_last if lexicon_pos >= taxon_pos else taxon_last
    return lexicon_last or taxon_last


def _label_for_term(term: str) -> str | None:
    """缩写展开式的语义类型（lexicon 命中）：HT→Condition、qRT-PCR→Method 等。"""
    matches = pre_annotate(term)
    return matches[0].label if matches else None
