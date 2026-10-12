"""用户真实导出验收：隔离 SQLite 事务，不写生产文献库，不调用模型/OCR。

LITERATURE_EXPORT_SAMPLES 为容器内样本目录；不将原始研究清单提交到 Git。
"""

import hashlib
import os
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.literature import bibliometrics, reference_batches, reference_import
from yuxi.literature.access import _access
from yuxi.storage.postgres.models_business import Tenant
from yuxi.storage.postgres.models_knowledge import (
    LiteratureCitation,
    LiteraturePaper,
    LiteratureReferenceBatch,
    LiteratureReferenceMember,
    LiteratureReferenceRecord,
)
from yuxi.utils.datetime_utils import utc_now

pytestmark = pytest.mark.e2e


async def test_real_four_database_exports_import_catalog_and_statistics(tmp_path, monkeypatch):
    async def store_source(tenant_id, batch_id, data):
        return f"fixture/{tenant_id}/{batch_id}"

    monkeypatch.setattr(reference_import, "_store_source", store_source)
    directory = os.environ.get("LITERATURE_EXPORT_SAMPLES")
    if not directory:
        pytest.skip("E2E 样本目录 LITERATURE_EXPORT_SAMPLES 未配置")
    samples = Path(directory)
    expected = {
        "CNKI.xls": (215, "cnki-xls"),
        "WanFangdata.xlsx": (500, "wanfang-xlsx"),
        "pubmed.txt": (1742, "pubmed-nbib"),
        "wos.csv": (20092, "wos-csv"),
    }
    for name, (count, fmt) in expected.items():
        data = (samples / name).read_bytes()
        records, actual, _ = reference_import.parse_reference_file(data)
        assert (len(records), actual) == (count, fmt)
        assert any(record["year"] for record in records)
        assert any(record["authors"] for record in records)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'exports.sqlite'}")
    async with engine.begin() as conn:
        for model in (Tenant, LiteraturePaper, LiteratureCitation, LiteratureReferenceBatch, LiteratureReferenceMember, LiteratureReferenceRecord):
            await conn.run_sync(model.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        db.add(Tenant(id=1, name="Isolated source acceptance", created_at=utc_now()))
        await db.commit()

        @asynccontextmanager
        async def transaction():
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise

        monkeypatch.setattr(reference_import.pg_manager, "get_async_session_context", transaction)
        monkeypatch.setattr(reference_import.pg_manager, "async_engine", engine)

        async def scope():
            allowed = frozenset((await db.execute(select(LiteraturePaper.rid))).scalars())
            return _access.set(SimpleNamespace(tenant_id=1, rids=allowed, user=SimpleNamespace(uid="fixture-reader")))

        reports = []
        for name, (count, _) in expected.items():
            token = await scope()
            try:
                data = (samples / name).read_bytes()
                report = await reference_import.import_references(1, data, source_name=name)
                assert report["records"] == count
                assert report["papers_written"] + report["papers_skipped"] + report["no_identity"] == count
                assert report["source_sha256"] == hashlib.sha256(data).hexdigest()
                reports.append(report)
            finally:
                _access.reset(token)
        token = await scope()
        try:
            # Every imported canonical member appears in the exact same analysis corpus.
            catalog = await reference_batches.list_records(1, only_imports=True)
            summary = await bibliometrics.bibliometrics(1, only_imports=True)
            unique = await db.scalar(select(func.count()).select_from(LiteraturePaper))
            assert catalog["total"] == summary["overview"]["papers"] == unique
            assert len(catalog["items"]) == 25
            assert unique == 19998  # 审核四份真实导出后锁定的 DOI/PMID/指纹去重结果
            assert sum(row["count"] for row in summary["yearly"]) + summary["overview"]["missing_year"] == unique
            assert len((await reference_batches.list_batches(1))["items"]) == 4
            source_counts = {
                item["database"]: item["papers"] for item in (await reference_batches.list_sources(1))["items"]
            }
            assert source_counts[""] == unique
            database_by_format = {
                "cnki-xls": "cnki",
                "wanfang-xlsx": "wanfang",
                "pubmed-nbib": "pubmed",
                "wos-csv": "wos",
            }
            for report in reports:
                selected = await reference_batches.list_records(1, batch_ids=[report["batch_id"]])
                assert selected["total"] == report["papers_unique"]
                database = database_by_format[report["format_detected"]]
                selected = await reference_batches.list_records(1, database=database)
                assert selected["total"] == source_counts[database] == report["papers_unique"]
            last = await reference_batches.list_records(1, only_imports=True, offset=unique - 10)
            assert len(last["items"]) == 10
            # CSV and merged CSV from the PubMed folder refer to the same 1742 records.
            for name in ("pubmed.csv", "pubmed-merged.csv"):
                report = await reference_import.import_references(1, (samples / name).read_bytes(), source_name=name)
                assert report["papers_written"] == 0 and report["papers_unique"] == 1742
            assert await db.scalar(select(func.count()).select_from(LiteraturePaper)) == unique
        finally:
            _access.reset(token)
    await engine.dispose()
