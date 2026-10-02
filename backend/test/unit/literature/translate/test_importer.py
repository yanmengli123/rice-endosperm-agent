"""PaperAgent 数据导入器测试（P3）：dry-run 不写库、正式执行幂等、RID 兼容断言。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.literature import importer
from yuxi.storage.postgres.models_knowledge import (
    LiteratureCitation,
    LiteratureCompileJob,
    LiteraturePaper,
    LiteratureVariant,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

TENANT = 7


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
        self.uploads: list[str] = []

    async def aupload_file(self, *, bucket_name, object_name, data, content_type):
        self.uploads.append(object_name)


def _build_paperagent_fixture(root: Path) -> None:
    """最小 PaperAgent 目录：biblio.db（两篇+一引边）+ knowledge_base 一篇。"""
    biblio = root / "data" / "biblio"
    biblio.mkdir(parents=True)
    conn = sqlite3.connect(biblio / "biblio.db")
    conn.execute(
        "CREATE TABLE papers_meta (rid TEXT PRIMARY KEY, kind TEXT, doi TEXT, title TEXT,"
        " paper_rank REAL DEFAULT 0, cocitation_cluster INTEGER DEFAULT 0,"
        " impact_factor REAL, quartile TEXT DEFAULT '', ai_value_score REAL, topic_score REAL)"
    )
    conn.execute("CREATE TABLE citations (citing_doi TEXT, cited_doi TEXT, cited_brief TEXT)")
    conn.execute(
        "INSERT INTO papers_meta VALUES ('doi-10.1002_adma.202407106','paper','10.1002/adma.202407106',"
        "'Starch synthesis',0.02,3,12.5,'Q1',4.5,0.8)"
    )
    conn.execute(
        "INSERT INTO papers_meta VALUES ('doi-10.1016_j.snb.2022.132616','paper','10.1016/j.snb.2022.132616',"
        "'Gas sensor',0.001,0,8.1,'Q2',3.0,0.5)"
    )
    conn.execute("INSERT INTO citations VALUES ('10.1002/adma.202407106','10.1016/j.snb.2022.132616','[12]')")
    conn.commit()
    conn.close()

    kb = root / "knowledge_base" / "10.1002_adma.202407106"
    kb.mkdir(parents=True)
    (kb / "zh.md").write_text("# 中文译文\n\n淀粉合成研究。", encoding="utf-8")
    (kb / "_note.md").write_text("---\ntype: paper-note\n---\n# 笔记", encoding="utf-8")


@pytest_asyncio.fixture
async def env(monkeypatch, tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for model in (LiteraturePaper, LiteratureCitation, LiteratureVariant, LiteratureCompileJob):
            await conn.run_sync(model.__table__.create)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        monkeypatch.setattr(importer.pg_manager, "get_async_session_context", lambda: _AsyncSessionContext(session))
        minio = _FakeMinio()
        monkeypatch.setattr(importer, "get_minio_client", lambda: minio)
        fixture_root = tmp_path / "paperagent"
        _build_paperagent_fixture(fixture_root)
        yield session, minio, fixture_root
    await engine.dispose()


async def test_dry_run_writes_nothing(env):
    session, minio, fixture_root = env
    report = await importer.import_paperagent_data(tenant_id=TENANT, paperagent_root=fixture_root, dry_run=True)
    assert report.dry_run is True
    assert report.papers_source == 2
    assert report.papers_written == 0
    assert report.variants_source == 1
    assert (await session.execute(select(LiteraturePaper))).scalars().all() == []
    assert minio.uploads == []


async def test_import_is_idempotent_and_maps_identity(env):
    session, minio, fixture_root = env
    first = await importer.import_paperagent_data(tenant_id=TENANT, paperagent_root=fixture_root, dry_run=False)
    assert first.papers_written == 2
    assert first.citations_written == 1
    assert first.variants_written == 1
    assert not first.conflicts, "doi 行目录名必须与 doi_to_dirname 重建一致（P0 身份契约变现）"

    papers = {p.rid: p for p in (await session.execute(select(LiteraturePaper))).scalars().all()}
    assert set(papers) == {"doi-10.1002_adma.202407106", "doi-10.1016_j.snb.2022.132616"}
    assert papers["doi-10.1002_adma.202407106"].impact_factor == 12.5
    assert papers["doi-10.1002_adma.202407106"].quartile == "Q1"
    # 引用边：citing DOI → RID 归一
    edges = (await session.execute(select(LiteratureCitation))).scalars().all()
    assert len(edges) == 1 and edges[0].citing_rid == "doi-10.1002_adma.202407106"
    # 变体 lang 进身份
    variants = (await session.execute(select(LiteratureVariant))).scalars().all()
    assert variants[0].lang == "zh" and variants[0].variant_kind == "zh"
    # MinIO：译文 + 笔记
    assert any(name.endswith("/variants/10.1002_adma.202407106/zh.md") for name in minio.uploads) or any(
        name.endswith("10.1002_adma.202407106/zh.md") for name in minio.uploads
    )
    assert any(name.endswith("/L1_note.md") for name in minio.uploads)

    # 重跑幂等：不新增行
    second = await importer.import_paperagent_data(tenant_id=TENANT, paperagent_root=fixture_root, dry_run=False)
    assert second.papers_written == 2  # upsert 覆盖，不新增
    edges_again = (await session.execute(select(LiteratureCitation))).scalars().all()
    assert len(edges_again) == 1
    variants_again = (await session.execute(select(LiteratureVariant))).scalars().all()
    assert len(variants_again) == 1


async def test_import_requires_biblio_db(tmp_path):
    with pytest.raises(ValueError, match="biblio.db"):
        await importer.import_paperagent_data(tenant_id=TENANT, paperagent_root=tmp_path, dry_run=True)
