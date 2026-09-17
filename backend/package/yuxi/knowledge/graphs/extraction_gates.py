"""抽取校验门 G1–G4：把 LLM 输出变成可度量、可拒绝的候选（确定性、无 I/O）。

- G1 结构/闭集：实体类型与谓词必须在闭集词表内；
- G2 逐字：实体 surface 与关系 evidence_quote 必须是主句原文子串（``find`` 命中）——
  幻觉率的直接度量口径 = G2 拒绝 ÷ 关系候选总数；
- G3 标识符来源：标识符形态的 surface 必须来自词法层预标注（LLM 不得生成标识符）；
- G4 身份不变量：非空、谓词大写归一、端点可解析。

G5（同 identity 跨单元合并）在抽取器聚合层完成；G6（人工抽检）是运营环节，不在代码内。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from yuxi.knowledge.graphs.extraction_units import ExtractionWindow
from yuxi.knowledge.graphs.lexicon import LexiconMatch, identifier_kind, identifier_values
from yuxi.knowledge.graphs.predicate_triggers import check_predicate_trigger

GATE_VERSION = "gates_v2"

G1_SCHEMA = "G1_SCHEMA"
G2_VERBATIM_SURFACE = "G2_VERBATIM_SURFACE"
G2_VERBATIM_QUOTE = "G2_VERBATIM_QUOTE"
G2_MISSING_ENDPOINT = "G2_MISSING_ENDPOINT"
G3_IDENTIFIER_PROVENANCE = "G3_IDENTIFIER_PROVENANCE"
G4_IDENTITY = "G4_IDENTITY"
G7_TRIGGER_UNVERIFIED = "G7_TRIGGER_UNVERIFIED"

_CONTEXT_FIELDS = ("direction", "directness", "condition", "tissue", "stage", "cultivar", "genetic_background")


@dataclass
class GateStats:
    entity_candidates: int = 0
    relation_candidates: int = 0
    accepted_entities: int = 0
    accepted_relations: int = 0
    trigger_verified_relations: int = 0
    trigger_unverified_relations: int = 0
    rejected: dict[str, int] = field(default_factory=dict)

    def reject(self, code: str) -> None:
        self.rejected[code] = self.rejected.get(code, 0) + 1

    def merge(self, other: GateStats) -> None:
        self.entity_candidates += other.entity_candidates
        self.relation_candidates += other.relation_candidates
        self.accepted_entities += other.accepted_entities
        self.accepted_relations += other.accepted_relations
        self.trigger_verified_relations += other.trigger_verified_relations
        self.trigger_unverified_relations += other.trigger_unverified_relations
        for code, count in other.rejected.items():
            self.rejected[code] = self.rejected.get(code, 0) + count

    @property
    def hallucination_rate(self) -> float | None:
        """G2 逐字失败占关系候选的比例；无候选时为 None（不产生 0 的假安心）。"""
        if self.relation_candidates == 0:
            return None
        failed = self.rejected.get(G2_VERBATIM_QUOTE, 0) + self.rejected.get(G2_MISSING_ENDPOINT, 0)
        return round(failed / self.relation_candidates, 4)

    def to_metadata(self) -> dict[str, Any]:
        return {
            "gate_version": GATE_VERSION,
            "entity_candidates": self.entity_candidates,
            "relation_candidates": self.relation_candidates,
            "accepted_entities": self.accepted_entities,
            "accepted_relations": self.accepted_relations,
            "trigger_verified_relations": self.trigger_verified_relations,
            "trigger_unverified_relations": self.trigger_unverified_relations,
            "rejected": dict(sorted(self.rejected.items())),
            "hallucination_rate": self.hallucination_rate,
            "identifier_violations": self.rejected.get(G3_IDENTIFIER_PROVENANCE, 0),
        }


@dataclass
class GateOutcome:
    entities: list[dict[str, Any]]
    relations: list[dict[str, Any]]
    stats: GateStats


def apply_gates(
    unit_result: Any,
    window: ExtractionWindow,
    preannotations: list[LexiconMatch],
    *,
    entity_types: tuple[str, ...] | frozenset[str],
    relation_types: frozenset[str],
    strict_triggers: bool = False,
) -> GateOutcome:
    """对一个抽取单元的 LLM 输出施加 G1–G4 与 G7 标记（strict_triggers 时 G7 未通过即拒绝）。"""
    stats = GateStats()
    allowed_types = set(entity_types)
    allowed_identifiers = identifier_values(preannotations)
    main_text = window.main_text
    if not isinstance(unit_result, dict):
        return GateOutcome([], [], stats)

    accepted_entities: list[dict[str, Any]] = []
    entity_by_surface: dict[str, dict[str, Any]] = {}
    for raw in unit_result.get("entities") or []:
        stats.entity_candidates += 1
        if not isinstance(raw, dict):
            stats.reject(G1_SCHEMA)
            continue
        surface = str(raw.get("surface") or "").strip()
        label = str(raw.get("type") or "").strip()
        if not surface:
            stats.reject(G4_IDENTITY)
            continue
        if label not in allowed_types:
            stats.reject(G1_SCHEMA)
            continue
        if surface not in main_text:
            stats.reject(G2_VERBATIM_SURFACE)
            continue
        kind = identifier_kind(surface)
        if kind is not None and surface not in allowed_identifiers:
            stats.reject(G3_IDENTIFIER_PROVENANCE)
            continue
        normalized_name = str(raw.get("normalized_name") or "").strip() or surface
        entity = {
            "surface": surface,
            "label": label,
            "normalized_name": normalized_name,
            "identifier_kind": kind,
            # 实体在本单元的逐字主句：节点「点开即见原文」的数据来源（G2 保证 surface ⊆ main_text）
            "mention_quote": main_text,
        }
        accepted_entities.append(entity)
        entity_by_surface[surface] = entity
        stats.accepted_entities += 1

    accepted_relations: list[dict[str, Any]] = []
    for raw in unit_result.get("relations") or []:
        stats.relation_candidates += 1
        if not isinstance(raw, dict):
            stats.reject(G1_SCHEMA)
            continue
        predicate = str(raw.get("predicate") or "").strip().upper()
        if predicate not in relation_types:
            stats.reject(G1_SCHEMA)
            continue
        subject = entity_by_surface.get(str(raw.get("subject") or "").strip())
        obj = entity_by_surface.get(str(raw.get("object") or "").strip())
        if subject is None or obj is None or subject is obj:
            stats.reject(G2_MISSING_ENDPOINT)
            continue
        quote = str(raw.get("evidence_quote") or "").strip()
        if not quote or quote not in main_text:
            stats.reject(G2_VERBATIM_QUOTE)
            continue
        trigger = check_predicate_trigger(predicate, main_text, subject["surface"], obj["surface"])
        if trigger.verified:
            stats.trigger_verified_relations += 1
        else:
            stats.trigger_unverified_relations += 1
            if strict_triggers:
                stats.reject(G7_TRIGGER_UNVERIFIED)
                continue
        accepted_relations.append(
            {
                "subject": subject["surface"],
                "predicate": predicate,
                "object": obj["surface"],
                "evidence_quote": quote,
                "confidence": _confidence(raw.get("confidence")),
                "hedge": bool(raw.get("hedge")),
                "context": _context(raw.get("context")),
                "trigger_verified": trigger.verified,
                "trigger_term": trigger.trigger,
            }
        )
        stats.accepted_relations += 1

    return GateOutcome(accepted_entities, accepted_relations, stats)


def _confidence(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0.0, number))


def _context(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    context: dict[str, str] = {}
    for key in _CONTEXT_FIELDS:
        item = value.get(key)
        if item is None:
            continue
        text = str(item).strip()
        if text and text.lower() not in {"null", "none", "unknown", "n/a"}:
            context[key] = text
    return context
