from __future__ import annotations

import io
import json
import zipfile

from yuxi.knowledge.graphs.graph_snapshot_export import (
    export_filename,
    export_graph_snapshot_csv_zip,
    export_graph_snapshot_json,
)

_SNAPSHOT = {
    "schema": "graph_snapshot_v1",
    "retrieval_id": "kr_abcdef123456",
    "outcome": "HIT",
    "review_policy": "approved_plus_candidate",
    "projection_hash": "h" * 64,
    "total_raw_edge_count": 733,
    "aggregation": {"strategy": "kb_id+relation_group+target_normalized+direction", "group_count": 257},
    "nodes": [
        {
            "entity_id": "e1",
            "kb_id": "kb-a",
            "name": "OsMYB73",
            "label": "Gene",
            "canonical_identity": "osmyb73",
            "is_seed": True,
        },
        {
            "entity_id": "e2",
            "kb_id": "kb-a",
            "name": 'chalky, "endosperm"\n现象',
            "label": "Trait",
            "canonical_identity": "chalkiness",
            "is_seed": False,
        },
    ],
    "edges": [
        {
            "triple_id": "t1",
            "kb_id": "kb-a",
            "source_entity_id": "e1",
            "target_entity_id": "e2",
            "predicate": "影响",
            "relation_group": "ASSOCIATION_OR_CONTEXT",
            "predicates": ["affects", "影响"],
            "parallel_count": 36,
            "review_status": "CANDIDATE",
            "support_count": 53,
            "conflict_status": "NONE",
        }
    ],
    "suppressed": {"review_policy": 0, "rejected": 0},
}


def test_json_export_is_deterministic_and_enveloped():
    first = export_graph_snapshot_json(_SNAPSHOT, run_id="run-1")
    second = export_graph_snapshot_json(_SNAPSHOT, run_id="run-1")
    assert first == second  # 无时钟字段：同 run 字节恒定，ETag 语义成立

    payload = json.loads(first.decode("utf-8"))
    assert payload["schema"] == "graph_snapshot_export_v1"
    assert payload["source"] == "published_payload"
    assert payload["run_id"] == "run-1"
    assert payload["projection_hash"] == _SNAPSHOT["projection_hash"]
    assert payload["snapshot"] == _SNAPSHOT


def test_csv_zip_contains_bom_csv_manifest_and_escapes():
    body = export_graph_snapshot_csv_zip(_SNAPSHOT, run_id="run-1")
    assert body == export_graph_snapshot_csv_zip(_SNAPSHOT, run_id="run-1")

    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        names = set(archive.namelist())
        assert names == {"nodes.csv", "edges.csv", "manifest.json"}
        assert {item.date_time for item in archive.infolist()} == {(1980, 1, 1, 0, 0, 0)}
        assert {item.create_system for item in archive.infolist()} == {3}

        nodes = archive.read("nodes.csv").decode("utf-8-sig")
        assert nodes.startswith("entity_id,kb_id,name,label,canonical_identity,is_seed")
        # 逗号/引号/换行的 name 必须被标准 quoting 包住（roundtrip CSV 同款约定）：
        # 字段整体加引号、内嵌引号翻倍、换行按 csv 模块规范保留为裸 \n
        assert '"chalky, ""endosperm""\n现象"' in nodes

        edges = archive.read("edges.csv").decode("utf-8-sig")
        assert "affects;影响" in edges
        assert "36" in edges

        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        assert manifest["projection_hash"] == _SNAPSHOT["projection_hash"]
        assert manifest["counts"] == {
            "nodes": 2,
            "edges": 1,
            "total_raw_edge_count": 733,
            "suppressed": {"review_policy": 0, "rejected": 0},
        }
        assert manifest["columns"]["nodes.csv"][0] == "entity_id"


def test_export_filename_is_safe_and_stable():
    name = export_filename(_SNAPSHOT, "csv")
    assert name == "graph-snapshot_abcdef12_approved_plus_candidate.csv"
    assert export_filename(_SNAPSHOT, "json").endswith(".json")
