"""VOSviewer 无向分析投影；原始有向事实独立保留，永不回写事实层。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import io
import json
import math
import time
import zipfile
from collections import Counter
from typing import Any

from yuxi.utils.auth_utils import _get_jwt_secret_key

FORMAT = "yuxi-vosviewer-v1"
SNAPSHOT_TTL_SECONDS = 900
LAYOUT_ALGORITHM = "yuxi-seeded-force-v1"
# 斥力只对网格近邻桶内前 N 个邻居计算：聚团最坏情况的确定性预算上限。
LAYOUT_NEIGHBOR_CAP = 48
VOSVIEWER_MAX_CLUSTER = 1000


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


def _layout_iterations(count: int) -> int:
    return 250 if count <= 300 else (150 if count <= 1200 else 80)


def _seed_layout(count: int, links: list[dict]) -> tuple[list[float], list[float]]:
    """确定性种子布局：黄金角螺旋初始化 + 网格桶斥力与弹簧引力的固定步迭代。

    无随机源，节点遍历按 vos_id 升序、连线按已排序对遍历，同一网络在同一
    运行时必然得到同一坐标。坐标量化到 1e-6 并线性归一到单位圆盘。布局仅是
    导出时的初始视图（让 -json 打开即显示地图），可在 VOSviewer 内重排替换，
    不代表任何事实语义。
    """
    if count <= 0:
        return [], []
    if count == 1:
        return [0.0], [0.0]
    golden = 2.399963229728653
    xs = [((i + 1) / count) ** 0.5 * math.cos(golden * i) for i in range(count)]
    ys = [((i + 1) / count) ** 0.5 * math.sin(golden * i) for i in range(count)]
    iterations = _layout_iterations(count)
    ideal = (1.0 / count) ** 0.5
    cell = max(ideal, 1e-6)
    max_strength = max((link["strength"] for link in links), default=1)
    delta_x = [0.0] * count
    delta_y = [0.0] * count
    for step in range(iterations):
        temperature = 0.1 * (1.0 - step / iterations)
        for i in range(count):
            delta_x[i] = 0.0
            delta_y[i] = 0.0
        # 斥力：只对自身网格单元及半开邻域（东/北/东北/东南）内的节点生效，
        # 保证每对节点恰好计一次；桶内按 id 升序截断到邻居预算上限。
        buckets: dict[tuple[int, int], list[int]] = {}
        for i in range(count):
            buckets.setdefault((int(xs[i] / cell) + 2, int(ys[i] / cell) + 2), []).append(i)
        for gx, gy in sorted(buckets):
            members = buckets[(gx, gy)]
            same_bucket = ((0, 0),)
            half_space = ((1, 0), (0, 1), (1, 1), (1, -1))
            for offset_x, offset_y in same_bucket + half_space:
                if offset_x == offset_y == 0:
                    others = members
                else:
                    others = buckets.get((gx + offset_x, gy + offset_y))
                if not others:
                    continue
                for i in members[:LAYOUT_NEIGHBOR_CAP]:
                    for j in others[:LAYOUT_NEIGHBOR_CAP]:
                        if i == j or (i > j and offset_x == offset_y == 0):
                            continue
                        diff_x = xs[i] - xs[j]
                        diff_y = ys[i] - ys[j]
                        distance = math.sqrt(diff_x * diff_x + diff_y * diff_y)
                        if distance < 1e-9:
                            # 初始位置唯一，重合只可能来自数值塌缩：按 id 差定向推开。
                            diff_x, diff_y, distance = 1e-9 * (i - j), 0.0, 1e-9
                        force = ideal * ideal / distance
                        push_x = force * diff_x / distance
                        push_y = force * diff_y / distance
                        delta_x[i] += push_x
                        delta_y[i] += push_y
                        delta_x[j] -= push_x
                        delta_y[j] -= push_y
        # 引力：连线弹簧，强度归一后按距离超调拉回。
        for link in links:
            i, j = link["source_id"] - 1, link["target_id"] - 1
            diff_x = xs[i] - xs[j]
            diff_y = ys[i] - ys[j]
            distance = math.sqrt(diff_x * diff_x + diff_y * diff_y)
            if distance < 1e-9:
                continue
            force = distance * distance / ideal * (link["strength"] / max_strength)
            pull_x = force * diff_x / distance
            pull_y = force * diff_y / distance
            delta_x[i] -= pull_x
            delta_y[i] -= pull_y
            delta_x[j] += pull_x
            delta_y[j] += pull_y
        for i in range(count):
            shift = math.sqrt(delta_x[i] * delta_x[i] + delta_y[i] * delta_y[i])
            if shift > temperature:
                scale = temperature / shift
                delta_x[i] *= scale
                delta_y[i] *= scale
            xs[i] += delta_x[i]
            ys[i] += delta_y[i]
    mean_x = sum(xs) / count
    mean_y = sum(ys) / count
    spread = max(math.sqrt((xs[i] - mean_x) ** 2 + (ys[i] - mean_y) ** 2) for i in range(count)) or 1.0
    scale = 1.0 / spread
    return (
        [round((xs[i] - mean_x) * scale, 6) for i in range(count)],
        [round((ys[i] - mean_y) * scale, 6) for i in range(count)],
    )


def _connected_components(count: int, links: list[dict]) -> tuple[list[int] | None, int]:
    """无向连通分量编号（1..C，按分量内最小 vos_id 升序）与分量总数。

    连通分量是纯结构事实而非派生聚类；分量数超过 VOSviewer 的 cluster
    上限时编号返回 None（宁缺毋假），但总数仍如实返回供 manifest 披露。
    """
    parent = list(range(count + 1))

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for link in links:
        root_source, root_target = find(link["source_id"]), find(link["target_id"])
        if root_source != root_target:
            if root_source < root_target:
                parent[root_target] = root_source
            else:
                parent[root_source] = root_target
    roots = sorted({find(i) for i in range(1, count + 1)})
    if len(roots) > VOSVIEWER_MAX_CLUSTER:
        return None, len(roots)
    numbering = {root: index for index, root in enumerate(roots, 1)}
    return [numbering[find(i)] for i in range(1, count + 1)], len(roots)


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

    if len(entities) < 3:
        raise ValueError("VOSviewer 1.6.21 至少需要 3 个实体，请扩大图谱范围后重新导出")

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
    # 官方 JSON 契约（VOSviewer 1.6.21 FileData 二进制常量池 + 官方示例文件双重验证）：
    # items 携带平铺 x/y 坐标（-json 打开即显示地图）、description 单数字符串、
    # weights 命名对象；links 字段名为 source_id/target_id。坐标与 cluster 是
    # 导出侧的确定性种子视图，可随时在查看器内重排替换，不构成事实语义。
    xs, ys = _seed_layout(len(items), links)
    clusters, component_count = _connected_components(len(items), links)
    counts["connected_components"] = component_count
    network_items = []
    for item in items:
        number = item["id"]
        entry = {
            "id": number,
            "label": item["label"],
            "description": item["description"],
            "x": xs[number - 1],
            "y": ys[number - 1],
            "weights": item["weights"],
        }
        if clusters is not None:
            entry["cluster"] = clusters[number - 1]
        network_items.append(entry)
    network = {
        "network": {
            "items": network_items,
            "links": links,
        },
        "info": {
            "title": "知识图谱实体关联分析",
            "description": (
                "无向连线强度为不同三元组数量；方向、谓词与审核状态见 facts.jsonl。"
                "坐标与连通分量为导出侧种子视图，可在 VOSviewer 内重排。"
            ),
        },
    }
    identity = {
        "items": [{"vos_id": number, **entities[entity_id]} for entity_id, number in vos_id.items()],
        "links": [
            {"source_id": source, "target_id": target, "triple_ids": ids}
            for (source, target), ids in sorted(pair_facts.items())
        ],
    }
    launcher = r"""@echo off
rem Yuxi VOSviewer package launcher - opens vosviewer.json directly as a map.
rem Lookup order: VOSVIEWER_PATH env var, default install path, system PATH.
setlocal
set "EXE="
set "DEFAULT_EXE=D:\yuxi\VOSviewer_1.6.21_exe\VOSviewer.exe"
if defined VOSVIEWER_PATH set "EXE=%VOSVIEWER_PATH%"
if not defined EXE if exist "%DEFAULT_EXE%" set "EXE=%DEFAULT_EXE%"
if not defined EXE (
  where VOSviewer.exe >nul 2>nul
  if not errorlevel 1 set "EXE=VOSviewer.exe"
)
if not defined EXE (
  echo VOSviewer.exe not found.
  echo Set VOSVIEWER_PATH to the full path of VOSviewer.exe and run this file again.
  pause
  exit /b 1
)
start "" "%EXE%" -json "%~dp0vosviewer.json"
endlocal
""".replace("\n", "\r\n").encode("ascii")
    members = {
        "vosviewer.json": _json(network),
        "identity-map.json": _json(identity),
        "facts.jsonl": b"".join(
            _json(fact) + b"\n" for fact in sorted(facts.values(), key=lambda row: row["triple_id"])
        ),
        # 不导出 map.txt：map 文件强制要求 X/Y 坐标列，独立维护一份坐标副本只会
        # 与 vosviewer.json 漂移；地图入口统一是 vosviewer.json（已含种子坐标，
        # -json 打开即显示地图）。
        "network.txt": "".join(
            f"{link['source_id']}\t{link['target_id']}\t{link['strength']}\n" for link in links
        ).encode("utf-8"),
        "open-with-vosviewer.cmd": launcher,
        "README.txt": (
            "语析知识图谱 VOSviewer 分析包\n"
            "================================\n"
            "\n"
            "打开方式（任选其一，均打开即显示地图——vosviewer.json 已含确定性种子坐标）：\n"
            "1. 双击 open-with-vosviewer.cmd：自动定位 VOSviewer（优先 VOSVIEWER_PATH 环境变量，\n"
            "   其次本机默认安装路径，最后系统 PATH），以 -json 参数打开 vosviewer.json。\n"
            "2. 命令行：VOSviewer.exe -json <本目录>\\vosviewer.json\n"
            "3. VOSviewer 内 File > Open，文件类型选 VOSviewer JSON files，选择 vosviewer.json。\n"
            "\n"
            "坐标与聚类说明：x/y 为导出时按固定规则计算的种子布局（算法与参数见 manifest.json\n"
            "的 analysis 节），仅作初始视图；如需 VOSviewer 官方布局，在查看器内重排并另存。\n"
            "cluster 为无向连通分量编号（纯结构事实）；分量数超过查看器上限时省略 cluster。\n"
            "\n"
            "口径与审计：\n"
            "- 连线为无向投影，强度 = 两实体间不同三元组数；方向、谓词、审核状态与证据定位\n"
            "  以 facts.jsonl 为准（含 triple_id，可在语析内回查证据）。\n"
            "- 实体身份对照见 identity-map.json；孤立实体、自环与排除事实以 manifest.json 为准。\n"
            "- 本包为派生分析（DERIVED_ANALYSIS_ONLY）：聚类、密度、坐标结论均不写回知识库，\n"
            "  也不构成事实证据。\n"
            "\n"
            "安全提示：私有图谱保持本地或企业内网使用；VOSviewer 的 Share 会上传地图并公开分享。\n"
        ).encode(),
    }
    manifest = {
        "format": FORMAT,
        "adapter_version": "2",
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
            "coordinates_included": True,
            "coordinates": {
                "algorithm": LAYOUT_ALGORITHM,
                "deterministic": True,
                "iterations": _layout_iterations(len(items)),
                "neighbor_cap": LAYOUT_NEIGHBOR_CAP,
                "quantization": 0.000001,
                "replaceable_in_viewer": True,
                "note": "种子布局仅作初始视图；查看器内重排后的坐标不回写语析",
            },
            "clusters_included": clusters is not None,
            "clustering": {
                "algorithm": "connected_components",
                "count": component_count,
                "omitted_reason": (None if clusters is not None else "components_exceed_viewer_limit_1000"),
            },
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
