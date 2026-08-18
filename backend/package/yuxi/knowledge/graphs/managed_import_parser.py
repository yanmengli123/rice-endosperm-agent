from __future__ import annotations

import csv
import io
import re
from collections import Counter, defaultdict
from typing import Any

from yuxi.knowledge.graphs.graph_utils import (
    compute_entity_id,
    compute_triple_id,
    normalize_entity_name,
)
from yuxi.knowledge.research_evidence import (
    build_evidence_semantics,
    identifier_has_scientific_notation,
    validate_doi,
    validate_pmid,
)
from yuxi.utils import hashstr

SCHEMA_VERSION = "rice-endosperm-csv-v3"
NORMALIZER_VERSION = "managed-graph-v3"

NODE_HEADERS = {
    "node_id",
    "name",
    "node_type",
    "rap_id",
    "msu_id",
    "out_degree",
    "in_degree",
    "publication_count",
}
RELATIONSHIP_HEADERS = {
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
}

NODE_TYPE_MAPPING = {
    "RICE_GENE": ("Gene", "confirmed"),
    "RICE_GENE_CANDIDATE": ("Gene", "candidate"),
    "GENE": ("Gene", "unspecified"),
    "ALLELE_MUTANT": ("AlleleMutant", None),
    "PHENOTYPE": ("Phenotype", None),
    "PROCESS": ("Process", None),
    "QTL_LOCUS": ("QTL", None),
    "CIS_ELEMENT": ("CisElement", None),
    "RNA": ("RNA", None),
}

GENE_RELATION_TYPES = {
    "DIRECT_BINDING",
    "TRANSCRIPTIONAL_ACTIVATION",
    "TRANSCRIPTIONAL_REPRESSION",
    "TRANSCRIPTIONAL_REGULATION",
    "PROTEIN_ACTIVITY_REGULATION",
    "PROTEIN_DEGRADATION",
    "REQUIRED_FOR",
    "PROMOTES_PROCESS",
    "INHIBITS_PROCESS",
    "PROMOTES_PHENOTYPE",
    "SUPPRESSES_PHENOTYPE",
    "REGULATES_PROCESS",
    "REGULATES_PHENOTYPE",
    "EXPRESSION_IN",
    "COEXPRESSION",
}
ALLELE_RELATION_TYPES = {"MUTANT_EFFECT"}
PERTURBATION_RELATION_TYPES = {
    "KNOCKOUT_EFFECT",
    "CRISPR_EFFECT",
    "RNAI_EFFECT",
    "OVEREXPRESSION_EFFECT",
}

_DANGEROUS_CYPHER = re.compile(
    r"\b(?:DROP|DELETE|DETACH\s+DELETE|REMOVE|CREATE|MERGE|SET|LOAD\s+CSV|CALL|APOC\.)\b",
    re.IGNORECASE,
)


class ManagedGraphImportValidationError(ValueError):
    def __init__(self, report: dict[str, Any]):
        self.report = report
        super().__init__("图谱导入文件未通过预检")


def parse_managed_graph_import(
    *,
    kb_id: str,
    nodes_bytes: bytes,
    relationships_bytes: bytes,
    cypher_bytes: bytes | None = None,
    resolutions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate CSV input and build the PostgreSQL canonical import plan.

    Cypher is deliberately treated as an auditable description. It never enters
    the executable plan.
    """
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    info: list[dict[str, Any]] = []
    resolutions = resolutions or {}
    nodes = _read_csv(nodes_bytes, NODE_HEADERS, "nodes", blockers)
    relationships = _read_csv(relationships_bytes, RELATIONSHIP_HEADERS, "relationships", blockers)
    cypher_report = _inspect_cypher(cypher_bytes, blockers)
    _validate_identifier_columns(nodes, relationships, blockers)

    if blockers:
        return _result(blockers, warnings, info, [], [], cypher_report, {}, None)

    groups, _ = _group_nodes(nodes)
    conflicts, semantic_splits = _find_node_issues(groups, relationships, warnings)
    chosen_rows, unresolved = _resolve_groups(groups, conflicts, resolutions)
    entities, external_routes, row_to_entity = _build_entities(
        kb_id,
        groups,
        chosen_rows,
        semantic_splits,
        resolutions,
    )

    known_external_ids = set(external_routes)
    canonical_relation_counts = Counter()
    triple_by_id: dict[str, dict[str, Any]] = {}
    triple_sources: list[dict[str, Any]] = []
    evidence_by_id: dict[str, dict[str, Any]] = {}
    evidence_sources: list[dict[str, Any]] = []

    for row in relationships:
        row_number = row["_row_number"]
        start_id = row["start_id"].strip()
        end_id = row["end_id"].strip()
        relation_type = row["relation_type"].strip().upper()
        missing = [item for item in (start_id, end_id) if item and item not in known_external_ids]
        if not start_id or not end_id or not relation_type:
            blockers.append(
                {
                    "code": "RELATION_REQUIRED_FIELD",
                    "severity": "BLOCKER",
                    "row_number": row_number,
                    "message": "关系行缺少 start_id、end_id 或 relation_type",
                }
            )
            continue
        if missing:
            blockers.append(
                {
                    "code": "DANGLING_RELATION",
                    "severity": "BLOCKER",
                    "row_number": row_number,
                    "external_ids": missing,
                    "message": "关系端点未在节点 CSV 中定义",
                }
            )
            continue

        source = _route_endpoint(external_routes[start_id], row, "start", semantic_splits, resolutions)
        target = _route_endpoint(external_routes[end_id], row, "end", semantic_splits, resolutions)
        triple_id = _triple_id(kb_id, source, relation_type, target)
        canonical_relation_counts[triple_id] += 1
        triple_by_id.setdefault(triple_id, _triple(kb_id, triple_id, source, relation_type, target))
        triple_sources.append(
            {
                "triple_id": triple_id,
                "source_id": f"relationships:{row_number}",
                "row_number": row_number,
                "raw_data": _without_internal_fields(row),
            }
        )

        alignment_status = _evidence_alignment_status(row)
        if alignment_status == "AMBIGUOUS":
            blockers.append(
                {
                    "code": "EVIDENCE_ALIGNMENT_AMBIGUOUS",
                    "severity": "BLOCKER",
                    "row_number": row_number,
                    "message": "PMID、DOI 与证据引文数量不一致；无法可靠对齐，已阻止导入且不会猜测配对关系",
                }
            )
            continue

        for evidence in _build_evidence_records(kb_id, triple_id, row, source=source, target=target):
            evidence_by_id.setdefault(evidence["evidence_id"], evidence)
            evidence_sources.append(
                {
                    "evidence_id": evidence["evidence_id"],
                    "row_number": row_number,
                    "raw_data": _without_internal_fields(row),
                }
            )

    _append_allele_of_triples(
        kb_id,
        semantic_splits,
        external_routes,
        triple_by_id,
        triple_sources,
    )
    _apply_triple_statistics(triple_by_id, evidence_by_id)

    duplicate_node_rows = sum(max(len(group) - 1, 0) for group in groups.values())
    duplicate_relation_rows = sum(max(count - 1, 0) for count in canonical_relation_counts.values())
    if duplicate_node_rows:
        info.append(
            {
                "code": "DUPLICATE_NODE_ALIASES",
                "severity": "INFO",
                "count": duplicate_node_rows,
                "message": "重复节点行已保留为来源记录，并合并到规范实体",
            }
        )
    if duplicate_relation_rows:
        info.append(
            {
                "code": "DUPLICATE_RELATION_SOURCES",
                "severity": "INFO",
                "count": duplicate_relation_rows,
                "message": "重复关系行已保留为来源/证据，规范三元组已完成去重",
            }
        )
    info.append(
        {
            "code": "SOURCE_STATISTICS_RECALCULATED",
            "severity": "INFO",
            "message": (
                "CSV degree、publication_count、support_count 与 literature_count 仅供审计；"
                "规范统计已从去重图谱和证据重算"
            ),
        }
    )
    if cypher_report.get("provided"):
        info.append(
            {
                "code": "CYPHER_AUDIT_ONLY",
                "severity": "INFO",
                "message": "Cypher 写语句已保存到审计区，执行权限保持关闭",
            }
        )

    entity_sources = []
    for rows in groups.values():
        for row in rows:
            entity = row_to_entity[row["_row_number"]]
            entity_sources.append(
                {
                    "entity_id": entity["entity_id"],
                    "external_id": row["node_id"].strip(),
                    "row_number": row["_row_number"],
                    "raw_data": _without_internal_fields(row),
                }
            )

    plan = {
        "entities": list(entities.values()),
        "entity_sources": entity_sources,
        "triples": list(triple_by_id.values()),
        "triple_sources": triple_sources,
        "evidence": list(evidence_by_id.values()),
        "evidence_sources": evidence_sources,
    }
    all_blockers = [*blockers, *unresolved]
    counts = {
        "node_rows": len(nodes),
        "relationship_rows": len(relationships),
        "canonical_entities": len(entities),
        "canonical_triples": len(triple_by_id),
        "evidence_assertions": len(evidence_by_id),
        "duplicate_node_rows": duplicate_node_rows,
        "duplicate_relationship_rows": duplicate_relation_rows,
        "duplicate_canonical_triples": 0,
        "semantic_splits": len(semantic_splits),
        "unresolved_conflicts": len(all_blockers),
        "blockers": len(all_blockers),
        "warnings": len(warnings),
    }
    result = _result(blockers, warnings, info, unresolved, semantic_splits, cypher_report, counts, plan)
    result["effective_resolutions"] = _effective_resolutions(semantic_splits, warnings, resolutions)
    return result


def _read_csv(
    data: bytes,
    required_headers: set[str],
    kind: str,
    errors: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        errors.append({"code": "INVALID_ENCODING", "file": kind, "message": f"必须使用 UTF-8 编码：{exc}"})
        return []
    reader = csv.DictReader(io.StringIO(text, newline=""))
    headers = {header.strip() for header in (reader.fieldnames or []) if header}
    missing = sorted(required_headers - headers)
    if missing:
        errors.append(
            {
                "code": "MISSING_HEADERS",
                "file": kind,
                "fields": missing,
                "message": f"{kind} CSV 缺少必需字段",
            }
        )
        return []

    rows = []
    for row_number, row in enumerate(reader, start=2):
        clean = {str(key).strip(): (value or "").strip() for key, value in row.items() if key is not None}
        clean["_row_number"] = row_number
        rows.append(clean)
    return rows


def _validate_identifier_columns(
    nodes: list[dict[str, Any]],
    relationships: list[dict[str, Any]],
    blockers: list[dict[str, Any]],
) -> None:
    """Reject lossy identifiers before canonical identities are calculated."""
    for row in nodes:
        for field in ("rap_id", "msu_id"):
            value = row.get(field)
            if identifier_has_scientific_notation(value):
                blockers.append(
                    {
                        "code": "INVALID_SCIENTIFIC_NOTATION",
                        "severity": "BLOCKER",
                        "file": "nodes",
                        "row_number": row["_row_number"],
                        "field": field,
                        "value": value,
                        "message": f"{field} 必须使用权威字符串，科学计数法可能已丢失有效位",
                    }
                )

    for row in relationships:
        for pmid in _split_pipe(row.get("pmids") or ""):
            _value, status = validate_pmid(pmid)
            if status != "VALID":
                blockers.append(
                    {
                        "code": status,
                        "severity": "BLOCKER",
                        "file": "relationships",
                        "row_number": row["_row_number"],
                        "field": "pmids",
                        "value": pmid,
                        "message": "PMID 必须是 6–10 位数字字符串；系统不会从科学计数法猜测原值",
                    }
                )
        for doi in _split_pipe(row.get("dois") or ""):
            _value, status = validate_doi(doi)
            if status != "VALID":
                blockers.append(
                    {
                        "code": f"DOI_{status}",
                        "severity": "BLOCKER",
                        "file": "relationships",
                        "row_number": row["_row_number"],
                        "field": "dois",
                        "value": doi,
                        "message": "DOI 必须是以 10. 开头的完整字符串",
                    }
                )


def _group_nodes(nodes: list[dict[str, Any]]) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
    parent: dict[str, str] = {}

    def find(item: str) -> str:
        parent.setdefault(item, item)
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    registry_owner: dict[str, str] = {}
    for row in nodes:
        external_id = row["node_id"].strip()
        if not external_id:
            external_id = f"__missing_row_{row['_row_number']}"
        find(external_id)
        for field in ("rap_id", "msu_id"):
            registry_id = row[field].strip().casefold()
            if not registry_id:
                continue
            key = f"{field}:{registry_id}"
            owner = registry_owner.setdefault(key, external_id)
            union(external_id, owner)

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    external_to_group: dict[str, str] = {}
    for row in nodes:
        external_id = row["node_id"].strip() or f"__missing_row_{row['_row_number']}"
        group_id = find(external_id)
        groups[group_id].append(row)
        if row["node_id"].strip():
            external_to_group[row["node_id"].strip()] = group_id
    return dict(groups), external_to_group


def _find_node_issues(
    groups: dict[str, list[dict[str, Any]]],
    relationships: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    conflicts = []
    semantic_splits = []
    for group_id, rows in groups.items():
        required_missing = [
            row["_row_number"]
            for row in rows
            if not row["node_id"].strip() or not row["name"].strip() or not row["node_type"].strip()
        ]
        unsupported_rows = [
            row["_row_number"] for row in rows if row["node_type"].strip().upper() not in NODE_TYPE_MAPPING
        ]
        mapped_types = {_map_node_type(row["node_type"])[0] for row in rows if row["node_type"].strip()}
        names = {row["name"].strip() for row in rows if row["name"].strip()}
        folded_names = {name.casefold() for name in names}
        has_registry = any(row["rap_id"].strip() or row["msu_id"].strip() for row in rows)

        code = None
        message = None
        if required_missing:
            code, message = "NODE_REQUIRED_FIELD", "节点行缺少 node_id、name 或 node_type"
        elif unsupported_rows:
            code, message = "UNSUPPORTED_NODE_TYPE", "节点类型不在当前水稻胚乳规范类型表中"
        elif mapped_types == {"Gene", "AlleleMutant"}:
            split_id = hashstr(
                f"{group_id}:GENE_ALLELE_SPLIT:{','.join(str(row['_row_number']) for row in rows)}",
                length=24,
            )
            external_ids = sorted({row["node_id"].strip() for row in rows if row["node_id"].strip()})
            route_rows = []
            for relationship in relationships:
                endpoints = {}
                if relationship["start_id"].strip() in external_ids:
                    endpoints["start"] = _suggest_semantic_role(relationship["relation_type"])
                if relationship["end_id"].strip() in external_ids:
                    endpoints["end"] = _suggest_semantic_role(relationship["relation_type"])
                if endpoints:
                    route_rows.append(
                        {
                            "row_number": relationship["_row_number"],
                            "relation_type": relationship["relation_type"].strip().upper(),
                            "start_id": relationship["start_id"].strip(),
                            "end_id": relationship["end_id"].strip(),
                            "endpoints": endpoints,
                            "reason": _routing_reason(relationship["relation_type"]),
                        }
                    )
            semantic_splits.append(
                {
                    "split_id": split_id,
                    "group_id": group_id,
                    "code": "GENE_ALLELE_IDENTITY_SPLIT",
                    "severity": "INFO",
                    "message": "该外部 ID 将安全拆分为 Gene 与 AlleleMutant，并建立 ALLELE_OF",
                    "external_ids": external_ids,
                    "options": [
                        {
                            "row_number": row["_row_number"],
                            "role": "allele" if _map_node_type(row["node_type"])[0] == "AlleleMutant" else "gene",
                            "name": row["name"],
                            "node_type": row["node_type"],
                            "rap_id": row["rap_id"],
                            "msu_id": row["msu_id"],
                        }
                        for row in rows
                    ],
                    "relation_routes": route_rows,
                    "perturbation_policy": "KNOCKOUT/CRISPR/RNAI/OVEREXPRESSION 当前路由至 Gene，并保留关系类型",
                }
            )
            if any(route["relation_type"] in PERTURBATION_RELATION_TYPES for route in route_rows):
                warnings.append(
                    {
                        "code": "PERTURBATION_MODEL_SIMPLIFIED",
                        "severity": "WARNING",
                        "external_ids": external_ids,
                        "message": "扰动效应关系当前采用 Gene 端点的简化模型；关系类型和证据完整保留",
                    }
                )
        elif len(mapped_types) > 1:
            code, message = (
                "SEMANTIC_IDENTITY_COLLISION",
                "同一规范身份包含不兼容的生物学类型，无法安全自动拆分",
            )
        elif len(names) > 1 and len(folded_names) == 1 and not has_registry:
            review_id = hashstr(
                f"{group_id}:CASE_UNRESOLVED:{','.join(str(row['_row_number']) for row in rows)}",
                length=24,
            )
            warnings.append(
                {
                    "code": "CASE_UNRESOLVED",
                    "severity": "WARNING",
                    "review_id": review_id,
                    "group_id": group_id,
                    "external_ids": sorted({row["node_id"].strip() for row in rows if row["node_id"].strip()}),
                    "row_numbers": sorted(row["_row_number"] for row in rows),
                    "aliases": sorted(names),
                    "options": [
                        {
                            "row_number": row["_row_number"],
                            "name": row["name"],
                            "node_type": row["node_type"],
                        }
                        for row in rows
                    ],
                    "message": "仅大小写不同且缺少官方 ID；已按规范名称合并并完整保留别名",
                }
            )
        elif len(folded_names) > 1 and not has_registry:
            code, message = "NAME_CONFLICT", "同一外部 ID 对应多个不同名称"

        if code:
            conflict_id = hashstr(f"{group_id}:{code}:{','.join(str(row['_row_number']) for row in rows)}", length=24)
            conflicts.append(
                {
                    "conflict_id": conflict_id,
                    "code": code,
                    "severity": "BLOCKER",
                    "resolvable": code == "NAME_CONFLICT",
                    "message": message,
                    "external_ids": sorted({row["node_id"].strip() for row in rows if row["node_id"].strip()}),
                    "options": [
                        {
                            "row_number": row["_row_number"],
                            "name": row["name"],
                            "node_type": row["node_type"],
                            "rap_id": row["rap_id"],
                            "msu_id": row["msu_id"],
                        }
                        for row in rows
                    ],
                }
            )
    return conflicts, semantic_splits


def _resolve_groups(
    groups: dict[str, list[dict[str, Any]]],
    conflicts: list[dict[str, Any]],
    resolutions: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    conflict_by_row = {option["row_number"]: conflict for conflict in conflicts for option in conflict["options"]}
    chosen: dict[str, dict[str, Any]] = {}
    unresolved: list[dict[str, Any]] = []
    for group_id, rows in groups.items():
        conflict = next(
            (conflict_by_row.get(row["_row_number"]) for row in rows if row["_row_number"] in conflict_by_row), None
        )
        if not conflict:
            review_id = hashstr(
                f"{group_id}:CASE_UNRESOLVED:{','.join(str(row['_row_number']) for row in rows)}",
                length=24,
            )
            case_resolution = resolutions.get(review_id)
            selected_row_number = (
                case_resolution.get("selected_row_number") if isinstance(case_resolution, dict) else case_resolution
            )
            chosen[group_id] = next(
                (row for row in rows if row["_row_number"] == selected_row_number),
                _preferred_row(rows),
            )
            continue
        if not conflict.get("resolvable", True):
            unresolved.append(conflict)
            chosen[group_id] = _preferred_row(rows)
            continue
        resolution = resolutions.get(conflict["conflict_id"])
        selected_row_number = resolution.get("selected_row_number") if isinstance(resolution, dict) else resolution
        selected = next((row for row in rows if row["_row_number"] == selected_row_number), None)
        if selected is None:
            unresolved.append(conflict)
            chosen[group_id] = _preferred_row(rows)
        else:
            chosen[group_id] = selected
    return chosen, unresolved


def _preferred_row(rows: list[dict[str, Any]]) -> dict[str, Any]:
    priority = {"RICE_GENE": 0, "GENE": 1, "RICE_GENE_CANDIDATE": 2}
    return min(rows, key=lambda row: (priority.get(row["node_type"].upper(), 10), row["_row_number"]))


def _map_node_type(node_type: str) -> tuple[str, str | None]:
    normalized = node_type.strip().upper()
    if normalized in NODE_TYPE_MAPPING:
        return NODE_TYPE_MAPPING[normalized]
    safe_label = re.sub(r"[^A-Za-z0-9]+", " ", node_type).title().replace(" ", "") or "Entity"
    return safe_label, None


def _build_entities(
    kb_id: str,
    groups: dict[str, list[dict[str, Any]]],
    chosen_rows: dict[str, dict[str, Any]],
    semantic_splits: list[dict[str, Any]],
    resolutions: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[int, dict[str, Any]]]:
    entities: dict[str, dict[str, Any]] = {}
    external_routes: dict[str, dict[str, Any]] = {}
    row_to_entity: dict[int, dict[str, Any]] = {}
    split_by_group = {item["group_id"]: item for item in semantic_splits}
    for group_id, rows in groups.items():
        chosen = chosen_rows.get(group_id)
        if not chosen:
            continue
        split = split_by_group.get(group_id)
        if split:
            split_resolution = resolutions.get(split["split_id"], {})
            split_resolution = split_resolution if isinstance(split_resolution, dict) else {}
            rows_by_role = {
                "gene": [row for row in rows if _map_node_type(row["node_type"])[0] == "Gene"],
                "allele": [row for row in rows if _map_node_type(row["node_type"])[0] == "AlleleMutant"],
            }
            gene_rows = rows_by_role["gene"]
            allele_rows = rows_by_role["allele"]
            gene = _build_entity(
                kb_id,
                _preferred_row(gene_rows),
                gene_rows,
                name_override=split_resolution.get("gene_name"),
            )
            allele = _build_entity(
                kb_id,
                _preferred_row(allele_rows),
                allele_rows,
                parent_identity=gene["canonical_identity"],
                name_override=split_resolution.get("allele_name"),
            )
            entities[gene["entity_id"]] = gene
            entities[allele["entity_id"]] = allele
            split["preview"] = {
                "gene": _entity_preview(gene),
                "allele": _entity_preview(allele),
                "allele_of": f"{allele['name']} → ALLELE_OF → {gene['name']}",
            }
            for row in rows:
                role = "allele" if _map_node_type(row["node_type"])[0] == "AlleleMutant" else "gene"
                row_to_entity[row["_row_number"]] = allele if role == "allele" else gene
            for external_id in split["external_ids"]:
                external_routes[external_id] = {
                    "default": gene,
                    "gene": gene,
                    "allele": allele,
                    "split_id": split["split_id"],
                }
            continue

        case_unresolved = (
            len({row["name"].strip() for row in rows}) > 1
            and len({row["name"].strip().casefold() for row in rows}) == 1
            and not any(row["rap_id"].strip() or row["msu_id"].strip() for row in rows)
        )
        entity = _build_entity(kb_id, chosen, rows, case_unresolved=case_unresolved)
        entities[entity["entity_id"]] = entity
        for row in rows:
            row_to_entity[row["_row_number"]] = entity
            if row["node_id"].strip():
                external_routes[row["node_id"].strip()] = {"default": entity}
    return entities, external_routes, row_to_entity


def _build_entity(
    kb_id: str,
    chosen: dict[str, Any],
    source_rows: list[dict[str, Any]],
    *,
    parent_identity: str | None = None,
    name_override: str | None = None,
    case_unresolved: bool = False,
) -> dict[str, Any]:
    label, selected_status = _map_node_type(chosen["node_type"])
    name = (name_override or chosen["name"]).strip()
    normalized_name = normalize_entity_name(name)
    canonical_identity = _canonical_identity(label, name, source_rows, parent_identity)
    entity_id = compute_entity_id(kb_id, canonical_identity, label)
    statuses = {status for _, status in (_map_node_type(row["node_type"]) for row in source_rows) if status is not None}
    gene_status = "confirmed" if "confirmed" in statuses else selected_status
    attributes = {
        "source": "managed_csv_import",
        "external_ids": sorted({row["node_id"].strip() for row in source_rows if row["node_id"].strip()}),
        "source_node_types": sorted({row["node_type"].strip() for row in source_rows}),
        "aliases": sorted({row["name"].strip() for row in source_rows if row["name"].strip()}),
        "rap_ids": sorted({row["rap_id"].strip() for row in source_rows if row["rap_id"].strip()}),
        "msu_ids": sorted({row["msu_id"].strip() for row in source_rows if row["msu_id"].strip()}),
        "canonical_identity": canonical_identity,
    }
    if gene_status:
        attributes["gene_status"] = gene_status
    if case_unresolved:
        attributes["normalization_status"] = "CASE_UNRESOLVED"
    if parent_identity:
        attributes["parent_gene_identity"] = parent_identity
    return {
        "entity_id": entity_id,
        "kb_id": kb_id,
        "canonical_identity": canonical_identity,
        "normalized_name": normalized_name,
        "label": label,
        "name": name,
        "attributes": attributes,
        "content": " ".join(part for part in [name, label, *attributes["rap_ids"], *attributes["msu_ids"]] if part),
    }


def _canonical_identity(
    label: str,
    name: str,
    source_rows: list[dict[str, Any]],
    parent_identity: str | None,
) -> str:
    rap_ids = sorted({row["rap_id"].strip().casefold() for row in source_rows if row["rap_id"].strip()})
    msu_ids = sorted({row["msu_id"].strip().casefold() for row in source_rows if row["msu_id"].strip()})
    if label == "Gene":
        if rap_ids:
            return f"rap:{rap_ids[0]}"
        if msu_ids:
            return f"msu:{msu_ids[0]}"
        return f"gene-name:{normalize_entity_name(name)}"
    if label == "AlleleMutant":
        parent = parent_identity or "gene-unresolved"
        return f"allele:{parent}:{normalize_entity_name(name)}"
    return f"name:{normalize_entity_name(name)}"


def _entity_preview(entity: dict[str, Any]) -> dict[str, Any]:
    return {
        "entity_id": entity["entity_id"],
        "canonical_identity": entity["canonical_identity"],
        "label": entity["label"],
        "name": entity["name"],
        "gene_status": entity["attributes"].get("gene_status"),
    }


def _suggest_semantic_role(relation_type: str) -> str:
    normalized = relation_type.strip().upper()
    if normalized in ALLELE_RELATION_TYPES:
        return "allele"
    return "gene"


def _routing_reason(relation_type: str) -> str:
    normalized = relation_type.strip().upper()
    if normalized in ALLELE_RELATION_TYPES:
        return "突变体效应关系默认路由至 AlleleMutant"
    if normalized in PERTURBATION_RELATION_TYPES:
        return "扰动关系采用简化模型路由至 Gene，证据与原关系类型保留"
    if normalized in GENE_RELATION_TYPES:
        return "调控、表达、结合或过程关系默认路由至 Gene"
    return "未知关系语义保守路由至 Gene，可在预检中逐行覆盖"


def _route_endpoint(
    routes: dict[str, Any],
    row: dict[str, Any],
    endpoint: str,
    semantic_splits: list[dict[str, Any]],
    resolutions: dict[str, Any],
) -> dict[str, Any]:
    split_id = routes.get("split_id")
    if not split_id:
        return routes["default"]
    split = next(item for item in semantic_splits if item["split_id"] == split_id)
    suggested_role = next(
        (
            route["endpoints"].get(endpoint)
            for route in split["relation_routes"]
            if route["row_number"] == row["_row_number"] and route["endpoints"].get(endpoint)
        ),
        "gene",
    )
    resolution = resolutions.get(split_id, {})
    resolution = resolution if isinstance(resolution, dict) else {}
    relation_resolution = (resolution.get("relation_routes") or {}).get(str(row["_row_number"]), {})
    if not relation_resolution:
        relation_resolution = (resolution.get("relation_routes") or {}).get(row["_row_number"], {})
    if isinstance(relation_resolution, str):
        selected_role = relation_resolution
    else:
        selected_role = relation_resolution.get(endpoint, suggested_role)
    return routes.get(selected_role, routes[suggested_role])


def _triple_id(
    kb_id: str,
    source: dict[str, Any],
    relation_type: str,
    target: dict[str, Any],
) -> str:
    return compute_triple_id(
        kb_id,
        source["canonical_identity"],
        source["label"],
        relation_type,
        target["canonical_identity"],
        target["label"],
    )


def _triple(
    kb_id: str,
    triple_id: str,
    source: dict[str, Any],
    relation_type: str,
    target: dict[str, Any],
) -> dict[str, Any]:
    return {
        "triple_id": triple_id,
        "kb_id": kb_id,
        "source_entity_id": source["entity_id"],
        "target_entity_id": target["entity_id"],
        "relation_type": relation_type,
        "content": f"{source['name']} → {relation_type} → {target['name']}",
        "support_count": 0,
        "literature_count": 0,
        "best_evidence_level": None,
        "consensus_direction": "UNKNOWN",
    }


def _append_allele_of_triples(
    kb_id: str,
    semantic_splits: list[dict[str, Any]],
    external_routes: dict[str, dict[str, Any]],
    triple_by_id: dict[str, dict[str, Any]],
    triple_sources: list[dict[str, Any]],
) -> None:
    for index, split in enumerate(semantic_splits, start=1):
        routes = external_routes[split["external_ids"][0]]
        allele, gene = routes["allele"], routes["gene"]
        triple_id = _triple_id(kb_id, allele, "ALLELE_OF", gene)
        triple_by_id.setdefault(triple_id, _triple(kb_id, triple_id, allele, "ALLELE_OF", gene))
        triple_sources.append(
            {
                "triple_id": triple_id,
                "source_id": f"semantic-split:{split['split_id']}",
                "row_number": -index,
                "source_type": "normalizer",
                "raw_data": {
                    "rule": "GENE_ALLELE_IDENTITY_SPLIT",
                    "split_id": split["split_id"],
                    "external_ids": split["external_ids"],
                },
            }
        )


def _apply_triple_statistics(
    triples: dict[str, dict[str, Any]],
    evidence: dict[str, dict[str, Any]],
) -> None:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in evidence.values():
        grouped[item["triple_id"]].append(item)
    for triple_id, triple in triples.items():
        items = grouped.get(triple_id, [])
        aligned = [item for item in items if item["evidence_alignment_status"] == "ALIGNED"]
        literature_ids = {identity for item in aligned for identity in _exact_literature_keys(item)}
        levels = [item["evidence_level"] for item in items if item.get("evidence_level")]
        directions = {item["direction"] for item in items if item.get("direction") not in {None, "", "UNKNOWN"}}
        triple.update(
            {
                "support_count": len({item["evidence_id"] for item in items}),
                "literature_count": len(literature_ids),
                "best_evidence_level": min(levels, key=_evidence_level_rank) if levels else None,
                "consensus_direction": (
                    next(iter(directions)) if len(directions) == 1 else "CONFLICTED" if directions else "UNKNOWN"
                ),
            }
        )


def _evidence_level_rank(value: str) -> tuple[int, str]:
    digits = "".join(character for character in value if character.isdigit())
    return (int(digits) if digits else 999, value)


def _exact_literature_keys(evidence: dict[str, Any]) -> list[str]:
    metadata = evidence.get("metadata_json") or {}
    pmids = metadata.get("pmids") or []
    dois = metadata.get("dois") or []
    if pmids and dois and len(pmids) == len(dois):
        return [f"pmid:{pmid}|doi:{doi}" for pmid, doi in zip(pmids, dois, strict=True)]
    if pmids:
        return [f"pmid:{pmid}" for pmid in pmids]
    return [f"doi:{doi}" for doi in dois]


def _effective_resolutions(
    semantic_splits: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    resolutions: dict[str, Any],
) -> dict[str, Any]:
    effective = {key: dict(value) if isinstance(value, dict) else value for key, value in resolutions.items()}
    for split in semantic_splits:
        saved = effective.get(split["split_id"], {})
        saved = saved if isinstance(saved, dict) else {}
        saved_routes = saved.get("relation_routes") or {}
        relation_routes = {}
        for route in split["relation_routes"]:
            row_key = str(route["row_number"])
            relation_routes[row_key] = {
                **route["endpoints"],
                **(saved_routes.get(row_key) or saved_routes.get(route["row_number"]) or {}),
            }
        effective[split["split_id"]] = {
            "action": "split",
            "gene_name": saved.get("gene_name") or split["preview"]["gene"]["name"],
            "allele_name": saved.get("allele_name") or split["preview"]["allele"]["name"],
            "relation_routes": relation_routes,
        }
    priority = {"RICE_GENE": 0, "GENE": 1, "RICE_GENE_CANDIDATE": 2}
    for warning in warnings:
        if warning.get("code") != "CASE_UNRESOLVED" or not warning.get("review_id"):
            continue
        saved = effective.get(warning["review_id"], {})
        selected = saved.get("selected_row_number") if isinstance(saved, dict) else saved
        if not selected:
            selected = min(
                warning["options"],
                key=lambda option: (priority.get(option["node_type"].upper(), 10), option["row_number"]),
            )["row_number"]
        effective[warning["review_id"]] = {"selected_row_number": selected}
    return effective


def _build_evidence_records(
    kb_id: str,
    triple_id: str,
    row: dict[str, Any],
    *,
    source: dict[str, Any],
    target: dict[str, Any],
) -> list[dict[str, Any]]:
    pmids = _split_pipe(row["pmids"])
    dois = _split_pipe(row["dois"])
    quotes = _split_quotes(row["evidence_quotes"])
    width = max(len(pmids), len(dois), len(quotes), 1)
    records = []
    for index in range(width):
        pmid = pmids[index] if pmids else None
        doi = dois[index] if dois else None
        quote = quotes[index] if quotes else None
        literature_keys = [key for key in (f"pmid:{pmid}" if pmid else None, f"doi:{doi}" if doi else None) if key]
        literature_id = "|".join(literature_keys) or None
        identifier_status = "VALID" if literature_keys else "MISSING"
        semantics = build_evidence_semantics(
            source_name=source.get("name"),
            source_label=source.get("label"),
            relation_type=row["relation_type"],
            target_name=target.get("name"),
            quote=quote,
            direction=row["direction"],
        )
        identity = "|".join(
            [
                kb_id,
                triple_id,
                literature_id or "",
                row["direction"].strip().upper(),
                row["directness"].strip().upper(),
                row["best_evidence_level"].strip().upper(),
                quote or "",
            ]
        )
        records.append(
            {
                "evidence_id": hashstr(identity, length=32),
                "triple_id": triple_id,
                "kb_id": kb_id,
                "literature_id": literature_id,
                "pmid": pmid,
                "doi": doi,
                "identifier_status": identifier_status,
                "direction": row["direction"].strip().upper() or "UNKNOWN",
                "directness": row["directness"].strip().upper() or "UNKNOWN",
                "assertion_status": "ASSERTED",
                "evidence_level": row["best_evidence_level"].strip().upper() or None,
                "evidence_quote": quote,
                "evidence_methods": [],
                "source_scope": "relation_row",
                "evidence_alignment_status": "ALIGNED",
                "sentence_id": f"relationships:{row['_row_number']}:evidence:{index + 1}",
                "claim_eligible": bool(literature_keys and quote),
                **semantics,
                "metadata_json": {
                    "pmids": [pmid] if pmid else [],
                    "dois": [doi] if doi else [],
                    "quotes": [quote] if quote else [],
                    "declared_support_count": _parse_optional_int(row["support_count"]),
                    "declared_literature_count": _parse_optional_int(row["literature_count"]),
                    "source_row_number": row["_row_number"],
                },
            }
        )
    return records


def _evidence_alignment_status(row: dict[str, Any]) -> str:
    counts = [
        len(items)
        for items in (_split_pipe(row["pmids"]), _split_pipe(row["dois"]), _split_quotes(row["evidence_quotes"]))
        if items
    ]
    return "AMBIGUOUS" if len(set(counts)) > 1 else "ALIGNED"


def _inspect_cypher(data: bytes | None, errors: list[dict[str, Any]]) -> dict[str, Any]:
    if data is None:
        return {"provided": False, "execution_allowed": False, "statement_count": 0, "write_keywords": []}
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        errors.append({"code": "INVALID_ENCODING", "file": "cypher", "message": f"必须使用 UTF-8 编码：{exc}"})
        return {"provided": True, "execution_allowed": False, "statement_count": 0, "write_keywords": []}
    statements = [statement.strip() for statement in text.split(";") if statement.strip()]
    keywords = sorted({match.group(0).upper() for match in _DANGEROUS_CYPHER.finditer(text)})
    return {
        "provided": True,
        "execution_allowed": False,
        "statement_count": len(statements),
        "write_keywords": keywords,
        "message": "Cypher 仅保存和审计，服务端不会执行其中任何语句",
    }


def _result(
    blockers: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    info: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
    semantic_splits: list[dict[str, Any]],
    cypher_report: dict[str, Any],
    counts: dict[str, Any],
    plan: dict[str, Any] | None,
) -> dict[str, Any]:
    all_blockers = [*blockers, *conflicts]
    has_hard_blocker = bool(blockers) or any(not item.get("resolvable", True) for item in conflicts)
    return {
        "valid": not all_blockers,
        "status": "READY"
        if not all_blockers
        else "AWAITING_CONFLICT_RESOLUTION"
        if conflicts and not has_hard_blocker
        else "INVALID",
        "counts": counts,
        "blockers": all_blockers,
        "errors": all_blockers,
        "warnings": warnings,
        "info": info,
        "conflicts": conflicts,
        "semantic_splits": semantic_splits,
        "cypher": cypher_report,
        "schema_version": SCHEMA_VERSION,
        "normalizer_version": NORMALIZER_VERSION,
        "plan": plan,
    }


def _split_pipe(value: str) -> list[str]:
    return [item.strip() for item in value.split("|") if item.strip()]


def _split_quotes(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"\s*\|\|\s*", value) if item.strip()]


def _parse_optional_int(value: str) -> int | None:
    try:
        return int(value) if value.strip() else None
    except ValueError:
        return None


def _without_internal_fields(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if not key.startswith("_")}
