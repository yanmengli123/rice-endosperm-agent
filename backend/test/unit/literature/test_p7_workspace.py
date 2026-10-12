"""P7（G1-G6）后端契约测试：bib 导入、富化合并语义、日记、卡片、复核裁决、图谱。"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.literature import bib_import, cards, diary, graph_service, review
from yuxi.storage.postgres.models_knowledge import (
    LiteratureCard,
    LiteratureCitation,
    LiteratureDiaryEntry,
    LiteraturePaper,
    LiteratureReviewDecision,
    LiteratureReaderRender,
    LiteratureArtifactRevision,
    LiteratureCompileJob,
)

pytestmark = [pytest.mark.unit]

TENANT = 3

BIB_SAMPLE = """@article{Smith2024, title={Starch synthesis in rice}, author={Smith, J.},
  journal={Advances in Materials}, year={2024}, doi={10.1002/adma.202407106},
  cr={Doe J.; 10.1016/j.snb.2022.132616; irrelevant text}}
@book{LeeBook, title={Grain Chemistry}, publisher={Elsevier}, year={2021}}
"""


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


class _FakeMinio:
    def __init__(self):
        self.uploads: list[tuple[str, bytes]] = []
        self.files: dict[str, bytes] = {}

    async def aupload_file(self, *, bucket_name, object_name, data, content_type):
        self.uploads.append((object_name, bytes(data)))
        self.files[object_name] = bytes(data)

    async def adownload_file(self, bucket, object_name):
        if object_name not in self.files:
            raise FileNotFoundError(object_name)
        return self.files[object_name]


@pytest_asyncio.fixture
async def env(monkeypatch, tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for model in (
            LiteraturePaper,
            LiteratureCitation,
            LiteratureDiaryEntry,
            LiteratureCard,
            LiteratureReviewDecision,
            LiteratureReaderRender,
            LiteratureArtifactRevision,
            LiteratureCompileJob,
        ):
            await conn.run_sync(model.__table__.create)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        monkeypatch.setattr(bib_import.pg_manager, "get_async_session_context", lambda: _AsyncSessionContext(session))
        monkeypatch.setattr(cards.pg_manager, "get_async_session_context", lambda: _AsyncSessionContext(session))
        monkeypatch.setattr(diary.pg_manager, "get_async_session_context", lambda: _AsyncSessionContext(session))
        monkeypatch.setattr(review.pg_manager, "get_async_session_context", lambda: _AsyncSessionContext(session))
        monkeypatch.setattr(
            graph_service,
            "pg_manager",
            type("M", (), {"get_async_session_context": staticmethod(lambda: _AsyncSessionContext(session))})(),
        )
        minio = _FakeMinio()
        monkeypatch.setattr(bib_import, "get_minio_client", lambda: minio)
        yield session, minio
    await engine.dispose()


# --- G2：bib 导入 ----------------------------------------------------------------


async def test_bib_import_parses_dedups_and_maps_citations(env):
    session, _ = env
    first = await bib_import.import_bib(TENANT, BIB_SAMPLE, source_name="wos-export.bib")
    assert first["records"] == 2
    assert first["papers_written"] == 2  # DOI 文献 + 指纹书籍
    assert first["citations_written"] == 1  # CR 中唯一合法 DOI
    papers = {p.rid: p for p in (await session.execute(select(LiteraturePaper))).scalars().all()}
    assert papers["doi-10.1002_adma.202407106"].title == "Starch synthesis in rice"
    # 无 DOI 书籍：标题+年份指纹身份
    book_rids = [rid for rid in papers if rid.startswith("book__nd-")]
    assert len(book_rids) == 1 and papers[book_rids[0]].kind == "book"
    edges = (await session.execute(select(LiteratureCitation))).scalars().all()
    assert edges[0].citing_rid == "doi-10.1002_adma.202407106"
    assert edges[0].cited_doi == "10.1016/j.snb.2022.132616"

    # 幂等：重跑全部跳过
    second = await bib_import.import_bib(TENANT, BIB_SAMPLE)
    assert second["papers_skipped"] == 2 and second["papers_written"] == 0
    assert second["citations_written"] == 0


async def test_markdown_import_creates_fingerprint_identity(env):
    session, minio = env
    result = await bib_import.import_markdown_document(TENANT, markdown="# 我的笔记\n\n正文内容。", title="直导文献")
    assert result["duplicated"] is False
    paper = (
        (await session.execute(select(LiteraturePaper).where(LiteraturePaper.tenant_id == TENANT))).scalars().first()
    )
    assert paper.rid.startswith("nd-")
    assert any(f"/{paper.rid}/markdown-import/" in name and name.endswith("/imported.md") for name, _ in minio.uploads)
    version = paper.reading_version
    again = await bib_import.import_markdown_document(TENANT, markdown="# 我的笔记\n\n正文内容。", title="直导文献")
    assert again["duplicated"] and paper.reading_version == version
    # 空 Markdown 422
    with pytest.raises(ValueError, match="为空"):
        await bib_import.import_markdown_document(TENANT, markdown="   ")


async def test_markdown_import_resumes_after_object_upload_failure(env, monkeypatch):
    session, minio = env
    original = minio.aupload_file

    async def unavailable(**kwargs):
        raise ConnectionError("fixture storage unavailable")

    monkeypatch.setattr(minio, "aupload_file", unavailable)
    with pytest.raises(ConnectionError):
        await bib_import.import_markdown_document(TENANT, markdown="Recoverable scientific reading content")
    monkeypatch.setattr(minio, "aupload_file", original)
    result = await bib_import.import_markdown_document(TENANT, markdown="Recoverable scientific reading content")
    assert result["duplicated"] and result["reader_repaired"]
    paper = (await session.execute(select(LiteraturePaper))).scalar_one()
    assert paper.artifact_pointers["document"] and paper.source_revision_id is None


# --- G1：富化合并语义（离线：三源全失败路径） ---------------------------------------


async def test_enrich_paper_without_doi_fails_closed(env):
    session, _ = env
    await bib_import.import_markdown_document(TENANT, markdown="无 DOI 文献")
    paper = (
        (await session.execute(select(LiteraturePaper).where(LiteraturePaper.tenant_id == TENANT))).scalars().first()
    )
    result = await _enrich_stub(TENANT, paper.rid)
    assert result["ok"] is False and result["reason"] == "no_doi"


async def _enrich_stub(tenant_id, rid):
    """经真实 enrich_paper 但拦截网络（httpx 层不可达 → all_sources_failed）。"""
    import yuxi.literature.enrich as enrich_mod

    async def _net_fail(url, **kwargs):
        return None

    saved_get = enrich_mod._get_json
    enrich_mod._get_json = _net_fail
    try:
        return await enrich_mod.enrich_paper(tenant_id, rid)
    finally:
        enrich_mod._get_json = saved_get


# --- G5：日记（空内容即删除）-------------------------------------------------------


async def test_diary_write_read_delete_cycle(env):
    session, _ = env
    result = await diary.write_diary(TENANT, "u1", "2026-10-01", "今天读了三篇淀粉综述。")
    assert result["deleted"] is False
    got = await diary.get_diary(TENANT, "u1", "2026-10-01")
    assert "淀粉" in got["content"]
    # 清空 → 删除（上游语义：不留空白日记）
    result = await diary.write_diary(TENANT, "u1", "2026-10-01", "")
    assert result["deleted"] is True
    empty = await diary.get_diary(TENANT, "u1", "2026-10-01")
    assert empty["content"] == ""
    # 非法日期 422
    with pytest.raises(ValueError, match="非法日期"):
        await diary.write_diary(TENANT, "u1", "2026-13-99", "x")


# --- G6：卡片（幂等 upsert）-------------------------------------------------------


async def test_card_write_upsert_and_type_guard(env):
    session, _ = env
    await bib_import.import_bib(TENANT, BIB_SAMPLE)
    rid = "doi-10.1002_adma.202407106"
    first = await cards.write_card(
        TENANT, rid, card_type="card-qa", content="Q: 机制？A: OsNF-YB1 调控 [P003]", title="机制问答"
    )
    existing = (await cards.list_cards(TENANT, rid))[0]
    with pytest.raises(cards.CardConflictError):
        await cards.write_card(TENANT, rid, card_type="card-qa", content="更新后的答案", title="机制问答")
    again = await cards.write_card(
        TENANT,
        rid,
        card_type="card-qa",
        content="更新后的答案",
        title="机制问答",
        overwrite=True,
        expected_digest=existing["digest"],
    )
    assert first["slug"] == again["slug"]
    rows = (await session.execute(select(LiteratureCard))).scalars().all()
    assert len(rows) == 1 and rows[0].content == "更新后的答案"
    with pytest.raises(ValueError, match="未知卡片类型"):
        await cards.write_card(TENANT, rid, card_type="card-hack", content="x")
    with pytest.raises(ValueError, match="不能为空"):
        await cards.write_card(TENANT, rid, card_type="card-note", content="  ")


# --- G4：复核裁决（四选一 + 幂等 + 校验）-------------------------------------------


async def test_review_decide_validates_choice_and_text(env):
    """P10-A 契约：字母/语义词皆可；B 不强制 resolved_text（默认取复核项 Paddle 文本）；
    旧语义 C/D 携带 resolved_text 需 legacy_ack（见 test_p10a 契约套件）。"""
    session, _ = env
    ok = await review.decide(TENANT, "sir_r1", report_idx=0, choice="A", decided_by="u1")
    assert ok["choice"] == "A"
    ok2 = await review.decide(TENANT, "sir_r1", report_idx=2, choice="paddleocr", decided_by="u1")
    assert ok2["choice"] == "B"  # 语义词归一为字母
    with pytest.raises(ValueError, match="非法裁决"):
        await review.decide(TENANT, "sir_r1", report_idx=1, choice="E")
    # B 显式 resolved_text（AI 建议接受）仍允许
    await review.decide(TENANT, "sir_r1", report_idx=1, choice="B", resolved_text="Paddle 修正文本")
    await review.decide(TENANT, "sir_r1", report_idx=0, choice="A", decided_by="u1")
    decisions = await review.list_decisions(TENANT, "sir_r1")
    by_idx = {d["report_idx"]: d for d in decisions}
    assert by_idx[0]["choice"] == "A" and by_idx[1]["choice"] == "B"


async def test_dual_channel_requires_paddle_credential(monkeypatch, env):
    monkeypatch.delenv("YUXI_PADDLE_TOKEN", raising=False)
    with pytest.raises(ValueError, match="YUXI_PADDLE_TOKEN"):
        await review.enqueue_dual_channel_render("sir_r1")


# --- G3：图谱 --------------------------------------------------------------------


async def test_graph_network_and_neighbors_shape(env):
    await bib_import.import_bib(TENANT, BIB_SAMPLE)
    network = await graph_service.graph_network(TENANT, limit=100)
    assert network["total_nodes"] >= 1
    rids = {n["rid"] for n in network["nodes"]}
    assert "doi-10.1002_adma.202407106" in rids
    neighbors = await graph_service.graph_neighbors(TENANT, "doi-10.1002_adma.202407106")
    assert neighbors["cited_by_count"] == 0
    assert neighbors["reference_count"] == 1
    top = await graph_service.graph_top(TENANT)
    assert top and top[0]["rid"] == "doi-10.1016_j.snb.2022.132616"


async def test_graph_filters_preview_sources_and_current_l1(env):
    session, _ = env
    for ident, rid, year, external, quartile, reference in (
        (101, "p-a", 2022, 5, "Q1", False),
        (102, "p-b", 2024, 12, "Q2", False),
        (103, "p-c", 2019, 2, "Q1", True),
        (104, "p-d", 2024, 12, "Q2", False),
    ):
        session.add(
            LiteraturePaper(
                id=ident,
                tenant_id=TENANT,
                rid=rid,
                doi=f"10.1234/{rid}",
                title=rid,
                pub_year=year,
                external_cited=external,
                quartile=quartile,
                impact_factor=4.5,
                cocitation_cluster=2,
                is_reference=reference,
            )
        )
    session.add(LiteraturePaper(id=105, tenant_id=999, rid="foreign", title="must-not-count"))
    session.add_all(
        [
            LiteratureCitation(id=1, tenant_id=TENANT, citing_rid="p-a", cited_doi="10.1234/p-b"),
            LiteratureCitation(id=2, tenant_id=TENANT, citing_rid="p-c", cited_doi="10.1234/p-b"),
            LiteratureCitation(id=3, tenant_id=999, citing_rid="foreign", cited_doi="10.1234/p-b"),
            LiteratureCompileJob(
                id=1,
                tenant_id=TENANT,
                paper_id=102,
                level="L1",
                status="SUCCEEDED",
                note_uri="kbasset://bucket/l1.md",
                reading_version=1,
            ),
            LiteratureCompileJob(
                id=2,
                tenant_id=TENANT,
                paper_id=104,
                level="L1",
                status="SUCCEEDED",
                note_uri="kbasset://bucket/stale.md",
                reading_version=0,
            ),
        ]
    )
    await session.commit()
    network = await graph_service.graph_network(TENANT, exclude_isolated=False, citation_source="library")
    nodes = {node["rid"]: node for node in network["nodes"]}
    assert len(nodes) == 4 and nodes["p-b"]["citations"] == 2
    assert nodes["p-b"]["in_kb"] is True and nodes["p-d"]["in_kb"] is False
    filtered = await graph_service.graph_network(TENANT, year_min=2022, exclude_isolated=False)
    assert {node["rid"] for node in filtered["nodes"]} == {"p-a", "p-b", "p-d"}
    assert next(node for node in filtered["nodes"] if node["rid"] == "p-b")["citations"] == 1
    external = await graph_service.graph_network(
        TENANT, citation_source="wos", exclude_isolated=False, sort_by="times_cited", limit=1
    )
    assert external["nodes"][0]["rid"] == "p-b" and external["nodes"][0]["citations"] == 12
    assert external["meta"]["truncated"]
    parameters = dict(
        year_min=2022,
        min_times_cited=4,
        quartiles=["Q1", "Q2"],
        cluster=2,
        min_impact_factor=4,
        exclude_references=True,
        exclude_isolated=True,
    )
    result = await graph_service.graph_network(TENANT, **parameters)
    preview = await graph_service.graph_network(TENANT, preview=True, **parameters)
    assert {node["rid"] for node in result["nodes"]} == {"p-a", "p-b"}
    assert preview["nodes"] == preview["edges"] == []
    assert preview["meta"]["actual_nodes"] == len(result["nodes"]) == 2
    assert preview["meta"]["matched_nodes"] == 3 and preview["meta"]["isolated_removed"] == 1
    assert preview["meta"]["returned_nodes"] == 0
    compiled = await graph_service.graph_network(TENANT, in_kb_only=True, exclude_isolated=False)
    assert [node["rid"] for node in compiled["nodes"]] == ["p-b"]
    cited = await graph_service.graph_network(TENANT, min_library_citations=2, exclude_isolated=False)
    assert [node["rid"] for node in cited["nodes"]] == ["p-b"]


@pytest.mark.parametrize(
    "filters",
    [{"year_min": 2025, "year_max": 2020}, {"quartiles": ["Q9"]}, {"citation_source": "other"}, {"sort_by": "content"}],
)
async def test_graph_rejects_invalid_filters(env, filters):
    with pytest.raises(ValueError):
        await graph_service.graph_network(TENANT, **filters)
