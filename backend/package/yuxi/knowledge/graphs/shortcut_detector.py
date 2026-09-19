"""捷径边检测（R5c / 难题一「反传递」的运营工具）。

科研图谱的传递性往往不成立（A 促进 B、B 抑制 C ≠ A 抑制 C）。「A→C 存在且
A→B、B→C 均存在、而 A→C 的引文与 B 无词法重叠」的直连边，是 LLM 脑补传递
推理的高危信号——本模块把这类可疑边列成报表供人工复核。

定位是**运营工具不是门禁**：检索期不跑（零延迟成本），审核页按需调用。
判定纯确定性：图拓扑（长度 2 路径 + 直连共存）+ 引文词法重叠（无模型）。
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select

from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeGraphEntity,
    KnowledgeGraphTriple,
    KnowledgeGraphTripleMention,
)

SHORTCUT_DETECTOR_VERSION = "shortcut_detector_v1"
# 检测上限：运营报表有界，防大库全图遍历失控
MAX_SHORTCUT_SUSPECTS = 200
# 引文分词下限：过短 token（纯数字/单字符）不参与重叠判定
_MIN_TOKEN_LEN = 2
_TOKEN_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9]{1,}|[\u4e00-\u9fff]{2,}")


def _tokens(text: str) -> set[str]:
    return {token.lower() for token in _TOKEN_PATTERN.findall(text or "")}


def quote_mentions_entity(bridge_tokens: set[str], quote: str) -> bool:
    """引文是否词法提及桥实体 B（重叠判定）：任一 B 名 token 出现在引文中。"""
    if not bridge_tokens:
        return True  # 桥实体无可用 token 时无法排除，不报可疑（宁缺勿错）
    return bool(bridge_tokens & _tokens(quote))


def find_shortcut_suspects(
    triples: list[dict[str, Any]],
    quotes_by_triple: dict[str, list[str]],
    bridge_tokens_by_entity: dict[str, set[str]],
    *,
    limit: int = MAX_SHORTCUT_SUSPECTS,
) -> list[dict[str, Any]]:
    """纯函数检测：直连边 A→C 与长度 2 路径 A→B→C 共存，且 A→C 全部引文都不提及 B。

    ``triples`` 行需含 triple_id/source_entity_id/target_entity_id/relation_type/content。
    """
    adjacency: dict[str, set[str]] = {}
    direct: dict[tuple[str, str], dict[str, Any]] = {}
    for triple in triples:
        source, target = triple["source_entity_id"], triple["target_entity_id"]
        if source == target:
            continue
        adjacency.setdefault(source, set()).add(target)
        direct.setdefault((source, target), triple)
    inverse: dict[str, set[str]] = {}
    for source, targets in adjacency.items():
        for destination in targets:
            inverse.setdefault(destination, set()).add(source)

    suspects: list[dict[str, Any]] = []
    seen: set[str] = set()
    for (source, target), direct_triple in direct.items():
        if len(suspects) >= limit:
            break
        bridges = adjacency.get(source, set()) & inverse.get(target, set())
        bridges.discard(target)
        bridges.discard(source)
        if not bridges:
            continue
        quotes = quotes_by_triple.get(direct_triple["triple_id"]) or []
        if not quotes:
            continue  # 无引文的旧数据不判定（I1 修复入口另有报表）
        for bridge in bridges:
            if quote_mentions_entity(bridge_tokens_by_entity.get(bridge, set()), " || ".join(quotes)):
                continue  # 引文提及 B：直连边有自己的证据，不是传递脑补
            suspect_id = f"{direct_triple['triple_id']}:{bridge}"
            if suspect_id in seen:
                continue
            seen.add(suspect_id)
            suspects.append(
                {
                    "triple_id": direct_triple["triple_id"],
                    "relation_type": direct_triple["relation_type"],
                    "content": direct_triple.get("content"),
                    "bridge_entity_id": bridge,
                    "quotes": [quote[:300] for quote in quotes[:3]],
                    "suspect_id": suspect_id,
                }
            )
            break
    return suspects


async def detect_shortcut_edges(kb_id: str, *, limit: int = MAX_SHORTCUT_SUSPECTS) -> list[dict[str, Any]]:
    """DB 入口：拉取 kb 全部三元组 + 引文 + 实体名 token，跑纯函数检测。"""
    async with pg_manager.get_async_session_context() as session:
        triples = [
            {
                "triple_id": row.triple_id,
                "source_entity_id": row.source_entity_id,
                "target_entity_id": row.target_entity_id,
                "relation_type": row.relation_type,
                "content": row.content,
            }
            for row in (
                (
                    await session.execute(
                        select(
                            KnowledgeGraphTriple.triple_id,
                            KnowledgeGraphTriple.source_entity_id,
                            KnowledgeGraphTriple.target_entity_id,
                            KnowledgeGraphTriple.relation_type,
                            KnowledgeGraphTriple.content,
                        ).where(KnowledgeGraphTriple.kb_id == kb_id)
                    )
                ).all()
            )
        ]
        if not triples:
            return []
        triple_ids = [triple["triple_id"] for triple in triples]
        quotes_by_triple: dict[str, list[str]] = {}
        for row in (
            await session.execute(
                select(
                    KnowledgeGraphTripleMention.triple_id,
                    KnowledgeGraphTripleMention.text,
                ).where(KnowledgeGraphTripleMention.triple_id.in_(triple_ids))
            )
        ).all():
            if row.text:
                quotes_by_triple.setdefault(row.triple_id, []).append(row.text)
        entity_rows = (
            await session.execute(
                select(
                    KnowledgeGraphEntity.entity_id,
                    KnowledgeGraphEntity.name,
                    KnowledgeGraphEntity.normalized_name,
                ).where(KnowledgeGraphEntity.kb_id == kb_id)
            )
        ).all()
    bridge_tokens = {
        entity_id: _tokens(f"{name} {normalized_name}") for entity_id, name, normalized_name in entity_rows
    }
    return find_shortcut_suspects(triples, quotes_by_triple, bridge_tokens, limit=limit)
