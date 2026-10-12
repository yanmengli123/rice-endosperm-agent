"""真实事务题录→批次→清单/计量共用范围；SQLite 开启 FK，不依赖真实用户数据。"""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.literature import bibliometrics, reference_batches, reference_import
from yuxi.literature.access import _access, paper_visibility
from yuxi.storage.postgres.models_business import Tenant
from yuxi.storage.postgres.models_knowledge import (
    LiteratureCitation,
    LiteraturePaper,
    LiteratureReferenceBatch,
    LiteratureReferenceMember,
    LiteratureReferenceRecord,
)
from yuxi.utils.datetime_utils import utc_now

pytestmark = pytest.mark.unit


@pytest_asyncio.fixture
async def env(monkeypatch):
    async def store_source(tenant_id, batch_id, data):
        return f"fixture/{tenant_id}/{batch_id}"

    monkeypatch.setattr(reference_import, "_store_source", store_source)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as conn:
        for model in (Tenant, LiteraturePaper, LiteratureCitation, LiteratureReferenceBatch, LiteratureReferenceMember, LiteratureReferenceRecord):
            await conn.run_sync(model.__table__.create)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        db.add_all([Tenant(id=1, name="A", created_at=utc_now()), Tenant(id=2, name="B", created_at=utc_now())])
        await db.commit()

        @asynccontextmanager
        async def transaction():
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise

        @asynccontextmanager
        async def scope(uid="alice", tenant=1, allowed=None):
            rids = set(
                (await db.execute(select(LiteraturePaper.rid).where(LiteraturePaper.tenant_id == tenant))).scalars()
            )
            token = _access.set(
                SimpleNamespace(
                    tenant_id=tenant,
                    rids=frozenset(rids if allowed is None else allowed),
                    user=SimpleNamespace(uid=uid),
                )
            )
            try:
                yield
            finally:
                _access.reset(token)

        monkeypatch.setattr(reference_import.pg_manager, "get_async_session_context", transaction)
        monkeypatch.setattr(reference_import.pg_manager, "async_engine", engine)
        yield SimpleNamespace(db=db, scope=scope)
    await engine.dispose()


async def test_import_receipt_is_idempotent_and_combined_members_are_distinct(env):
    data = b"PMID- 11111\nTI  - Rice A\nDP  - 2024\nAID - 10.1000/a [doi]\n\nPMID- 22222\nTI  - Rice B\nDP  - 2025\n"
    async with env.scope():
        first = await reference_import.import_references(1, data, source_name="a.nbib")
    async with env.scope():
        repeated = await reference_import.import_references(1, data, source_name="renamed.nbib")
        second = await reference_import.import_references(
            1,
            b"PMID- 11111\nTI  - Rice A\nAID - 10.1000/a [doi]\n\nPMID- 33333\nTI  - Rice C\nDP  - 2025\n",
            source_name="b.nbib",
        )
    assert first["batch_id"] == repeated["batch_id"]
    assert (repeated["papers_written"], repeated["papers_unique"]) == (0, 2)
    assert await env.db.scalar(select(func.count()).select_from(LiteratureReferenceBatch)) == 2
    assert await env.db.scalar(select(func.count()).select_from(LiteratureReferenceMember)) == 4
    assert await env.db.scalar(select(func.count()).select_from(LiteratureReferenceRecord)) == 4
    env.db.add(LiteraturePaper(tenant_id=1, rid="legacy", title="Not in the upload", pub_year=1990))
    await env.db.commit()
    async with env.scope():
        rows = await reference_batches.list_records(1, only_imports=True, limit=2)
        overview = await bibliometrics.bibliometrics(1, only_imports=True)
        assert rows["total"] == overview["overview"]["papers"] == 3
        assert len(rows["items"]) == 2
        assert (await reference_batches.list_records(1, batch_ids=[second["batch_id"]]))["total"] == 2
        assert (await reference_batches.list_records(1))["total"] == 4
        assert len((await reference_batches.list_batches(1))["items"]) == 2


async def test_batch_owner_tenant_and_current_pdf_acl_are_rechecked(env):
    data = b"PMID- 11111\nTI  - Rice\nDP  - 2024\n"
    async with env.scope():
        report = await reference_import.import_references(1, data, source_name="private-filename.nbib")
    async with env.scope(uid="bob"):
        assert (await reference_batches.list_batches(1))["items"] == []
        assert (await reference_batches.list_records(1, only_imports=True))["total"] == 0
        with pytest.raises(PermissionError):
            await reference_batches.list_records(1, batch_ids=[report["batch_id"]])
    async with env.scope(tenant=2):
        with pytest.raises(PermissionError):
            await reference_batches.list_records(2, batch_ids=[report["batch_id"]])
    async with env.scope(allowed=set()):
        assert (await reference_batches.list_records(1, only_imports=True))["total"] == 0
        assert (await bibliometrics.bibliometrics(1, only_imports=True))["overview"]["papers"] == 0


async def test_database_lists_include_history_and_shared_members_without_first_source_bias(env):
    env.db.add_all(
        [
            LiteraturePaper(tenant_id=1, rid="old-cnki", title="Old CNKI", source_main="cnki-xls"),
            LiteraturePaper(tenant_id=1, rid="old-other", title="Old uncategorized"),
        ]
    )
    await env.db.commit()
    async with env.scope():
        await reference_import.import_references(
            1, b"PMID- 12345\nTI  - Rice\nAID - 10.1000/rice [doi]\nOT  - rice\nOT  - grain\n"
        )
    async with env.scope():
        await reference_import.import_references(1, b"Publication Type,Article Title,DOI\nJ,Rice,10.1000/rice\n")
    async with env.scope():
        counts = {item["database"]: item["papers"] for item in (await reference_batches.list_sources(1))["items"]}
        assert counts == {"": 3, "pubmed": 1, "wos": 1, "cnki": 1, "wanfang": 0, "other": 1}
        assert (await reference_batches.list_records(1))["total"] == 3
        assert (await reference_batches.list_records(1, database="cnki"))["items"][0]["rid"] == "old-cnki"
        wos = await reference_batches.list_records(1, database="wos")
        assert wos["total"] == 1 and wos["items"][0]["source"] == "pubmed-nbib"
        assert (await bibliometrics.bibliometrics(1, database="wos"))["overview"]["papers"] == 1
        assert (await bibliometrics.metric_network(1, field="keywords", database="wos", min_pair=1))["links"][0][
            "weight"
        ] == 1
        with pytest.raises(ValueError, match="数据库"):
            await reference_batches.list_records(1, database="invalid")
    async with env.scope(uid="bob"):
        # A different actor cannot learn membership of Alice's private WoS upload.
        assert (await reference_batches.list_records(1, database="wos"))["total"] == 0
    async with env.scope(allowed={"old-cnki"}):
        counts = {item["database"]: item["papers"] for item in (await reference_batches.list_sources(1))["items"]}
        assert counts[""] == counts["cnki"] == 1 and counts["wos"] == 0


async def test_all_historical_batches_remain_reachable_after_first_page(env):
    timestamp = utc_now()
    env.db.add_all(
        [
            LiteratureReferenceBatch(
                tenant_id=1,
                batch_id=f"{index:064x}",
                created_by="alice",
                source_name=f"history-{index}.csv",
                source_sha256=f"{index:064x}",
                format="pubmed-csv",
                report_json={"papers_unique": 0},
                created_at=timestamp,
            )
            for index in range(52)
        ]
    )
    await env.db.commit()
    async with env.scope():
        first = await reference_batches.list_batches(1)
        second = await reference_batches.list_batches(1, offset=50)
        assert len(first["items"]) == 50 and first["has_more"]
        assert len(second["items"]) == 2 and not second["has_more"]
        assert len({row["batch_id"] for row in first["items"] + second["items"]}) == 52
    async with env.scope(uid="bob"):
        assert (await reference_batches.list_batches(1, offset=50))["items"] == []


async def test_csv_then_complete_medline_fills_only_missing_metadata_and_respects_lock(env):
    csv = b"PMID,Title,Authors,Journal/Book,Publication Year,DOI\n12345,Rice,Li W,Rice Science,2024,10.1000/rice\n"
    full = b"PMID- 12345\nTI  - Rice\nDP  - 2024\nAB  - Full abstract\nOT  - grain filling\nAID - 10.1000/rice [doi]\n"
    async with env.scope():
        await reference_import.import_references(1, csv)
    async with env.scope():
        report = await reference_import.import_references(1, full)
    [paper] = (await env.db.execute(select(LiteraturePaper))).scalars().all()
    assert report["metadata_enriched"] == 1
    assert paper.abstract == "Full abstract" and paper.keywords_json == ["grain filling"]
    assert paper.authors_json == ["Li W"]
    paper.abstract = None
    paper.meta_locked = True
    await env.db.commit()
    async with env.scope():
        report = await reference_import.import_references(1, full)
    assert report["metadata_enriched"] == 0 and paper.abstract is None


async def test_year_and_literal_search_filters_match_list_and_network(env):
    data = (
        b"PMID- 11111\nTI  - Rice% method\nDP  - 2024\nOT  - rice\nOT  - grain\n\n"
        b"PMID- 22222\nTI  - Rice other\nDP  - 2025\nOT  - rice\nOT  - grain\n"
    )
    async with env.scope():
        await reference_import.import_references(1, data)
    async with env.scope():
        filters = {"only_imports": True, "query": "%", "year_from": 2024, "year_to": 2024}
        rows = await reference_batches.list_records(1, **filters)
        overview = await bibliometrics.bibliometrics(1, **filters)
        network = await bibliometrics.metric_network(1, field="keywords", min_pair=1, **filters)
        assert rows["total"] == overview["overview"]["papers"] == 1
        assert rows["items"][0]["title"] == "Rice% method"
        assert network["links"][0]["weight"] == 1
        with pytest.raises(ValueError, match="起始年份"):
            await reference_batches.list_records(1, year_from=2025, year_to=2024)


async def test_new_doi_alias_is_persisted_after_pmid_only_import(env):
    async with env.scope():
        await reference_import.import_references(1, b"PMID- 12345\nTI  - Rice\nDP  - 2024\n")
    async with env.scope():
        await reference_import.import_references(1, b"PMID- 12345\nTI  - Rice\nAID - 10.1000/rice [doi]\n")
    async with env.scope():
        report = await reference_import.import_references(1, b"TY  - JOUR\nTI  - Rice\nDO  - 10.1000/rice\nER  -\n")
    assert report["papers_written"] == 0
    assert await env.db.scalar(select(func.count()).select_from(LiteraturePaper)) == 1
    [paper] = (await env.db.execute(select(LiteraturePaper))).scalars().all()
    assert paper.doi == "10.1000/rice" and paper.rid.startswith("nd-")


@pytest.mark.parametrize("meta_locked", [False, True])
@pytest.mark.parametrize("pmid,doi", [("12345", "10.1000/different"), ("99999", "10.1000/rice")])
async def test_single_explicit_match_cannot_contradict_other_populated_identity(env, meta_locked, pmid, doi):
    async with env.scope():
        await reference_import.import_references(1, b"PMID- 12345\nTI  - Rice\nAID - 10.1000/rice [doi]\n")
    [paper] = (await env.db.execute(select(LiteraturePaper))).scalars().all()
    paper.meta_locked = meta_locked
    await env.db.commit()
    data = (f"PMID- 88888\nTI  - Pending row must roll back\n\nPMID- {pmid}\nTI  - Rice\nAID - {doi} [doi]\n").encode()
    async with env.scope():
        with pytest.raises(ValueError, match="身份.*冲突"):
            await reference_import.import_references(1, data)
    [unchanged] = (await env.db.execute(select(LiteraturePaper))).scalars().all()
    assert (unchanged.pmid, unchanged.doi, unchanged.meta_locked) == ("12345", "10.1000/rice", meta_locked)
    assert await env.db.scalar(select(func.count()).select_from(LiteratureReferenceBatch)) == 1


def test_large_postgres_acl_uses_one_array_without_widening_visibility(monkeypatch):
    from yuxi.literature.access import pg_manager

    monkeypatch.setattr(pg_manager, "async_engine", SimpleNamespace(dialect=SimpleNamespace(name="postgresql")))
    token = _access.set(SimpleNamespace(tenant_id=1, rids=frozenset(f"rid-{index}" for index in range(50000))))
    try:
        compiled = paper_visibility(1).compile(dialect=postgresql.dialect())
        assert "ANY" in str(compiled)
        assert len(compiled.params) == 2
        assert len(next(value for value in compiled.params.values() if isinstance(value, list))) == 50000
        forbidden = paper_visibility(2).compile(dialect=postgresql.dialect())
        assert next(value for value in forbidden.params.values() if isinstance(value, list)) == []
    finally:
        _access.reset(token)


async def test_explicit_pmid_beats_different_title_fingerprint_without_merging_old_rows(env):
    async with env.scope():
        await reference_import.import_references(1, b"PMID- 12345\nTI  - Rice.\nDP  - 2024\n")
        await reference_import.import_references(1, b"TY  - JOUR\nTI  - Rice\nPY  - 2024\nER  -\n")
    async with env.scope():
        report = await reference_import.import_references(
            1, b"PMID,Title,Authors,Journal/Book,Publication Year\n12345,Rice,Li,Rice,2024\n"
        )
        selected = await reference_batches.list_records(1, batch_ids=[report["batch_id"]])
    assert report["papers_written"] == 0 and selected["items"][0]["pmid"] == "12345"
    assert await env.db.scalar(select(func.count()).select_from(LiteraturePaper)) == 2
    async with env.scope():
        with pytest.raises(ValueError, match="指纹.*冲突"):
            await reference_import.import_references(1, b"PMID- 99999\nTI  - Rice.\nDP  - 2024\n")
