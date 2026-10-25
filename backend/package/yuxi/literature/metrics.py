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
from collections import Counter
from itertools import combinations

from sqlalchemy import select

from yuxi.storage.postgres.models_knowledge import LiteratureJournalMetric, LiteraturePaper
from yuxi.literature.access import current_access, paper_visibility
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


def compute_cocitation_clusters(citing_to_cited: dict[str, set[str]], min_cocitations: int = 2) -> dict[str, int]:
    """共被引聚类：两篇文献被同一篇引用 → 连边 → 连通分量编号（从 1 起，无簇为 0）。"""
    if min_cocitations < 1:
        raise ValueError("min_cocitations 必须至少为 1")
    # Co-citation connects the REFERENCED pair, not the citing papers. The
    # inverse graph is bibliographic coupling and must not share this label.
    pair_counts = Counter(pair for refs in citing_to_cited.values() for pair in combinations(sorted(refs), 2))
    pair_graph: dict[str, set[str]] = {}

    def _add(node: str, other: str) -> None:
        pair_graph.setdefault(node, set()).add(other)
        pair_graph.setdefault(other, set()).add(node)

    for (a, b), count in sorted(pair_counts.items()):
        if count >= min_cocitations:
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


async def recompute_metrics_for_tenant(tenant_id: int) -> dict:
    """重算并回填租户内 paper_rank 与 cocitation_cluster（幂等，全量覆盖）。

    边目标解析（P0 身份语义）：cited_doi 为 DOI 形态 → ``doi-<目录名>``（与
    papers_meta.rid 一致）；否则原样保留（导入源异构的兜底）。
    """
    from yuxi.literature.identity.rid import doi_to_dirname, is_doi

    async with pg_manager.get_async_session_context() as session:
        papers = list((await session.execute(select(LiteraturePaper).where(paper_visibility(tenant_id)))).scalars())
        visible = {paper.rid for paper in papers}
        edges: list[tuple[str, str]] = []
        for row in (
            (
                await session.execute(
                    select(LiteratureCitation).where(
                        LiteratureCitation.tenant_id == tenant_id, LiteratureCitation.citing_rid.in_(visible)
                    )
                )
            )
            .scalars()
            .all()
        ):
            target = row.cited_doi
            if is_doi(target):
                target = f"doi-{doi_to_dirname(target)}"
            if target in visible:
                edges.append((row.citing_rid, target))
        cited_by_paper: dict[str, set[str]] = {}
        for citing, cited in edges:
            cited_by_paper.setdefault(citing, set()).add(cited)

        ranks = compute_paper_rank(edges)
        clusters = compute_cocitation_clusters(cited_by_paper)
        # A request's ACL-scoped graph is a projection, not the tenant's
        # canonical graph. Never let a smaller visible graph overwrite shared
        # rankings (or leak hidden edges by computing globally for a requester).
        scoped = current_access() is not None
        projections = {}
        updated = 0
        for paper in papers:
            # 边键可能是 RID 或裸 DOI/短名（导入源异构）——按候选键双向匹配
            bare = paper.rid.removeprefix("doi-")
            rank = ranks.get(paper.rid, ranks.get(bare, 0.0))
            cluster = clusters.get(paper.rid, clusters.get(bare, 0))
            projections[paper.rid] = {"paper_rank": round(rank, 8), "cocitation_cluster": cluster}
            if not scoped:
                paper.paper_rank = round(rank, 8)
                paper.cocitation_cluster = cluster
            updated += 1
        await session.commit()
    logger.info("计量重算完成: tenant=%s papers=%s edges=%s", tenant_id, updated, len(edges))
    return {"papers": updated, "edges": len(edges), "persisted": not scoped, "projections": projections}


async def attach_journal_metrics(tenant_id: int) -> dict:
    """Actually attach latest imported metrics; no fake 'normalized' count."""
    from yuxi.literature.metrics import normalize_journal_name

    async with pg_manager.get_async_session_context() as session:
        rows = list(
            (
                await session.execute(
                    select(LiteratureJournalMetric)
                    .where(LiteratureJournalMetric.tenant_id == tenant_id)
                    .order_by(LiteratureJournalMetric.year.desc(), LiteratureJournalMetric.id.desc())
                )
            ).scalars()
        )
        latest = {}
        for row in rows:
            latest.setdefault(normalize_journal_name(row.journal_key), row)
        papers = list(
            (
                await session.execute(
                    select(LiteraturePaper)
                    .where(paper_visibility(tenant_id), LiteraturePaper.journal.isnot(None))
                    .with_for_update()
                )
            ).scalars()
        )
        matched, protected = 0, 0
        for paper in papers:
            if paper.meta_locked:
                protected += 1
                continue
            metric = latest.get(normalize_journal_name(paper.journal))
            if metric is not None:
                paper.impact_factor, paper.quartile = metric.jif, metric.quartile or ""
                matched += 1
        await session.commit()
    return {"matched": matched, "unmatched": len(papers) - matched - protected, "protected": protected}
