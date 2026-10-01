"""文献计量图谱查询（G3；PG 为 canonical，Neo4j 投影属后续接入）。

数据 API（network/neighbors/top）对标 paperlit graph 端点形状；前端本轮以
轻量邻居面板消费，3D 立体渲染不承诺（决议 13）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import LiteratureCitation, LiteraturePaper

MAX_NODE_LIMIT = 5000


async def graph_network(
    tenant_id: int,
    *,
    min_citations: int = 0,
    limit: int = 300,
) -> dict[str, Any]:
    """引用网络（节点 = 有边文献；按入度过滤/截断，MAX_NODE_LIMIT 封顶）。"""
    limit = max(1, min(int(limit), MAX_NODE_LIMIT))
    async with pg_manager.get_async_session_context() as session:
        edges = (
            await session.execute(
                select(LiteratureCitation.citing_rid, LiteratureCitation.cited_doi).where(
                    LiteratureCitation.tenant_id == tenant_id
                )
            )
        ).all()
        papers = (
            (await session.execute(select(LiteraturePaper).where(LiteraturePaper.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
    paper_by_rid = {p.rid: p for p in papers}
    # 统计入度（被引次数）——决定 top 节点
    in_degree: dict[str, int] = {}
    for _citing, cited in edges:
        key = f"doi-{cited}" if "/" in cited else cited
        in_degree[key] = in_degree.get(key, 0) + 1
    node_rids = {
        rid
        for rid in ({e[0] for e in edges} | {f"doi-{c}" if "/" in c else c for _, c in edges})
        if rid in paper_by_rid
    }
    selected = sorted(
        node_rids,
        key=lambda rid: -(in_degree.get(rid, 0)),
    )[:limit]
    selected_set = set(selected)
    nodes = []
    for rid in selected:
        paper = paper_by_rid[rid]
        nodes.append(
            {
                "rid": rid,
                "title": paper.title,
                "kind": paper.kind,
                "citations": in_degree.get(rid, 0),
                "paper_rank": paper.paper_rank,
                "impact_factor": paper.impact_factor,
                "quartile": paper.quartile,
                "cocitation_cluster": paper.cocitation_cluster,
            }
        )
    graph_edges = [
        {"source": citing, "target": f"doi-{cited}" if "/" in cited else cited}
        for citing, cited in edges
        if citing in selected_set and (f"doi-{cited}" if "/" in cited else cited) in selected_set
    ]
    if min_citations:
        nodes = [n for n in nodes if n["citations"] >= min_citations]
        keep = {n["rid"] for n in nodes}
        graph_edges = [e for e in graph_edges if e["source"] in keep and e["target"] in keep]
    return {"nodes": nodes, "edges": graph_edges, "total_nodes": len(node_rids), "truncated": len(node_rids) > limit}


async def graph_neighbors(tenant_id: int, rid: str) -> dict[str, Any]:
    """单文献引用邻居（出=参考文献，入=被引）。"""
    async with pg_manager.get_async_session_context() as session:
        outgoing = (
            (
                await session.execute(
                    select(LiteratureCitation.cited_doi).where(
                        LiteratureCitation.tenant_id == tenant_id,
                        LiteratureCitation.citing_rid == rid,
                    )
                )
            )
            .scalars()
            .all()
        )
        incoming = (
            (
                await session.execute(
                    select(LiteratureCitation.citing_rid).where(
                        LiteratureCitation.tenant_id == tenant_id,
                        LiteratureCitation.cited_doi == rid.removeprefix("doi-"),
                    )
                )
            )
            .scalars()
            .all()
        )
        paper_count = (
            await session.execute(
                select(func.count()).select_from(LiteraturePaper).where(LiteraturePaper.tenant_id == tenant_id)
            )
        ).scalar_one()
    return {
        "rid": rid,
        "references": outgoing[:50],
        "cited_by": incoming[:50],
        "reference_count": len(outgoing),
        "cited_by_count": len(incoming),
        "library_size": int(paper_count),
    }


async def graph_top(tenant_id: int, *, limit: int = 20) -> list[dict[str, Any]]:
    """被引次数 Top 文献（计量面板）。"""
    async with pg_manager.get_async_session_context() as session:
        rows = (
            await session.execute(
                select(
                    LiteratureCitation.cited_doi,
                    func.count().label("cited_count"),
                )
                .where(LiteratureCitation.tenant_id == tenant_id)
                .group_by(LiteratureCitation.cited_doi)
                .order_by(func.count().desc())
                .limit(limit)
            )
        ).all()
    from yuxi.literature.identity.rid import doi_to_dirname, is_doi

    result = []
    for cited_doi, count in rows:
        # RID 归一（与 metrics.recompute 同口径）：DOI 形态 → doi-<目录名>
        key = f"doi-{doi_to_dirname(cited_doi)}" if is_doi(cited_doi) else cited_doi
        result.append({"rid": key, "cited_count": int(count)})
    return result


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
        papers = (
            (await session.execute(select(LiteraturePaper).where(LiteraturePaper.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
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
