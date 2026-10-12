"""计量核心测试（P4）：期刊名归一（&/and 等价——上游 2026-09-21 口径）、
PaperRank 确定性、ilike 转义（AGENTS.md 事故回归）、租户级重算。
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.literature import metrics
from yuxi.storage.postgres.models_knowledge import LiteratureCitation, LiteraturePaper

pytestmark = [pytest.mark.unit]


class _AsyncSessionContext:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, exc_type, *_args):
        if exc_type is None:
            await self.db.commit()
        else:
            await self.db.rollback()
        return False


def test_journal_normalization_and_ilike_escape():
    # & 与 and 等价（上游口径变更教训）
    assert metrics.normalize_journal_name("Adv Mater & Interfaces") == metrics.normalize_journal_name(
        "Adv Mater and Interfaces"
    )
    assert metrics.normalize_journal_name("Nature: Food") == metrics.normalize_journal_name("Nature Food")
    assert metrics.normalize_journal_name("Nano-Micro Lett.") == metrics.normalize_journal_name("nano micro lett")
    # LIKE 元字符转义（AGENTS.md：%/_ 未转义会崩或全表匹配）
    assert metrics.escape_like("100%_a") == "100\\%\\_a"
    assert metrics.escape_like("back\\slash") == "back\\\\slash"


def test_paper_rank_is_deterministic_and_cited_nodes_rank_higher():
    edges = [("doi-a", "doi-c"), ("doi-b", "doi-c"), ("doi-c", "doi-d")]
    ranks1 = metrics.compute_paper_rank(edges)
    ranks2 = metrics.compute_paper_rank(edges)
    assert ranks1 == ranks2, "同输入必须同输出（确定性）"
    # a 只有出边无入边（rank 最低）；c 被两篇引用，rank 高于 a；
    # d 是汇聚节点（入边来自高 rank 的 c 且无出边）会积累 rank——网络流语义如此
    assert ranks1["doi-c"] > ranks1["doi-a"] > 0
    assert ranks1["doi-d"] > 0


def test_cocitation_clusters_shared_cited_pairs():
    # a、b 共同引用 c/d：共被引节点是 c/d，绝不是 a/b（后者是文献耦合）。
    clusters = metrics.compute_cocitation_clusters({"a": {"c", "d"}, "b": {"c", "d"}, "e": {"x"}})
    assert clusters["c"] == clusters["d"] and clusters["c"] >= 1
    assert clusters.get("a", 0) == clusters.get("b", 0) == 0
    assert clusters.get("e", 0) == 0


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for model in (LiteraturePaper, LiteratureCitation):
            await conn.run_sync(model.__table__.create)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        rids = ("doi-10.1001_a", "doi-10.1001_b", "doi-10.1001_c", "doi-10.1001_d")
        for i, rid in enumerate(rids):
            session.add(LiteraturePaper(id=i + 1, tenant_id=1, rid=rid, kind="paper"))
        # 引用边使用真实 DOI 形态（生产数据来自 WOS 导出/bib 导入）
        session.add(LiteratureCitation(id=101, tenant_id=1, citing_rid="doi-10.1001_a", cited_doi="10.1001/c"))
        session.add(LiteratureCitation(id=102, tenant_id=1, citing_rid="doi-10.1001_b", cited_doi="10.1001/c"))
        session.add(LiteratureCitation(id=103, tenant_id=1, citing_rid="doi-10.1001_c", cited_doi="10.1001/d"))
        await session.commit()
        monkeypatch.setattr(metrics.pg_manager, "get_async_session_context", lambda: _AsyncSessionContext(session))
        yield session
    await engine.dispose()


async def test_recompute_backfills_rank_and_cluster(env):
    session = env
    result = await metrics.recompute_metrics_for_tenant(1)
    assert result["papers"] == 4
    papers = {p.rid: p for p in (await session.execute(select(LiteraturePaper))).scalars().all()}
    assert papers["doi-10.1001_c"].paper_rank > papers["doi-10.1001_a"].paper_rank > 0
    assert papers["doi-10.1001_d"].paper_rank > 0  # d 为汇聚节点（网络流语义）
    # Shared single reference is coupling, not co-citation.
    assert papers["doi-10.1001_a"].cocitation_cluster == papers["doi-10.1001_b"].cocitation_cluster == 0
    assert papers["doi-10.1001_c"].cocitation_cluster == 0
    assert papers["doi-10.1001_d"].cocitation_cluster == 0
    # 幂等：重跑结果一致
    before = {rid: (p.paper_rank, p.cocitation_cluster) for rid, p in papers.items()}
    await metrics.recompute_metrics_for_tenant(1)
    papers2 = {p.rid: p for p in (await session.execute(select(LiteraturePaper))).scalars().all()}
    assert {rid: (p.paper_rank, p.cocitation_cluster) for rid, p in papers2.items()} == before
