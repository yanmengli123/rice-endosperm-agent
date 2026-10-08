"""知识图谱导出服务。

三个变体：
- roundtrip / evidence 从 PostgreSQL 规范层（权威数据）导出，绝不从 Neo4j 投影导出：
  - roundtrip：nodes.csv + relationships.csv + manifest.json 的 zip 包，严格符合
    rice-endosperm-csv-v3 契约，可直接经 GraphImportModal 重新导入（导出前会用
    parse_managed_graph_import 自校验，拒绝输出无法再导入的包）。
  - evidence：三张 sheet（实体 / 三元组 / 证据明细全语义槽）的 xlsx，供科研审阅。
- projection：从 Neo4j 导出本库投影的全部节点（Entity / Chunk）与全部关系
  （RELATION / MENTIONS），nodes.jsonl + relationships.jsonl + manifest.json 的 zip 包。
  单个读事务内先计数再流式写出（时点一致、不在内存累积整表），写出行数与预计数
  逐类相等才出包（fail-closed），并与规范层做 entity / triple / mention 键集对账写入清单。
  投影是可重建的派生态，但「有迹可循」不能只存在于在线系统里：v2 追加三个成员把原始证据
  一并带走——evidence.jsonl（PG mention 逐条引文 + 导出时重验 + 审核态 + 精确关联边键）、
  chunks.jsonl（被引用段落全文与 sha256，离线可自证「引文 ⊆ 原文」）、decisions.jsonl
  （人工决策叠加层）。证据权威始终在 PostgreSQL mention 表，Neo4j 边上的 text 只是单条预览；
  三个成员与图成员按同一套旁路统计写字数/哈希，manifest 最后写（含全部成员 sha256 与
  I1/I2 的第三维覆盖对账 coverage）。仅结构轻量包（include_evidence=false）保持 v1 行为。

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

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import zipfile
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from yuxi.knowledge.graphs.graph_evidence_service import parse_chunk_provenance, verify_quote
from yuxi.knowledge.graphs.graph_utils import mention_key
from yuxi.knowledge.graphs.managed_import_parser import (
    NODE_HEADERS,
    NODE_TYPE_MAPPING,
    NORMALIZER_VERSION,
    RELATIONSHIP_HEADERS,
    SCHEMA_VERSION,
    parse_managed_graph_import,
)
from yuxi.repositories.knowledge_graph_import_repository import KnowledgeGraphImportRepository
from yuxi.repositories.knowledge_graph_repository import KnowledgeGraphRepository
from yuxi.repositories.knowledge_graph_review_repository import KnowledgeGraphReviewRepository
from yuxi.storage.neo4j import get_shared_neo4j_connection, safe_neo4j_label
from yuxi.utils import logger

EXPORT_TOOL_VERSION = "graph-export-v1"
EXPORT_VARIANTS = ("roundtrip", "evidence", "projection")
PROJECTION_SCHEMA_VERSION = "neo4j-projection-jsonl-v1"
# v2：在 nodes/relationships 之外追加 evidence/chunks/decisions 三个成员（原文证据与人工决策）
PROJECTION_EVIDENCE_SCHEMA_VERSION = "neo4j-projection-jsonl-v2"
PROJECTION_EVIDENCE_MEMBERS = ("evidence.jsonl", "chunks.jsonl", "decisions.jsonl")
VERIFICATION_UNVERIFIABLE = "UNVERIFIABLE"
PROJECTION_NODE_CLASSES = ("entity", "chunk", "other")
PROJECTION_KNOWN_RELATIONSHIP_TYPES = ("RELATION", "MENTIONS")
RECONCILIATION_SAMPLE_LIMIT = 100
# 证据成员读取与写入的批量参数：PG 侧键集分页 / chunk 分批取全文 / JSONL 每批落盘行数
PROJECTION_EVIDENCE_PAGE_SIZE = 1000
PROJECTION_CHUNK_BATCH_SIZE = 500
PROJECTION_JSONL_WRITE_BATCH_ROWS = 200
# HTTP 响应头所见证据摘要的校验态顺序（ASCII，避免非 latin-1 头值）
EVIDENCE_SUMMARY_STATUSES = ("OK", "DEGRADED", "MISSING", VERIFICATION_UNVERIFIABLE)
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
            has_identifier = bool(item.get("pmid") or item.get("doi") or metadata.get("pmids") or metadata.get("dois"))
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
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
    filename = f"graph-roundtrip-{kb_id}.zip"
    return {"filename": filename, "content": buffer.getvalue(), "media_type": "application/zip", "manifest": manifest}


def build_evidence_workbook(
    *,
    kb_id: str,
    entities: list[dict[str, Any]],
    triples: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    aliases_by_entity: dict[str, list[str]] | None = None,
    mention_evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """构造证据明细 xlsx（实体 / 三元组 / 证据宽表三张 sheet；传入 mention_evidence 时追加两张原文引文 sheet）。"""
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
        "entity_id",
        "canonical_identity",
        "label",
        "name",
        "normalized_name",
        "gene_status",
        "rap_ids",
        "msu_ids",
        "aliases",
        "external_ids",
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
        "triple_id",
        "source",
        "target",
        "relation_type",
        "content",
        "support_count",
        "literature_count",
        "best_evidence_level",
        "consensus_direction",
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
        "evidence_id",
        "triple_id",
        "source",
        "target",
        "relation_type",
        "pmid",
        "doi",
        "literature_id",
        "identifier_status",
        "direction",
        "directness",
        "assertion_status",
        "evidence_level",
        "evidence_alignment_status",
        "outcome_class",
        "yield_measure_type",
        "experimental_subject_type",
        "subject_material",
        "perturbs",
        "perturbation_direction",
        "condition",
        "cultivar",
        "genetic_background",
        "development_stage",
        "observed_effect",
        "observed_relation",
        "inferred_gene_function",
        "sentence_id",
        "claim_eligible",
        "evidence_quote",
        "metadata_json",
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

    # 原文引文（chunk 抽取轨）：与图谱面板同源的逐字引文，导出时逐条重验；LLM 轨知识库不再是空表
    if mention_evidence is not None:
        triple_by_id = {triple["triple_id"]: triple for triple in triples}
        mention_headers = [
            "quote",
            "verification",
            "filename",
            "section",
            "literature",
            "page",
            "chunk_id",
            "review_status",
            "confidence",
            "hedge",
            "trigger_verified",
            "trigger_term",
            "verifier_confirmed",
            "pinned_by",
            "extractor_type",
        ]
        triple_rows: list[list[Any]] = []
        entity_rows: list[list[Any]] = []
        for record in mention_evidence:
            if record.get("source") != "chunk_mention":
                continue
            row = projection_evidence_row(record)
            tail = [
                row["quote"] or "",
                row["verification"],
                row["filename"] or "",
                row["section"] or "",
                row["literature"] or "",
                row["page"] or "",
                row["chunk_id"] or "",
                row["review_status"] or "",
                "" if row["confidence"] is None else row["confidence"],
                "" if row["hedge"] is None else bool(row["hedge"]),
                "" if row["trigger_verified"] is None else bool(row["trigger_verified"]),
                row["trigger_term"] or "",
                "" if row["verifier_confirmed"] is None else bool(row["verifier_confirmed"]),
                row["pinned_by"] or "",
                row["extractor_type"] or "",
            ]
            if row["kind"] == "triple":
                triple = triple_by_id.get(row["business_id"]) or {}
                triple_rows.append(
                    [
                        row["business_id"],
                        display_name(triple.get("source_entity_id") or ""),
                        triple.get("relation_type") or "",
                        display_name(triple.get("target_entity_id") or ""),
                        *tail,
                    ]
                )
            else:
                entity_rows.append([row["business_id"], display_name(row["business_id"]), *tail])
        fill_sheet(
            workbook.create_sheet("原文引文-三元组"),
            ["triple_id", "source", "relation_type", "target", *mention_headers],
            triple_rows,
        )
        fill_sheet(workbook.create_sheet("原文引文-实体"), ["entity_id", "entity", *mention_headers], entity_rows)

    output = io.BytesIO()
    workbook.save(output)
    filename = f"graph-evidence-{kb_id}.xlsx"
    media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return {"filename": filename, "content": output.getvalue(), "media_type": media_type}


# ------------------------------------------------------------ projection ----


def projection_cypher(label: str) -> dict[str, str]:
    """本库投影的四条只读查询；计数与数据流必须在同一读事务内执行才是同一快照。"""
    scope = f"MilvusKB:`{label}`"
    return {
        "node_counts": (
            f"MATCH (n:{scope}) "
            "RETURN count(n) AS total, "
            "sum(CASE WHEN n:Entity THEN 1 ELSE 0 END) AS entity, "
            "sum(CASE WHEN n:Chunk THEN 1 ELSE 0 END) AS chunk"
        ),
        "relationship_counts": (
            f"MATCH (:{scope})-[r]->(:{scope}) RETURN type(r) AS relationship_type, count(r) AS total"
        ),
        "nodes": (
            f"MATCH (n:{scope}) RETURN elementId(n) AS element_id, labels(n) AS labels, properties(n) AS properties"
        ),
        "relationships": (
            f"MATCH (a:{scope})-[r]->(b:{scope}) "
            "RETURN elementId(r) AS element_id, type(r) AS relationship_type, properties(r) AS properties, "
            "elementId(a) AS start_element_id, labels(a) AS start_labels, properties(a) AS start_properties, "
            "elementId(b) AS end_element_id, labels(b) AS end_labels, properties(b) AS end_properties"
        ),
    }


_BUSINESS_ID_KEY = {"entity": "entity_id", "chunk": "chunk_id"}


def _node_class(labels: Iterable[str]) -> str:
    label_set = set(labels)
    if "Chunk" in label_set:
        return "chunk"
    if "Entity" in label_set:
        return "entity"
    return "other"


def _business_id(node_class: str, properties: dict[str, Any]) -> str | None:
    key = _BUSINESS_ID_KEY.get(node_class)
    value = properties.get(key) if key else None
    return str(value) if value not in (None, "") else None


def _endpoint(element_id: Any, labels: Any, properties: Any) -> dict[str, Any]:
    sorted_labels = sorted(str(value) for value in labels or [])
    node_class = _node_class(sorted_labels)
    return {
        "element_id": element_id,
        "node_class": node_class,
        "business_id": _business_id(node_class, dict(properties or {})),
    }


def projection_node_row(record: dict[str, Any]) -> dict[str, Any]:
    labels = sorted(str(value) for value in record.get("labels") or [])
    properties = dict(record.get("properties") or {})
    node_class = _node_class(labels)
    return {
        "element_id": record.get("element_id"),
        "node_class": node_class,
        "business_id": _business_id(node_class, properties),
        "labels": labels,
        "properties": properties,
    }


def projection_relationship_row(record: dict[str, Any]) -> dict[str, Any]:
    relationship_type = str(record.get("relationship_type") or "")
    properties = dict(record.get("properties") or {})
    start = _endpoint(record.get("start_element_id"), record.get("start_labels"), record.get("start_properties"))
    end = _endpoint(record.get("end_element_id"), record.get("end_labels"), record.get("end_properties"))
    if relationship_type == "MENTIONS":
        business_id = (
            mention_key(start["business_id"], end["business_id"])
            if start["business_id"] and end["business_id"]
            else None
        )
    else:
        triple_id = properties.get("triple_id")
        business_id = str(triple_id) if triple_id not in (None, "") else None
    return {
        "element_id": record.get("element_id"),
        "relationship_type": relationship_type,
        "business_id": business_id,
        "start": start,
        "end": end,
        "properties": properties,
    }


class _HashingWriter:
    """zip 成员流式写入时同步累计 sha256 与字节数（摘要针对解压后的成员内容）。"""

    def __init__(self, stream):
        self._stream = stream
        self._hash = hashlib.sha256()
        self.bytes_written = 0

    def write(self, data: bytes) -> None:
        self._hash.update(data)
        self.bytes_written += len(data)
        self._stream.write(data)

    def hexdigest(self) -> str:
        return self._hash.hexdigest()


def projection_evidence_row(record: dict[str, Any]) -> dict[str, Any]:
    """PG 统一证据行 → evidence.jsonl 行：导出时对 chunk 全文重验并解析【章节】【文献】来源。

    verification 三态沿用面板口径（OK/DEGRADED/MISSING）；托管导入的 relation_evidence
    没有可比对的源文本，标 UNVERIFIABLE 而不假装通过。
    """
    quote = record.get("quote") or ""
    content = record.get("chunk_content") or ""
    if record.get("source") == "relation_evidence":
        verification, offset = (VERIFICATION_UNVERIFIABLE, None) if quote else ("MISSING", None)
    else:
        verification, offset = verify_quote(quote, content)
    stored_offset = record.get("quote_start_char")
    provenance = parse_chunk_provenance(content, record.get("source_provenance")) if content else {}
    return {
        "kind": record.get("kind"),
        "source": record.get("source") or "chunk_mention",
        "business_id": record.get("target_id"),
        "edge_business_id": record.get("edge_business_id"),
        "chunk_id": record.get("chunk_id"),
        "file_id": record.get("file_id"),
        "filename": record.get("filename"),
        "chunk_index": record.get("chunk_index"),
        "quote": quote or None,
        "quote_start_char": stored_offset if stored_offset is not None else offset,
        "verification": verification,
        "section": provenance.get("section"),
        "literature": provenance.get("literature"),
        "page": provenance.get("page"),
        "extractor_type": record.get("extractor_type"),
        "confidence": record.get("confidence"),
        "hedge": record.get("hedge"),
        "context": record.get("context"),
        "trigger_verified": record.get("trigger_verified"),
        "trigger_term": record.get("trigger_term"),
        "verifier_confirmed": record.get("verifier_confirmed"),
        "pinned_by": record.get("pinned_by"),
        "pinned_at": record.get("pinned_at"),
        "review_status": record.get("review_status"),
        "pmid": record.get("pmid"),
        "doi": record.get("doi"),
    }


def projection_chunk_row(record: dict[str, Any], *, include_text: bool) -> dict[str, Any]:
    """chunks.jsonl 行：段落全文 + sha256（离线复验 quote ⊆ content 的依据）。"""
    content = record.get("content") or ""
    return {
        "chunk_id": record.get("chunk_id"),
        "file_id": record.get("file_id"),
        "filename": record.get("filename"),
        "chunk_index": record.get("chunk_index"),
        "start_char_pos": record.get("start_char_pos"),
        "end_char_pos": record.get("end_char_pos"),
        "source_provenance": record.get("source_provenance"),
        "content": (content or None) if include_text else None,
        "content_length": len(content),
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest() if content else None,
    }


def projection_decision_row(record: dict[str, Any]) -> dict[str, Any]:
    """decisions.jsonl 行：人工决策叠加层（审计：谁在何时依据哪句原文做了什么判断）。"""
    return {
        "target_kind": record.get("target_kind"),
        "target_id": record.get("target_id"),
        "action": record.get("action"),
        "reason": record.get("reason"),
        "actor_uid": record.get("actor_uid"),
        "version": record.get("version"),
        "pinned_chunk_id": record.get("pinned_chunk_id"),
        "pinned_quote": record.get("pinned_quote"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
    }


class EvidenceTally:
    """证据成员写入过程的旁路统计：校验分布、边覆盖键集，供 coverage 对账与 manifest 汇总。"""

    def __init__(self) -> None:
        self.rows_by_kind: Counter[str] = Counter()
        self.rows_by_source: Counter[str] = Counter()
        self.verification: Counter[str] = Counter()
        self.review_status: Counter[str] = Counter()
        # 有引文且来自 chunk mention 的边键：RELATION=(triple_id, chunk_id)，MENTIONS=mention_key
        self.relation_edge_keys: set[tuple[str, str]] = set()
        self.mention_edge_keys: set[str] = set()
        # 全部证据键（含无引文旧数据）与其审核态，供"有证据无边"按 REJECTED 拆分
        self.relation_key_status: dict[tuple[str, str], str] = {}
        self.mention_key_status: dict[str, str] = {}
        self.chunk_ids: set[str] = set()

    def add(self, row: dict[str, Any]) -> None:
        self.rows_by_kind[row["kind"]] += 1
        self.rows_by_source[row["source"]] += 1
        self.verification[row["verification"]] += 1
        if row.get("review_status"):
            self.review_status[row["review_status"]] += 1
        if row["source"] != "chunk_mention" or not row.get("chunk_id"):
            return
        self.chunk_ids.add(row["chunk_id"])
        if row["kind"] == "triple":
            key = (row["business_id"], row["chunk_id"])
            self.relation_key_status[key] = row.get("review_status") or "UNKNOWN"
            if row.get("quote"):
                self.relation_edge_keys.add(key)
        else:
            key = row["edge_business_id"]
            self.mention_key_status[key] = row.get("review_status") or "UNKNOWN"
            if row.get("quote"):
                self.mention_edge_keys.add(key)


def evidence_coverage(tally: ProjectionTally, evidence: EvidenceTally) -> dict[str, Any]:
    """I1/I2 的导出对账：投影边 ↔ PG 引文行双向核对；REJECTED 的"有证据无边"是预期而非漂移。"""

    def sample(values: set | list, joiner: str | None = None) -> list[str]:
        # RELATION 边的证据键是 (triple_id, chunk_id)：渲染成 triple_id@chunk_id 才是精确关联键的样子
        rendered = (
            [joiner.join(str(part) for part in item) for item in values] if joiner else [str(item) for item in values]
        )
        return sorted(rendered)[:RECONCILIATION_SAMPLE_LIMIT]

    relation_missing = tally.relation_edge_keys - evidence.relation_edge_keys
    mention_missing = tally.mention_keys - evidence.mention_edge_keys
    relation_extra = set(evidence.relation_key_status) - tally.relation_edge_keys
    mention_extra = set(evidence.mention_key_status) - tally.mention_keys
    relation_extra_rejected = {key for key in relation_extra if evidence.relation_key_status[key] == "REJECTED"}
    mention_extra_rejected = {key for key in mention_extra if evidence.mention_key_status[key] == "REJECTED"}

    def section(missing: set, extra: set, extra_rejected: set, status_map: dict, joiner: str | None = None) -> dict:
        orphan = {key for key in extra if status_map[key] != "REJECTED"}
        return {
            "edges_without_evidence_count": len(missing),
            "edges_without_evidence": sample(missing, joiner),
            "evidence_without_edge_count": len(extra),
            "evidence_without_edge": sample(extra, joiner),
            "evidence_without_edge_rejected_count": len(extra_rejected),
            "evidence_without_edge_rejected": sample(extra_rejected, joiner),
            "evidence_without_edge_orphan_count": len(orphan),
            "evidence_without_edge_orphan": sample(orphan, joiner),
        }

    relation_section = section(
        relation_missing, relation_extra, relation_extra_rejected, evidence.relation_key_status, joiner="@"
    )
    mention_section = section(mention_missing, mention_extra, mention_extra_rejected, evidence.mention_key_status)
    evidence_complete = (
        relation_section["edges_without_evidence_count"] == 0
        and mention_section["edges_without_evidence_count"] == 0
        and relation_section["evidence_without_edge_orphan_count"] == 0
        and mention_section["evidence_without_edge_orphan_count"] == 0
    )
    return {
        "evidence_complete": evidence_complete,
        "relation_edges": relation_section,
        "mention_edges": mention_section,
    }


def summarize_projection_evidence(
    files: dict[str, Any],
    evidence_tally: EvidenceTally,
    tally: ProjectionTally,
    *,
    include_chunk_text: bool,
) -> dict[str, Any]:
    """manifest 的 evidence 段：三个追加成员的计数 / 校验分布 / 审核态分布 / I1-I2 覆盖对账。"""
    referenced_chunks = len(evidence_tally.chunk_ids)
    chunk_rows = files["chunks.jsonl"]["rows"]
    return {
        "include_chunk_text": include_chunk_text,
        "counts": {
            "evidence_rows": sum(evidence_tally.rows_by_kind.values()),
            "by_kind": dict(evidence_tally.rows_by_kind),
            "by_source": dict(evidence_tally.rows_by_source),
            "referenced_chunks": referenced_chunks,
            "chunk_rows": chunk_rows,
            # 引用到但已不在 PG 的段落（文件重解析/删除）：不静默，如实计数供对账
            "missing_chunk_rows": max(referenced_chunks - chunk_rows, 0),
            "decision_rows": files["decisions.jsonl"]["rows"],
        },
        "verification": dict(evidence_tally.verification),
        "review_status": dict(evidence_tally.review_status),
        "coverage": evidence_coverage(tally, evidence_tally),
    }


def write_projection_evidence_members(
    archive: zipfile.ZipFile,
    files: dict[str, Any],
    tally: ProjectionTally,
    evidence_bundle: dict[str, Any],
    *,
    include_chunk_text: bool,
) -> dict[str, Any]:
    """[同步] 追加 evidence / chunks / decisions 三个成员（内存证据包版本，供测试与离线脚本）。

    流式导出（大库）请走 export_projection_package，它按 PG 键集分页逐行写入同一批成员。
    """
    evidence_tally = EvidenceTally()
    files["evidence.jsonl"] = write_jsonl_member(
        archive, "evidence.jsonl", evidence_bundle.get("rows") or [], projection_evidence_row, evidence_tally.add
    )
    files["chunks.jsonl"] = write_jsonl_member(
        archive,
        "chunks.jsonl",
        evidence_bundle.get("chunks") or [],
        lambda record: projection_chunk_row(record, include_text=include_chunk_text),
    )
    files["decisions.jsonl"] = write_jsonl_member(
        archive, "decisions.jsonl", evidence_bundle.get("decisions") or [], projection_decision_row
    )
    return summarize_projection_evidence(files, evidence_tally, tally, include_chunk_text=include_chunk_text)


def evidence_summary_header(package: dict[str, Any]) -> str | None:
    """导出包的 ASCII 证据摘要（X-Export-Evidence-Summary）：仅结构包与非 v2 包返回 None。

    头部值必须是 latin-1 可编码字符，因此只放英文键值，不给前端渲染文案。
    """
    evidence = (package.get("manifest") or {}).get("evidence")
    if not evidence:
        return None
    counts = evidence["counts"]
    verification = evidence["verification"]
    parts = [f"evidence={counts['evidence_rows']}"]
    for status in EVIDENCE_SUMMARY_STATUSES:
        if verification.get(status):
            parts.append(f"{status.lower()}={verification[status]}")
    parts.append(f"chunks={counts['chunk_rows']}")
    parts.append(f"decisions={counts['decision_rows']}")
    if not evidence["coverage"]["evidence_complete"]:
        parts.append("complete=false")
    return "; ".join(parts)


def _json_default(value: Any) -> str:
    # Neo4j 时间/空间属性类型不可直接 JSON 序列化，退化为其字符串表示
    return str(value)


def write_jsonl_member(
    archive: zipfile.ZipFile,
    name: str,
    records: Iterable[dict[str, Any]],
    transform: Callable[[dict[str, Any]], dict[str, Any]],
    on_row: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """把记录流逐行转换后写入 zip 成员（JSON Lines），全程不在内存中累积整表。"""
    with archive.open(name, "w", force_zip64=True) as member:
        writer = _HashingWriter(member)
        rows = 0
        for record in records:
            row = transform(record)
            if on_row is not None:
                on_row(row)
            writer.write((json.dumps(row, ensure_ascii=False, default=_json_default) + "\n").encode("utf-8"))
            rows += 1
    return {"sha256": writer.hexdigest(), "rows": rows, "bytes": writer.bytes_written}


async def _as_async_iterable(records: Any):
    """证据数据源统一入口：异步迭代器（PG 流式）与普通可迭代对象（小表列表）都能喂。"""
    if hasattr(records, "__aiter__"):
        async for item in records:
            yield item
    else:
        for item in records:
            yield item


async def write_jsonl_member_async(
    archive: zipfile.ZipFile,
    name: str,
    records: Any,
    transform: Callable[[dict[str, Any]], dict[str, Any]],
    on_row: Callable[[dict[str, Any]], None] | None = None,
    *,
    batch_rows: int = PROJECTION_JSONL_WRITE_BATCH_ROWS,
) -> dict[str, Any]:
    """异步数据源版的流式 JSONL 成员写入：records 可为异步迭代器（PG 键集分页）或普通可迭代对象。

    逐行转换 + 旁路统计后按批交给线程落盘（deflate 不进事件循环），
    返回与 write_jsonl_member 同形的 {sha256, rows, bytes}。
    """
    with archive.open(name, "w", force_zip64=True) as member:
        writer = _HashingWriter(member)
        pending: list[bytes] = []
        rows = 0

        async def flush() -> None:
            if pending:
                payload = b"".join(pending)
                pending.clear()
                await asyncio.to_thread(writer.write, payload)

        async for record in _as_async_iterable(records):
            row = transform(record)
            if on_row is not None:
                on_row(row)
            pending.append((json.dumps(row, ensure_ascii=False, default=_json_default) + "\n").encode("utf-8"))
            rows += 1
            if len(pending) >= batch_rows:
                await flush()
        await flush()
    return {"sha256": writer.hexdigest(), "rows": rows, "bytes": writer.bytes_written}


class ProjectionTally:
    """流式写入过程中的旁路统计：分类计数与业务键集合，供计数校验与对账。"""

    def __init__(self) -> None:
        self.node_classes: Counter[str] = Counter()
        self.relationship_types: Counter[str] = Counter()
        self.entity_ids: set[str] = set()
        self.chunk_ids: set[str] = set()
        self.triple_ids: set[str] = set()
        self.mention_keys: set[str] = set()
        self.relation_edge_keys: set[tuple[str, str]] = set()
        self.nodes_without_business_id = 0
        self.relationships_without_business_id = 0

    def add_node(self, row: dict[str, Any]) -> None:
        self.node_classes[row["node_class"]] += 1
        business_id = row.get("business_id")
        if not business_id:
            self.nodes_without_business_id += 1
        elif row["node_class"] == "entity":
            self.entity_ids.add(business_id)
        elif row["node_class"] == "chunk":
            self.chunk_ids.add(business_id)

    def add_relationship(self, row: dict[str, Any]) -> None:
        relationship_type = row["relationship_type"]
        self.relationship_types[relationship_type] += 1
        business_id = row.get("business_id")
        if not business_id:
            self.relationships_without_business_id += 1
        elif relationship_type == "MENTIONS":
            self.mention_keys.add(business_id)
        elif relationship_type == "RELATION":
            self.triple_ids.add(business_id)
            chunk_id = (row.get("properties") or {}).get("chunk_id")
            if chunk_id:
                # 证据覆盖对账的边粒度：RELATION 边 MERGE 键含 chunk_id，与 triple_mentions 一一对应
                self.relation_edge_keys.add((business_id, str(chunk_id)))


def verify_projection_counts(
    expected_nodes: dict[str, int],
    expected_relationships: dict[str, int],
    tally: ProjectionTally,
) -> dict[str, Any]:
    """事务内预计数与实际写出行数必须逐类相等；同一快照下不等即导出器缺陷，拒绝出包。"""
    mismatches: list[str] = []
    written_nodes = sum(tally.node_classes.values())
    if written_nodes != expected_nodes["total"]:
        mismatches.append(f"节点总数 期望 {expected_nodes['total']} 实际 {written_nodes}")
    for node_class in ("entity", "chunk"):
        expected = expected_nodes.get(node_class, 0)
        actual = tally.node_classes.get(node_class, 0)
        if expected != actual:
            mismatches.append(f"{node_class} 节点 期望 {expected} 实际 {actual}")
    for relationship_type in sorted(set(expected_relationships) | set(tally.relationship_types)):
        expected = expected_relationships.get(relationship_type, 0)
        actual = tally.relationship_types.get(relationship_type, 0)
        if expected != actual:
            mismatches.append(f"{relationship_type} 关系 期望 {expected} 实际 {actual}")
    if mismatches:
        raise RuntimeError("投影导出计数校验失败，拒绝导出：" + "；".join(mismatches))
    return {
        "status": "PASSED",
        "nodes": {"expected": dict(expected_nodes), "written": dict(tally.node_classes)},
        "relationships": {"expected": dict(expected_relationships), "written": dict(tally.relationship_types)},
    }


def reconcile_projection(reference: dict[str, Any], tally: ProjectionTally) -> dict[str, Any]:
    """规范层 ↔ 投影 键集对账：缺失 = 投影落后于规范层，多出 = 投影漂移（回滚/重置未清干净）。

    chunk 维度特殊：只有 graph_indexed 的 chunk 才应有 Chunk 节点，所以"缺失"按已索引集合算；
    "多出"按 PG 全部 chunk 算（PG 已无此 chunk 即为重新解析后的投影残留）。
    人工审核 REJECTED 的三元组/实体按设计不在投影中：从"应有"集合剔除，仍投影则记为漂移（I4）。
    """

    def diff(canonical: set[str], projected: set[str]) -> dict[str, Any]:
        missing = sorted(canonical - projected)
        extra = sorted(projected - canonical)
        return {
            "canonical_count": len(canonical),
            "projected_count": len(projected),
            "missing_in_projection_count": len(missing),
            "missing_in_projection": missing[:RECONCILIATION_SAMPLE_LIMIT],
            "extra_in_projection_count": len(extra),
            "extra_in_projection": extra[:RECONCILIATION_SAMPLE_LIMIT],
        }

    rejected_entities = set(reference.get("rejected_entity_ids") or ())
    rejected_triples = set(reference.get("rejected_triple_ids") or ())
    canonical_entities = set(reference.get("entity_ids") or ()) - rejected_entities
    canonical_triples = set(reference.get("triple_ids") or ()) - rejected_triples
    canonical_mentions = {
        key for key in set(reference.get("mention_keys") or ()) if key.rsplit("->", 1)[-1] not in rejected_entities
    }
    rejected_still_projected = sorted((tally.triple_ids & rejected_triples) | (tally.entity_ids & rejected_entities))

    all_chunks = set(reference.get("chunk_ids") or ())
    indexed_chunks = set(reference.get("graph_indexed_chunk_ids") or ())
    chunks = diff(indexed_chunks, tally.chunk_ids)
    stale_chunks = sorted(tally.chunk_ids - all_chunks)
    unflagged_chunks = sorted((tally.chunk_ids & all_chunks) - indexed_chunks)
    chunks.update(
        {
            "canonical_total_chunks": len(all_chunks),
            "extra_in_projection_count": len(stale_chunks),
            "extra_in_projection": stale_chunks[:RECONCILIATION_SAMPLE_LIMIT],
            "projected_but_not_flagged_count": len(unflagged_chunks),
            "projected_but_not_flagged": unflagged_chunks[:RECONCILIATION_SAMPLE_LIMIT],
        }
    )

    sections = {
        "entities": diff(canonical_entities, tally.entity_ids - rejected_entities),
        "triples": diff(canonical_triples, tally.triple_ids - rejected_triples),
        "mentions": diff(canonical_mentions, tally.mention_keys),
        "chunks": chunks,
    }
    matched = (
        all(
            section["missing_in_projection_count"] == 0 and section["extra_in_projection_count"] == 0
            for section in sections.values()
        )
        and not rejected_still_projected
    )
    return {
        "matched": matched,
        "sample_limit": RECONCILIATION_SAMPLE_LIMIT,
        **sections,
        "review": {
            "rejected_triples": len(rejected_triples),
            "rejected_entities": len(rejected_entities),
            "rejected_still_projected_count": len(rejected_still_projected),
            "rejected_still_projected": rejected_still_projected[:RECONCILIATION_SAMPLE_LIMIT],
        },
    }


def build_projection_manifest(
    *,
    kb_id: str,
    kb_name: str | None,
    label: str,
    exported_by: str,
    files: dict[str, Any],
    verification: dict[str, Any],
    reconciliation: dict[str, Any],
    tally: ProjectionTally,
    evidence: dict[str, Any] | None = None,
    include_chunk_text: bool = True,
) -> dict[str, Any]:
    relationship_types = sorted(tally.relationship_types)
    unknown_types = [item for item in relationship_types if item not in PROJECTION_KNOWN_RELATIONSHIP_TYPES]
    manifest = {
        "schema_version": PROJECTION_EVIDENCE_SCHEMA_VERSION if evidence else PROJECTION_SCHEMA_VERSION,
        "export_tool_version": EXPORT_TOOL_VERSION,
        "source": {"store": "neo4j", "role": "rebuildable_projection", "authority": "postgresql_canonical"},
        "kb_id": kb_id,
        "kb_name": kb_name,
        "exported_at": datetime.now(UTC).isoformat(),
        "exported_by": exported_by,
        "scope": {
            "node_match": f"(n:MilvusKB:`{label}`)",
            "relationship_match": f"(a:MilvusKB:`{label}`)-[r]->(b:MilvusKB:`{label}`)",
            "node_classes": list(PROJECTION_NODE_CLASSES),
            "relationship_types": relationship_types,
            "consistency": (
                "neo4j_single_read_transaction + postgresql_read_after" if evidence else "single_read_transaction"
            ),
        },
        "counts": {
            "nodes": {
                "total": sum(tally.node_classes.values()),
                **{node_class: tally.node_classes.get(node_class, 0) for node_class in PROJECTION_NODE_CLASSES},
            },
            "relationships": {
                "total": sum(tally.relationship_types.values()),
                **{item: tally.relationship_types[item] for item in relationship_types},
            },
            "distinct_business_ids": {
                "entities": len(tally.entity_ids),
                "chunks": len(tally.chunk_ids),
                "triples": len(tally.triple_ids),
                "mentions": len(tally.mention_keys),
            },
            "without_business_id": {
                "nodes": tally.nodes_without_business_id,
                "relationships": tally.relationships_without_business_id,
            },
        },
        "files": files,
        "verification": verification,
        "reconciliation": reconciliation,
        "row_schema": {
            "nodes.jsonl": {
                "element_id": "Neo4j elementId，仅在本实例生命周期内稳定",
                "node_class": "entity | chunk | other",
                "business_id": "entity 节点为 entity_id，chunk 节点为 chunk_id",
                "labels": "节点全部标签（排序）",
                "properties": "节点完整属性映射（原样）",
            },
            "relationships.jsonl": {
                "element_id": "Neo4j elementId，仅在本实例生命周期内稳定",
                "relationship_type": "RELATION | MENTIONS | 其他契约外类型",
                "business_id": "RELATION 为 triple_id；MENTIONS 为 chunk_id->entity_id",
                "start / end": "端点的 element_id、node_class、business_id",
                "properties": "关系完整属性映射（原样）",
            },
        },
        "notes": [
            "Neo4j 是 PostgreSQL 规范层的可重建投影；本包反映导出时刻已物化的投影状态，权威数据以规范层导出为准。",
            "chunk 抽取轨的 RELATION 边按（chunk, 关系）粒度存储，同一 triple_id 可对应多条边；"
            "distinct_business_ids.triples 为去重后的三元组数。",
            "节点与关系在同一 Neo4j 读事务内读取；跨实例关联请使用 business_id 而非 element_id。",
        ],
    }
    if evidence:
        manifest["evidence"] = evidence
        manifest["row_schema"]["evidence.jsonl"] = {
            "kind": "triple | entity（证据所属对象类型）",
            "source": "chunk_mention（chunk 抽取轨）| relation_evidence（托管导入）",
            "business_id": "triple_id 或 entity_id",
            "edge_business_id": (
                "与 relationships.jsonl.business_id 同键：triple 为 triple_id，"
                "entity mention 为 chunk_id->entity_id；结合 chunk_id 可唯一定位 RELATION 边"
            ),
            "quote": "逐字引文；verification=OK 时保证是 chunks.jsonl 中对应 content 的子串",
            "verification": (
                "OK | DEGRADED（引文与当前原文不一致）| MISSING（旧数据无引文）| UNVERIFIABLE（托管导入无源文本）"
            ),
            "section / literature / page": "由 chunk 前缀标记行（【章节】【文献】【页码】）与 source_provenance 解析",
            "review_status / pinned_by": "与图谱面板同源的审核态与固定证据标记",
        }
        manifest["row_schema"]["chunks.jsonl"] = {
            "content": "段落全文（原文）；include_chunk_text=false 时不导出，仅保留哈希",
            "content_sha256": "导出时全文 sha256，离线复验 quote ⊆ content 的依据",
        }
        manifest["row_schema"]["decisions.jsonl"] = {
            "说明": "人工审核决策叠加层（APPROVE/REJECT/SUPERSEDE/RENAME/RETYPE），reason 与 pinned_quote 是审计依据",
        }
        note = "证据权威在 PostgreSQL mention 表（evidence.jsonl），Neo4j 边上的 text 仅是单条预览；"
        if include_chunk_text:
            note += "chunks.jsonl 提供段落全文与哈希，可离线复验引文。"
        else:
            note += "本次未导出段落全文（include_chunk_text=false），无法离线复验，仅保留 content_sha256。"
        manifest["notes"].append(note)
        manifest["notes"].append("旧数据 mention 无引文以 verification=MISSING 标出，重置图谱后重跑构建可回填。")
        manifest["notes"].append(
            "人工 REJECTED 对象的证据仍在包内（边已从投影删除、PG mention 保留）："
            "coverage 里单列为「有证据无边·已拒绝」并附 decisions.jsonl 的拒绝理由，这是审计痕迹而非漂移。"
        )
        if not evidence["coverage"]["evidence_complete"]:
            manifest["notes"].append("证据覆盖对账未通过（详见 evidence.coverage）：部分投影边缺引文或存在无边证据。")
    else:
        manifest["notes"].append(
            "本包为仅结构轻量导出（include_evidence=false）：不含原文证据，无法离线验证；"
            "证据明细请使用含证据的投影导出或 /api/graph/evidence/*。"
        )
    if unknown_types:
        manifest["notes"].append(f"出现契约外关系类型 {unknown_types}，已原样导出。")
    if not reconciliation["matched"]:
        manifest["notes"].append(
            "对账未通过：投影与规范层键集存在差异（详见 reconciliation）；"
            "可通过重新执行导入或重建投影修复，权威数据以规范层为准。"
        )
    return manifest


def write_projection_graph_members(
    kb_id: str,
    run: Callable[[str], Iterable[dict[str, Any]]],
    reference: dict[str, Any],
) -> dict[str, Any]:
    """阶段 A：在调用方已开启的读事务上写 nodes/relationships，并完成计数校验与键集对账。

    run(cypher) 返回该查询的记录流（dict 形式）。返回 staged（buffer / files / tally /
    verification / reconciliation），manifest 由调用方最后写。
    """
    label = safe_neo4j_label(kb_id)
    cypher = projection_cypher(label)
    node_counts_record = next(iter(run(cypher["node_counts"])), None) or {}
    expected_nodes = {key: int(node_counts_record.get(key) or 0) for key in ("total", "entity", "chunk")}
    if expected_nodes["total"] == 0:
        raise ValueError("当前知识库在 Neo4j 中没有可导出的投影节点")
    expected_relationships = {
        str(item["relationship_type"]): int(item["total"] or 0) for item in run(cypher["relationship_counts"])
    }

    tally = ProjectionTally()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        files = {
            "nodes.jsonl": write_jsonl_member(
                archive, "nodes.jsonl", run(cypher["nodes"]), projection_node_row, tally.add_node
            ),
            "relationships.jsonl": write_jsonl_member(
                archive,
                "relationships.jsonl",
                run(cypher["relationships"]),
                projection_relationship_row,
                tally.add_relationship,
            ),
        }
    return {
        "label": label,
        "buffer": buffer,
        "files": files,
        "tally": tally,
        "verification": verify_projection_counts(expected_nodes, expected_relationships, tally),
        "reconciliation": reconcile_projection(reference, tally),
    }


def close_projection_package(
    *,
    staged: dict[str, Any],
    kb_id: str,
    reference: dict[str, Any],
    exported_by: str,
    evidence_section: dict[str, Any] | None,
    include_chunk_text: bool,
) -> dict[str, Any]:
    """阶段 C：清单最后写（全部成员 sha256 + evidence 段），先让包内证据自证、再自述。"""
    manifest = build_projection_manifest(
        kb_id=kb_id,
        kb_name=reference.get("kb_name"),
        label=staged["label"],
        exported_by=exported_by,
        files=staged["files"],
        verification=staged["verification"],
        reconciliation=staged["reconciliation"],
        tally=staged["tally"],
        evidence=evidence_section,
        include_chunk_text=include_chunk_text,
    )
    with zipfile.ZipFile(staged["buffer"], "a", zipfile.ZIP_DEFLATED) as archive:
        # 与 JSONL 成员同样走 zip64 通道：大包（>4GB 偏移）下也不因中央目录写不进而失败
        with archive.open("manifest.json", "w", force_zip64=True) as member:
            member.write(json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
    return {
        "filename": f"graph-projection-{kb_id}.zip",
        "content": staged["buffer"].getvalue(),
        "media_type": "application/zip",
        "manifest": manifest,
    }


def assemble_projection_package(
    *,
    kb_id: str,
    run: Callable[[str], Iterable[dict[str, Any]]],
    reference: dict[str, Any],
    exported_by: str = "system",
    evidence_bundle: dict[str, Any] | None = None,
    include_chunk_text: bool = True,
) -> dict[str, Any]:
    """[同步] 在调用方已开启的读事务上装配整包；run(cypher) 返回该查询的记录流（dict 形式）。

    evidence_bundle（PG 先行读取的内存证据包）提供 evidence/chunks/decisions 三个追加成员与
    I1/I2 覆盖对账；缺省时为仅结构轻量包（schema v1，行为与既往完全一致）。
    大库请走 export_projection_package：证据读取按 PG 键集分页流式进行，不整表进内存。
    """
    staged = write_projection_graph_members(kb_id, run, reference)
    evidence_section: dict[str, Any] | None = None
    if evidence_bundle is not None:
        with zipfile.ZipFile(staged["buffer"], "a", zipfile.ZIP_DEFLATED) as archive:
            evidence_section = write_projection_evidence_members(
                archive, staged["files"], staged["tally"], evidence_bundle, include_chunk_text=include_chunk_text
            )
    return close_projection_package(
        staged=staged,
        kb_id=kb_id,
        reference=reference,
        exported_by=exported_by,
        evidence_section=evidence_section,
        include_chunk_text=include_chunk_text,
    )


def build_projection_graph_stage(*, kb_id: str, driver: Any, reference: dict[str, Any]) -> dict[str, Any]:
    """阶段 A（驱动版）：单个显式读事务内写图成员并 commit。

    不用 execute_read 是因为其自动重试会把 zip 成员写重。
    """
    with driver.session() as session, session.begin_transaction() as tx:
        staged = write_projection_graph_members(
            kb_id, lambda query: (record.data() for record in tx.run(query)), reference
        )
        tx.commit()
    return staged


async def export_projection_package(
    *,
    kb_id: str,
    driver: Any,
    reference: dict[str, Any],
    exported_by: str = "system",
    include_evidence: bool = True,
    include_chunk_text: bool = True,
    graph_repository: Any = None,
    review_repository: Any = None,
    evidence_page_size: int = PROJECTION_EVIDENCE_PAGE_SIZE,
    chunk_batch_size: int = PROJECTION_CHUNK_BATCH_SIZE,
) -> dict[str, Any]:
    """[异步] 导出权威入口：阶段 A（Neo4j 单读事务写图成员，线程内完成）→ 阶段 B（PG 键集分页
    流式追加 evidence/chunks/decisions）→ 阶段 C（清单最后写）。

    include_evidence=false 时退化为仅结构轻量包（v1 行契约，不含证据成员）。
    两库两快照的一致性如实写进 manifest.scope.consistency，差异落进 coverage 而不是被掩盖。
    """
    graph_repository = graph_repository or KnowledgeGraphRepository()
    review_repository = review_repository or KnowledgeGraphReviewRepository()
    staged = await asyncio.to_thread(build_projection_graph_stage, kb_id=kb_id, driver=driver, reference=reference)

    evidence_section: dict[str, Any] | None = None
    if include_evidence:
        evidence_tally = EvidenceTally()
        with zipfile.ZipFile(staged["buffer"], "a", zipfile.ZIP_DEFLATED) as archive:
            staged["files"]["evidence.jsonl"] = await write_jsonl_member_async(
                archive,
                "evidence.jsonl",
                graph_repository.iter_projection_evidence(kb_id, page_size=evidence_page_size),
                projection_evidence_row,
                evidence_tally.add,
            )
            staged["files"]["chunks.jsonl"] = await write_jsonl_member_async(
                archive,
                "chunks.jsonl",
                graph_repository.iter_projection_chunks(
                    kb_id, sorted(evidence_tally.chunk_ids), batch_size=chunk_batch_size
                ),
                lambda record: projection_chunk_row(record, include_text=include_chunk_text),
            )
            staged["files"]["decisions.jsonl"] = await write_jsonl_member_async(
                archive, "decisions.jsonl", await review_repository.list_decisions(kb_id), projection_decision_row
            )
        evidence_section = summarize_projection_evidence(
            staged["files"], evidence_tally, staged["tally"], include_chunk_text=include_chunk_text
        )
    return close_projection_package(
        staged=staged,
        kb_id=kb_id,
        reference=reference,
        exported_by=exported_by,
        evidence_section=evidence_section,
        include_chunk_text=include_chunk_text,
    )


def build_projection_package(
    *,
    kb_id: str,
    driver: Any,
    reference: dict[str, Any],
    exported_by: str = "system",
) -> dict[str, Any]:
    """[同步] 仅结构包（无证据成员），保留给既有调用方与离线脚本。

    含原文证据的导出走 export_projection_package：证据成员按 PG 键集分页流式写入。
    """
    staged = build_projection_graph_stage(kb_id=kb_id, driver=driver, reference=reference)
    return close_projection_package(
        staged=staged,
        kb_id=kb_id,
        reference=reference,
        exported_by=exported_by,
        evidence_section=None,
        include_chunk_text=True,
    )


class ManagedGraphExportService:
    """图谱导出服务：单 session 一致性快照 -> 纯函数构造 -> 自校验 -> 返回字节。"""

    def __init__(self):
        self.repository = KnowledgeGraphImportRepository()
        self.graph_repository = KnowledgeGraphRepository()
        self.review_repository = KnowledgeGraphReviewRepository()

    async def export(
        self,
        kb_id: str,
        *,
        variant: str = "roundtrip",
        exported_by: str = "system",
        include_evidence: bool = True,
        include_chunk_text: bool = True,
    ) -> dict[str, Any]:
        if variant not in EXPORT_VARIANTS:
            raise ValueError(f"不支持的导出变体: {variant}（可选 {'/'.join(EXPORT_VARIANTS)}）")
        if variant == "projection":
            return await self._export_projection(
                kb_id,
                exported_by=exported_by,
                include_evidence=include_evidence,
                include_chunk_text=include_chunk_text,
            )

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
            # 原文引文来自 mention 表：chunk 抽取轨的库（LLM 轨）证据明细不再空表，且与图谱面板同源
            mention_evidence = await self.graph_repository.list_projection_evidence(kb_id)
            package = build_evidence_workbook(
                kb_id=kb_id,
                entities=entities,
                triples=triples,
                evidence=evidence,
                aliases_by_entity=aliases_by_entity,
                mention_evidence=mention_evidence,
            )
        logger.info(
            f"图谱导出完成 kb={kb_id} variant={variant} by={exported_by} "
            f"entities={len(entities)} triples={len(triples)} evidence={len(evidence)}"
        )
        return package

    async def _export_projection(
        self,
        kb_id: str,
        *,
        exported_by: str,
        include_evidence: bool = True,
        include_chunk_text: bool = True,
    ) -> dict[str, Any]:
        connection = get_shared_neo4j_connection()
        if connection.driver is None:
            raise ValueError("Neo4j 未连接（LITE_MODE 或连接失败），无法导出投影")
        reference = await self.repository.projection_reference(kb_id)
        package = await export_projection_package(
            kb_id=kb_id,
            driver=connection.driver,
            reference=reference,
            exported_by=exported_by,
            include_evidence=include_evidence,
            include_chunk_text=include_chunk_text,
            graph_repository=self.graph_repository,
            review_repository=self.review_repository,
        )
        manifest = package["manifest"]
        counts = manifest["counts"]
        evidence = manifest.get("evidence") or {}
        evidence_counts = evidence.get("counts") or {}
        logger.info(
            f"图谱投影导出完成 kb={kb_id} variant=projection by={exported_by} "
            f"nodes={counts['nodes']['total']} relationships={counts['relationships']['total']} "
            f"evidence_rows={evidence_counts.get('evidence_rows', 0)} chunks={evidence_counts.get('chunk_rows', 0)} "
            f"decisions={evidence_counts.get('decision_rows', 0)} "
            f"reconciled={manifest['reconciliation']['matched']} "
            f"evidence_complete={(evidence.get('coverage') or {}).get('evidence_complete')}"
        )
        return package
