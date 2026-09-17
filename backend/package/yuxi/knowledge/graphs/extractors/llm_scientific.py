"""闭集词表科研抽取器（extractor_type = ``llm_scientific``）。

与通用 ``llm`` 抽取器的差异（全部服务于科研级可度量质量）：

1. 句窗抽取单元：检索 chunk 在服务内二次切成「主句 + 同段前后语境」，
   只允许从主句抽取（``extraction_units``）；
2. 闭集词表：实体类型与谓词分别限定在 ``SCIENTIFIC_ENTITY_TYPES`` 与托管导入的
   关系白名单内，LLM 只能选不能造；
3. 词法预标注注入：标识符/词典命中随单元一起给模型，标识符实体只承认预标注（G3）；
4. 校验门 G1–G4 在解析后逐候选执行，统计随 chunk 的 ``extraction_result.metadata`` 落库，
   幻觉率（G2 逐字失败率）成为每次构建可读的硬指标；
5. 批量窗口一次调用（默认 8 单元/次）、``temperature=0``、模型快照由服务层冻结。

输出形状兼容 ``normalize_extraction_result``：实体附 ``aliases``（surface 变体，服务层
收割进别名表）与标识符 ``attributes``；关系附 ``confidence/hedge/context``。
"""

from __future__ import annotations

from typing import Any

import json_repair

from yuxi.knowledge.graphs.extraction_gates import GateStats, apply_gates
from yuxi.knowledge.graphs.extraction_units import (
    EXTRACTION_UNIT_VERSION,
    ExtractionWindow,
    build_extraction_windows,
)
from yuxi.knowledge.graphs.graph_utils import normalize_entity_name
from yuxi.knowledge.graphs.lexicon import (
    LEXICON_VERSION,
    SCIENTIFIC_ENTITY_TYPES,
    LexiconMatch,
    lexicon_snapshot,
    pre_annotate,
)
from yuxi.knowledge.graphs.managed_import_parser import (
    ALLELE_RELATION_TYPES,
    GENE_RELATION_TYPES,
    PERTURBATION_RELATION_TYPES,
)
from yuxi.models.chat import select_model

from .base import GraphExtractor

PROMPT_VERSION = "scientific_v1"
VERIFIER_PROMPT_VERSION = "verifier_v1"
G8_VERIFIER_REJECTED = "G8_VERIFIER_REJECTED"
DEFAULT_BATCH_SIZE = 8
MAX_BATCH_SIZE = 32
DEFAULT_CONTEXT_SENTENCES = 1

SCIENTIFIC_RELATION_TYPES: frozenset[str] = frozenset(
    GENE_RELATION_TYPES | ALLELE_RELATION_TYPES | PERTURBATION_RELATION_TYPES | {"ALLELE_OF"}
)

_ENTITY_TYPE_GUIDE = {
    "Gene": "基因（symbol 或 RAP/MSU 编号；蛋白以基因名指代时也记为 Gene）",
    "AlleleMutant": "等位基因/突变体/转基因株系（如 gif1、crispr-osnf-yb1、OE-GIF1）",
    "Phenotype": "可观测性状（垩白、粒重、淀粉含量、灌浆速率等）",
    "Process": "生物过程（淀粉合成、胚乳发育、程序性细胞死亡等）",
    "QTL": "数量性状位点",
    "CisElement": "顺式作用元件/启动子 motif",
    "RNA": "非编码 RNA、miRNA、转录本",
    "Protein": "以蛋白本体讨论的分子（复合体、酶、转录因子蛋白）",
    "Pathway": "信号或代谢通路",
    "Tissue": "组织/器官（胚乳、糊粉层、穗、根、叶）",
    "DevelopmentStage": "发育时期（10 DAP、灌浆期、抽穗期）",
    "Condition": "处理/环境条件与激素处理（ABA 处理、干旱、高温）",
    "Cultivar": "品种/材料背景（Nipponbare、9311、ZH11）",
    "Experiment": "特定实验/试验体系",
    "Publication": "文献（仅在句中以文献为主体时）",
    "Method": "实验方法（qRT-PCR、CRISPR/Cas9、Y2H、EMSA）",
}

_RELATION_GUIDE = {
    "DIRECT_BINDING": "subject 直接结合 object（蛋白-DNA 或蛋白-蛋白）",
    "TRANSCRIPTIONAL_ACTIVATION": "subject 转录激活 object 基因",
    "TRANSCRIPTIONAL_REPRESSION": "subject 转录抑制 object 基因",
    "TRANSCRIPTIONAL_REGULATION": "subject 转录调控 object 基因（方向未明）",
    "PROTEIN_ACTIVITY_REGULATION": "subject 调节 object 的蛋白活性",
    "PROTEIN_DEGRADATION": "subject 介导 object 蛋白降解",
    "REQUIRED_FOR": "subject 为 object（过程/表型）所必需",
    "PROMOTES_PROCESS": "subject 促进 object 生物过程",
    "INHIBITS_PROCESS": "subject 抑制 object 生物过程",
    "REGULATES_PROCESS": "subject 调控 object 生物过程（方向未明）",
    "PROMOTES_PHENOTYPE": "subject 增强 object 表型",
    "SUPPRESSES_PHENOTYPE": "subject 削弱 object 表型",
    "REGULATES_PHENOTYPE": "subject 调控 object 表型（方向未明）",
    "EXPRESSION_IN": "subject 基因在 object 组织/时期中表达",
    "COEXPRESSION": "subject 与 object 共表达",
    "MUTANT_EFFECT": "subject 突变体/等位导致 object 表型或过程改变",
    "KNOCKOUT_EFFECT": "敲除 subject 基因导致 object 改变",
    "CRISPR_EFFECT": "CRISPR 编辑 subject 基因导致 object 改变",
    "RNAI_EFFECT": "RNAi 干扰 subject 基因导致 object 改变",
    "OVEREXPRESSION_EFFECT": "过表达 subject 基因导致 object 改变",
    "ALLELE_OF": "subject 突变体/等位是 object 基因的等位",
}

_SYSTEM_RULES = """你是水稻分子生物学文献的实体与关系标注器。输入是若干抽取单元，每个单元有「主句」与「语境」。
铁律（违反任一条的输出会被程序拒绝并计入错误率）：
1. 实体 type 只能从 ENTITY_TYPES 枚举中选择；关系 predicate 只能从 RELATION_TYPES 枚举中选择，
   并遵守方向约定：subject 是施加方/调节因子/被扰动基因，object 是受动方/靶/结果。
2. 只从「主句」抽取；「语境」仅用于理解指代，禁止从语境句抽取任何实体或关系。
3. entity.surface 与 relation.evidence_quote 必须逐字复制主句原文的连续子串，禁止改写、翻译、补全、合并。
4. 标识符类实体（RAP/MSU 基因编号、DOI、PMID）只能确认「预标注」中已列出的，不得新增。
5. 只抽取主句中明确陈述的关系，禁止推断；含 may/might/suggest/possibly/可能/提示 等推测语气的关系照抽但 hedge=true。
6. normalized_name 给出该实体的规范名（基因用官方 symbol，条件/组织用英文小写通名）；不确定就复制 surface。
7. 不确定一律省略——漏标代价低于错标。空单元输出空数组。
输出严格 JSON，不要输出解释：
{"units": [{"unit": <单元编号>, "entities": [{"surface": str, "type": str, "normalized_name": str}],
  "relations": [{"subject": <实体surface>, "predicate": str, "object": <实体surface>, "evidence_quote": str,
  "confidence": 0~1, "hedge": bool,
  "context": {"direction": "activates|inhibits|null", "directness": "direct|indirect|null", "condition": str|null,
  "tissue": str|null, "stage": str|null, "cultivar": str|null, "genetic_background": str|null}}]}]}"""

_VERIFIER_RULES = """你是关系抽取复核器（独立第二模型）。对每条候选关系，只判断「主句是否明确陈述了该关系且方向正确」。
规则：
1. 只依据主句本身，禁止依据常识或推断；主句没有明确说的一律 supported=false。
2. supported=true 时，span 必须逐字复制主句中陈述该关系的连续片段（含两个实体与谓词动词），禁止改写。
3. 方向错误（subject/object 颠倒）判 false；推测语气（may/suggest/可能）若主句确有陈述可判 true。
4. 不确定一律 false——漏判代价低于误判。
输出严格 JSON，不要输出解释：{"verdicts": [{"id": <候选编号>, "supported": bool, "span": str|null}]}"""


class LLMScientificGraphExtractor(GraphExtractor):
    extractor_type = "llm_scientific"

    def validate_options(self) -> None:
        if not self.options.get("model_spec"):
            raise ValueError("科研抽取器需要 model_spec")
        if self.options.get("prompt") or self.options.get("schema"):
            raise ValueError("科研抽取器使用固定闭集词表 Prompt，不支持自定义 prompt/schema")
        concurrency_count = self.options.get("concurrency_count", 1)
        try:
            concurrency_count = int(concurrency_count)
        except (TypeError, ValueError) as exc:
            raise ValueError("科研抽取器 concurrency_count 必须是整数") from exc
        if concurrency_count < 1 or concurrency_count > 1000:
            raise ValueError("科研抽取器 concurrency_count 必须在 1 到 1000 之间")
        batch_size = self.options.get("batch_size", DEFAULT_BATCH_SIZE)
        try:
            batch_size = int(batch_size)
        except (TypeError, ValueError) as exc:
            raise ValueError("科研抽取器 batch_size 必须是整数") from exc
        if batch_size < 1 or batch_size > MAX_BATCH_SIZE:
            raise ValueError(f"科研抽取器 batch_size 必须在 1 到 {MAX_BATCH_SIZE} 之间")
        context_sentences = self.options.get("context_sentences", DEFAULT_CONTEXT_SENTENCES)
        if context_sentences not in (0, 1, 2):
            raise ValueError("科研抽取器 context_sentences 只能是 0、1 或 2")
        if self.options.get("model_params") is not None and not isinstance(self.options["model_params"], dict):
            raise ValueError("科研抽取器 model_params 必须是对象")
        if not isinstance(self.options.get("strict_triggers", False), bool):
            raise ValueError("科研抽取器 strict_triggers 必须是布尔值")
        verifier = self.options.get("verifier_model_spec")
        if verifier is not None and (not isinstance(verifier, str) or not verifier.strip()):
            raise ValueError("科研抽取器 verifier_model_spec 必须是非空字符串")

    @property
    def batch_size(self) -> int:
        return int(self.options.get("batch_size", DEFAULT_BATCH_SIZE))

    @property
    def context_sentences(self) -> int:
        return int(self.options.get("context_sentences", DEFAULT_CONTEXT_SENTENCES))

    @property
    def strict_triggers(self) -> bool:
        return bool(self.options.get("strict_triggers", False))

    @property
    def verifier_model_spec(self) -> str | None:
        value = self.options.get("verifier_model_spec")
        return value.strip() if isinstance(value, str) and value.strip() else None

    def model_params(self) -> dict[str, Any]:
        """温度默认锁 0：抽取是判别任务，可复现性优先于多样性。"""
        return {"temperature": 0, **(self.options.get("model_params") or {})}

    async def extract(self, text: str, *, chunk_metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        self.validate_options()
        windows = build_extraction_windows(text, context_sentences=self.context_sentences)
        preannotations = {window.index: pre_annotate(window.main_text) for window in windows}
        stats = GateStats()
        accepted_entities: list[dict[str, Any]] = []
        accepted_relations: list[dict[str, Any]] = []
        llm_calls = 0
        verifier_calls = 0

        if windows:
            model = select_model(
                model_spec=self.options["model_spec"],
                timeout=90.0,
                model_params=self.model_params(),
            )
            verifier = (
                select_model(model_spec=self.verifier_model_spec, timeout=90.0, model_params={"temperature": 0})
                if self.verifier_model_spec
                else None
            )
            for start in range(0, len(windows), self.batch_size):
                batch = windows[start : start + self.batch_size]
                prompt = self.build_prompt(batch, preannotations)
                response = await model.call(prompt, stream=False)
                llm_calls += 1
                units_by_index = self._parse_units(response.content if response else "")
                batch_relations: list[tuple[dict[str, Any], ExtractionWindow]] = []
                for window in batch:
                    outcome = apply_gates(
                        units_by_index.get(window.index + 1, {}),
                        window,
                        preannotations[window.index],
                        entity_types=SCIENTIFIC_ENTITY_TYPES,
                        relation_types=SCIENTIFIC_RELATION_TYPES,
                        strict_triggers=self.strict_triggers,
                    )
                    stats.merge(outcome.stats)
                    accepted_entities.extend(outcome.entities)
                    batch_relations.extend((relation, window) for relation in outcome.relations)
                if verifier is not None and batch_relations:
                    verifier_response = await verifier.call(self.build_verifier_prompt(batch_relations), stream=False)
                    verifier_calls += 1
                    confirmed = self._parse_verdicts(
                        verifier_response.content if verifier_response else "", batch_relations
                    )
                    for index, (relation, window) in enumerate(batch_relations):
                        if index in confirmed:
                            relation["verifier_confirmed"] = True
                        else:
                            stats.reject(G8_VERIFIER_REJECTED)
                            stats.accepted_relations -= 1
                    batch_relations = [item for index, item in enumerate(batch_relations) if index in confirmed]
                accepted_relations.extend(
                    {**relation, "window_index": window.index} for relation, window in batch_relations
                )

        result = self.aggregate(accepted_entities, accepted_relations)
        result["metadata"] = {
            "extractor_type": self.extractor_type,
            "schema_version": 2,
            "prompt_version": PROMPT_VERSION,
            "lexicon_version": LEXICON_VERSION,
            "extraction_unit_version": EXTRACTION_UNIT_VERSION,
            "model_spec": self.options["model_spec"],
            "strict_triggers": self.strict_triggers,
            "verifier_model_spec": self.verifier_model_spec,
            "verifier_prompt_version": VERIFIER_PROMPT_VERSION if self.verifier_model_spec else None,
            "windows": len(windows),
            "llm_calls": llm_calls,
            "verifier_calls": verifier_calls,
            "batch_size": self.batch_size,
            "lexicon": lexicon_snapshot(),
            "gates": stats.to_metadata(),
        }
        return result

    @staticmethod
    def build_verifier_prompt(batch_relations: list[tuple[dict[str, Any], ExtractionWindow]]) -> str:
        """双模型复核提示词：只允许「支持/不支持 + 逐字 span」，不允许自由发挥。"""
        lines = []
        for index, (relation, window) in enumerate(batch_relations):
            guide = _RELATION_GUIDE.get(relation["predicate"], "")
            lines.append(
                f"{index + 1}. 主句：{window.main_text}\n"
                f"   候选关系：{relation['subject']} --{relation['predicate']}（{guide}）--> {relation['object']}"
            )
        return f"{_VERIFIER_RULES}\n\n候选（共 {len(batch_relations)} 条）：\n" + "\n".join(lines)

    @staticmethod
    def _parse_verdicts(content: str, batch_relations: list[tuple[dict[str, Any], ExtractionWindow]]) -> set[int]:
        """返回被复核确认的候选下标：supported 为真且 span 逐字来自该主句；解析失败视为全部未确认。"""
        parsed = json_repair.loads(content or "")
        verdicts = parsed.get("verdicts") if isinstance(parsed, dict) else parsed
        if not isinstance(verdicts, list):
            raise ValueError(f"复核模型响应缺少 verdicts 数组: {str(content)[:200]}")
        confirmed: set[int] = set()
        for verdict in verdicts:
            if not isinstance(verdict, dict):
                continue
            try:
                index = int(verdict.get("id")) - 1
            except (TypeError, ValueError):
                continue
            if index < 0 or index >= len(batch_relations) or not verdict.get("supported"):
                continue
            span = str(verdict.get("span") or "").strip()
            if span and span in batch_relations[index][1].main_text:
                confirmed.add(index)
        return confirmed

    def build_prompt(self, batch: list[ExtractionWindow], preannotations: dict[int, list[LexiconMatch]]) -> str:
        entity_lines = "\n".join(f"- {name}: {guide}" for name, guide in _ENTITY_TYPE_GUIDE.items())
        relation_lines = "\n".join(f"- {name}: {_RELATION_GUIDE[name]}" for name in sorted(SCIENTIFIC_RELATION_TYPES))
        unit_blocks = []
        for window in batch:
            hints = preannotations.get(window.index) or []
            hint_text = (
                "；".join(
                    f"{match.surface}({match.label or match.kind}{'/标识符' if match.kind == 'identifier' else ''})"
                    for match in hints
                )
                or "无"
            )
            context_before = window.context_before or "（无）"
            context_after = window.context_after or "（无）"
            unit_blocks.append(
                f"### 单元 {window.index + 1}\n"
                f"语境（前）：{context_before}\n"
                f"主句：{window.main_text}\n"
                f"语境（后）：{context_after}\n"
                f"预标注：{hint_text}"
            )
        return (
            f"{_SYSTEM_RULES}\n\n"
            f"ENTITY_TYPES：\n{entity_lines}\n\n"
            f"RELATION_TYPES（方向约定）：\n{relation_lines}\n\n"
            f"抽取单元（共 {len(batch)} 个，单元编号与输出 unit 字段一一对应）：\n\n" + "\n\n".join(unit_blocks)
        )

    @staticmethod
    def _parse_units(content: str) -> dict[int, dict[str, Any]]:
        parsed = json_repair.loads(content or "")
        if isinstance(parsed, list):
            units = parsed
        elif isinstance(parsed, dict):
            units = parsed.get("units")
        else:
            units = None
        if not isinstance(units, list):
            raise ValueError(f"科研抽取器响应缺少 units 数组: {str(content)[:200]}")
        by_index: dict[int, dict[str, Any]] = {}
        for unit in units:
            if not isinstance(unit, dict):
                continue
            try:
                index = int(unit.get("unit"))
            except (TypeError, ValueError):
                continue
            by_index[index] = unit
        return by_index

    @staticmethod
    def aggregate(entities: list[dict[str, Any]], relations: list[dict[str, Any]]) -> dict[str, Any]:
        """G5 跨单元合并：同 (normalized_name, label) 归一实体，surface 变体收为 aliases，首个主句作本 chunk 的
        实体引文；同 (subject, predicate, object) 合并关系，confidence 取最大、hedge 取全体与、G7/复核取任一通过。"""
        merged_entities: dict[tuple[str, str], dict[str, Any]] = {}
        entity_key_by_surface_label: dict[tuple[str, str], tuple[str, str]] = {}
        for entity in entities:
            canonical_text = entity["normalized_name"]
            key = (normalize_entity_name(canonical_text), entity["label"])
            record = merged_entities.get(key)
            if record is None:
                record = {
                    "text": canonical_text,
                    "label": entity["label"],
                    "attributes": [],
                    "aliases": [],
                    "mention_quote": entity.get("mention_quote") or "",
                }
                merged_entities[key] = record
            surface = entity["surface"]
            if normalize_entity_name(surface) != key[0] and surface not in record["aliases"]:
                record["aliases"].append(surface)
            kind = entity.get("identifier_kind")
            if kind in {"rap_id", "msu_id"}:
                attribute = {"text": surface, "label": kind}
                if attribute not in record["attributes"]:
                    record["attributes"].append(attribute)
            entity_key_by_surface_label[(surface, entity["label"])] = key

        surface_to_key: dict[str, tuple[str, str]] = {}
        for (surface, _label), key in entity_key_by_surface_label.items():
            surface_to_key.setdefault(surface, key)

        merged_relations: dict[tuple[tuple[str, str], str, tuple[str, str]], dict[str, Any]] = {}
        for relation in relations:
            source_key = surface_to_key.get(relation["subject"])
            target_key = surface_to_key.get(relation["object"])
            if source_key is None or target_key is None or source_key == target_key:
                continue
            key = (source_key, relation["predicate"], target_key)
            record = merged_relations.get(key)
            if record is None:
                merged_relations[key] = {
                    "source": merged_entities[source_key],
                    "target": merged_entities[target_key],
                    "text": relation["evidence_quote"],
                    "label": relation["predicate"],
                    "confidence": relation["confidence"],
                    "hedge": relation["hedge"],
                    "context": dict(relation["context"]),
                    "trigger_verified": bool(relation.get("trigger_verified")),
                    "trigger_term": relation.get("trigger_term"),
                    "verifier_confirmed": relation.get("verifier_confirmed"),
                    "mention_count": 1,
                }
                continue
            record["confidence"] = max(record["confidence"], relation["confidence"])
            record["hedge"] = record["hedge"] and relation["hedge"]
            record["mention_count"] += 1
            if relation.get("trigger_verified") and not record["trigger_verified"]:
                record["trigger_verified"] = True
                record["trigger_term"] = relation.get("trigger_term")
            if relation.get("verifier_confirmed"):
                record["verifier_confirmed"] = True
            for field, value in relation["context"].items():
                record["context"].setdefault(field, value)

        return {"entities": list(merged_entities.values()), "relations": list(merged_relations.values())}


# 词表漂移守卫：提示词指南必须与闭集完全一致，否则模型看到的枚举与门禁不同源
assert set(_ENTITY_TYPE_GUIDE) == set(SCIENTIFIC_ENTITY_TYPES)
assert set(_RELATION_GUIDE) == set(SCIENTIFIC_RELATION_TYPES)
