"""抽取校验门 G1–G4、G7、G9：把 LLM 输出变成可度量、可拒绝、可送审的候选（确定性、无 I/O）。

- G1 结构/闭集：实体类型与谓词必须在闭集词表内；
- G2 逐字：实体 surface 与关系 evidence_quote 必须是主句原文子串（``find`` 命中）——
  幻觉率的直接度量口径 = G2 拒绝 ÷ 关系候选总数；
- G3 标识符来源：标识符形态的 surface 必须来自词法层预标注（LLM 不得生成标识符）；
- G4 身份不变量：非空、谓词大写归一、端点可解析；
- G7 谓词触发词：strict 模式下未通过不再直接丢弃，改判 REVIEW（可疑数据进人工队列）；
- G9 极性否定：引文含否定词而模型未标 negative 时改判 REVIEW——否定词可能修饰
  其他成分，程序无法确定性裁决，丢弃与放行都不可接受。

产物三路由：PASS（进图谱）/ REVIEW（``GateOutcome.reviews``，服务层落审核队列表）/
DROP（仅计数）。G5（同 identity 跨单元合并）在抽取器聚合层完成；G6（人工抽检）
是运营环节，不在代码内。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from yuxi.knowledge.graphs.extraction_units import ExtractionWindow
from yuxi.knowledge.graphs.lexicon import LexiconMatch, identifier_kind, identifier_values
from yuxi.knowledge.graphs.predicate_triggers import check_predicate_trigger

GATE_VERSION = "gates_v4"

G1_SCHEMA = "G1_SCHEMA"
G2_VERBATIM_SURFACE = "G2_VERBATIM_SURFACE"
G2_VERBATIM_QUOTE = "G2_VERBATIM_QUOTE"
G2_MISSING_ENDPOINT = "G2_MISSING_ENDPOINT"
G3_IDENTIFIER_PROVENANCE = "G3_IDENTIFIER_PROVENANCE"
G4_IDENTITY = "G4_IDENTITY"
G7_TRIGGER_UNVERIFIED = "G7_TRIGGER_UNVERIFIED"
G9_NEGATION_REVIEW = "G9_NEGATION_REVIEW"
# R5a：引文未覆盖关系端点（只抄了谓词片段）——送审不拒
G9_SPAN_SUPPORT = "G9_SPAN_SUPPORT"
# R5b：LLM 显式标注的未归类概念（本体漂移信号）——送审 RETYPE 定型
NEW_CONCEPT_LABEL = "NEW_CONCEPT"

# 否定标记（G9）：引文中出现而模型未标 negative 时的送审信号。
# 只收高置信形态，避免 "not only/no more than" 类修饰误伤过多。
_NEGATION_MARKERS = (
    " not ",
    "n't ",
    " failed to",
    " lacks",
    " lacked",
    " absence of",
    " without ",
    "无",
    "未",
    "不",
    "缺乏",
    "缺少",
)

_CONTEXT_FIELDS = ("direction", "directness", "condition", "tissue", "stage", "cultivar", "genetic_background")
# N 元组维度（D5）：polarity/magnitude/baseline 是关系的一等语境维度，
# 经 context 透传后在仓储层列化（不再只有 context_json 自由 JSON）
_NARY_CONTEXT_FIELDS = ("polarity", "magnitude", "baseline")
_POLARITY_VALUES = frozenset({"positive", "negative", "neutral"})
_MAGNITUDE_VALUES = frozenset({"significant", "moderate", "slight"})


def contains_negation(text: str) -> bool:
    """引文是否含否定标记（G9 送审判定；中英高置信形态）。"""
    padded = f" {text} "
    return any(marker in padded or marker in text for marker in _NEGATION_MARKERS)


@dataclass
class GateStats:
    entity_candidates: int = 0
    relation_candidates: int = 0
    accepted_entities: int = 0
    accepted_relations: int = 0
    trigger_verified_relations: int = 0
    trigger_unverified_relations: int = 0
    rejected: dict[str, int] = field(default_factory=dict)
    review_routed: dict[str, int] = field(default_factory=dict)

    def reject(self, code: str) -> None:
        self.rejected[code] = self.rejected.get(code, 0) + 1

    def route_review(self, code: str) -> None:
        self.review_routed[code] = self.review_routed.get(code, 0) + 1

    def merge(self, other: GateStats) -> None:
        self.entity_candidates += other.entity_candidates
        self.relation_candidates += other.relation_candidates
        self.accepted_entities += other.accepted_entities
        self.accepted_relations += other.accepted_relations
        self.trigger_verified_relations += other.trigger_verified_relations
        self.trigger_unverified_relations += other.trigger_unverified_relations
        for code, count in other.rejected.items():
            self.rejected[code] = self.rejected.get(code, 0) + count
        for code, count in other.review_routed.items():
            self.review_routed[code] = self.review_routed.get(code, 0) + count

    @property
    def hallucination_rate(self) -> float | None:
        """G2 逐字失败占关系候选的比例；无候选时为 None（不产生 0 的假安心）。

        分母包含 REVIEW 送审的候选——它们同样消耗了模型输出与审核预算。
        """
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
            "review_routed": dict(sorted(self.review_routed.items())),
            "review_total": sum(self.review_routed.values()),
            "hallucination_rate": self.hallucination_rate,
            "identifier_violations": self.rejected.get(G3_IDENTIFIER_PROVENANCE, 0),
        }


@dataclass
class GateOutcome:
    entities: list[dict[str, Any]]
    relations: list[dict[str, Any]]
    stats: GateStats
    # REVIEW 路由产物：不进图谱、不丢弃，服务层持久化到 knowledge_graph_gate_reviews
    reviews: list[dict[str, Any]] = field(default_factory=list)


def apply_gates(
    unit_result: Any,
    window: ExtractionWindow,
    preannotations: list[LexiconMatch],
    *,
    entity_types: tuple[str, ...] | frozenset[str],
    relation_types: frozenset[str],
    strict_triggers: bool = False,
) -> GateOutcome:
    """对一个抽取单元的 LLM 输出施加 G1–G4、G7 与 G9（strict_triggers 时 G7 未通过改判 REVIEW）。"""
    stats = GateStats()
    allowed_types = set(entity_types)
    allowed_identifiers = identifier_values(preannotations)
    main_text = window.main_text
    if not isinstance(unit_result, dict):
        return GateOutcome([], [], stats)

    accepted_entities: list[dict[str, Any]] = []
    entity_by_surface: dict[str, dict[str, Any]] = {}
    # REVIEW 路由产物（实体循环的 NEW_CONCEPT 与关系循环的 G7/G9 都会写入）
    reviews: list[dict[str, Any]] = []
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
        if label == NEW_CONCEPT_LABEL:
            # R5b 本体漂移逃生口：无法归入闭集的概念显式标注 NEW_CONCEPT——
            # 不进图谱（闭集永远是闭集），也不丢弃（可能是新本体类型的信号），
            # 送人工裁决（RETYPE 定型后入图）。G2 逐字校验照常。
            if surface not in main_text:
                stats.reject(G2_VERBATIM_SURFACE)
                continue
            stats.route_review(NEW_CONCEPT_LABEL)
            reviews.append(
                {
                    "gate_code": NEW_CONCEPT_LABEL,
                    "candidate_kind": "ENTITY",
                    "subject": surface,
                    "subject_label": str(raw.get("normalized_name") or "").strip() or surface,
                    "predicate": "NEW_CONCEPT",
                    "object": "",
                    "evidence_quote": main_text,
                    "confidence": _confidence(raw.get("confidence") if "confidence" in raw else None),
                    "hedge": False,
                    "context": _context(raw.get("context")),
                }
            )
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
        # R5a G9 span-supports：quote 必须覆盖两个端点——只抄谓词片段不含主客体的
        # 引文支撑不了关系（"did not localize" 不含 OsPIP2;1 也能过旧 G2）。
        # 合法短 quote 存在，故送审不拒。
        if subject["surface"] not in quote or obj["surface"] not in quote:
            stats.route_review(G9_SPAN_SUPPORT)
            reviews.append(
                {
                    **{
                        "subject": subject["surface"],
                        "subject_label": subject["label"],
                        "predicate": predicate,
                        "object": obj["surface"],
                        "object_label": obj["label"],
                        "evidence_quote": quote,
                        "confidence": _confidence(raw.get("confidence")),
                        "hedge": bool(raw.get("hedge")),
                        "context": _context(raw.get("context")),
                    },
                    "gate_code": G9_SPAN_SUPPORT,
                    "window_index": window.index,
                }
            )
            continue
        context = _context(raw.get("context"))
        candidate = {
            "subject": subject["surface"],
            "subject_label": subject["label"],
            "predicate": predicate,
            "object": obj["surface"],
            "object_label": obj["label"],
            "evidence_quote": quote,
            "confidence": _confidence(raw.get("confidence")),
            "hedge": bool(raw.get("hedge")),
            "context": context,
        }
        trigger = check_predicate_trigger(predicate, main_text, subject["surface"], obj["surface"])
        if trigger.verified:
            stats.trigger_verified_relations += 1
        else:
            stats.trigger_unverified_relations += 1
            if strict_triggers:
                # G7 strict 失败：句中找不到该谓词的触发词——可能确实是脑补，
                # 也可能是触发词典缺词；改判 REVIEW 由人工裁决，不再静默丢弃
                stats.route_review(G7_TRIGGER_UNVERIFIED)
                reviews.append({**candidate, "gate_code": G7_TRIGGER_UNVERIFIED, "window_index": window.index})
                continue
        # G9：引文含否定而模型未标 negative——否定词可能修饰其他成分，
        # 程序无法确定性裁决，送审而不是拒（负向关系错抽成正向是图谱级事故）
        if contains_negation(quote) and context.get("polarity") != "negative":
            stats.route_review(G9_NEGATION_REVIEW)
            reviews.append({**candidate, "gate_code": G9_NEGATION_REVIEW, "window_index": window.index})
            continue
        accepted_relations.append(
            {
                "subject": subject["surface"],
                "predicate": predicate,
                "object": obj["surface"],
                "evidence_quote": quote,
                "confidence": _confidence(raw.get("confidence")),
                "hedge": bool(raw.get("hedge")),
                "context": context,
                "trigger_verified": trigger.verified,
                "trigger_term": trigger.trigger,
            }
        )
        stats.accepted_relations += 1

    return GateOutcome(accepted_entities, accepted_relations, stats, reviews)


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
    for key in (*_CONTEXT_FIELDS, *_NARY_CONTEXT_FIELDS):
        item = value.get(key)
        if item is None:
            continue
        text = str(item).strip().lower() if key in _NARY_CONTEXT_FIELDS else str(item).strip()
        if not text or text in {"null", "none", "unknown", "n/a"}:
            continue
        if key == "polarity" and text not in _POLARITY_VALUES:
            continue
        if key == "magnitude" and text not in _MAGNITUDE_VALUES:
            continue
        context[key] = text
    return context
