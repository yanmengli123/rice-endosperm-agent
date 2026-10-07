"""VOSviewer 分析包构建器与签名快照契约测试。

纯函数用例：无向合并、自环/悬空/结构排除、候选门禁、审核态校验、
恒等式 fail-closed、官方 JSON 契约（links source/target、descriptions 数组）、
zip 成员与 sha256、map.txt 字段清洗、快照签名（TTL/操作者/篡改/过期）。
"""

import io
import json
import time
import zipfile

import pytest

from yuxi.knowledge.graphs.vosviewer_export import (
    SnapshotExpiredError,
    build_vosviewer_package,
    graph_fingerprint,
    issue_snapshot,
    verify_snapshot,
)

ACTOR = "user-1"
KB = "kb_vos"


def node(node_id, name, entity_type, entity_id=None):
    entity_id = entity_id or f"eid-{node_id}"
    return {
        "id": node_id,
        "name": name,
        "type": entity_type,
        "labels": ["Entity"],
        "properties": {"entity_id": entity_id, "name": name, "label": entity_type},
    }


def edge(edge_id, source_id, target_id, predicate="REGULATES", status="APPROVED", triple_id=None):
    return {
        "id": edge_id,
        "source_id": source_id,
        "target_id": target_id,
        "type": predicate,
        "properties": {
            "triple_id": triple_id or f"tid-{edge_id}",
            "review_status": status,
            "chunk_id": f"chunk-{edge_id}",
            "file_id": f"file-{edge_id}",
        },
    }


def snapshot_of(data):
    return issue_snapshot(data, kb_id=KB, actor_uid=ACTOR, query={"scope": "full_graph"})


def base_data(nodes, edges, policy="approved_only"):
    return {
        "nodes": nodes,
        "edges": edges,
        "truncated": False,
        "truncation_reasons": [],
        "meta": {"scope": "full_graph", "review_policy": policy, "generated_at": "2026-10-07T00:00:00+00:00"},
    }


def build(data, **kwargs):
    defaults = dict(kb_id=KB, data=data, snapshot=snapshot_of(data), exported_by=ACTOR)
    defaults.update(kwargs)
    return build_vosviewer_package(**defaults)


def read_member(package, name):
    with zipfile.ZipFile(io.BytesIO(package["content"])) as archive:
        return archive.read(name)


def test_undirected_merge_and_direction_preserved_in_facts():
    data = base_data(
        [node("a", "GeneA", "Gene"), node("b", "ProteinB", "Protein")],
        [
            edge("e1", "a", "b", "activates"),
            edge("e2", "a", "b", "activates"),
            edge("e3", "b", "a", "inhibits"),
        ],
    )
    package = build(data)
    network = json.loads(read_member(package, "vosviewer.json"))
    links = network["network"]["links"]
    assert len(links) == 1
    assert links[0]["strength"] == 3
    assert set(links[0]) == {"source", "target", "strength"}
    facts = [json.loads(line) for line in read_member(package, "facts.jsonl").decode().splitlines()]
    predicates = {(f["predicate"], f["source_entity_id"]) for f in facts}
    assert ("activates", "eid-a") in predicates and ("inhibits", "eid-b") in predicates


def test_self_loop_dangling_and_structural_exclusion_counted():
    data = base_data(
        [
            node("a", "GeneA", "Gene"),
            node("b", "GeneB", "Gene"),
            node("iso", "Isolated", "Chemical"),
            {
                "id": "c1",
                "name": "chunk-1",
                "type": "Chunk",
                "labels": ["Chunk"],
                "node_kind": "chunk",
                "properties": {},
            },
        ],
        [
            edge("e1", "a", "a"),
            edge("e2", "a", "ghost"),
            edge("m1", "c1", "a", "MENTIONS"),
        ],
    )
    package = build(data)
    manifest = json.loads(read_member(package, "manifest.json"))
    counts = manifest["counts"]
    assert counts["entities"] == 3
    # 全部事实为自环/悬空/结构边：没有跨类型连线，三个实体在网络意义上均为孤立
    assert counts["isolated_entities"] == 3
    assert counts["self_loop_facts"] == 1
    assert counts["dangling_facts"] == 1
    assert counts["structural_nodes"] == 1
    assert counts["structural_edges"] == 1
    assert {row["reason"] for row in manifest["excluded_facts"]} == {"entity_self_loop", "missing_endpoint"}
    network = json.loads(read_member(package, "vosviewer.json"))
    assert network["network"]["links"] == []
    assert len(network["network"]["items"]) == 3


def test_items_contract_and_identity_map():
    data = base_data([node("a", "基因甲", "Gene"), node("b", "ProteinB", "Protein")], [edge("e1", "a", "b")])
    package = build(data)
    network = json.loads(read_member(package, "vosviewer.json"))
    item = network["network"]["items"][0]
    assert set(item) == {"id", "label", "descriptions", "weights"}
    assert isinstance(item["descriptions"], list) and isinstance(item["weights"], list)
    labels = [i["label"] for i in network["network"]["items"]]
    assert "基因甲" in labels  # 中文名原样保留（UTF-8）
    identity = json.loads(read_member(package, "identity-map.json"))
    assert len(identity["items"]) == 2
    vos_ids = sorted(i["vos_id"] for i in identity["items"])
    assert vos_ids == [1, 2]
    link = identity["links"][0]
    assert link["triple_ids"] == ["tid-e1"]


def test_candidate_gate_requires_explicit_allow():
    data = base_data([node("a", "A", "Gene"), node("b", "B", "Gene")], [edge("e1", "a", "b", status="CANDIDATE")])
    with pytest.raises(ValueError, match="未审核关系"):
        build(data)
    explore = base_data(
        [node("a", "A", "Gene"), node("b", "B", "Gene")],
        [edge("e1", "a", "b", status="CANDIDATE")],
        policy="candidates_visible",
    )
    package = build(explore, allow_candidates=True)
    manifest = json.loads(read_member(package, "manifest.json"))
    assert manifest["counts"]["candidate_facts"] == 1
    assert manifest["allow_candidates"] is True
    assert manifest["review_policy"] == "candidates_visible"


def test_rejected_relation_and_inconsistent_projection_fail_closed():
    data = base_data([node("a", "A", "Gene"), node("b", "B", "Gene")], [edge("e1", "a", "b", status="REJECTED")])
    with pytest.raises(ValueError, match="已拒绝关系"):
        build(data, allow_candidates=True)
    bad = base_data(
        [node("x", "A", "Gene", entity_id="eid-dup"), node("y", "Different", "Gene", entity_id="eid-dup")],
        [],
    )
    with pytest.raises(ValueError, match="名称或类型不一致"):
        build(bad)


def test_concurrent_update_blocks_export():
    data = base_data([node("a", "A", "Gene"), node("b", "B", "Gene")], [edge("e1", "a", "b")])
    data["truncation_reasons"] = ["concurrent_update"]
    with pytest.raises(ValueError, match="正在更新"):
        build(data)


def test_snapshot_token_signature_expiry_and_actor_binding(monkeypatch):
    data = base_data([node("a", "A", "Gene")], [])
    snapshot = issue_snapshot(data, kb_id=KB, actor_uid=ACTOR, query={})
    claims = verify_snapshot(snapshot["token"], kb_id=KB, actor_uid=ACTOR)
    assert claims["sha256"] == snapshot["id"] == graph_fingerprint(data)
    with pytest.raises(ValueError):
        verify_snapshot(snapshot["token"], kb_id="kb-other", actor_uid=ACTOR)
    with pytest.raises(ValueError):
        verify_snapshot(snapshot["token"], kb_id=KB, actor_uid="user-2")
    tampered = snapshot["token"][:-2] + ("ab" if not snapshot["token"].endswith("ab") else "cd")
    with pytest.raises(ValueError):
        verify_snapshot(tampered, kb_id=KB, actor_uid=ACTOR)
    monkeypatch.setattr(time, "time", lambda: snapshot["expires_at"] + 1)
    with pytest.raises(SnapshotExpiredError):
        verify_snapshot(snapshot["token"], kb_id=KB, actor_uid=ACTOR)


def test_fingerprint_ignores_ordering_and_time():
    d1 = base_data(
        [node("a", "A", "Gene"), node("b", "B", "Gene")],
        [edge("e1", "a", "b"), edge("e2", "b", "a")],
    )
    d2 = base_data(
        [node("b", "B", "Gene"), node("a", "A", "Gene")],
        [edge("e2", "b", "a"), edge("e1", "a", "b")],
    )
    d2["meta"]["generated_at"] = "2099-01-01T00:00:00+00:00"
    assert graph_fingerprint(d1) == graph_fingerprint(d2)


def test_zip_members_sha256_and_deterministic_order():
    data = base_data([node("a", "A", "Gene"), node("b", "B", "Gene")], [edge("e1", "a", "b")])
    package = build(data)
    # 路由契约：导出服务统一返回 content/filename/media_type（缺 media_type 会在路由层 KeyError）
    assert package["media_type"] == "application/zip"
    assert package["filename"].startswith("vosviewer-") and package["filename"].endswith(".zip")
    with zipfile.ZipFile(io.BytesIO(package["content"])) as archive:
        names = archive.namelist()
    assert set(names) == {
        "vosviewer.json",
        "identity-map.json",
        "facts.jsonl",
        "network.txt",
        "README.txt",
        "manifest.json",
    }
    manifest = json.loads(read_member(package, "manifest.json"))
    for name in ("vosviewer.json", "identity-map.json", "facts.jsonl", "network.txt", "README.txt"):
        import hashlib

        assert manifest["files"][name]["sha256"] == hashlib.sha256(read_member(package, name)).hexdigest()
    # 确定性：同数据两次构建字节一致（固定 zip 时间戳 + 排序）
    assert (
        package["content"]
        == build(base_data([node("a", "A", "Gene"), node("b", "B", "Gene")], [edge("e1", "a", "b")]))["content"]
    )


def test_no_map_file_without_coordinates():
    # VOSviewer map 文件强制要求 X/Y 坐标列；快照在布局前无坐标，
    # 导出包不得包含 map.txt（否则 File → Open map 报 "X and Y columns are missing"）。
    # 正确入口：vosviewer.json（JSON，自动建图）或 network.txt（network file，自动建图）。
    dirty = node("a", "Gene\tA\nB", "Gene")
    data = base_data([dirty, node("b", "B", "Gene")], [edge("e1", "a", "b")])
    package = build(data)
    with zipfile.ZipFile(io.BytesIO(package["content"])) as archive:
        names = archive.namelist()
        network = json.loads(archive.read("vosviewer.json"))
    assert "map.txt" not in names
    # 制表符/换行污染的名称在 JSON label 中原样保留（JSON 字符串安全），识别走 label
    labels = [i["label"] for i in network["network"]["items"]]
    assert "Gene\tA\nB" in labels


