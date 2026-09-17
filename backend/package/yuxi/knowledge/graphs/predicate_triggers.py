"""G7 谓词触发词门：确定性语义校验，防「张冠李戴」。

G2 逐字门只能证明 surface 与 evidence_quote 来自原文，证明不了句子真的陈述了该谓词
（两个真实实体 + 真实片段，关系却是模型脑补的），也拦不住 subject/object 颠倒。
本门用每个谓词的触发词词典 + 方向句式规则做纯规则复核：

- 触发词命中（matched）：句中存在该谓词语义的动词/短语（中英）；
- 方向合规（direction_ok）：主动句 subject < 触发词 < object；被动句 object 在前且句中有
  被动标记（by/被/受/由）；扰动类谓词（敲除/过表达…）触发词可在 subject 之前；
  对称谓词（共表达、直接结合）不判方向。

默认只做标记（``trigger_verified`` 进 mention 行，供信任分级与面板徽标），
``strict_triggers`` 模式下未通过即拒绝。零模型、可审计、词典随审阅反馈增补。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

TRIGGER_VERSION = "predicate_triggers_v1"

PREDICATE_TRIGGERS: dict[str, tuple[str, ...]] = {
    "DIRECT_BINDING": ("bind", "interact", "associat", "complex with", "结合", "互作", "相互作用", "形成复合体"),
    "TRANSCRIPTIONAL_ACTIVATION": (
        "activat",
        "transactivat",
        "upregulat",
        "up-regulat",
        "induc",
        "enhanc",
        "promot",
        "increas",
        "stimulat",
        "激活",
        "上调",
        "诱导",
        "促进",
        "增强",
        "提高",
    ),
    "TRANSCRIPTIONAL_REPRESSION": (
        "repress",
        "suppress",
        "inhibit",
        "downregulat",
        "down-regulat",
        "silenc",
        "reduc",
        "decreas",
        "抑制",
        "下调",
        "沉默",
        "降低",
        "减少",
    ),
    "TRANSCRIPTIONAL_REGULATION": ("regulat", "modulat", "control", "affect", "alter", "调控", "调节", "影响", "控制"),
    "PROTEIN_ACTIVITY_REGULATION": (
        "phosphorylat",
        "dephosphorylat",
        "activat",
        "inhibit",
        "modulat",
        "regulat",
        "stabiliz",
        "磷酸化",
        "活性",
        "稳定",
    ),
    "PROTEIN_DEGRADATION": ("degrad", "ubiquitinat", "proteolys", "turnover", "destabiliz", "降解", "泛素化"),
    "REQUIRED_FOR": ("requir", "essential", "necessary", "indispensable", "needed", "必需", "必要", "不可或缺", "依赖"),
    "PROMOTES_PROCESS": (
        "promot",
        "enhanc",
        "facilitat",
        "accelerat",
        "stimulat",
        "increas",
        "positively regulat",
        "促进",
        "增强",
        "加速",
        "正调控",
    ),
    "INHIBITS_PROCESS": (
        "inhibit",
        "suppress",
        "repress",
        "impair",
        "block",
        "delay",
        "negatively regulat",
        "reduc",
        "抑制",
        "阻断",
        "延迟",
        "负调控",
        "减弱",
    ),
    "REGULATES_PROCESS": (
        "regulat",
        "modulat",
        "control",
        "involved in",
        "mediat",
        "affect",
        "调控",
        "调节",
        "参与",
        "介导",
        "影响",
    ),
    "PROMOTES_PHENOTYPE": (
        "increas",
        "enhanc",
        "improv",
        "promot",
        "elevat",
        "larger",
        "higher",
        "增加",
        "提高",
        "增强",
        "改善",
        "更大",
        "更高",
    ),
    "SUPPRESSES_PHENOTYPE": (
        "decreas",
        "reduc",
        "suppress",
        "lower",
        "smaller",
        "impair",
        "diminish",
        "降低",
        "减少",
        "抑制",
        "更小",
        "更低",
        "减弱",
    ),
    "REGULATES_PHENOTYPE": (
        "regulat",
        "control",
        "affect",
        "determin",
        "modulat",
        "contribut",
        "调控",
        "影响",
        "决定",
        "调节",
        "贡献",
    ),
    "EXPRESSION_IN": (
        "express",
        "accumulat",
        "localiz",
        "detected in",
        "transcri",
        "abundant in",
        "表达",
        "积累",
        "定位",
        "富集",
        "检测到",
    ),
    "COEXPRESSION": ("co-express", "coexpress", "co-regulat", "correlated expression", "共表达", "共调控"),
    "MUTANT_EFFECT": (
        "show",
        "exhibit",
        "display",
        "result",
        "lead to",
        "led to",
        "caus",
        "reduc",
        "increas",
        "decreas",
        "impair",
        "enhanc",
        "defect",
        "phenotype",
        "表现出",
        "表现为",
        "导致",
        "引起",
        "降低",
        "增加",
        "减少",
        "缺陷",
    ),
    "KNOCKOUT_EFFECT": (
        "knockout",
        "knock-out",
        "knocked out",
        "loss of function",
        "loss-of-function",
        "null mutant",
        "disrupt",
        "敲除",
        "功能缺失",
    ),
    "CRISPR_EFFECT": ("crispr", "cas9", "edited", "editing", "gene-edited", "编辑"),
    "RNAI_EFFECT": ("rnai", "knockdown", "knock-down", "silenc", "interference", "干扰", "敲低", "沉默"),
    "OVEREXPRESSION_EFFECT": ("overexpress", "over-express", "ectopic expression", "过表达", "异位表达"),
    "ALLELE_OF": ("allele", "mutant of", "mutation in", "mutant allele", "等位", "突变体", "突变"),
}

# 触发词可出现在 subject 之前（"Overexpression of GIF1 increased …"）
_PERTURBATION_PREDICATES = frozenset({"KNOCKOUT_EFFECT", "CRISPR_EFFECT", "RNAI_EFFECT", "OVEREXPRESSION_EFFECT"})
# 不判方向
_SYMMETRIC_PREDICATES = frozenset({"COEXPRESSION", "DIRECT_BINDING"})
_PASSIVE_MARKERS = (" by ", "被", "受", "由")


@dataclass(frozen=True)
class TriggerCheck:
    matched: bool
    trigger: str | None
    direction_ok: bool | None

    @property
    def verified(self) -> bool:
        return self.matched and self.direction_ok is not False


def _compile(term: str) -> re.Pattern:
    if re.search(r"[A-Za-z]", term):
        # 英文词干前缀匹配：activat → activates/activated/activation
        return re.compile(rf"(?<![A-Za-z]){re.escape(term)}[a-z]*", re.IGNORECASE)
    return re.compile(re.escape(term))


_COMPILED: dict[str, tuple[tuple[str, re.Pattern], ...]] = {
    predicate: tuple((term, _compile(term)) for term in terms) for predicate, terms in PREDICATE_TRIGGERS.items()
}


def check_predicate_trigger(
    predicate: str,
    sentence: str,
    subject_surface: str,
    object_surface: str,
) -> TriggerCheck:
    """在主句上校验谓词触发词与方向。surface 由 G2 保证在句中，取首次出现位置。"""
    compiled = _COMPILED.get(predicate)
    if not compiled:
        return TriggerCheck(matched=False, trigger=None, direction_ok=None)

    hits: list[tuple[int, str]] = []
    for term, pattern in compiled:
        for match in pattern.finditer(sentence):
            hits.append((match.start(), term))
    if not hits:
        return TriggerCheck(matched=False, trigger=None, direction_ok=None)
    hits.sort()
    first_trigger = hits[0][1]

    if predicate in _SYMMETRIC_PREDICATES:
        return TriggerCheck(matched=True, trigger=first_trigger, direction_ok=None)

    subject_pos = sentence.find(subject_surface)
    object_pos = sentence.find(object_surface)
    if subject_pos < 0 or object_pos < 0 or subject_pos == object_pos:
        return TriggerCheck(matched=True, trigger=first_trigger, direction_ok=None)

    passive = any(marker in sentence for marker in _PASSIVE_MARKERS)
    if predicate in _PERTURBATION_PREDICATES:
        active_ok = subject_pos < object_pos and any(pos < object_pos for pos, _ in hits)
    else:
        active_ok = any(subject_pos < pos < object_pos for pos, _ in hits)
    passive_ok = passive and object_pos < subject_pos and any(pos > object_pos for pos, _ in hits)
    return TriggerCheck(matched=True, trigger=first_trigger, direction_ok=active_ok or passive_ok)
