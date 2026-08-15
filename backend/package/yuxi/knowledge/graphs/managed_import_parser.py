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
from yuxi.utils import hashstr

SCHEMA_VERSION = "rice-endosperm-csv-v1"
NORMALIZER_VERSION = "managed-graph-v1"

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
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    nodes = _read_csv(nodes_bytes, NODE_HEADERS, "nodes", errors)
    relationships = _read_csv(relationships_bytes, RELATIONSHIP_HEADERS, "relationships", errors)
    cypher_report = _inspect_cypher(cypher_bytes, errors)

    if errors:
        return _result(errors, warnings, [], cypher_report, {}, None)

    groups, _ = _group_nodes(nodes)
    conflicts = _find_node_conflicts(groups)
    chosen_rows, unresolved = _resolve_groups(groups, conflicts, resolutions or {})
    entities, external_to_entity, row_to_entity = _build_entities(kb_id, groups, chosen_rows)

    known_external_ids = set(external_to_entity)
    duplicate_relation_keys = Counter()
    triple_by_id: dict[str, dict[str, Any]] = {}
    triple_sources: list[dict[str, Any]] = []
    evidence_by_id: dict[str, dict[str, Any]] = {}
    evidence_sources: list[dict[str, Any]] = []

    for row in relationships:
        row_number = row["_row_number"]
        start_id = row["start_id"].strip()
        end_id = row["end_id"].strip()
        relation_type = row["relation_type"].strip()
        missing = [item for item in (start_id, end_id) if item not in known_external_ids]
        if not start_id or not end_id or not relation_type:
            errors.append(
                {
                    "code": "RELATION_REQUIRED_FIELD",
                    "row_number": row_number,
                    "message": "关系行缺少 start_id、end_id 或 relation_type",
                }
            )
            continue
        if missing:
            errors.append(
                {
                    "code": "DANGLING_RELATION",
                    "row_number": row_number,
                    "external_ids": missing,
                    "message": "关系端点未在节点 CSV 中定义",
                }
            )
            continue

        source = external_to_entity[start_id]
        target = external_to_entity[end_id]
        triple_id = compute_triple_id(
            kb_id,
            source["normalized_name"],
            source["label"],
            relation_type,
            target["normalized_name"],
            target["label"],
        )
        duplicate_relation_keys[(start_id, end_id, relation_type)] += 1
        triple_by_id.setdefault(
            triple_id,
            {
                "triple_id": triple_id,
                "kb_id": kb_id,
                "source_entity_id": source["entity_id"],
                "target_entity_id": target["entity_id"],
                "relation_type": relation_type,
                "content": f"{source['name']} → {relation_type} → {target['name']}",
            },
        )
        triple_sources.append(
            {
                "triple_id": triple_id,
                "source_id": f"relationships:{row_number}",
                "row_number": row_number,
                "raw_data": _without_internal_fields(row),
            }
        )

        evidence = _build_evidence(kb_id, triple_id, row)
        evidence_by_id.setdefault(evidence["evidence_id"], evidence)
        evidence_sources.append(
            {
                "evidence_id": evidence["evidence_id"],
                "row_number": row_number,
                "raw_data": _without_internal_fields(row),
            }
        )
        _append_evidence_alignment_warning(row, warnings)

    duplicate_node_rows = sum(max(len(group) - 1, 0) for group in groups.values())
    duplicate_relation_rows = sum(max(count - 1, 0) for count in duplicate_relation_keys.values())
    if duplicate_node_rows:
        warnings.append(
            {
                "code": "DUPLICATE_NODE_ROWS",
                "count": duplicate_node_rows,
                "message": "重复节点行将保留为来源记录，并合并到规范实体",
            }
        )
    if duplicate_relation_rows:
        warnings.append(
            {
                "code": "DUPLICATE_RELATION_ROWS",
                "count": duplicate_relation_rows,
                "message": "重复关系行将保留为独立来源/证据，并合并到规范三元组",
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
    counts = {
        "node_rows": len(nodes),
        "relationship_rows": len(relationships),
        "canonical_entities": len(entities),
        "canonical_triples": len(triple_by_id),
        "evidence_assertions": len(evidence_by_id),
        "duplicate_node_rows": duplicate_node_rows,
        "duplicate_relationship_rows": duplicate_relation_rows,
        "unresolved_conflicts": len(unresolved),
    }
    return _result(errors, warnings, unresolved, cypher_report, counts, plan)


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


def _find_node_conflicts(groups: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    conflicts = []
    for group_id, rows in groups.items():
        required_missing = [
            row["_row_number"]
            for row in rows
            if not row["node_id"].strip() or not row["name"].strip() or not row["node_type"].strip()
        ]
        mapped_types = {_map_node_type(row["node_type"])[0] for row in rows}
        names = {row["name"].strip() for row in rows if row["name"].strip()}
        folded_names = {name.casefold() for name in names}
        has_registry = any(row["rap_id"].strip() or row["msu_id"].strip() for row in rows)

        code = None
        message = None
        if required_missing:
            code, message = "NODE_REQUIRED_FIELD", "节点行缺少 node_id、name 或 node_type"
        elif len(mapped_types) > 1:
            code, message = (
                "SEMANTIC_TYPE_CONFLICT",
                "不同规范类型将分别保留；请选择关系端点默认指向的记录",
            )
        elif len(names) > 1 and len(folded_names) == 1 and not has_registry:
            code, message = "CASE_VARIANT_CONFLICT", "缺少官方注册标识，大小写差异不能自动合并"
        elif len(folded_names) > 1 and not has_registry:
            code, message = "NAME_CONFLICT", "同一外部 ID 对应多个不同名称"

        if code:
            conflict_id = hashstr(f"{group_id}:{code}:{','.join(str(row['_row_number']) for row in rows)}", length=24)
            conflicts.append(
                {
                    "conflict_id": conflict_id,
                    "code": code,
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
    return conflicts


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
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[int, dict[str, Any]]]:
    entities: dict[str, dict[str, Any]] = {}
    external_to_entity: dict[str, dict[str, Any]] = {}
    row_to_entity: dict[int, dict[str, Any]] = {}
    for group_id, rows in groups.items():
        chosen = chosen_rows.get(group_id)
        if not chosen:
            continue
        semantic_split = len({_map_node_type(row["node_type"])[0] for row in rows}) > 1
        primary_entity = _build_entity(kb_id, chosen, [chosen] if semantic_split else rows)
        entities[primary_entity["entity_id"]] = primary_entity
        for row in rows:
            entity = _build_entity(kb_id, row, [row]) if semantic_split else primary_entity
            entities.setdefault(entity["entity_id"], entity)
            row_to_entity[row["_row_number"]] = entity
            if row["node_id"].strip():
                external_to_entity[row["node_id"].strip()] = primary_entity
    return entities, external_to_entity, row_to_entity


def _build_entity(kb_id: str, chosen: dict[str, Any], source_rows: list[dict[str, Any]]) -> dict[str, Any]:
    label, selected_status = _map_node_type(chosen["node_type"])
    normalized_name = normalize_entity_name(chosen["name"])
    entity_id = compute_entity_id(kb_id, normalized_name, label)
    statuses = {status for _, status in (_map_node_type(row["node_type"]) for row in source_rows) if status is not None}
    gene_status = "confirmed" if "confirmed" in statuses else selected_status
    attributes = {
        "source": "managed_csv_import",
        "external_ids": sorted({row["node_id"].strip() for row in source_rows if row["node_id"].strip()}),
        "source_node_types": sorted({row["node_type"].strip() for row in source_rows}),
        "aliases": sorted({row["name"].strip() for row in source_rows if row["name"].strip()}),
        "rap_ids": sorted({row["rap_id"].strip() for row in source_rows if row["rap_id"].strip()}),
        "msu_ids": sorted({row["msu_id"].strip() for row in source_rows if row["msu_id"].strip()}),
    }
    if gene_status:
        attributes["gene_status"] = gene_status
    return {
        "entity_id": entity_id,
        "kb_id": kb_id,
        "normalized_name": normalized_name,
        "label": label,
        "name": chosen["name"].strip(),
        "attributes": attributes,
        "content": " ".join(
            part for part in [chosen["name"].strip(), label, *attributes["rap_ids"], *attributes["msu_ids"]] if part
        ),
    }


def _build_evidence(kb_id: str, triple_id: str, row: dict[str, Any]) -> dict[str, Any]:
    pmids = _split_pipe(row["pmids"])
    dois = _split_pipe(row["dois"])
    quotes = _split_quotes(row["evidence_quotes"])
    literature_ids = sorted({*[f"pmid:{item}" for item in pmids], *[f"doi:{item}" for item in dois]})
    literature_id = literature_ids[0] if literature_ids else None
    identity = "|".join(
        [
            kb_id,
            triple_id,
            *literature_ids,
            row["direction"].strip().upper(),
            row["directness"].strip().upper(),
            row["best_evidence_level"].strip().upper(),
            "||".join(quotes),
        ]
    )
    return {
        "evidence_id": hashstr(identity, length=32),
        "triple_id": triple_id,
        "kb_id": kb_id,
        "literature_id": literature_id,
        "pmid": pmids[0] if len(pmids) == 1 else None,
        "doi": dois[0] if len(dois) == 1 else None,
        "direction": row["direction"].strip().upper() or "UNKNOWN",
        "directness": row["directness"].strip().upper() or "UNKNOWN",
        "evidence_level": row["best_evidence_level"].strip().upper() or None,
        "evidence_quote": " || ".join(quotes) or None,
        "evidence_methods": [],
        "source_scope": "relation_row",
        "metadata_json": {
            "pmids": pmids,
            "dois": dois,
            "quotes": quotes,
            "declared_support_count": _parse_optional_int(row["support_count"]),
            "declared_literature_count": _parse_optional_int(row["literature_count"]),
        },
    }


def _append_evidence_alignment_warning(row: dict[str, Any], warnings: list[dict[str, Any]]) -> None:
    counts = [
        len(items)
        for items in (_split_pipe(row["pmids"]), _split_pipe(row["dois"]), _split_quotes(row["evidence_quotes"]))
        if items
    ]
    if len(set(counts)) > 1:
        warnings.append(
            {
                "code": "EVIDENCE_ALIGNMENT_AMBIGUOUS",
                "row_number": row["_row_number"],
                "message": "PMID、DOI 与证据引文数量不一致；已按整行证据原样保存，未猜测一一对应关系",
            }
        )


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
    errors: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
    cypher_report: dict[str, Any],
    counts: dict[str, Any],
    plan: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "valid": not errors and not conflicts,
        "status": "READY"
        if not errors and not conflicts
        else "AWAITING_CONFLICT_RESOLUTION"
        if conflicts and not errors
        else "INVALID",
        "counts": counts,
        "errors": errors,
        "warnings": warnings,
        "conflicts": conflicts,
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
