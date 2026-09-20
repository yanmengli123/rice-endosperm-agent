"""图谱构建相关的纯函数工具集。

将数据变换逻辑从 MilvusGraphService 中抽离，
使 service 类专注于 I/O 和业务编排。
"""

from __future__ import annotations

from typing import Any

from yuxi.utils import hashstr

# 关系级证据属性（llm_scientific 轨）：从抽取结果一路透传到 knowledge_graph_triple_mentions 行
RELATION_EVIDENCE_FIELDS = ("confidence", "hedge", "context", "trigger_verified", "trigger_term", "verifier_confirmed")

# ── 谓词分级（D9 检索期剪枝依据）────────────────────────────────
# TIER_A 因果/调控：回答「为什么/怎么调控」的主力边，检索期默认展开；
# TIER_B 状态/表达：定位、表达、共现类陈述，检索期默认展开但排序靠后；
# TIER_C 结构/元数据：方法桥与条件挂载，只作为多跳的中继边（拉方法参数/条件
#   进 context），永不直接作为答案证据渲染，也永不参与 claim 升格。
TIER_A = "TIER_A"
TIER_B = "TIER_B"
TIER_C = "TIER_C"

STRUCTURAL_PREDICATES: frozenset[str] = frozenset({"OBSERVED_BY", "UNDER_CONDITION"})

PREDICATE_TIERS: dict[str, str] = {
    **{
        predicate: TIER_A
        for predicate in (
            "DIRECT_BINDING",
            "TRANSCRIPTIONAL_ACTIVATION",
            "TRANSCRIPTIONAL_REPRESSION",
            "TRANSCRIPTIONAL_REGULATION",
            "PROTEIN_ACTIVITY_REGULATION",
            "PROTEIN_DEGRADATION",
            "REQUIRED_FOR",
            "PROMOTES_PROCESS",
            "INHIBITS_PROCESS",
            "REGULATES_PROCESS",
            "PROMOTES_PHENOTYPE",
            "SUPPRESSES_PHENOTYPE",
            "REGULATES_PHENOTYPE",
            "MUTANT_EFFECT",
            "KNOCKOUT_EFFECT",
            "CRISPR_EFFECT",
            "RNAI_EFFECT",
            "OVEREXPRESSION_EFFECT",
        )
    },
    **{predicate: TIER_B for predicate in ("EXPRESSION_IN", "COEXPRESSION", "ALLELE_OF")},
    **{predicate: TIER_C for predicate in STRUCTURAL_PREDICATES},
}


def predicate_tier(relation_type: str) -> str:
    """谓词分级；未知谓词（含托管导入的自由关系）按 TIER_B 对待——可用但不优先。"""
    return PREDICATE_TIERS.get(relation_type, TIER_B)


def is_structural_predicate(relation_type: str) -> bool:
    """结构谓词永不参与 claim 升格 / 直接证据渲染（ADR-0001 权威边界的图谱侧延伸）。"""
    return relation_type in STRUCTURAL_PREDICATES


# ── mention 极性推导（D6 冲突聚合的判定基元）──────────────────────
# 优先级：context.polarity（模型显式标注）> context.direction > 谓词语义先验。
# 推导不出来的（None）不参与冲突判定——宁缺勿错。
_NEGATIVE_SEMANTIC_PREDICATES = frozenset(
    {
        "TRANSCRIPTIONAL_REPRESSION",
        "PROTEIN_DEGRADATION",
        "INHIBITS_PROCESS",
        "SUPPRESSES_PHENOTYPE",
    }
)
_POSITIVE_SEMANTIC_PREDICATES = frozenset(
    {
        "TRANSCRIPTIONAL_ACTIVATION",
        "PROMOTES_PROCESS",
        "PROMOTES_PHENOTYPE",
    }
)


def derive_mention_polarity(relation_type: str, context: dict[str, Any] | None) -> str | None:
    """从 mention 语境推导极性：positive / negative / None（无法判定）。

    冲突检测只对同一 (subject, predicate, object, condition) 下同时出现
    positive 与 negative 的三元组登记 CONTESTED；推不出的 mention 不参与。
    """
    context = context or {}
    polarity = str(context.get("polarity") or "").strip().lower()
    if polarity in {"positive", "negative", "neutral"}:
        return polarity
    direction = str(context.get("direction") or "").strip().lower()
    if direction == "inhibits":
        return "negative"
    if direction == "activates":
        return "positive"
    if relation_type in _NEGATIVE_SEMANTIC_PREDICATES:
        return "negative"
    if relation_type in _POSITIVE_SEMANTIC_PREDICATES:
        return "positive"
    return None


def condition_key(condition_text: str | None) -> str:
    """冲突聚合的条件维度键：condition 入键，「HT 下促 / CT 下抑」不是矛盾而是对比。"""
    return normalize_entity_name(str(condition_text or "")) or "_"


def normalize_entity_name(text: str) -> str:
    """统一实体名称：去首尾空白、小写化、压缩内部连续空白。"""
    return " ".join(text.strip().lower().split())


def compute_entity_id(kb_id: str, normalized_name: str, label: str) -> str:
    return hashstr(f"{kb_id}:{normalized_name}:{label}", length=32)


def compute_triple_id(
    kb_id: str,
    source_normalized_name: str,
    source_label: str,
    relation_type: str,
    target_normalized_name: str,
    target_label: str,
) -> str:
    return hashstr(
        f"{kb_id}:{source_normalized_name}:{source_label}:{relation_type}:{target_normalized_name}:{target_label}",
        length=32,
    )


def graph_entity_collection_name(kb_id: str) -> str:
    return f"{kb_id}_entity"


def graph_triple_collection_name(kb_id: str) -> str:
    return f"{kb_id}_triple"


def mention_key(chunk_id: str, entity_id: str) -> str:
    """Chunk→Entity 提及的业务键；PG entity_mentions 与 Neo4j MENTIONS 边对账共用。"""
    return f"{chunk_id}->{entity_id}"


def build_graph_payload(normalized_result: dict[str, Any]) -> dict[str, Any]:
    """将抽取器产出的标准化结果转换为 Neo4j 写入所需的图结构。

    返回的 entities 已完成去重合并：同名同 label 的实体只保留一份，
    属性（attributes）取并集。
    """
    entities: list[dict[str, Any]] = []
    entity_by_key: dict[tuple[str, str], dict[str, Any]] = {}

    def add_entity(entity: dict[str, Any]) -> str:
        key = (normalize_entity_name(entity["text"]), entity.get("label") or "Entity")
        existing = entity_by_key.get(key)
        if existing is not None:
            known_attributes = {(attr["text"], attr["label"]) for attr in existing.get("attributes") or []}
            for attribute in entity.get("attributes") or []:
                attribute_key = (attribute["text"], attribute["label"])
                if attribute_key not in known_attributes:
                    existing.setdefault("attributes", []).append(attribute)
                    known_attributes.add(attribute_key)
            for alias in entity.get("aliases") or []:
                if alias not in existing["aliases"] and alias != existing["text"]:
                    existing["aliases"].append(alias)
            if not existing.get("mention_quote") and entity.get("mention_quote"):
                existing["mention_quote"] = entity["mention_quote"]
            return existing["id"]

        graph_entity = {
            "id": f"e{len(entities) + 1}",
            "text": entity["text"],
            "label": entity.get("label") or "Entity",
            "attributes": list(entity.get("attributes") or []),
            "aliases": list(entity.get("aliases") or []),
            "mention_quote": entity.get("mention_quote") or "",
        }
        entities.append(graph_entity)
        entity_by_key[key] = graph_entity
        return graph_entity["id"]

    for entity in normalized_result["entities"]:
        add_entity(entity)

    relations = []
    for relation in normalized_result["relations"]:
        graph_relation = {
            "source": add_entity(relation["source"]),
            "target": add_entity(relation["target"]),
            "text": relation["text"],
            "label": relation.get("label") or "RELATED_TO",
        }
        # 科研抽取器的关系级证据属性随边一起投影到 mention 行
        for field in RELATION_EVIDENCE_FIELDS:
            if relation.get(field) is not None:
                graph_relation[field] = relation[field]
        relations.append(graph_relation)

    return {"entities": entities, "relations": relations, "metadata": normalized_result["metadata"]}


# ─── Cypher 模板 ────────────────────────────────────────────────
# 将大段 Cypher 字符串集中管理，提升 write_chunk_graph 的可读性。


def cypher_merge_chunk(db_label: str) -> str:
    """MERGE Chunk 节点并写入元数据。"""
    return f"""
    MERGE (c:Chunk:MilvusKB:`{db_label}` {{chunk_id: $chunk_id}})
    SET c.file_id = $file_id,
        c.kb_id = $kb_id,
        c.chunk_index = $chunk_index,
        c.content_preview = $content_preview,
        c.start_char_pos = $start_char_pos,
        c.end_char_pos = $end_char_pos
    """


def cypher_merge_entity_mention(db_label: str) -> str:
    """MERGE Entity 节点并创建 Chunk → Entity 的 MENTIONS 关系。"""
    return f"""
    MATCH (c:Chunk:MilvusKB:`{db_label}` {{chunk_id: $chunk_id}})
    MERGE (e:Entity:MilvusKB:`{db_label}` {{
        kb_id: $kb_id,
        normalized_name: $normalized_name,
        label: $entity_label
    }})
    SET e.entity_id = $entity_id,
        e.name = $name,
        e.attributes = $attributes
    MERGE (c)-[m:MENTIONS {{chunk_id: $chunk_id, file_id: $file_id, kb_id: $kb_id}}]->(e)
    """


def cypher_merge_relation(db_label: str) -> str:
    """MERGE 两个 Entity 之间的 RELATION 边。"""
    return f"""
    MATCH (source:Entity:MilvusKB:`{db_label}` {{
        kb_id: $kb_id,
        normalized_name: $source_name,
        label: $source_label
    }})
    MATCH (target:Entity:MilvusKB:`{db_label}` {{
        kb_id: $kb_id,
        normalized_name: $target_name,
        label: $target_label
    }})
    MERGE (source)-[r:RELATION {{
        kb_id: $kb_id,
        chunk_id: $chunk_id,
        source_name: $source_name,
        target_name: $target_name,
        type: $relation_type
    }}]->(target)
    SET r.triple_id = $triple_id,
        r.text = $text,
        r.file_id = $file_id,
        r.extractor_type = $extractor_type,
        r.review_status = coalesce(r.review_status, 'CANDIDATE')
    """


def compute_triple_risk_score(
    *,
    hedge_any: bool,
    machine_verified_any: bool,
    confidence_max: float | None,
    literature_count: int,
    conflict_status: str,
) -> float:
    """候选三元组的机器证据风险分（越高越该先被人看）。

    只用已物化的 mention 聚合信号（hedge/触发词或双模型验证/置信度/文献数）
    与冲突态——与人工决策状态无关（那是审核结果不是审核难度）。纯函数，构建
    末尾批量回填进 knowledge_graph_triples.risk_score，队列 risk_desc 排序用。
    """
    risk = 1.0
    if hedge_any:
        risk += 1.5  # 推测性表述（may/might 等）
    if confidence_max is not None and confidence_max < 0.7:
        risk += 1.5  # 模型自己都不太确定
    if int(literature_count or 0) < 2:
        risk += 1.0  # 单源或零源
    if not machine_verified_any:
        risk += 1.0  # 触发词与双模型复核都没过
    if (conflict_status or "NONE") == "CONTESTED":
        risk += 2.0  # 已登记极性矛盾
    if machine_verified_any and confidence_max is not None and confidence_max >= 0.9:
        risk -= 0.5  # 双保险高置信，轻微降权
    return round(max(risk, 0.0), 2)
