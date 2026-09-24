"""Deterministic export of a published conversation graph snapshot.

The export replays the **published** payload (``message.extra_metadata.graph_snapshot``)
byte-for-byte deterministically: no wall-clock fields enter the output, so the same
run always exports identical bytes and ``projection_hash`` doubles as a valid ETag.
The audit row (``knowledge_retrieval_runs.graph_snapshot_json``) is never an export
source — dark-launch data must not leak through downloads (ADR-0004 §7 discipline).

Formats follow the established roundtrip conventions of ``graph_export_service``:
CSV files use utf-8-sig (Excel-safe) with standard quoting, and the ``csv`` variant
is a zip containing ``nodes.csv`` / ``edges.csv`` / ``manifest.json`` where the
manifest carries the projection hash for independent verification.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from typing import Any


from yuxi.knowledge.contracts.graph_snapshot_projection import GRAPH_SNAPSHOT_SCHEMA

EXPORT_SCHEMA = "graph_snapshot_export_v1"
_ASSERTION = "projection_hash 与消息发布载荷一致；导出字节确定性（无时钟字段）。"
_NODE_COLUMNS = ("entity_id", "kb_id", "name", "label", "canonical_identity", "is_seed")
_EDGE_COLUMNS = (
    "triple_id",
    "kb_id",
    "source_entity_id",
    "target_entity_id",
    "predicate",
    "relation_group",
    "predicates",
    "parallel_count",
    "review_status",
    "review_version",
    "support_count",
    "literature_count",
    "best_evidence_level",
    "conflict_status",
    "risk_score",
)


def _envelope(snapshot: dict[str, Any], *, run_id: str) -> dict[str, Any]:
    return {
        "schema": EXPORT_SCHEMA,
        "source": "published_payload",
        "run_id": str(run_id),
        "retrieval_id": snapshot.get("retrieval_id"),
        "projection_hash": snapshot.get("projection_hash"),
        "snapshot": snapshot,
    }


def _csv_bytes(columns: tuple[str, ...], rows: list[dict[str, Any]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(columns), extrasaction="ignore", lineterminator="\r\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key) for key in columns})
    # utf-8-sig BOM：Excel 双击打开不乱码（roundtrip CSV 同款约定）
    return buffer.getvalue().encode("utf-8-sig")


def _edge_rows(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for edge in snapshot.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        row = dict(edge)
        predicates = row.get("predicates")
        if isinstance(predicates, list):
            row["predicates"] = ";".join(str(item) for item in predicates)
        else:
            row["predicates"] = row.get("predicate")
        rows.append(row)
    return rows


def _manifest(snapshot: dict[str, Any], *, run_id: str) -> dict[str, Any]:
    return {
        "schema": EXPORT_SCHEMA,
        "source": "published_payload",
        "run_id": str(run_id),
        "retrieval_id": snapshot.get("retrieval_id"),
        "projection_hash": snapshot.get("projection_hash"),
        "snapshot_schema": snapshot.get("schema") or GRAPH_SNAPSHOT_SCHEMA,
        "review_policy": snapshot.get("review_policy"),
        "outcome": snapshot.get("outcome"),
        "aggregation": snapshot.get("aggregation"),
        "counts": {
            "nodes": len(snapshot.get("nodes") or []),
            "edges": len(snapshot.get("edges") or []),
            "total_raw_edge_count": snapshot.get("total_raw_edge_count"),
            "suppressed": snapshot.get("suppressed"),
        },
        "columns": {"nodes.csv": list(_NODE_COLUMNS), "edges.csv": list(_EDGE_COLUMNS)},
        "integrity": _ASSERTION,
    }


def export_graph_snapshot_json(snapshot: dict[str, Any], *, run_id: str) -> bytes:
    return json.dumps(_envelope(snapshot, run_id=run_id), ensure_ascii=False, indent=2).encode("utf-8")


def export_graph_snapshot_csv_zip(snapshot: dict[str, Any], *, run_id: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("nodes.csv", _csv_bytes(_NODE_COLUMNS, snapshot.get("nodes") or []))
        archive.writestr("edges.csv", _csv_bytes(_EDGE_COLUMNS, _edge_rows(snapshot)))
        archive.writestr(
            "manifest.json",
            json.dumps(_manifest(snapshot, run_id=run_id), ensure_ascii=False, indent=2),
        )
    return buffer.getvalue()


def export_filename(snapshot: dict[str, Any], fmt: str) -> str:
    retrieval = str(snapshot.get("retrieval_id") or "run")
    short = retrieval.removeprefix("kr_")[:8] or "snapshot"
    policy = str(snapshot.get("review_policy") or "policy")
    return f"graph-snapshot_{short}_{policy}.{fmt}"


__all__ = [
    "EXPORT_SCHEMA",
    "export_filename",
    "export_graph_snapshot_csv_zip",
    "export_graph_snapshot_json",
]
