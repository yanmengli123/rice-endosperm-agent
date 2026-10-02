"""文献计量图谱查询（G3；PG 为 canonical，Neo4j 是可重建投影）。

数据 API（network/neighbors/top）对标 paperlit graph 端点形状；前端提供
共享数据集的 2D Canvas 与 Three.js WebGL 3D 视图（决议 15）。
"""

from __future__ import annotations

from typing import Any
from types import SimpleNamespace

from yuxi.literature.access import paper_visibility

from sqlalchemy import select
from collections import Counter
from yuxi.literature.identity.rid import doi_to_dirname, is_doi

from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import LiteratureCitation, LiteraturePaper

MAX_NODE_LIMIT = 5000


async def _visible_graph(tenant_id: int):
    async with pg_manager.get_async_session_context() as session:
        papers = (await session.execute(select(LiteraturePaper).where(paper_visibility(tenant_id)))).scalars().all()
        raw_edges = (
            await session.execute(
                select(LiteratureCitation.citing_rid, LiteratureCitation.cited_doi).where(
                    LiteratureCitation.tenant_id == tenant_id
                )
            )
        ).all()
        library = (
            await session.execute(
                select(LiteraturePaper.rid, LiteraturePaper.doi).where(LiteraturePaper.tenant_id == tenant_id)
            )
        ).all()
    by_rid = {p.rid: p for p in papers}
    visible_dois = {p.doi: p.rid for p in papers if p.doi}
    hidden_dois = {doi for rid, doi in library if rid not in by_rid and doi} - set(visible_dois)
    edges = []
    for citing, doi in raw_edges:
        if citing not in by_rid or doi in hidden_dois:
            continue
        target = visible_dois.get(doi) or (f"doi-{doi_to_dirname(doi)}" if is_doi(doi) else doi)
        if target not in by_rid:
            # External references are metadata of the visible citing paper,
            # not hidden library documents. Preserve upstream bibliography.
            by_rid[target] = SimpleNamespace(
                rid=target,
                doi=doi,
                title=doi,
                kind="paper",
                paper_rank=0,
                impact_factor=None,
                quartile=None,
                cocitation_cluster=None,
            )
        edges.append((citing, target))
    return by_rid, edges


async def graph_network(tenant_id: int, *, min_citations: int = 0, limit: int = 300) -> dict[str, Any]:
    """Count and rank only edges whose two endpoints are visible."""
    limit = max(1, min(int(limit), MAX_NODE_LIMIT))
    papers, edges = await _visible_graph(tenant_id)
    counts = Counter(target for _, target in edges)
    connected = {rid for edge in edges for rid in edge}
    selected = sorted((rid for rid in connected if counts[rid] >= min_citations), key=lambda rid: (-counts[rid], rid))[
        :limit
    ]
    nodes = [
        {
            "rid": rid,
            "title": papers[rid].title,
            "kind": papers[rid].kind,
            "citations": counts[rid],
            "paper_rank": papers[rid].paper_rank,
            "impact_factor": papers[rid].impact_factor,
            "quartile": papers[rid].quartile,
            "cocitation_cluster": papers[rid].cocitation_cluster,
        }
        for rid in selected
    ]
    keep = set(selected)
    return {
        "nodes": nodes,
        "edges": [{"source": a, "target": b} for a, b in edges if a in keep and b in keep],
        "total_nodes": len(connected),
        "truncated": len(connected) > limit,
    }


async def graph_neighbors(tenant_id: int, rid: str) -> dict[str, Any]:
    papers, edges = await _visible_graph(tenant_id)
    if rid not in papers:
        raise ValueError("文献不存在或无权访问")
    outgoing = [papers[b].doi or b for a, b in edges if a == rid]
    incoming = [a for a, b in edges if b == rid]
    return {
        "rid": rid,
        "references": outgoing[:50],
        "cited_by": incoming[:50],
        "reference_count": len(outgoing),
        "cited_by_count": len(incoming),
        "library_size": len(papers),
    }


async def graph_top(tenant_id: int, *, limit: int = 20) -> list[dict[str, Any]]:
    _, edges = await _visible_graph(tenant_id)
    counts = Counter(b for _, b in edges)
    return [
        {"rid": rid, "cited_count": count}
        for rid, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
    ]


async def project_to_neo4j(tenant_id: int) -> dict[str, Any]:
    """引用图投影到 Neo4j（决议 13：PG 为 canonical，投影供多跳/图治理消费）。

    节点 = literature_papers（rid 唯一键 + 计量属性）；边 = literature_citations
    （cited_doi 归一到 rid，与 graph_top/metrics 同口径）。幂等 MERGE。
    """
    from yuxi.literature.identity.rid import doi_to_dirname, is_doi
    from yuxi.storage.neo4j import get_shared_neo4j_connection, neo4j_write

    connection = get_shared_neo4j_connection()
    if not connection.driver:
        raise ValueError("Neo4j 不可用（连接未建立）；投影需启用 graph 服务")

    async with pg_manager.get_async_session_context() as session:
        papers = (await session.execute(select(LiteraturePaper).where(paper_visibility(tenant_id)))).scalars().all()
        edges = (
            await session.execute(
                select(LiteratureCitation.citing_rid, LiteratureCitation.cited_doi).where(
                    LiteratureCitation.tenant_id == tenant_id
                )
            )
        ).all()
    known_rids = {p.rid for p in papers}
    rid_by_doi_key: dict[str, str] = {}
    for paper in papers:
        if paper.doi and is_doi(paper.doi):
            rid_by_doi_key[paper.doi] = paper.rid
    node_payload = [
        {
            "rid": p.rid,
            "title": p.title or "",
            "kind": p.kind,
            "quartile": p.quartile or "",
            "impact_factor": p.impact_factor,
            "paper_rank": p.paper_rank,
            "cluster": p.cocitation_cluster,
        }
        for p in papers
    ]
    edge_payload = []
    for citing, cited in edges:
        target = rid_by_doi_key.get(cited) or (f"doi-{doi_to_dirname(cited)}" if is_doi(cited) else cited)
        if citing in known_rids and target in known_rids:
            edge_payload.append({"source": citing, "target": target})
    if not node_payload:
        raise ValueError("租户内无文献，无可投影")

    def _write(tx) -> None:
        tx.run(
            """
            UNWIND $nodes AS node
            MERGE (p:LitPaper {tenant_id: $tenant_id, rid: node.rid})
            SET p.title = node.title, p.kind = node.kind, p.quartile = node.quartile,
                p.impact_factor = node.impact_factor, p.paper_rank = node.paper_rank,
                p.cocitation_cluster = node.cluster
            """,
            nodes=node_payload,
            tenant_id=tenant_id,
        )
        tx.run(
            """
            UNWIND $edges AS edge
            MATCH (a:LitPaper {tenant_id: $tenant_id, rid: edge.source})
            MATCH (b:LitPaper {tenant_id: $tenant_id, rid: edge.target})
            MERGE (a)-[:CITES]->(b)
            """,
            edges=edge_payload,
            tenant_id=tenant_id,
        )

    neo4j_write(connection.driver, _write)
    return {"nodes": len(node_payload), "edges": len(edge_payload), "tenant_id": tenant_id}
