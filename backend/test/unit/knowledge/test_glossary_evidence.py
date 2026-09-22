from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.knowledge.evidence.glossary import (
    extract_glossary_terms,
    fold_term_key,
    normalize_glossary_key,
    query_glossary_for_scope,
)
from yuxi.storage.postgres.models_business import Base
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeCanonicalAlias,
    KnowledgeCanonicalRecord,
    KnowledgeRelease,
)


def test_glossary_term_extraction_and_normalization_are_deterministic():
    assert extract_glossary_terms("术语PCR含义是什么") == ["PCR"]
    assert extract_glossary_terms('what does "CRISPR" stand for?') == ["CRISPR"]
    assert normalize_glossary_key("  ＰＣＲ  ") == "pcr"


def test_glossary_term_extraction_prefers_multiword_phrase_over_fragments():
    # 复合术语作为整体短语优先于单词碎片（frame shift 不再被拆成 frame/shift 独立探测）
    assert extract_glossary_terms("frame shift是什么意思") == ["frame shift", "frame", "shift"]
    assert extract_glossary_terms("what does frame shift stand for?") == ["frame shift", "frame", "shift"]
    # 停用词只修剪短语边缘：句中疑问词被剔除，术语 token 保留
    assert extract_glossary_terms("reading frame shift 是什么意思") == [
        "reading frame shift",
        "reading",
        "frame",
        "shift",
    ]


def test_fold_term_key_collapses_orthographic_variants():
    assert fold_term_key("frameshift") == fold_term_key("frame-shift") == fold_term_key("frame shift")
    assert fold_term_key("reading-frame shift") == "readingframeshift"
    assert fold_term_key("　FRAME　shift ") == fold_term_key("frameshift")
    # 非拉丁术语折叠恒等，中文词条不受影响
    assert fold_term_key("聚合酶链式反应") == "聚合酶链式反应"


def _seed_glossary(db, records: list[dict], release_id: str = "rel_active", revision_id: str = "rev_active") -> None:
    db.add_all(
        [
            KnowledgeRelease(
                id=1,
                release_id=release_id,
                kb_id="kb_glossary",
                tenant_id=7,
                contract_ref="glossary@1.0.0",
                manifest_hash="a" * 64,
                manifest_json={"sources": [{"dataset_revision_id": revision_id}]},
                status="ACTIVE",
                source_count=1,
            ),
            *[
                KnowledgeCanonicalRecord(
                    id=index,
                    record_id=item["record_id"],
                    revision_id=revision_id,
                    kb_id="kb_glossary",
                    tenant_id=7,
                    record_key=item["record_key"],
                    normalized_key=item["normalized_key"],
                    fold_key=item.get("fold_key", ""),
                    row_number=item.get("row_number", index + 1),
                    fields_json={"term": item["record_key"]},
                    projection_text=item["projection_text"],
                    projection_hash=item["projection_hash"],
                )
                for index, item in enumerate(records, start=1)
            ],
            *[
                KnowledgeCanonicalAlias(
                    id=index,
                    revision_id=revision_id,
                    record_id=item["record_id"],
                    alias=item["alias"],
                    normalized_alias=item["normalized_alias"],
                    fold_key=item.get("fold_key", ""),
                )
                for index, item in enumerate(
                    [alias for record in records for alias in record.get("aliases") or []], start=1
                )
            ],
        ]
    )


_GLOSSARY_SCOPE = {
    "tenant_id": 7,
    "members": [
        {
            "kb_id": "kb_glossary",
            "contract_key": "glossary",
            "structured_enabled": True,
            "governance_status": "PUBLISHED",
            "active_release_id": "rel_active",
        }
    ],
}


@pytest.mark.asyncio
async def test_glossary_lookup_reads_only_active_release_revision():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn,
                tables=[
                    KnowledgeCanonicalRecord.__table__,
                    KnowledgeCanonicalAlias.__table__,
                    KnowledgeRelease.__table__,
                ],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        db.add_all(
            [
                KnowledgeRelease(
                    id=1,
                    release_id="rel_active",
                    kb_id="kb_glossary",
                    tenant_id=7,
                    contract_ref="glossary@1.0.0",
                    manifest_hash="a" * 64,
                    manifest_json={"sources": [{"dataset_revision_id": "rev_active"}]},
                    status="ACTIVE",
                    source_count=1,
                ),
                KnowledgeCanonicalRecord(
                    id=1,
                    record_id="rec_active",
                    revision_id="rev_active",
                    kb_id="kb_glossary",
                    tenant_id=7,
                    record_key="PCR",
                    normalized_key="pcr",
                    row_number=2,
                    fields_json={"term": "PCR", "definition": "Polymerase chain reaction"},
                    projection_text="term：PCR\ndefinition：Polymerase chain reaction",
                    projection_hash="b" * 64,
                ),
                KnowledgeCanonicalAlias(
                    id=1,
                    revision_id="rev_active",
                    record_id="rec_active",
                    alias="聚合酶链式反应",
                    normalized_alias="聚合酶链式反应",
                ),
                KnowledgeCanonicalRecord(
                    id=2,
                    record_id="rec_draft",
                    revision_id="rev_draft",
                    kb_id="kb_glossary",
                    tenant_id=7,
                    record_key="PCR",
                    normalized_key="pcr",
                    row_number=2,
                    fields_json={"term": "PCR", "definition": "unpublished"},
                    projection_text="unpublished",
                    projection_hash="c" * 64,
                ),
            ]
        )
        await db.commit()

        exact = await query_glossary_for_scope(db, scope_snapshot=_GLOSSARY_SCOPE, terms=["ＰＣＲ"])
        alias = await query_glossary_for_scope(db, scope_snapshot=_GLOSSARY_SCOPE, terms=["聚合酶链式反应"])

    await engine.dispose()
    assert exact["outcome"] == "HIT"
    assert alias["outcome"] == "HIT"
    assert exact["revision_ids"] == ["rev_active"]
    assert [item["revision_id"] for item in exact["evidence"]] == ["rev_active"]
    assert exact["evidence"][0]["metadata"]["match_type"] == "EXACT"
    assert alias["evidence"][0]["metadata"]["match_type"] == "ALIAS"
    assert exact["evidence"][0]["metadata"]["coverage_semantics"] == "CLOSED_WORLD_ACTIVE_REVISION"


@pytest.mark.asyncio
async def test_glossary_lookup_folds_spelling_variants_of_compound_terms():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn,
                tables=[
                    KnowledgeCanonicalRecord.__table__,
                    KnowledgeCanonicalAlias.__table__,
                    KnowledgeRelease.__table__,
                ],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        _seed_glossary(
            db,
            [
                {
                    "record_id": "rec_fs",
                    "record_key": "frameshift",
                    "normalized_key": "frameshift",
                    "fold_key": "frameshift",
                    "projection_text": "term：frameshift\ndefinition：移码突变",
                    "projection_hash": "b" * 64,
                    "aliases": [
                        {
                            "record_id": "rec_fs",
                            "alias": "frame-shift",
                            "normalized_alias": "frame-shift",
                            "fold_key": "frameshift",
                        },
                        {
                            "record_id": "rec_fs",
                            "alias": "reading-frame shift",
                            "normalized_alias": "reading-frame shift",
                            "fold_key": "readingframeshift",
                        },
                    ],
                }
            ],
        )
        await db.commit()

        spaced = await query_glossary_for_scope(db, scope_snapshot=_GLOSSARY_SCOPE, terms=["frame shift"])
        closed = await query_glossary_for_scope(db, scope_snapshot=_GLOSSARY_SCOPE, terms=["frameshift"])
        hyphen = await query_glossary_for_scope(db, scope_snapshot=_GLOSSARY_SCOPE, terms=["frame-shift"])
        three_word = await query_glossary_for_scope(db, scope_snapshot=_GLOSSARY_SCOPE, terms=["reading frame shift"])
        upper = await query_glossary_for_scope(db, scope_snapshot=_GLOSSARY_SCOPE, terms=["FRAMESHIFT"])
        miss = await query_glossary_for_scope(db, scope_snapshot=_GLOSSARY_SCOPE, terms=["orphan term"])

    await engine.dispose()
    assert spaced["outcome"] == "HIT"
    assert spaced["evidence"][0]["metadata"]["match_type"] == "FOLD"
    assert closed["outcome"] == "HIT"
    assert closed["evidence"][0]["metadata"]["match_type"] == "EXACT"
    assert hyphen["outcome"] == "HIT"
    assert hyphen["evidence"][0]["metadata"]["match_type"] == "ALIAS"
    assert three_word["outcome"] == "HIT"
    assert three_word["evidence"][0]["metadata"]["match_type"] == "FOLD"
    assert upper["outcome"] == "HIT"
    assert miss["outcome"] == "MISS"
    assert miss["reason_code"] == "TERM_NOT_IN_ACTIVE_GLOSSARY"


@pytest.mark.asyncio
async def test_glossary_phrase_tier_suppresses_fragment_ambiguity():
    """词典同时收录 frameshift 与无关词条 frame 时，"frame shift" 必须命中前者而非歧义。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn,
                tables=[
                    KnowledgeCanonicalRecord.__table__,
                    KnowledgeCanonicalAlias.__table__,
                    KnowledgeRelease.__table__,
                ],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        _seed_glossary(
            db,
            [
                {
                    "record_id": "rec_fs",
                    "record_key": "frameshift",
                    "normalized_key": "frameshift",
                    "fold_key": "frameshift",
                    "projection_text": "term：frameshift\ndefinition：移码突变",
                    "projection_hash": "b" * 64,
                },
                {
                    "record_id": "rec_frame",
                    "record_key": "frame",
                    "normalized_key": "frame",
                    "fold_key": "frame",
                    "projection_text": "term：frame\ndefinition：阅读框",
                    "projection_hash": "d" * 64,
                },
            ],
        )
        await db.commit()

        terms = extract_glossary_terms("frame shift是什么意思")
        result = await query_glossary_for_scope(db, scope_snapshot=_GLOSSARY_SCOPE, terms=terms)

    await engine.dispose()
    assert terms == ["frame shift", "frame", "shift"]
    assert result["outcome"] == "HIT"
    assert len(result["evidence"]) == 1
    assert result["evidence"][0]["record_key"] == "frameshift"
    assert result["evidence"][0]["metadata"]["match_type"] == "FOLD"


@pytest.mark.asyncio
async def test_glossary_fold_collision_yields_ambiguous_not_silent_pick():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn,
                tables=[
                    KnowledgeCanonicalRecord.__table__,
                    KnowledgeCanonicalAlias.__table__,
                    KnowledgeRelease.__table__,
                ],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        _seed_glossary(
            db,
            [
                {
                    "record_id": "rec_closed",
                    "record_key": "frameshift",
                    "normalized_key": "frameshift",
                    "fold_key": "frameshift",
                    "projection_text": "term：frameshift\ndefinition：甲",
                    "projection_hash": "b" * 64,
                },
                {
                    "record_id": "rec_spaced",
                    "record_key": "frame shift",
                    "normalized_key": "frame shift",
                    "fold_key": "frameshift",
                    "projection_text": "term：frame shift\ndefinition：乙",
                    "projection_hash": "d" * 64,
                },
            ],
        )
        await db.commit()

        result = await query_glossary_for_scope(db, scope_snapshot=_GLOSSARY_SCOPE, terms=["frame_shift"])

    await engine.dispose()
    assert result["outcome"] == "AMBIGUOUS"
    assert len(result["evidence"]) == 2
    assert {item["metadata"]["match_type"] for item in result["evidence"]} == {"FOLD"}
