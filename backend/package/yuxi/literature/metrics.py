"""文献计量核心（P4，paperlit 语义移植；图投影 Neo4j 属后续接入）。

- PaperRank：``literature_citations`` 引用边上的 PageRank（networkx，d=0.85，
  上游口径）；PG 为 canonical，结果回填 ``literature_papers.paper_rank``
- 共被引聚类：共享被引文献对 → 连通分量（P4 最小实现；Louvain/igraph 留
  大库接入时评估——上游 lit 库量级下连通分量已可用）
- 期刊名归一化：&/and 等价（上游 2026-09-21 口径变更教训）+ 大小写/冒号/
  连字符归一；`ilike` 一律手写转义（AGENTS.md：容器 SQLAlchemy 无 autoescape）
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import select

from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import LiteratureCitation, LiteraturePaper

logger = logging.getLogger(__name__)

DAMPING = 0.85


def normalize_journal_name(raw: str) -> str:
    """期刊名归一化键：& ≡ and；小写；冒号/连字符/多空白折叠；去末尾句点。"""
    text = (raw or "").strip().lower().rstrip(".")
    text = re.sub(r"\s*&\s*|\s+and\s+", " and ", text)
    text = text.replace(":", " ").replace("-", " ")
    return re.sub(r"\s+", " ", text).strip()


def escape_like(raw: str) -> str:
    """LIKE 转义（AGENTS.md 硬规范：容器 SQLAlchemy 2.0.50 的 ilike 无 autoescape）。"""
    return raw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def compute_paper_rank(edges: list[tuple[str, str]]) -> dict[str, float]:
    """PageRank over (citing, cited) 边（确定性：同输入同输出）。"""
    import networkx as nx

    graph = nx.DiGraph()
    graph.add_edges_from(edges)
    if graph.number_of_nodes() == 0:
        return {}
    return {node: float(rank) for node, rank in nx.pagerank(graph, alpha=DAMPING).items()}


def compute_cocitation_clusters(citing_to_cited: dict[str, set[str]]) -> dict[str, int]:
    """共被引聚类：两篇文献被同一篇引用 → 连边 → 连通分量编号（从 1 起，无簇为 0）。"""
    cited_index: dict[str, list[str]] = {}
    for citing, cited_set in citing_to_cited.items():
        for cited in cited_set:
            cited_index.setdefault(cited, []).append(citing)

    pair_graph: dict[str, set[str]] = {}

    def _add(node: str, other: str) -> None:
        pair_graph.setdefault(node, set()).add(other)
        pair_graph.setdefault(other, set()).add(node)

    for cited, citings in cited_index.items():
        for i, a in enumerate(citings):
            for b in citings[i + 1 :]:
                if a != b:
                    _add(a, b)

    cluster_of: dict[str, int] = {}
    cluster_id = 0
    for node in sorted(pair_graph):
        if node in cluster_of:
            continue
        cluster_id += 1
        stack = [node]
        while stack:
            current = stack.pop()
            if current in cluster_of:
                continue
            cluster_of[current] = cluster_id
            stack.extend(pair_graph.get(current, ()))
    return cluster_of


async def recompute_metrics_for_tenant(tenant_id: int) -> dict[str, int]:
    """重算并回填租户内 paper_rank 与 cocitation_cluster（幂等，全量覆盖）。

    边目标解析（P0 身份语义）：cited_doi 为 DOI 形态 → ``doi-<目录名>``（与
    papers_meta.rid 一致）；否则原样保留（导入源异构的兜底）。
    """
    from yuxi.literature.identity.rid import doi_to_dirname, is_doi

    async with pg_manager.get_async_session_context() as session:
        edges: list[tuple[str, str]] = []
        for row in (
            (await session.execute(select(LiteratureCitation).where(LiteratureCitation.tenant_id == tenant_id)))
            .scalars()
            .all()
        ):
            target = row.cited_doi
            if is_doi(target):
                target = f"doi-{doi_to_dirname(target)}"
            edges.append((row.citing_rid, target))
        cited_by_paper: dict[str, set[str]] = {}
        for citing, cited in edges:
            cited_by_paper.setdefault(citing, set()).add(cited)

        ranks = compute_paper_rank(edges)
        clusters = compute_cocitation_clusters(cited_by_paper)
        papers = (
            (await session.execute(select(LiteraturePaper).where(LiteraturePaper.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
        updated = 0
        for paper in papers:
            # 边键可能是 RID 或裸 DOI/短名（导入源异构）——按候选键双向匹配
            bare = paper.rid.removeprefix("doi-")
            rank = ranks.get(paper.rid, ranks.get(bare, 0.0))
            cluster = clusters.get(paper.rid, clusters.get(bare, 0))
            paper.paper_rank = round(rank, 8)
            paper.cocitation_cluster = cluster
            updated += 1
        await session.commit()
    logger.info("计量重算完成: tenant=%s papers=%s edges=%s", tenant_id, updated, len(edges))
    return {"papers": updated, "edges": len(edges)}
