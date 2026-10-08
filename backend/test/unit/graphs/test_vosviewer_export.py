"""VOSviewer 分析包构建器与签名快照契约测试。

纯函数用例：无向合并、自环/悬空/结构排除、候选门禁、审核态校验、
恒等式 fail-closed、官方 JSON 契约（links source_id/target_id、description
单数、weights 命名对象、平铺 x/y 坐标）、确定性种子布局与连通分量、
zip 成员与 sha256、启动器内容、快照签名（TTL/操作者/篡改/过期）。
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
    # 官方契约（1.6.21 FileData 常量池 + 官方示例双重验证）：source_id/target_id
    assert set(links[0]) == {"source_id", "target_id", "strength"}
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
    # 官方契约：description 单数字符串、weights 命名对象、平铺 x/y；cluster 为连通分量编号
    assert set(item) == {"id", "label", "description", "x", "y", "cluster", "weights"}
    assert isinstance(item["description"], str) and isinstance(item["weights"], dict)
    assert item["cluster"] == 1  # 两个实体相连 → 同一连通分量
    assert set(item["weights"]) == {"Links", "Total link strength", "Distinct facts"}
    labels = [i["label"] for i in network["network"]["items"]]
    assert "基因甲" in labels  # 中文名原样保留（UTF-8）
    identity = json.loads(read_member(package, "identity-map.json"))
    assert len(identity["items"]) == 2
    vos_ids = sorted(i["vos_id"] for i in identity["items"])
    assert vos_ids == [1, 2]
    link = identity["links"][0]
    assert link["triple_ids"] == ["tid-e1"]


def test_seed_layout_deterministic_finite_and_effective():
    # 两簇各 3 个实体，簇内相连、簇间无连线：布局应有限、唯一、且簇内平均距离小于簇间
    nodes = [node(f"n{i}", f"N{i}", "Gene", entity_id=f"eid-{i}") for i in range(6)]
    edges = [edge(f"e{i}", f"n{i}", f"n{(i + 1) % 3}", triple_id=f"tid-{i}") for i in range(3)]
    edges += [edge(f"f{i}", f"n{i + 3}", f"n{3 + (i + 1) % 3}", triple_id=f"fid-{i}") for i in range(3)]
    data = base_data(nodes, edges)
    package = build(data)
    network = json.loads(read_member(package, "vosviewer.json"))
    items = {item["id"]: item for item in network["network"]["items"]}
    positions = [(item["x"], item["y"]) for item in network["network"]["items"]]
    assert len(set(positions)) == len(positions)  # 无坐标塌缩
    import math

    assert all(math.isfinite(x) and math.isfinite(y) for x, y in positions)
    def distance(a, b):
        return math.hypot(items[a]["x"] - items[b]["x"], items[a]["y"] - items[b]["y"])

    cluster_a, cluster_b = [1, 2, 3], [4, 5, 6]
    within = sum(distance(a, b) for a in cluster_a for b in cluster_a if a < b) / 3
    across = sum(distance(a, b) for a in cluster_a for b in cluster_b) / 9
    assert within < across  # 弹簧引力确实把相连实体放得更近
    manifest = json.loads(read_member(package, "manifest.json"))
    analysis = manifest["analysis"]
    assert analysis["coordinates_included"] is True
    assert analysis["coordinates"]["deterministic"] is True
    assert analysis["coordinates"]["replaceable_in_viewer"] is True
    assert analysis["clusters_included"] is True
    assert manifest["counts"]["connected_components"] == 2
    # 确定性：同数据两次构建全字节一致（含坐标量化）
    assert package["content"] == build(base_data(nodes, edges))["content"]


def test_cluster_omitted_when_components_exceed_viewer_limit():
    # 1001 个互不相连的实体 → 1001 个连通分量，超过 VOSviewer cluster 上限 1000：
    # 宁缺毋假——省略 cluster 字段，但 manifest 仍如实披露分量总数。
    nodes = [node(f"n{i}", f"N{i}", "Gene", entity_id=f"eid-{i}") for i in range(1001)]
    package = build(base_data(nodes, []))
    network = json.loads(read_member(package, "vosviewer.json"))
    assert all("cluster" not in item for item in network["network"]["items"])
    manifest = json.loads(read_member(package, "manifest.json"))
    assert manifest["analysis"]["clusters_included"] is False
    assert manifest["analysis"]["clustering"]["omitted_reason"] == "components_exceed_viewer_limit_1000"
    assert manifest["analysis"]["clustering"]["count"] == 1001
    assert manifest["counts"]["connected_components"] == 1001
    assert manifest["counts"]["isolated_entities"] == 1001


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
        "open-with-vosviewer.cmd",
        "README.txt",
        "manifest.json",
    }
    manifest = json.loads(read_member(package, "manifest.json"))
    for name in (
        "vosviewer.json",
        "identity-map.json",
        "facts.jsonl",
        "network.txt",
        "open-with-vosviewer.cmd",
        "README.txt",
    ):
        import hashlib

        assert manifest["files"][name]["sha256"] == hashlib.sha256(read_member(package, name)).hexdigest()
    # 确定性：同数据两次构建字节一致（固定 zip 时间戳 + 排序 + 坐标量化）
    assert (
        package["content"]
        == build(base_data([node("a", "A", "Gene"), node("b", "B", "Gene")], [edge("e1", "a", "b")]))["content"]
    )


def test_launcher_is_crlf_batch_with_json_flag():
    package = build(base_data([node("a", "A", "Gene"), node("b", "B", "Gene")], [edge("e1", "a", "b")]))
    raw = read_member(package, "open-with-vosviewer.cmd")
    text = raw.decode("ascii")
    assert "\r\n" in text and "\n" not in text.replace("\r\n", "")  # 批处理要求 CRLF
    assert '-json "%~dp0vosviewer.json"' in text  # 手册 5.1：-json 打开（或创建）地图
    assert "VOSVIEWER_PATH" in text  # 定位顺序：环境变量 → 默认路径 → PATH
    assert "D:\\yuxi\\VOSviewer_1.6.21_exe\\VOSviewer.exe" in text


def test_no_map_file_single_coordinate_authority():
    # 不导出 map.txt：坐标的权威副本只在 vosviewer.json（items.x/y），另造一份
    # map 文件副本只会与 JSON 漂移；打开入口统一为 -json（打开即显示地图）。
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


