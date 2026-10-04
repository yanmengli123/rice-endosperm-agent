"""PaperAgent 数据导入器测试（P3）：dry-run 不写库、正式执行幂等、RID 兼容断言。"""

from __future__ import annotations

import sqlite3
import json
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.literature import importer
from yuxi.storage.postgres.models_knowledge import (
    LiteratureCitation,
    LiteratureCompileJob,
    LiteratureNoteSearch,
    LiteraturePaper,
    LiteratureVariant,
    KnowledgeBase,
    KnowledgeFile,
    KnowledgeParseRevision,
    LiteratureReaderRender,
    LiteratureArtifactRevision,
    KnowledgeWiki,
    WikiPublication,
    WikiSourceBinding,
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
        self.files = {}

    async def aupload_file(self, *, bucket_name, object_name, data, content_type):
        self.uploads.append(object_name)
        self.files[object_name] = bytes(data)

    async def adownload_file(self, bucket, name):
        return self.files[name]


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
        for model in (
            LiteraturePaper,
            LiteratureCitation,
            LiteratureVariant,
            LiteratureCompileJob,
            LiteratureNoteSearch,
            KnowledgeBase,
            KnowledgeFile,
            KnowledgeParseRevision,
            LiteratureReaderRender,
            LiteratureArtifactRevision,
            KnowledgeWiki,
            WikiPublication,
            WikiSourceBinding,
        ):
            await conn.run_sync(model.__table__.create)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        monkeypatch.setattr(importer.pg_manager, "get_async_session_context", lambda: _AsyncSessionContext(session))
        minio = _FakeMinio()
        monkeypatch.setattr(importer, "get_minio_client", lambda: minio)

        async def publish(*args, **kwargs):
            return {"status": "NO_BINDING"}

        monkeypatch.setattr("yuxi.literature.notes.wiki_publish.publish_literature_notes", publish)
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
    assert any("/variants/10.1002_adma.202407106/" in name and name.endswith("/zh.md") for name in minio.uploads)
    assert any(name.endswith("/L1_note.md") for name in minio.uploads)

    # 重跑幂等：不新增行
    uploaded = len(minio.uploads)
    second = await importer.import_paperagent_data(tenant_id=TENANT, paperagent_root=fixture_root, dry_run=False)
    assert second.papers_written == 0 and second.papers_skipped == 2
    assert second.variants_written == 0 and len(minio.uploads) == uploaded
    edges_again = (await session.execute(select(LiteratureCitation))).scalars().all()
    assert len(edges_again) == 1
    variants_again = (await session.execute(select(LiteratureVariant))).scalars().all()
    assert len(variants_again) == 1


async def test_import_requires_biblio_db(tmp_path):
    with pytest.raises(ValueError, match="biblio.db"):
        await importer.import_paperagent_data(tenant_id=TENANT, paperagent_root=tmp_path, dry_run=True)


async def test_import_prefers_final_document_and_preserves_local_freeze(env):
    from yuxi.literature import artifacts

    session, minio, root = env
    dirname = "10.1002_adma.202407106"
    rid = f"doi-{dirname}"
    library = root / "library" / dirname
    library.mkdir(parents=True)
    kb = root / "knowledge_base" / dirname
    original = {"paragraphs": [{"text_en": "Uncorrected library text."}]}
    final = {"paragraphs": [{"para_id": "P009", "text_en": "Final corrected knowledge document."}]}
    (library / "document.json").write_text(json.dumps(original), encoding="utf-8")
    (kb / "document.json").write_text(json.dumps(final), encoding="utf-8")
    report = await importer.import_paperagent_data(tenant_id=TENANT, paperagent_root=root, dry_run=False)
    assert report.reader_renders_written == 1
    document = await artifacts.resolve_effective_document(TENANT, rid, storage=minio)
    assert json.loads(document["content"])["paragraphs"][0]["text_en"] == final["paragraphs"][0]["text_en"]
    assert document["frozen"]
    saved = await artifacts.store_revision(
        TENANT,
        rid,
        "document",
        content=json.dumps(
            {"paragraphs": [{"para_id": "P009", "text_en": "Local human revision must survive migration."}]}
        ).encode(),
        content_type="application/json",
        operator="u1",
        freeze=True,
        storage=minio,
    )
    paper = (await session.execute(select(LiteraturePaper).where(LiteraturePaper.rid == rid))).scalar_one()
    before = (paper.source_revision_id, paper.reading_version)
    (kb / "document.json").write_text(json.dumps(original), encoding="utf-8")
    again = await importer.import_paperagent_data(tenant_id=TENANT, paperagent_root=root, dry_run=False)
    assert any("保留已冻结" in item for item in again.conflicts)
    assert (paper.source_revision_id, paper.reading_version) == before
    document = await artifacts.resolve_effective_document(TENANT, rid, storage=minio)
    assert document["revision_id"] == saved["revision_id"]
    assert json.loads(document["content"])["paragraphs"][0]["text_en"].startswith("Local human")
    assert again.notes_written == 0 and again.variants_written == 0


@pytest.mark.parametrize("snapshots", [1, 2])
async def test_registered_trash_snapshot_restores_core_without_guessing(env, snapshots):
    session, storage, root = env
    dirname = "10.1002_adma.202407106"
    kb = root / "knowledge_base" / dirname
    destination = root / "knowledge_base/.trash" / (dirname + "__20261003123000")
    destination.parent.mkdir()
    kb.rename(destination)
    body = {"paragraphs": [{"para_id": "P009", "text_en": "Archived corrected knowledge document."}]}
    (destination / "document.json").write_text(json.dumps(body), encoding="utf-8")
    with sqlite3.connect(root / "data/biblio/biblio.db") as conn:
        conn.execute("CREATE TABLE kb_trash (key TEXT, dirname TEXT)")
        conn.execute("INSERT INTO kb_trash VALUES (?, ?)", (f"doi-{dirname}", dirname))
    if snapshots == 2:
        duplicate = destination.parent / dirname
        duplicate.mkdir()
        (duplicate / "document.json").write_text(json.dumps(body), encoding="utf-8")
    preview = await importer.import_paperagent_data(tenant_id=TENANT, paperagent_root=root, dry_run=True)
    report = await importer.import_paperagent_data(tenant_id=TENANT, paperagent_root=root, dry_run=False)
    assert preview.reader_renders_written == report.reader_renders_written == int(snapshots == 1)
    assert report.variants_written == int(snapshots == 1)
    assert report.notes_written == int(snapshots == 1)
    if snapshots == 1:
        projection = (await session.execute(select(LiteratureNoteSearch))).scalar_one()
        assert "笔记" in projection.body
        assert any(name.endswith("document.json") for name in storage.uploads)
