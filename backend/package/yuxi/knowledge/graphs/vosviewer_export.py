"""VOSviewer 无向分析投影；原始有向事实独立保留，永不回写事实层。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import io
import json
import time
import zipfile
from collections import Counter
from typing import Any

from yuxi.utils.auth_utils import _get_jwt_secret_key

FORMAT = "yuxi-vosviewer-v1"
SNAPSHOT_TTL_SECONDS = 900


class SnapshotExpiredError(ValueError):
    pass


def _json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def graph_fingerprint(data: dict) -> str:
    """忽略请求时间和数组排列；实体、事实属性、审核策略及截断范围均参与校验。"""
    payload = {
        "nodes": sorted(data.get("nodes", []), key=lambda row: str(row.get("id"))),
        "edges": sorted(data.get("edges", []), key=lambda row: str(row.get("id"))),
        "review_policy": data.get("meta", {}).get("review_policy"),
        "scope": data.get("meta", {}).get("scope"),
        "truncated": bool(data.get("truncated")),
        "truncation_reasons": sorted(data.get("truncation_reasons", [])),
    }
    return hashlib.sha256(_json(payload)).hexdigest()


def issue_snapshot(data: dict, *, kb_id: str, actor_uid: str, query: dict) -> dict:
    fingerprint = graph_fingerprint(data)
    claims = {
        "format": FORMAT,
        "kb_id": kb_id,
        "actor_uid": actor_uid,
        "sha256": fingerprint,
        "query": query,
        "generated_at": data.get("meta", {}).get("generated_at"),
        "expires_at": int(time.time()) + SNAPSHOT_TTL_SECONDS,
    }
    body = base64.urlsafe_b64encode(_json(claims)).decode().rstrip("=")
    signature = hmac.new(_snapshot_key(), body.encode(), hashlib.sha256).hexdigest()
    return {
        "token": f"{body}.{signature}",
        "id": fingerprint,
        "sha256": fingerprint,
        "generated_at": claims["generated_at"],
        "expires_at": claims["expires_at"],
    }


def verify_snapshot(token: str, *, kb_id: str, actor_uid: str) -> dict:
    try:
        body, signature = token.split(".")
        expected = hmac.new(_snapshot_key(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if claims["format"] != FORMAT or claims["kb_id"] != kb_id or claims["actor_uid"] != actor_uid:
            raise ValueError
        if claims["expires_at"] <= time.time():
            raise SnapshotExpiredError("图谱快照已过期，请刷新图谱后重新导出")
        return claims
    except SnapshotExpiredError:
        raise
    except (ValueError, KeyError, TypeError, UnicodeError) as exc:
        raise ValueError("无效的图谱快照凭证，请刷新图谱后重试") from exc


def _snapshot_key() -> bytes:
    # 独立签名域，分析凭证不是登录令牌，也不能用公开的 account_scope_id 签名。
    return hmac.new(_get_jwt_secret_key().encode(), FORMAT.encode(), hashlib.sha256).digest()


def build_vosviewer_package(
    *, kb_id: str, data: dict, snapshot: dict, exported_by: str, allow_candidates: bool = False
) -> dict:
    physical_entities = {}
    entities = {}
    excluded_nodes = 0
    for node in data.get("nodes", []):
        properties = node.get("properties") or {}
        if node.get("node_kind") == "chunk" or "Chunk" in node.get("labels", []):
            excluded_nodes += 1
            continue
        entity_id = node.get("entity_id") or properties.get("entity_id")
        if not entity_id:
            raise ValueError("实体缺少稳定 entity_id，请重建图谱后重新导出")
        entity = {
            "entity_id": str(entity_id),
            "label": str(node.get("name") or properties.get("name") or entity_id),
            "entity_type": str(node.get("type") or properties.get("label") or "Entity"),
        }
        if entity_id in entities and entities[entity_id] != entity:
            raise ValueError("同一 entity_id 的名称或类型不一致，请检查图谱投影")
        entities[str(entity_id)] = entity
        physical_entities[str(node["id"])] = str(entity_id)

    if not entities:
        raise ValueError("当前范围没有可导出的实体")

    facts = {}
    excluded_edges = duplicate_edges = 0
    for edge in data.get("edges", []):
        properties = edge.get("properties") or {}
        if (
            edge.get("edge_kind") == "mention"
            or edge.get("relationship_type") == "MENTIONS"
            or str(edge.get("type") or "").upper() == "MENTIONS"
        ):
            excluded_edges += 1
            continue
        triple_id = edge.get("triple_id") or properties.get("triple_id")
        if not triple_id:
            raise ValueError("关系缺少稳定 triple_id，请重建图谱后重新导出")
        status = properties.get("review_status") or ("CANONICAL" if properties.get("managed_projection") else "UNKNOWN")
        if status == "REJECTED":
            raise ValueError("当前投影仍包含已拒绝关系，请刷新或修复投影后导出")
        fact = {
            "triple_id": str(triple_id),
            "source_entity_id": physical_entities.get(str(edge.get("source_id"))),
            "target_entity_id": physical_entities.get(str(edge.get("target_id"))),
            "predicate": str(edge.get("type") or properties.get("type") or "RELATED_TO"),
            "review_status": status,
            "evidence_locator": {"kb_id": kb_id, "triple_id": str(triple_id)},
        }
        prior = facts.get(str(triple_id))
        if prior:
            if prior != fact:
                raise ValueError("同一 triple_id 的端点、谓词或审核状态不一致，请检查图谱投影")
            duplicate_edges += 1
        else:
            facts[str(triple_id)] = fact

    candidate_count = sum(fact["review_status"] not in {"APPROVED", "CANONICAL"} for fact in facts.values())
    if candidate_count and not allow_candidates:
        raise ValueError("当前范围包含未审核关系；正式分析请选择仅已审核，探索分析需明确允许候选关系")
    if data.get("meta", {}).get("review_policy") == "approved_only" and candidate_count:
        raise ValueError("仅已审核快照包含候选关系，请检查审核投影")
    if "concurrent_update" in data.get("truncation_reasons", []):
        raise ValueError("图谱正在更新，本次快照不完整，请刷新后导出")

    vos_id = {entity_id: index for index, entity_id in enumerate(sorted(entities), 1)}
    pair_facts: dict[tuple[int, int], list[str]] = {}
    degree = Counter()
    self_loops = dangling = 0
    exclusions = []
    for fact in sorted(facts.values(), key=lambda row: row["triple_id"]):
        source, target = fact["source_entity_id"], fact["target_entity_id"]
        if source is None or target is None:
            dangling += 1
            exclusions.append({"triple_id": fact["triple_id"], "reason": "missing_endpoint"})
            continue
        degree.update({source, target})
        if source == target:
            self_loops += 1
            exclusions.append({"triple_id": fact["triple_id"], "reason": "entity_self_loop"})
            continue
        pair = tuple(sorted((vos_id[source], vos_id[target])))
        pair_facts.setdefault(pair, []).append(fact["triple_id"])

    links = [
        {"source_id": source, "target_id": target, "strength": len(ids)}
        for (source, target), ids in sorted(pair_facts.items())
    ]
    link_strength = Counter()
    neighbors = Counter()
    for link in links:
        for endpoint in (link["source_id"], link["target_id"]):
            link_strength[endpoint] += link["strength"]
            neighbors[endpoint] += 1
    items = []
    for entity_id, number in vos_id.items():
        entity = entities[entity_id]
        items.append(
            {
                "id": number,
                "label": entity["label"],
                "description": f"类型：{html.escape(entity['entity_type'])}<br>entity_id：{html.escape(entity_id)}",
                "weights": {
                    "Links": neighbors[number],
                    "Total link strength": link_strength[number],
                    "Distinct facts": degree[entity_id],
                },
            }
        )
    counts = {
        "input_nodes": len(data.get("nodes", [])),
        "input_edges": len(data.get("edges", [])),
        "entities": len(items),
        "distinct_facts": len(facts),
        "links": len(links),
        "total_link_strength": sum(link["strength"] for link in links),
        "self_loop_facts": self_loops,
        "dangling_facts": dangling,
        "duplicate_edges": duplicate_edges,
        "structural_nodes": excluded_nodes,
        "structural_edges": excluded_edges,
        "isolated_entities": sum(neighbors[number] == 0 for number in vos_id.values()),
        "candidate_facts": candidate_count,
    }
    if counts["distinct_facts"] != counts["total_link_strength"] + self_loops + dangling:
        raise ValueError("VOSviewer 包对账失败：事实数 != 连线强度合计 + 自环 + 悬空，拒绝出包")
    # 官方 JSON 契约：items.descriptions 为字符串数组；links 字段名为 source/target；
    # weights 为数值数组（标签仅在 map.txt 头部承载，JSON 端按 manifest 顺序解读）
    network = {
        "network": {
            "items": [
                {
                    "id": item["id"],
                    "label": item["label"],
                    "descriptions": [item["description"]],
                    "weights": [
                        item["weights"]["Links"],
                        item["weights"]["Total link strength"],
                        item["weights"]["Distinct facts"],
                    ],
                }
                for item in items
            ],
            "links": [
                {"source": link["source_id"], "target": link["target_id"], "strength": link["strength"]}
                for link in links
            ],
        },
        "info": {
            "title": "知识图谱实体关联分析",
            "description": "无向连线强度为不同三元组数量；方向、谓词与审核状态见 facts.jsonl。聚类与密度属于派生分析。",
        },
    }
    identity = {
        "items": [{"vos_id": number, **entities[entity_id]} for entity_id, number in vos_id.items()],
        "links": [
            {"source_id": source, "target_id": target, "triple_ids": ids}
            for (source, target), ids in sorted(pair_facts.items())
        ],
    }
    members = {
        "vosviewer.json": _json(network),
        "identity-map.json": _json(identity),
        "facts.jsonl": b"".join(
            _json(fact) + b"\n" for fact in sorted(facts.values(), key=lambda row: row["triple_id"])
        ),
        # 注意：不导出 map.txt——VOSviewer map 文件强制要求 X/Y 坐标列，而快照
        # 在布局发生前没有坐标；伪造坐标会让所有点塌缩在原点。正确入口是
        # vosviewer.json（JSON，自动建图）或 network.txt（VOSviewer 自动建图布局）。
        "network.txt": "".join(
            f"{link['source_id']}\t{link['target_id']}\t{link['strength']}\n" for link in links
        ).encode("utf-8"),
        "README.txt": (
            "打开方式：VOSviewer File > Open，文件类型选 VOSviewer JSON file，打开 vosviewer.json；\n"
            "或选 VOSviewer network file 打开 network.txt——VOSviewer 会自动创建地图并运行布局。\n"
            "请勿先打开无坐标的 map 文件（本包不含 map.txt，坐标由 VOSviewer 布局生成）。\n"
            "首次分析保留全部连通分量；孤立实体、自环及排除事实以 manifest.json 和 facts.jsonl 为准。\n"
            "请记录归一化、分辨率、随机种子，并用 Save 保存坐标与聚类；结果仅用于派生分析。\n"
            "私有图谱保持本地或企业内网使用；VOSviewer Share 会上传地图并公开分享文件。\n"
        ).encode(),
    }
    manifest = {
        "format": FORMAT,
        "adapter_version": "1",
        "target_version": "1.6.21",
        "kb_id": kb_id,
        "snapshot_id": snapshot["sha256"],
        "generated_at": snapshot["generated_at"],
        "exported_by": exported_by,
        "scope": data.get("meta", {}).get("scope"),
        "review_policy": data.get("meta", {}).get("review_policy"),
        "truncated": bool(data.get("truncated")),
        "truncation_reasons": data.get("truncation_reasons", []),
        "metric": "distinct_triples",
        "projection": "undirected_entity_association",
        "authority": "DERIVED_ANALYSIS_ONLY",
        "allow_candidates": allow_candidates,
        "include_chunk_text": False,
        "counts": counts,
        "excluded_facts": exclusions,
        "analysis": {
            "coordinates_included": False,
            "clusters_included": False,
            "random_seeds": None,
            "parameters": "record_in_viewer_when_analysis_is_saved",
        },
        "files": {
            name: {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
            for name, content in members.items()
        },
    }
    members["manifest.json"] = _json(manifest)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, content)
    return {
        "content": output.getvalue(),
        "filename": f"vosviewer-{snapshot['sha256'][:16]}.zip",
        "media_type": "application/zip",
        "manifest": manifest,
    }
