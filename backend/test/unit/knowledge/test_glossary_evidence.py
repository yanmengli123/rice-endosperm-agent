from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.knowledge.evidence.glossary import (
    extract_glossary_terms,
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

        scope = {
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
        exact = await query_glossary_for_scope(db, scope_snapshot=scope, terms=["ＰＣＲ"])
        alias = await query_glossary_for_scope(db, scope_snapshot=scope, terms=["聚合酶链式反应"])

    await engine.dispose()
    assert exact["outcome"] == "HIT"
    assert alias["outcome"] == "HIT"
    assert exact["revision_ids"] == ["rev_active"]
    assert [item["revision_id"] for item in exact["evidence"]] == ["rev_active"]
    assert exact["evidence"][0]["metadata"]["coverage_semantics"] == "CLOSED_WORLD_ACTIVE_REVISION"
