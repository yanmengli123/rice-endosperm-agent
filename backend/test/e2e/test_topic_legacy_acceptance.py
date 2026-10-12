"""User's four exports + existing result ZIP, isolated DB/objects, no vendors."""

import os
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.literature import reference_import, topic_analysis, topic_legacy
from yuxi.literature.access import _access
from yuxi.storage.postgres.models_business import Tenant
from yuxi.storage.postgres.models_knowledge import (
    LiteratureCitation,
    LiteraturePaper,
    LiteratureReferenceBatch,
    LiteratureReferenceMember,
    LiteratureReferenceRecord,
    LiteratureTopicRun,
    LiteratureTopicDocument,
)
from yuxi.utils.datetime_utils import utc_now

pytestmark = pytest.mark.e2e


async def test_four_database_history_maps_to_canonical_papers_without_losing_occurrences(tmp_path, monkeypatch):
    root = os.environ.get("LITERATURE_TOPIC_SAMPLES")
    if not root:
        pytest.skip("用户真实样本目录 LITERATURE_TOPIC_SAMPLES 未配置")
    root = Path(root)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'topics.sqlite'}")
    async with engine.begin() as conn:
        for model in (
            Tenant,
            LiteraturePaper,
            LiteratureCitation,
            LiteratureReferenceBatch,
            LiteratureReferenceMember,
            LiteratureReferenceRecord,
            LiteratureTopicRun,
            LiteratureTopicDocument,
        ):
            await conn.run_sync(model.__table__.create)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        db.add(Tenant(id=1, name="Isolated topic acceptance", created_at=utc_now()))
        await db.commit()

    @asynccontextmanager
    async def transaction():
        async with sessions() as db:
            try:
                yield db
            except BaseException:
                await db.rollback()
                raise

    @asynccontextmanager
    async def scope(allowed=None):
        async with sessions() as db:
            rids = frozenset((await db.execute(select(LiteraturePaper.rid))).scalars())
        token = _access.set(
            SimpleNamespace(
                tenant_id=1, rids=rids if allowed is None else allowed, user=SimpleNamespace(uid="fixture-owner")
            )
        )
        try:
            yield
        finally:
            _access.reset(token)

    objects = {}

    class MemoryStore:
        async def aupload_file(self, bucket, key, data, content_type=None):
            objects[key] = data

        async def adownload_file(self, bucket, key):
            return objects[key]

    async def store_source(*args):
        return "fixture/source"

    monkeypatch.setattr(topic_analysis.pg_manager, "get_async_session_context", transaction)
    monkeypatch.setattr(topic_analysis.pg_manager, "async_engine", engine)
    monkeypatch.setattr(topic_analysis, "get_minio_client", MemoryStore)
    monkeypatch.setattr(reference_import, "_store_source", store_source)
    for filename, expected in (
        ("CNKI.xls", 215),
        ("WanFangdata.xlsx", 500),
        ("pubmed_merged_bertopic.csv", 1742),
        ("wos.csv", 20092),
    ):
        async with scope():
            report = await reference_import.import_references(1, (root / filename).read_bytes(), source_name=filename)
            assert report["source_records"] == expected
    async with scope():
        assert (await topic_analysis.preview(1, mode="records"))["documents"] == 22549
        with (root / "four_database_bertopic_results.zip").open("rb") as stream:
            imported = await topic_legacy.import_archive(1, stream)
        run_id = imported["run_id"]
        assert imported["status"] == "SUCCEEDED" and imported["has_embeddings"]
        assert imported["progress"]["documents"] == 22549
        joint = imported["models"]["joint_bge_m3"]
        assert len(joint["topics"]) == 42 and joint["outliers"] == 8562
        assert joint["language_concentration"]["zh"]["documents"] == 715
        assert len(imported["models"]["chinese_bge_zh"]["topics"]) == 20
        assert joint["hierarchy"] and joint["topics"][0]["word_frequencies"]
        page = await topic_analysis.result_view(1, run_id, model="joint_bge_m3", offset=22525)
        assert page["total"] == 22549 and len(page["items"]) == 24
        assert len((await topic_analysis.list_runs(1))["items"]) == 1
        exported = await topic_analysis.export_run(1, run_id, model="joint_bge_m3")
        assert exported.startswith(b"\xef\xbb\xbf")
    async with scope(allowed=frozenset()):
        with pytest.raises(PermissionError):
            await topic_analysis.get_run(1, run_id)
    await engine.dispose()
