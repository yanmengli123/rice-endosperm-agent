"""LLM 抽取图谱晋升包：规范层数据适配 → v3 往返包自校验。"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from yuxi.knowledge.graphs.graph_export_service import reverse_map_node_type
from yuxi.knowledge.graphs.llm_graph_promotion import LLMGraphPromotionService, adapt_promotion_source


def _source() -> dict:
    return {
        "entities": [
            {
                "entity_id": "e_gif1",
                "canonical_identity": "name:gif1",
                "normalized_name": "gif1",
                "label": "Gene",
                "name": "GIF1",
                "attributes": [{"text": "Os03g0642100", "label": "rap_id"}, {"text": "GIF1", "label": "symbol"}],
            },
            {
                "entity_id": "e_weight",
                "canonical_identity": "name:grain weight",
                "normalized_name": "grain weight",
                "label": "Phenotype",
                "name": "grain weight",
                "attributes": [],
            },
            {
                "entity_id": "e_qpcr",
                "canonical_identity": "name:qrt-pcr",
                "normalized_name": "qrt-pcr",
                "label": "Method",
                "name": "qRT-PCR",
                "attributes": [],
            },
            {
                "entity_id": "e_orphan",
                "canonical_identity": "name:orphan",
                "normalized_name": "orphan",
                "label": "Process",
                "name": "orphan process",
                "attributes": [],
            },
        ],
        "triples": [
            {
                "triple_id": "t1",
                "source_entity_id": "e_gif1",
                "target_entity_id": "e_weight",
                "relation_type": "OVEREXPRESSION_EFFECT",
                "support_count": 2,
                "literature_count": 2,
                "consensus_direction": "UNKNOWN",
                "best_evidence_level": None,
            },
            {
                "triple_id": "t2",
                "source_entity_id": "e_gif1",
                "target_entity_id": "e_qpcr",
                "relation_type": "EXPRESSION_IN",
                "support_count": 1,
                "literature_count": 1,
                "consensus_direction": "UNKNOWN",
                "best_evidence_level": None,
            },
        ],
        "mentions": [
            {
                "triple_id": "t1",
                "file_id": "f1",
                "chunk_id": "c1",
                "text": "Overexpression of GIF1 increased grain weight",
                "extractor_type": "llm_scientific",
            },
            {
                "triple_id": "t1",
                "file_id": "f2",
                "chunk_id": "c9",
                "text": "GIF1 overexpression enhanced grain weight",
                "extractor_type": "llm_scientific",
            },
            {"triple_id": "t2", "file_id": "f1", "chunk_id": "c2", "text": "", "extractor_type": "llm_scientific"},
        ],
        "aliases": [{"entity_id": "e_gif1", "alias": "GRAIN INCOMPLETE FILLING 1", "alias_type": "EXTRACTED"}],
    }


def test_adapt_promotion_source_maps_registry_attributes_and_drops_orphans():
    adapted = adapt_promotion_source(_source())

    entity_ids = {entity["entity_id"] for entity in adapted["entities"]}
    assert entity_ids == {"e_gif1", "e_weight", "e_qpcr"}
    gif1 = next(entity for entity in adapted["entities"] if entity["entity_id"] == "e_gif1")
    assert gif1["attributes"] == {"rap_ids": ["Os03g0642100"]}
    assert adapted["aliases_by_entity"] == {"e_gif1": ["GRAIN INCOMPLETE FILLING 1"]}
    # 空引文 mention 不成证据行；证据按 (file, chunk) 编号，文献维度按 file 计
    assert [item["evidence_id"] for item in adapted["evidence"]] == ["f1:c1", "f2:c9"]
    assert all(item["evidence_alignment_status"] == "ALIGNED" for item in adapted["evidence"])
    assert adapted["extractor_types"] == ["llm_scientific"]
    assert all(triple["best_evidence_level"] == "" for triple in adapted["triples"])


def test_method_label_maps_to_v3_node_type():
    assert reverse_map_node_type("Method", None) == "METHOD"


@pytest.mark.asyncio
async def test_promotion_export_builds_self_validated_v3_package():
    repo = SimpleNamespace(list_promotion_source=AsyncMock(return_value=_source()))
    service = LLMGraphPromotionService(graph_repo=repo)

    package = await service.export("kb_test", min_support_count=1, exported_by="user_1")

    repo.list_promotion_source.assert_awaited_once_with("kb_test", min_support_count=1)
    assert package["filename"] == "graph-promotion-kb_test.zip"
    assert package["media_type"] == "application/zip"
    manifest = package["manifest"]
    assert manifest["promotion"]["min_support_count"] == 1
    assert manifest["promotion"]["review_required"] is True
    assert manifest["promotion"]["extractor_types"] == ["llm_scientific"]
    assert manifest["reparse_self_check"]["blockers"] == 0
    assert manifest["counts"]["triples"] == 2
    assert manifest["skipped"]["unsupported_label_entities_count"] == 0

    with zipfile.ZipFile(io.BytesIO(package["content"])) as archive:
        assert set(archive.namelist()) == {"nodes.csv", "relationships.csv", "manifest.json"}
        nodes = list(csv.DictReader(io.StringIO(archive.read("nodes.csv").decode("utf-8-sig"))))
        relationships = list(csv.DictReader(io.StringIO(archive.read("relationships.csv").decode("utf-8-sig"))))
        assert json.loads(archive.read("manifest.json"))["promotion"]["source_track"] == "chunk_extraction"

    gene_row = next(row for row in nodes if row["name"] == "GIF1")
    assert gene_row["node_type"] == "GENE"
    assert gene_row["rap_id"] == "Os03g0642100"  # 导入时按 rap 列重建 rap: 规范身份
    assert gene_row["aliases"] == "GRAIN INCOMPLETE FILLING 1"
    assert next(row for row in nodes if row["name"] == "qRT-PCR")["node_type"] == "METHOD"
    assert all(row["name"] != "orphan process" for row in nodes)

    t1_rows = [row for row in relationships if row["relation_type"] == "OVEREXPRESSION_EFFECT"]
    assert [row["evidence_quotes"] for row in t1_rows] == [
        "Overexpression of GIF1 increased grain weight",
        "GIF1 overexpression enhanced grain weight",
    ]
    assert all(row["pmids"] == "" and row["dois"] == "" for row in t1_rows)
    # 无逐字引文的三元组仍导出一行空证据，交由审阅补齐
    assert len([row for row in relationships if row["relation_type"] == "EXPRESSION_IN"]) == 1


@pytest.mark.asyncio
async def test_promotion_export_rejects_when_nothing_meets_threshold():
    source = _source()
    source["triples"] = []
    source["mentions"] = []
    repo = SimpleNamespace(list_promotion_source=AsyncMock(return_value=source))

    with pytest.raises(ValueError, match="没有可导出"):
        await LLMGraphPromotionService(graph_repo=repo).export("kb_test", min_support_count=3)
