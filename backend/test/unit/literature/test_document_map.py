from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.literature.deepread_context import extract_bound_file_id
from yuxi.literature.document_map import build_document_map, render_document_map
from yuxi.storage.postgres.models_knowledge import (
    EvidenceAnchorRecord,
    EvidenceSpanRecord,
    KnowledgeFile,
    KnowledgeParseArtifact,
    KnowledgeParseRevision,
)

pytestmark = [pytest.mark.unit]

_REVISION_ID = "rev_0001"
_FILE_ID = "file_rev_0001"
_KB_ID = "kb_1"


def _next_id_factory():
    counter = {"value": 0}

    def _next_id() -> int:
        counter["value"] += 1
        return counter["value"]

    return _next_id


_next_id = _next_id_factory()


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(KnowledgeFile.__table__.create)
        await conn.run_sync(KnowledgeParseRevision.__table__.create)
        await conn.run_sync(KnowledgeParseArtifact.__table__.create)
        await conn.run_sync(EvidenceSpanRecord.__table__.create)
        await conn.run_sync(EvidenceAnchorRecord.__table__.create)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


def _seed_revision(
    session,
    *,
    status: str = "INDEXED_FULL",
    sealed_at=datetime(2026, 1, 1, tzinfo=UTC),
    revision_id: str = _REVISION_ID,
    parser_fingerprint: str = "f" * 64,
    with_file: bool = True,
):
    session.add(
        KnowledgeParseRevision(
            id=_next_id(),
            revision_id=revision_id,
            tenant_id=1,
            kb_id=_KB_ID,
            file_id=_FILE_ID,
            source_sha256="a" * 64,
            parser_fingerprint=parser_fingerprint,
            pipeline_version="v1",
            status=status,
            sealed_at=sealed_at,
        )
    )
    if with_file:
        session.add(
            KnowledgeFile(
                id=_next_id(),
                file_id=_FILE_ID,
                kb_id=_KB_ID,
                filename="paper.pdf",
                original_filename="论文.pdf",
                status="parsed",
                is_folder=False,
            )
        )


def _seed_span(
    session,
    *,
    span_id,
    quote,
    evidence_type="sentence",
    container_label=None,
    document_partition="ABSTRACT",
    page_number=1,
    parse_revision_id=_REVISION_ID,
):
    session.add(
        EvidenceSpanRecord(
            id=_next_id(),
            tenant_id=1,
            parse_revision_id=parse_revision_id,
            kb_id=_KB_ID,
            file_id=_FILE_ID,
            span_id=span_id,
            sentence_index=0,
            quote=quote,
            quote_hash="h" * 64,
            evidence_id=f"ev_{span_id}",
            evidence_type=evidence_type,
            document_partition=document_partition,
            page_number=page_number,
            container_label=container_label,
            metadata_json={},
        )
    )


def _seed_anchor(session, *, anchor_id, anchor_type, document_partition, page):
    session.add(
        EvidenceAnchorRecord(
            id=_next_id(),
            anchor_id=anchor_id,
            parse_revision_id=_REVISION_ID,
            page=page,
            bbox=[0.0, 0.0, 10.0, 10.0],
            word_start=0,
            word_end=5,
            quote_hash="h" * 64,
            prefix_hash="p" * 64,
            suffix_hash="s" * 64,
            quote="anchor quote",
            anchor_type=anchor_type,
            locatable=True,
            document_partition=document_partition,
        )
    )


def test_binding_file_id_present_returns_file_id():
    snapshot = {
        "literature_binding": {
            "rid": "r1",
            "kb_id": _KB_ID,
            "file_id": _FILE_ID,
            "compile_revision_id": "c1",
            "parse_revision_id": _REVISION_ID,
            "binding_revision": 3,
        }
    }
    assert extract_bound_file_id(snapshot) == _FILE_ID


@pytest.mark.asyncio
async def test_build_document_map_partitions_and_figures(db):
    _seed_revision(db)
    _seed_span(
        db,
        span_id="s1",
        quote="We propose a method for rice endosperm analysis.",
        document_partition="ABSTRACT",
        page_number=1,
    )
    _seed_span(
        db, span_id="s2", quote="Grain length increased under drought.", document_partition="RESULTS", page_number=5
    )
    _seed_span(
        db,
        span_id="s3",
        quote="Fig 2 yield comparison",
        evidence_type="caption",
        container_label="Figure 2",
        document_partition="RESULTS",
        page_number=6,
    )
    _seed_span(
        db,
        span_id="s4",
        quote="Table 1 trait values",
        evidence_type="caption",
        container_label="Table 1",
        document_partition="RESULTS",
        page_number=7,
    )
    _seed_span(
        db,
        span_id="s5",
        quote="cell value",
        evidence_type="table_row",
        container_label="Table 1",
        document_partition="RESULTS",
        page_number=7,
    )
    _seed_anchor(db, anchor_id="a1", anchor_type="title", document_partition="TITLE", page=1)
    _seed_anchor(db, anchor_id="a2", anchor_type="header", document_partition="RESULTS", page=5)
    _seed_anchor(db, anchor_id="a3", anchor_type="footer", document_partition="RESULTS", page=7)
    await db.commit()

    document_map = await build_document_map(db, file_id=_FILE_ID)

    assert document_map is not None
    assert document_map["revision_id"] == _REVISION_ID
    # sections aggregate sentence-span page ranges; UNKNOWN/TITLE excluded; nav-ordered
    assert [p["partition"] for p in document_map["sections"]] == ["ABSTRACT", "RESULTS"]
    assert document_map["sections"][0]["page_start"] == 1
    assert document_map["sections"][0]["page_end"] == 1
    assert document_map["sections"][1]["page_start"] == 5
    assert document_map["sections"][1]["page_end"] == 5
    # figures only from caption container_labels (normalized to label strings)
    assert document_map["figures"] == ["Figure 2"]
    # tables dedupe caption + table_row on normalized label
    assert document_map["tables"] == ["Table 1"]
    # page_count is the observed max anchor page
    assert document_map["page_count"] == 7


@pytest.mark.asyncio
async def test_build_document_map_no_terminal_revision_returns_none(db):
    # no revision at all -> None (degrade, never crash)
    assert await build_document_map(db, file_id=_FILE_ID) is None


@pytest.mark.asyncio
async def test_build_document_map_ignores_non_terminal_revision(db):
    _seed_revision(db, status="PARSING", sealed_at=None)
    await db.commit()
    assert await build_document_map(db, file_id=_FILE_ID) is None


@pytest.mark.asyncio
async def test_build_document_map_empty_returns_none(db):
    _seed_revision(db)
    await db.commit()
    # terminal revision exists but has no spans/anchors -> degrade to None
    assert await build_document_map(db, file_id=_FILE_ID) is None


@pytest.mark.asyncio
async def test_build_document_map_prefers_fine_outline(db, monkeypatch):
    # 细章节大纲（来自 unified_article 产物）优先进入返回 dict 与渲染
    async def _fake_outline(_db, *, revision_id):
        assert revision_id == _REVISION_ID
        return ["Introduction", "Methods › Sampling", "Results"]

    monkeypatch.setattr("yuxi.literature.document_map._load_article_outline", _fake_outline)
    _seed_revision(db)
    _seed_span(db, span_id="s1", quote="Background sentence.", document_partition="MAIN_TEXT", page_number=1)
    await db.commit()

    document_map = await build_document_map(db, file_id=_FILE_ID)
    assert document_map is not None
    assert document_map["outline"] == ["Introduction", "Methods › Sampling", "Results"]
    rendered = render_document_map(document_map)
    assert "章节大纲" in rendered
    assert "Methods › Sampling" in rendered


@pytest.mark.asyncio
async def test_build_document_map_outline_failopen_to_coarse(db, monkeypatch):
    # 大纲加载失败（None）→ 回退粗分区地图，不留 outline 占位
    async def _no_outline(_db, *, revision_id):
        return None

    monkeypatch.setattr("yuxi.literature.document_map._load_article_outline", _no_outline)
    _seed_revision(db)
    _seed_span(db, span_id="s1", quote="Result sentence.", document_partition="RESULTS", page_number=5)
    await db.commit()

    document_map = await build_document_map(db, file_id=_FILE_ID)
    assert document_map is not None
    assert document_map["outline"] == []
    rendered = render_document_map(document_map)
    assert "章节大纲" not in rendered
    assert "章节结构" in rendered


def test_render_document_map_none_is_empty():
    assert render_document_map(None) == ""
    assert render_document_map({}) == ""


def test_render_document_map_contains_delimited_navigation_block():
    rendered = render_document_map(
        {
            "revision_id": _REVISION_ID,
            "title": "A rice paper",
            "sections": [{"partition": "RESULTS", "page_start": 5, "page_end": 5}],
            "figures": ["Figure 2"],
            "tables": ["Table 1"],
            "page_count": 7,
        }
    )
    assert "<LITERATURE_DOCUMENT_MAP>" in rendered
    assert "</LITERATURE_DOCUMENT_MAP>" in rendered
    assert "非证据" in rendered
    # navigation-only: never emits an invented per-section summary (anti-hallucination)
    assert "认为" not in rendered
    assert "RESULTS" in rendered
    assert "Figure 2" in rendered
    assert "Table 1" in rendered
    assert "已建立锚点至第 7 页" in rendered


def test_render_document_map_truncates_to_soft_cap():
    rendered = render_document_map(
        {
            "revision_id": _REVISION_ID,
            "title": "T" * 6000,
            "sections": [],
            "figures": [],
            "tables": [],
            "page_count": 1,
        }
    )
    assert len(rendered) < 6400  # soft-capped, never the full 6000-char title


def test_binding_file_id_absent_returns_none():
    assert extract_bound_file_id({"members": []}) is None
    assert extract_bound_file_id({"literature_binding": {}}) is None
    assert extract_bound_file_id({"literature_binding": None}) is None
    assert extract_bound_file_id(None) is None


@pytest.mark.asyncio
async def test_build_document_map_prefers_frozen_binding_revision(db):
    # 质量回归拒绝激活场景：rev_0002 已 sealed 但激活/冻结版仍是 rev_0001。
    # binding 钉死的 parse_revision_id 必须优先于"最新终态"，否则地图与检索
    # 证据跨版本漂移；指定版本无产物时降级 None，不静默回退其他版本。
    _seed_revision(db)
    # 同文件不同 parser_fingerprint 才是合法的第二条 revision（表上有
    # (tenant_id, file_id, parser_fingerprint) 唯一约束，同指纹即复用同条）
    _seed_revision(
        db,
        revision_id="rev_0002",
        parser_fingerprint="e" * 64,
        sealed_at=datetime(2026, 2, 1, tzinfo=UTC),
        with_file=False,
    )
    _seed_span(db, span_id="s1", quote="Grain length increased.", document_partition="RESULTS", page_number=5)
    _seed_span(
        db,
        span_id="s2",
        quote="Fig 2 yield comparison",
        evidence_type="caption",
        container_label="Figure 2",
        document_partition="RESULTS",
        page_number=6,
    )
    _seed_span(
        db,
        span_id="s3",
        parse_revision_id="rev_0002",
        quote="Plants grown in the field.",
        document_partition="METHODS",
        page_number=3,
    )
    _seed_span(
        db,
        span_id="s4",
        parse_revision_id="rev_0002",
        quote="Fig 9 field map",
        evidence_type="caption",
        container_label="Figure 9",
        document_partition="METHODS",
        page_number=4,
    )
    await db.commit()

    bound = await build_document_map(db, file_id=_FILE_ID, parse_revision_id=_REVISION_ID)
    assert bound is not None
    assert bound["revision_id"] == _REVISION_ID
    assert [row["partition"] for row in bound["sections"]] == ["RESULTS"]
    assert bound["figures"] == ["Figure 2"]

    # 不传 binding 版本 → 回退最新终态（rev_0002）
    latest = await build_document_map(db, file_id=_FILE_ID)
    assert latest is not None
    assert latest["revision_id"] == "rev_0002"
    assert [row["partition"] for row in latest["sections"]] == ["METHODS"]
    assert latest["figures"] == ["Figure 9"]

    # binding 版本无产物 → None，不回退
    assert await build_document_map(db, file_id=_FILE_ID, parse_revision_id="rev_missing") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{"file_id": "other-file"}, {"tenant_id": 2}, {"kb_id": "other-kb"}])
async def test_explicit_revision_must_belong_to_requested_source(db, changes):
    _seed_revision(db)
    _seed_span(db, span_id="s1", quote="A result.", document_partition="RESULTS")
    await db.commit()
    args = {"file_id": _FILE_ID, "parse_revision_id": _REVISION_ID, "tenant_id": 1, "kb_id": _KB_ID}
    args.update(changes)
    assert await build_document_map(db, **args) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "digest", "length", "declared_limit", "actual_limit", "shape", "json"])
async def test_outline_blob_is_bounded_verified_and_always_closed(db, monkeypatch, failure):
    from yuxi.literature import document_map
    from yuxi.storage import minio

    # A small budget keeps this entirely offline and avoids a large test fixture.
    monkeypatch.setattr(document_map, "_MAX_ARTICLE_BYTES", 128)
    raw = json.dumps({"sections": [{"path": ["## Methods", "Sampling"]}]}).encode()
    if failure == "shape":
        raw = b'{"sections": "not-a-list"}'
    elif failure == "json":
        raw = b"not json"
    elif failure == "actual_limit":
        raw = b"x" * 200
    declared = 200 if failure == "declared_limit" else (10 if failure == "actual_limit" else len(raw))
    if failure == "length":
        declared += 1
    _seed_revision(db)
    db.add(
        KnowledgeParseArtifact(
            id=_next_id(),
            artifact_id="artifact-outline",
            revision_id=_REVISION_ID,
            kind="unified_article",
            object_uri="minio://fixture/article.json",
            size_bytes=declared,
            sha256="0" * 64 if failure == "digest" else hashlib.sha256(raw).hexdigest(),
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    await db.commit()
    reads, closed, released, downloads = [], [], [], []

    class Response:
        def read(self, amount):
            reads.append(amount)
            return raw[:amount]

        def close(self):
            closed.append(True)

        def release_conn(self):
            released.append(True)

    async def download(bucket, name):
        assert (bucket, name) == ("fixture", "article.json")
        downloads.append(True)
        return Response()

    monkeypatch.setattr(minio, "get_minio_client", lambda: SimpleNamespace(adownload_response=download))
    outline = await document_map._load_article_outline(db, revision_id=_REVISION_ID)
    assert outline == (["Methods › Sampling"] if failure is None else None)
    assert reads == ([] if failure == "declared_limit" else [129])
    assert closed == released == downloads
