"""知识图谱导出服务：从 PostgreSQL 规范层导出，绝不从 Neo4j 投影导出。

两个变体：
- roundtrip：nodes.csv + relationships.csv + manifest.json 的 zip 包，严格符合
  rice-endosperm-csv-v3 契约，可直接经 GraphImportModal 重新导入（导出前会用
  parse_managed_graph_import 自校验，拒绝输出无法再导入的包）。
- evidence：三张 sheet（实体 / 三元组 / 证据明细全语义槽）的 xlsx，供科研审阅。

往返保真策略（与解析器行为一一对应）：
- 每个实体按 registry id（rap/msu）拆成多行 node 行，同一 node_id；
  union-find 会重新合并，canonical_identity 取 casefold 排序后最小 rap/msu，
  与导入时一致，entity_id 在同库重导入时保持不变。
- AlleleMutant 通过携带父基因的 rap/msu id 与父基因行并入同一分组，
  触发 GENE_ALLELE_IDENTITY_SPLIT 重建 parent_identity。
- ALLELE_OF 三元组不导出：它是导入器自动生成的派生关系，直接导出会因
  端点语义路由错配破坏往返；重新导入时由 normalizer 再生成。
- 证据逐条一行（pmid/doi/quote 单值），重导入时恢复为 ALIGNED 证据；
  ROW_LEVEL 捆绑包按 metadata_json 中的 pmids/dois 还原。
"""

import csv
import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from yuxi.knowledge.graphs.managed_import_parser import (
    NODE_HEADERS,
    NODE_TYPE_MAPPING,
    NORMALIZER_VERSION,
    RELATIONSHIP_HEADERS,
    SCHEMA_VERSION,
    parse_managed_graph_import,
)
from yuxi.repositories.knowledge_graph_import_repository import KnowledgeGraphImportRepository
from yuxi.utils import logger

EXPORT_TOOL_VERSION = "graph-export-v1"
EXPORT_VARIANTS = ("roundtrip", "evidence")
# NODE_HEADERS/RELATIONSHIP_HEADERS 是 set（解析器只做包含校验）；
# 导出必须按契约文档顺序输出，这里显式固定并断言与解析器要求一致，防止契约漂移。
NODE_COLUMNS = [
    "node_id",
    "name",
    "node_type",
    "rap_id",
    "msu_id",
    "out_degree",
    "in_degree",
    "publication_count",
]
RELATIONSHIP_COLUMNS = [
    "start_id",
    "end_id",
    "relation_type",
    "direction",
    "directness",
    "best_evidence_level",
    "support_count",
    "literature_count",
    "pmids",
    "dois",
    "evidence_quotes",
]
assert set(NODE_COLUMNS) == set(NODE_HEADERS)
assert set(RELATIONSHIP_COLUMNS) == set(RELATIONSHIP_HEADERS)
# 导入器自动生成的派生关系类型，不进入往返包（由 normalizer 重建）
DERIVED_RELATION_TYPES = {"ALLELE_OF"}
# Gene 三态：label + gene_status -> node_type（NODE_TYPE_MAPPING 的反向表）
_GENE_NODE_TYPE_BY_STATUS = {
    "confirmed": "RICE_GENE",
    "candidate": "RICE_GENE_CANDIDATE",
}
LABEL_TO_NODE_TYPE = {label: node_type for node_type, (label, _status) in NODE_TYPE_MAPPING.items() if label != "Gene"}
GENE_NODE_TYPE_DEFAULT = "GENE"


def reverse_map_node_type(label: str, gene_status: str | None) -> str | None:
    """label(+gene_status) -> v3 node_type；未知 label 返回 None（导出时跳过）。"""
    if label == "Gene":
        return _GENE_NODE_TYPE_BY_STATUS.get(gene_status or "", GENE_NODE_TYPE_DEFAULT)
    return LABEL_TO_NODE_TYPE.get(label)


def _registry_rows(rap_ids: list[str], msu_ids: list[str]) -> list[dict[str, str]]:
    """每个 registry id 一行；都没有时输出一行空 registry 行。"""
    rows = [{"rap_id": rap_id, "msu_id": ""} for rap_id in rap_ids]
    rows.extend({"rap_id": "", "msu_id": msu_id} for msu_id in msu_ids)
    if not rows:
        rows = [{"rap_id": "", "msu_id": ""}]
    return rows


def _csv_bytes(header: list[str], rows: list[list[str]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _join_alias_values(values: list[str]) -> str:
    return "|".join(value.replace("|", " ") for value in values)


def _attributes(entity_or_parent: dict[str, Any]) -> dict[str, Any]:
    """attributes 统一按 dict 归一（chunk 抽取路径可能写入 list）。"""
    value = entity_or_parent.get("attributes")
    return value if isinstance(value, dict) else {}


def _metadata(item: dict[str, Any]) -> dict[str, Any]:
    value = item.get("metadata_json")
    return value if isinstance(value, dict) else {}


def build_roundtrip_csvs(
    *,
    entities: list[dict[str, Any]],
    triples: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    aliases_by_entity: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """从规范层 dict 数据构造 v3 nodes/relationships CSV 字节与统计信息（纯函数，可单测）。"""
    aliases_by_entity = aliases_by_entity or {}
    identity_to_entity = {entity["canonical_identity"]: entity for entity in entities}

    evidence_by_triple: dict[str, list[dict[str, Any]]] = {}
    for item in evidence:
        evidence_by_triple.setdefault(item["triple_id"], []).append(item)

    node_id_by_entity: dict[str, str] = {}
    used_node_ids: set[str] = set()
    skipped_entities: list[dict[str, str]] = []
    alleles_without_parent: list[str] = []
    alleles_attached_to_parent: list[str] = []

    for entity in entities:
        attributes = _attributes(entity)
        label = entity.get("label") or ""
        node_type = reverse_map_node_type(label, attributes.get("gene_status"))
        if node_type is None:
            skipped_entities.append(
                {"entity_id": entity["entity_id"], "label": label, "name": entity.get("name") or ""}
            )
            continue
        base = (attributes.get("external_ids") or [entity.get("normalized_name") or entity["entity_id"]])[0]
        node_id = base
        suffix = 2
        while node_id in used_node_ids:
            node_id = f"{base}~{suffix}"
            suffix += 1
        used_node_ids.add(node_id)
        node_id_by_entity[entity["entity_id"]] = node_id

    # AlleleMutant 挂靠：把 allele 行放到父基因的 node_id 下（union-find 按 node_id 并组），
    # 重导入时触发 GENE_ALLELE_IDENTITY_SPLIT 重建 parent_identity 与 ALLELE_OF。
    # 解析器数据模型为每组 1 基因 + 1 allele，因此同一父基因只挂靠第一个 allele。
    parent_claimed: set[str] = set()

    def effective_node_id(entity: dict[str, Any]) -> str:
        own = node_id_by_entity.get(entity["entity_id"])
        if own is None or (entity.get("label") or "") != "AlleleMutant":
            return own  # type: ignore[return-value]
        attributes = _attributes(entity)
        parent = identity_to_entity.get(attributes.get("parent_gene_identity"))
        parent_node = node_id_by_entity.get(parent["entity_id"]) if parent else None
        if parent_node is None:
            alleles_without_parent.append(entity["entity_id"])
            return own
        if parent_node in parent_claimed:
            # 父基因组已被其他 allele 占用；退回自身 id（identity 会漂移，manifest 记录）
            return own
        parent_claimed.add(parent_node)
        alleles_attached_to_parent.append(entity["entity_id"])
        return parent_node

    effective_node_id_by_entity: dict[str, str] = {}
    for entity in entities:
        if entity["entity_id"] in node_id_by_entity:
            effective_node_id_by_entity[entity["entity_id"]] = effective_node_id(entity)

    out_degree: dict[str, int] = {}
    in_degree: dict[str, int] = {}
    publication_count: dict[str, set[str]] = {}
    exportable_triples = []
    skipped_triples = 0
    for triple in triples:
        source_node = effective_node_id_by_entity.get(triple["source_entity_id"])
        target_node = effective_node_id_by_entity.get(triple["target_entity_id"])
        if source_node is None or target_node is None:
            skipped_triples += 1
            continue
        exportable_triples.append(triple)
        out_degree[triple["source_entity_id"]] = out_degree.get(triple["source_entity_id"], 0) + 1
        in_degree[triple["target_entity_id"]] = in_degree.get(triple["target_entity_id"], 0) + 1
        for item in evidence_by_triple.get(triple["triple_id"], []):
            key = item.get("literature_id") or item.get("pmid") or item.get("doi") or item.get("evidence_id")
            if key:
                publication_count.setdefault(triple["source_entity_id"], set()).add(key)
                publication_count.setdefault(triple["target_entity_id"], set()).add(key)

    node_rows: list[list[str]] = []
    for entity in entities:
        node_id = node_id_by_entity.get(entity["entity_id"])
        if node_id is None:
            continue
        attributes = _attributes(entity)
        label = entity.get("label") or ""
        node_type = reverse_map_node_type(label, attributes.get("gene_status"))
        rap_ids = [str(value) for value in (attributes.get("rap_ids") or []) if value]
        msu_ids = [str(value) for value in (attributes.get("msu_ids") or []) if value]
        # allele 行使用父基因 node_id（分组已由 effective_node_id 决定）
        row_node_id = effective_node_id_by_entity.get(entity["entity_id"]) or node_id

        alias_values = []
        seen_aliases = {entity.get("name", "")}
        for alias in [*(aliases_by_entity.get(entity["entity_id"]) or []), *(attributes.get("aliases") or [])]:
            text = str(alias).strip()
            if text and text not in seen_aliases:
                seen_aliases.add(text)
                alias_values.append(text)

        publication_value = str(len(publication_count.get(entity["entity_id"], set())))
        aliases_value = _join_alias_values(alias_values)
        degree_out = str(out_degree.get(entity["entity_id"], 0))
        degree_in = str(in_degree.get(entity["entity_id"], 0))
        # 列序：node_id, name, node_type, rap_id, msu_id, out_degree, in_degree, publication_count, aliases
        for registry in _registry_rows(rap_ids, msu_ids):
            node_rows.append(
                [
                    row_node_id,
                    entity.get("name") or "",
                    node_type,
                    registry["rap_id"],
                    registry["msu_id"],
                    degree_out,
                    degree_in,
                    publication_value,
                    aliases_value,
                ]
            )

    relation_rows: list[list[str]] = []
    exported_relation_count = 0
    for triple in exportable_triples:
        if triple["relation_type"] in DERIVED_RELATION_TYPES:
            continue
        exported_relation_count += 1
        start_id = effective_node_id_by_entity[triple["source_entity_id"]]
        end_id = effective_node_id_by_entity[triple["target_entity_id"]]
        items = evidence_by_triple.get(triple["triple_id"], [])
        if not items:
            relation_rows.append(
                [
                    start_id,
                    end_id,
                    triple["relation_type"],
                    triple.get("consensus_direction") or "",
                    "",
                    triple.get("best_evidence_level") or "",
                    "0",
                    "0",
                    "",
                    "",
                    "",
                ]
            )
            continue
        for item in items:
            metadata = _metadata(item)
            if item.get("evidence_alignment_status") == "ROW_LEVEL":
                pmids = "|".join(str(v) for v in (metadata.get("pmids") or []))
                dois = "|".join(str(v) for v in (metadata.get("dois") or []))
                quotes = item.get("evidence_quote") or ""
            else:
                pmids = item.get("pmid") or ""
                dois = item.get("doi") or ""
                quotes = item.get("evidence_quote") or ""
            has_identifier = bool(
                item.get("pmid") or item.get("doi") or metadata.get("pmids") or metadata.get("dois")
            )
            relation_rows.append(
                [
                    start_id,
                    end_id,
                    triple["relation_type"],
                    item.get("direction") or "",
                    item.get("directness") or "",
                    item.get("evidence_level") or triple.get("best_evidence_level") or "",
                    "1",
                    "1" if has_identifier else "0",
                    pmids,
                    dois,
                    quotes,
                ]
            )

    nodes_csv = _csv_bytes(NODE_COLUMNS + ["aliases"], node_rows)
    relationships_csv = _csv_bytes(RELATIONSHIP_COLUMNS, relation_rows)
    return {
        "nodes_csv": nodes_csv,
        "relationships_csv": relationships_csv,
        "node_row_count": len(node_rows),
        "relation_row_count": len(relation_rows),
        "exported_entities": len(node_id_by_entity),
        "exported_triples": exported_relation_count,
        "skipped_entities": skipped_entities,
        "skipped_triples": skipped_triples,
        "alleles_without_parent": alleles_without_parent,
        "alleles_attached_to_parent": alleles_attached_to_parent,
    }


def build_roundtrip_package(
    *,
    kb_id: str,
    entities: list[dict[str, Any]],
    triples: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    aliases_by_entity: dict[str, list[str]] | None = None,
    exported_by: str = "system",
    kb_name: str | None = None,
) -> dict[str, Any]:
    """构造往返 zip 包；导出前用解析器自校验，无法通过 v3 契约时抛 RuntimeError 拒绝导出。"""
    built = build_roundtrip_csvs(
        entities=entities, triples=triples, evidence=evidence, aliases_by_entity=aliases_by_entity
    )

    reparsed = parse_managed_graph_import(
        kb_id=kb_id,
        nodes_bytes=built["nodes_csv"],
        relationships_bytes=built["relationships_csv"],
    )
    if not reparsed.get("valid"):
        blockers = reparsed.get("blockers") or []
        preview = "; ".join(f"{item.get('code')}: {item.get('message')}" for item in blockers[:3])
        raise RuntimeError(f"导出包未通过 v3 契约自校验，拒绝导出：{preview}")

    # identity 保持性审计：重导入实体是否全部命中存量（幂等 upsert）。
    # chunk 抽取来源的图谱（自定义标签/身份方案）天然无法映射回 v3 契约，此位为 False。
    stored_entity_ids = {entity["entity_id"] for entity in entities}
    plan_entity_ids = {entity["entity_id"] for entity in (reparsed.get("plan") or {}).get("entities", [])}
    identity_preserved = plan_entity_ids <= stored_entity_ids

    skipped_entities = built["skipped_entities"]
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "normalizer_version": NORMALIZER_VERSION,
        "export_tool_version": EXPORT_TOOL_VERSION,
        "kb_id": kb_id,
        "kb_name": kb_name,
        "exported_at": datetime.now(UTC).isoformat(),
        "exported_by": exported_by,
        "counts": {
            "entities": built["exported_entities"],
            "triples": built["exported_triples"],
            "evidence": len(evidence),
            "node_rows": built["node_row_count"],
            "relationship_rows": built["relation_row_count"],
        },
        "files": {
            "nodes.csv": {"sha256": _sha256_hex(built["nodes_csv"]), "rows": built["node_row_count"]},
            "relationships.csv": {
                "sha256": _sha256_hex(built["relationships_csv"]),
                "rows": built["relation_row_count"],
            },
        },
        "skipped": {
            "unsupported_label_entities_count": len(skipped_entities),
            "unsupported_label_entities": skipped_entities[:100],
            "triples_with_skipped_endpoint": built["skipped_triples"],
            "alleles_without_parent": built["alleles_without_parent"],
        },
        "derived": {
            "alleles_attached_to_parent_node": len(built["alleles_attached_to_parent"]),
        },
        "notes": [
            "ALLELE_OF 三元组为导入器派生关系，未导出；重新导入时自动重建。",
            "AlleleMutant 节点行挂靠父基因 node_id，重导入时经语义拆分重建 parent_identity。",
            "aliases 列为附加信息，当前导入器忽略该列；大小写别名合并历史不随包往返。",
        ],
        "reparse_self_check": {
            "status": reparsed.get("status"),
            "counts": reparsed.get("counts"),
            "blockers": len(reparsed.get("blockers") or []),
            "warnings": len(reparsed.get("warnings") or []),
            "identity_preserved": identity_preserved,
        },
    }
    if not identity_preserved:
        manifest["notes"].append(
            "注意：本库部分实体来自非 v3 契约来源（如 chunk 抽取），其身份方案无法映射回契约；"
            "将本包重新导入原库会为已导出实体创建新身份，建议仅导入到新库。"
        )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("nodes.csv", built["nodes_csv"])
        archive.writestr("relationships.csv", built["relationships_csv"])
        archive.writestr(
            "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        )
    filename = f"graph-roundtrip-{kb_id}.zip"
    return {"filename": filename, "content": buffer.getvalue(), "media_type": "application/zip", "manifest": manifest}


def build_evidence_workbook(
    *,
    kb_id: str,
    entities: list[dict[str, Any]],
    triples: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    aliases_by_entity: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """构造证据明细 xlsx（实体 / 三元组 / 证据宽表三张 sheet）。"""
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    aliases_by_entity = aliases_by_entity or {}
    entity_by_id = {entity["entity_id"]: entity for entity in entities}

    workbook = Workbook()
    header_font = Font(bold=True)

    def fill_sheet(sheet, headers: list[str], rows: list[list[Any]]) -> None:
        sheet.append(headers)
        for cell in sheet[1]:
            cell.font = header_font
        for row in rows:
            sheet.append(row)
        for index, header in enumerate(headers, start=1):
            sheet.column_dimensions[get_column_letter(index)].width = min(max(len(header) * 2 + 6, 14), 60)
        sheet.freeze_panes = "A2"

    entity_sheet = workbook.active
    entity_sheet.title = "实体"
    entity_headers = [
        "entity_id", "canonical_identity", "label", "name", "normalized_name",
        "gene_status", "rap_ids", "msu_ids", "aliases", "external_ids",
    ]
    fill_sheet(
        entity_sheet,
        entity_headers,
        [
            [
                entity["entity_id"],
                entity.get("canonical_identity"),
                entity.get("label"),
                entity.get("name"),
                entity.get("normalized_name"),
                _attributes(entity).get("gene_status") or "",
                "|".join(str(v) for v in _attributes(entity).get("rap_ids") or []),
                "|".join(str(v) for v in _attributes(entity).get("msu_ids") or []),
                "|".join(aliases_by_entity.get(entity["entity_id"]) or []),
                "|".join(str(v) for v in _attributes(entity).get("external_ids") or []),
            ]
            for entity in entities
        ],
    )

    def display_name(entity_id: str) -> str:
        entity = entity_by_id.get(entity_id)
        if not entity:
            return entity_id
        return f"{entity.get('name')}({entity.get('label')})"

    triple_sheet = workbook.create_sheet("三元组")
    triple_headers = [
        "triple_id", "source", "target", "relation_type", "content",
        "support_count", "literature_count", "best_evidence_level", "consensus_direction",
    ]
    fill_sheet(
        triple_sheet,
        triple_headers,
        [
            [
                triple["triple_id"],
                display_name(triple["source_entity_id"]),
                display_name(triple["target_entity_id"]),
                triple.get("relation_type"),
                triple.get("content"),
                triple.get("support_count"),
                triple.get("literature_count"),
                triple.get("best_evidence_level"),
                triple.get("consensus_direction"),
            ]
            for triple in triples
        ],
    )

    evidence_headers = [
        "evidence_id", "triple_id", "source", "target", "relation_type", "pmid", "doi", "literature_id",
        "identifier_status", "direction", "directness", "assertion_status", "evidence_level",
        "evidence_alignment_status", "outcome_class", "yield_measure_type", "experimental_subject_type",
        "subject_material", "perturbs", "perturbation_direction", "condition", "cultivar",
        "genetic_background", "development_stage", "observed_effect", "observed_relation",
        "inferred_gene_function", "sentence_id", "claim_eligible", "evidence_quote", "metadata_json",
    ]
    triple_by_id = {triple["triple_id"]: triple for triple in triples}
    evidence_rows = []
    for item in evidence:
        triple = triple_by_id.get(item["triple_id"])
        evidence_rows.append(
            [
                item.get("evidence_id"),
                item.get("triple_id"),
                display_name(triple["source_entity_id"]) if triple else "",
                display_name(triple["target_entity_id"]) if triple else "",
                triple.get("relation_type") if triple else "",
                item.get("pmid"),
                item.get("doi"),
                item.get("literature_id"),
                item.get("identifier_status"),
                item.get("direction"),
                item.get("directness"),
                item.get("assertion_status"),
                item.get("evidence_level"),
                item.get("evidence_alignment_status"),
                item.get("outcome_class"),
                item.get("yield_measure_type"),
                item.get("experimental_subject_type"),
                item.get("subject_material"),
                item.get("perturbs"),
                item.get("perturbation_direction"),
                item.get("condition"),
                item.get("cultivar"),
                item.get("genetic_background"),
                item.get("development_stage"),
                item.get("observed_effect"),
                item.get("observed_relation"),
                item.get("inferred_gene_function"),
                item.get("sentence_id"),
                "是" if item.get("claim_eligible") else "否",
                item.get("evidence_quote"),
                json.dumps(_metadata(item), ensure_ascii=False),
            ]
        )
    evidence_sheet = workbook.create_sheet("证据明细")
    fill_sheet(evidence_sheet, evidence_headers, evidence_rows)

    output = io.BytesIO()
    workbook.save(output)
    filename = f"graph-evidence-{kb_id}.xlsx"
    media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return {"filename": filename, "content": output.getvalue(), "media_type": media_type}


class ManagedGraphExportService:
    """图谱导出服务：单 session 一致性快照 -> 纯函数构造 -> 自校验 -> 返回字节。"""

    def __init__(self):
        self.repository = KnowledgeGraphImportRepository()

    async def export(
        self,
        kb_id: str,
        *,
        variant: str = "roundtrip",
        exported_by: str = "system",
    ) -> dict[str, Any]:
        if variant not in EXPORT_VARIANTS:
            raise ValueError(f"不支持的导出变体: {variant}（可选 {'/'.join(EXPORT_VARIANTS)}）")

        snapshot = await self.repository.export_snapshot(kb_id)
        entities = snapshot["entities"]
        triples = snapshot["triples"]
        evidence = snapshot["evidence"]
        if not entities:
            raise ValueError("当前知识库没有可导出的图谱数据")
        aliases_by_entity = snapshot["aliases"]

        if variant == "roundtrip":
            package = build_roundtrip_package(
                kb_id=kb_id,
                entities=entities,
                triples=triples,
                evidence=evidence,
                aliases_by_entity=aliases_by_entity,
                exported_by=exported_by,
                kb_name=snapshot.get("kb_name"),
            )
        else:
            package = build_evidence_workbook(
                kb_id=kb_id, entities=entities, triples=triples, evidence=evidence, aliases_by_entity=aliases_by_entity
            )
        logger.info(
            f"图谱导出完成 kb={kb_id} variant={variant} "
            f"entities={len(entities)} triples={len(triples)} evidence={len(evidence)}"
        )
        return package


def content_disposition_header(filename: str) -> str:
    return f"attachment; filename*=UTF-8''{quote(filename)}"
