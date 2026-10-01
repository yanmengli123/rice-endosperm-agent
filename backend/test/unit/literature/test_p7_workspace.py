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
)

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

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
    assert any(name.endswith(f"/{paper.rid}/imported.md") for name, _ in minio.uploads)
    # 空 Markdown 422
    with pytest.raises(ValueError, match="为空"):
        await bib_import.import_markdown_document(TENANT, markdown="   ")


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
    again = await cards.write_card(TENANT, rid, card_type="card-qa", content="更新后的答案", title="机制问答")
    assert first["slug"] == again["slug"]
    rows = (await session.execute(select(LiteratureCard))).scalars().all()
    assert len(rows) == 1 and rows[0].content == "更新后的答案"
    with pytest.raises(ValueError, match="未知卡片类型"):
        await cards.write_card(TENANT, rid, card_type="card-hack", content="x")
    with pytest.raises(ValueError, match="不能为空"):
        await cards.write_card(TENANT, rid, card_type="card-note", content="  ")


# --- G4：复核裁决（四选一 + 幂等 + 校验）-------------------------------------------


async def test_review_decide_validates_choice_and_text(env):
    session, _ = env
    ok = await review.decide(TENANT, "sir_r1", report_idx=0, choice="A", decided_by="u1")
    assert ok["choice"] == "A"
    with pytest.raises(ValueError, match="非法裁决"):
        await review.decide(TENANT, "sir_r1", report_idx=1, choice="E")
    # B/C/D 需要结果文本（裁决语义）
    with pytest.raises(ValueError, match="resolved_text"):
        await review.decide(TENANT, "sir_r1", report_idx=1, choice="B")
    await review.decide(TENANT, "sir_r1", report_idx=1, choice="B", resolved_text="Paddle 修正文本")
    # 幂等覆盖
    await review.decide(TENANT, "sir_r1", report_idx=0, choice="C", resolved_text="合并")
    decisions = await review.list_decisions(TENANT, "sir_r1")
    by_idx = {d["report_idx"]: d for d in decisions}
    assert by_idx[0]["choice"] == "C" and by_idx[1]["choice"] == "B"


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
