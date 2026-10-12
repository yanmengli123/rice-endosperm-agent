"""文献计量聚合测试（G3）：快照过滤（参考条目/回收站/租户/预算失败关闭）
+ 纯聚合数值（趋势补零、期刊归一合并、国家提取、共现网络）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.literature import bibliometrics
from yuxi.storage.postgres.models_knowledge import LiteraturePaper
from yuxi.utils.datetime_utils import utc_now

# asyncio_mode=auto（pyproject.toml）：async 测试自动收集，无需显式标记；
# 显式 asyncio 标记会把同步纯函数一并打上并触发 PytestWarning。
pytestmark = [pytest.mark.unit]


def _paper(**overrides):
    base = {
        "kind": "paper",
        "language": "en",
        "pub_year": 2024,
        "journal": "Rice Science",
        "authors": [],
        "keywords": [],
        "affiliations": [],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------- 纯聚合


def test_aggregate_overview_and_yearly_zero_fill():
    papers = [
        _paper(pub_year=2023, keywords=["rice"], authors=["Zhang, Wei"]),
        _paper(pub_year=2025, language="zh", keywords=["Rice"], authors=["Zhang, Wei", "Li, Ming"]),
        _paper(pub_year=None),  # 无年份：计入 overview、不进趋势
    ]
    overview = bibliometrics.aggregate_overview(papers)
    assert overview["papers"] == 3
    assert overview["year_min"] == 2023 and overview["year_max"] == 2025
    assert overview["keywords"] == 1  # "rice" 与 "Rice" casefold 归一
    assert overview["authors"] == 2
    assert overview["by_language"] == {"en": 2, "zh": 1}
    assert overview["by_kind"] == {"paper": 3}

    yearly = bibliometrics.aggregate_yearly(papers)
    assert [item["year"] for item in yearly] == [2023, 2024, 2025]
    assert yearly[1] == {"year": 2024, "count": 0, "by_language": {}}  # 缺失年份补零
    assert yearly[0]["count"] == 1 and yearly[2]["count"] == 1


def test_journal_normalization_merges_writing_variants():
    papers = [
        _paper(journal="Adv Mater & Interfaces"),
        _paper(journal="Adv Mater and Interfaces"),
        _paper(journal="Rice Science"),
    ]
    top = bibliometrics.aggregate_journals(papers, top=10)
    assert top[0] == {"name": "Adv Mater & Interfaces", "normalized": "adv mater and interfaces", "count": 2}
    assert top[1]["count"] == 1


def test_affiliations_and_country_extraction():
    papers = [
        _paper(affiliations=["Natl Rice Res Inst, Hangzhou, Peoples R China", "Zhejiang Univ, Hangzhou, China"]),
        _paper(affiliations=["Huazhong Agr Univ, Wuhan, Peoples R China"]),
        _paper(affiliations=["中国农业科学院水稻研究所，北京"]),
    ]
    result = bibliometrics.aggregate_affiliations(papers, top=10)
    assert result["countries_top"][0] == {"name": "China", "count": 2}  # 按篇整计数（前两篇各计一次）
    names = {item["name"] for item in result["affiliations_top"]}
    assert "中国农业科学院水稻研究所，北京" in names
    assert next(item for item in result["affiliations_top"] if "Peoples R China" in item["name"])["country"] == "China"


def test_cooccurrence_network_keywords():
    papers = [
        _paper(keywords=["rice", "grain number"]),
        _paper(keywords=["Rice", "yield"]),
        _paper(keywords=["rice", "grain number", "yield"]),
    ]
    network = bibliometrics.build_cooccurrence_network(papers, field="keywords", top=10, min_pair=2)
    assert {node["id"] for node in network["nodes"]} == {"rice", "grain number", "yield"}
    weights = {(link["source"], link["target"]): link["weight"] for link in network["links"]}
    assert weights == {("grain number", "rice"): 2, ("rice", "yield"): 2}  # (grain, yield)=1 被过滤
    assert network["stats"] == {
        "nodes": 3,
        "links": 2,
        "pairs_total": 3,
        "pairs_scope": "top_nodes",
        "links_total": 2,
        "links_truncated": False,
    }


def test_cooccurrence_network_authors_and_top_cutoff():
    papers = [
        _paper(authors=["Zhang, Wei", "Li, Ming"]),
        _paper(authors=["Zhang, Wei", "Li, Ming", "Wang, Hong"]),
    ]
    network = bibliometrics.build_cooccurrence_network(papers, field="authors", top=2, min_pair=2)
    assert {node["id"] for node in network["nodes"]} == {"Zhang, Wei", "Li, Ming"}  # 频次截断
    assert all(link["source"] in {"Zhang, Wei", "Li, Ming"} for link in network["links"])


def test_network_rejects_unknown_field():
    with pytest.raises(ValueError, match="keywords 或 authors"):
        bibliometrics.build_cooccurrence_network([], field="journal")


def test_country_never_guesses_city_or_email_and_addresses_count_once():
    result = bibliometrics.aggregate_affiliations(
        [
            _paper(affiliations=["Rice Institute, Wuhan, China.", "Rice Institute, Wuhan, China."]),
            _paper(
                affiliations=[
                    "水稻研究所，北京",
                    "Rice Institute, Wuhan",
                    "Rice Univ, USA. Electronic address: a@example.com.",
                ]
            ),
        ]
    )
    assert result["countries_top"] == [{"name": "China", "count": 1}, {"name": "USA", "count": 1}]
    assert result["affiliations_top"][0]["count"] == 1
    assert bibliometrics._affiliation_country("Rice Institute, 100081, Beijing") == ""


def test_network_prunes_before_pair_expansion_and_exposes_truncation(monkeypatch):
    monkeypatch.setattr(bibliometrics, "MAX_NETWORK_LINKS", 1)
    network = bibliometrics.build_cooccurrence_network(
        [
            _paper(keywords=["a", "b", "c"] + [f"rare{i}" for i in range(1000)]),
            _paper(keywords=["a", "b", "c"]),
        ],
        field="keywords",
        top=3,
        min_pair=1,
    )
    assert network["stats"]["pairs_total"] == 3
    assert network["stats"]["links_truncated"] is True
    assert [node["id"] for node in network["nodes"]] == ["a", "b", "c"]


def test_network_pair_work_budget_fails_closed(monkeypatch):
    monkeypatch.setattr(bibliometrics, "MAX_NETWORK_PAIR_OPERATIONS", 2)
    with pytest.raises(ValueError, match="安全预算"):
        bibliometrics.build_cooccurrence_network([_paper(keywords=["a", "b", "c"])], field="keywords", top=3)


# ---------------------------------------------------------------- 快照过滤


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


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(LiteraturePaper.__table__.create)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        rows = (
            LiteraturePaper(tenant_id=1, rid="nd-a", kind="paper", pub_year=2023, keywords_json=["rice"]),
            LiteraturePaper(tenant_id=1, rid="nd-b", kind="paper", pub_year=2024, language="zh"),
            # 参考条目（PaperAgent 归档导入写入）：必须排除，否则趋势被打歪
            LiteraturePaper(tenant_id=1, rid="nd-ref", is_reference=True, pub_year=1990),
            # 回收站：排除
            LiteraturePaper(tenant_id=1, rid="nd-trash", trashed_at=utc_now()),
            # 其他租户：排除
            LiteraturePaper(tenant_id=2, rid="nd-other"),
        )
        for row in rows:
            session.add(row)
        await session.commit()
        monkeypatch.setattr(
            bibliometrics.pg_manager, "get_async_session_context", lambda: _AsyncSessionContext(session)
        )
        scope = SimpleNamespace(tenant_id=1, rids=frozenset({"nd-a", "nd-b", "nd-ref", "nd-trash"}))
        monkeypatch.setattr(bibliometrics, "current_access", lambda: scope)
        yield session
    await engine.dispose()


async def test_snapshot_excludes_references_trash_and_other_tenants(env):
    papers = await bibliometrics._snapshot(1)
    assert [paper.get("keywords") for paper in papers] == [["rice"], []]  # 仅 nd-a / nd-b
    assert await bibliometrics.bibliometrics(1, journals_top=5)  # 端点装配可跑通


async def test_snapshot_acl_filters_before_budget_and_counts(env, monkeypatch):
    from yuxi.literature.access import _access

    token = _access.set(SimpleNamespace(tenant_id=1, rids=frozenset({"nd-a"})))
    monkeypatch.setattr(bibliometrics, "MAX_BIBLIOMETRIC_PAPERS", 1)
    try:
        data = await bibliometrics.bibliometrics(1)
        assert data["overview"]["papers"] == 1
        assert data["yearly"] == [{"year": 2023, "count": 1, "by_language": {"unknown": 1}}]
    finally:
        _access.reset(token)


async def test_snapshot_permission_and_budget(env, monkeypatch):
    monkeypatch.setattr(bibliometrics, "current_access", lambda: None)
    with pytest.raises(PermissionError):
        await bibliometrics._snapshot(1)
    monkeypatch.setattr(bibliometrics, "current_access", lambda: SimpleNamespace(tenant_id=99, rids=frozenset()))
    with pytest.raises(PermissionError):
        await bibliometrics._snapshot(1)

    monkeypatch.setattr(bibliometrics, "MAX_BIBLIOMETRIC_PAPERS", 1)
    scope = SimpleNamespace(tenant_id=1, rids=frozenset({"nd-a", "nd-b"}))
    monkeypatch.setattr(bibliometrics, "current_access", lambda: scope)
    with pytest.raises(ValueError, match="安全预算"):
        await bibliometrics._snapshot(1)
    # 预算内正常返回
    monkeypatch.setattr(bibliometrics, "MAX_BIBLIOMETRIC_PAPERS", 2)
    assert len(await bibliometrics._snapshot(1)) == 2
