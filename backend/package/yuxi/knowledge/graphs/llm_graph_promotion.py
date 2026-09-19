"""LLM 抽取图谱 → 托管导入晋升包（两层真相模型的晋升环）。

chunk 抽取轨（generic_document 契约库上 ``llm``/``llm_scientific`` 抽取器产出）在平台
架构里是导航/语义层，永不 ``claim_eligible``。要成为规范事实，必须经人工审定后
走托管 CSV 导入。本模块把抽取轨的规范层数据（PostgreSQL ``knowledge_graph_*``）
适配成 ``rice-endosperm-csv-v3`` 往返包：

- 只导出佐证计数（``support_count``，去重 chunk 数）达到阈值的三元组及其端点实体，
  孤立实体不进审阅队列；
- 每条三元组 mention 的逐字引文成为一行证据（无 PMID/DOI → ALIGNED 单引文行，
  由审阅者补齐文献标识符）；
- 标识符属性（rap_id/msu_id）还原为节点行 registry 列，导入时重建 ``rap:``/``msu:``
  规范身份——这正是抽取轨 ``name:`` 身份到规范身份的升级点；
- 复用 ``build_roundtrip_package`` 的 v3 自校验：过不了契约的包直接拒绝导出。
"""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any

from yuxi.knowledge.graphs.graph_export_service import build_roundtrip_package
from yuxi.repositories.knowledge_graph_repository import KnowledgeGraphRepository

PROMOTION_EXPORT_VERSION = "llm-graph-promotion-v1"
_REGISTRY_ATTRIBUTE_LABELS = {"rap_id": "rap_ids", "msu_id": "msu_ids"}


class LLMGraphPromotionService:
    def __init__(self, graph_repo: KnowledgeGraphRepository | None = None):
        self.graph_repo = graph_repo or KnowledgeGraphRepository()

    async def export(
        self,
        kb_id: str,
        *,
        min_support_count: int = 1,
        exported_by: str = "system",
        max_hallucination_rate: float = 0.2,
    ) -> dict[str, Any]:
        # R7b 质量门禁：库级幻觉率超阈值拒绝导出——高幻觉库的「共识」不可信，
        # 先修抽取（词典/触发词/复核模型）再晋升。仓储无该能力（旧测试假件）时跳过。
        hallucination_aggregator = getattr(self.graph_repo, "aggregate_hallucination_rate", None)
        hallucination_rate = (await hallucination_aggregator(kb_id)) if hallucination_aggregator else None
        if hallucination_rate is not None and hallucination_rate > max_hallucination_rate:
            raise ValueError(
                f"库级幻觉率 {hallucination_rate:.4f} 超过晋升阈值 {max_hallucination_rate}，"
                "请先修复抽取质量（词典/触发词/复核模型）再导出"
            )
        source = await self.graph_repo.list_promotion_source(kb_id, min_support_count=min_support_count)
        adapted = adapt_promotion_source(source)
        if not adapted["triples"]:
            raise ValueError(f"知识库 {kb_id} 没有可导出的三元组（佐证阈值 support_count ≥ {min_support_count}）")
        package = build_roundtrip_package(
            kb_id=kb_id,
            entities=adapted["entities"],
            triples=adapted["triples"],
            evidence=adapted["evidence"],
            aliases_by_entity=adapted["aliases_by_entity"],
            exported_by=exported_by,
        )
        package["filename"] = f"graph-promotion-{kb_id}.zip"
        package["manifest"]["promotion"] = {
            "export_version": PROMOTION_EXPORT_VERSION,
            "source_track": "chunk_extraction",
            "min_support_count": min_support_count,
            "extractor_types": adapted["extractor_types"],
            "review_required": True,
            "notes": [
                "本包来自 LLM 自动抽取，未经人工审定，不具备 claim 资格；",
                "审定后经托管图谱导入并入规范图谱，导入时按 rap/msu 列重建规范身份；",
                "证据行缺 PMID/DOI 时请在审定阶段补齐，否则导入后仍为无文献标识符证据。",
            ],
        }
        package["content"] = _repack_manifest(package["content"], package["manifest"])
        return package


def adapt_promotion_source(source: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """抽取轨规范层数据 → build_roundtrip_package 输入形状（纯函数，可单测）。"""
    triples = list(source.get("triples") or [])
    referenced_entity_ids = {triple["source_entity_id"] for triple in triples} | {
        triple["target_entity_id"] for triple in triples
    }

    aliases_by_entity: dict[str, list[str]] = {}
    for alias in source.get("aliases") or []:
        aliases_by_entity.setdefault(alias["entity_id"], []).append(str(alias["alias"]))

    entities = []
    for entity in source.get("entities") or []:
        if entity["entity_id"] not in referenced_entity_ids:
            continue
        entities.append(
            {
                "entity_id": entity["entity_id"],
                "canonical_identity": entity["canonical_identity"],
                "normalized_name": entity.get("normalized_name"),
                "label": entity["label"],
                "name": entity["name"],
                "attributes": _attributes_dict(entity.get("attributes")),
            }
        )

    evidence = []
    extractor_types: set[str] = set()
    for mention in source.get("mentions") or []:
        quote = str(mention.get("text") or "").strip()
        if mention.get("extractor_type"):
            extractor_types.add(str(mention["extractor_type"]))
        if not quote:
            continue
        evidence.append(
            {
                "triple_id": mention["triple_id"],
                "evidence_id": f"{mention['file_id']}:{mention['chunk_id']}",
                "literature_id": mention["file_id"],
                "evidence_quote": quote,
                "evidence_alignment_status": "ALIGNED",
                "direction": "",
                "directness": "",
                "evidence_level": "",
                "pmid": "",
                "doi": "",
            }
        )

    adapted_triples = [
        {
            "triple_id": triple["triple_id"],
            "source_entity_id": triple["source_entity_id"],
            "target_entity_id": triple["target_entity_id"],
            "relation_type": triple["relation_type"],
            "consensus_direction": "",
            "best_evidence_level": "",
        }
        for triple in triples
    ]
    return {
        "entities": entities,
        "triples": adapted_triples,
        "evidence": evidence,
        "aliases_by_entity": aliases_by_entity,
        "extractor_types": sorted(extractor_types),
    }


def _repack_manifest(content: bytes, manifest: dict[str, Any]) -> bytes:
    """往返包的 manifest.json 在 build_roundtrip_package 内已封包，追加 promotion 段后重新写入。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(content)) as source, zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
        for name in source.namelist():
            if name != "manifest.json":
                target.writestr(name, source.read(name))
        target.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
    return buffer.getvalue()


def _attributes_dict(attributes: Any) -> dict[str, Any]:
    """chunk 抽取轨的 attributes 是 [{text,label}] 列表：registry 标识符还原为 rap_ids/msu_ids。"""
    if isinstance(attributes, dict):
        return dict(attributes)
    result: dict[str, Any] = {}
    for attribute in attributes or []:
        if not isinstance(attribute, dict):
            continue
        key = _REGISTRY_ATTRIBUTE_LABELS.get(str(attribute.get("label") or ""))
        text = str(attribute.get("text") or "").strip()
        if key and text:
            result.setdefault(key, [])
            if text not in result[key]:
                result[key].append(text)
    return result
