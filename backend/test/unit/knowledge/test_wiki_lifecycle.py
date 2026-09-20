"""Wiki lifecycle regressions against an isolated in-memory database only."""

import pytest
from sqlalchemy import BigInteger, Integer, MetaData, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.services import wiki_service as service
from yuxi.storage.postgres import models_knowledge as m


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    metadata = MetaData()
    for table in m.Base.metadata.sorted_tables:
        copied = table.to_metadata(metadata)
        for column in copied.columns:
            if column.primary_key and isinstance(column.type, BigInteger):
                column.type = Integer()
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
        await session.rollback()
    await engine.dispose()


async def seed(db):
    db.add(m.KnowledgeBase(kb_id="source", tenant_id=7, name="Source", kb_type="milvus", share_config={}))
    db.add_all([
        m.KnowledgeGraphEntity(entity_id="a", kb_id="source", canonical_identity="gene:a", normalized_name="a", label="Gene", name="GeneA"),
        m.KnowledgeGraphEntity(entity_id="b", kb_id="source", canonical_identity="trait:b", normalized_name="b", label="Trait", name="TraitB"),
        m.KnowledgeGraphEntityAlias(kb_id="source", entity_id="a", alias="AliasA", normalized_alias="aliasa"),
        m.KnowledgeGraphTriple(triple_id="t", kb_id="source", source_entity_id="a", target_entity_id="b", relation_type="associated", content="relation"),
        m.KnowledgeGraphRelationEvidence(evidence_id="e", triple_id="t", kb_id="source", claim_eligible=True, evidence_alignment_status="ALIGNED", assertion_status="asserted", evidence_quote="Authority sentence", pmid="12345678"),
    ])
    await db.flush()
    created = await service.create_wiki(db, tenant_id=7, actor_uid="owner", name="Wiki", description="", source_kb_ids=["source"], accessible_source_ids={"source"})
    return created["wiki_id"]


@pytest.mark.parametrize("field, changed_value", [("alias", "NewAlias"), ("entity_id", "b"), ("normalized_alias", "newalias")])
async def test_alias_change_changes_fingerprint_and_reverting_restores_hash(db, field, changed_value):
    wiki_id = await seed(db)
    wiki = await service._get_wiki(db, wiki_id, 7)
    original = (await service._current_source_manifests(db, wiki=wiki))[2]
    alias = (await db.execute(select(m.KnowledgeGraphEntityAlias))).scalar_one()
    original_value = getattr(alias, field)
    setattr(alias, field, changed_value)
    await db.flush()
    changed = (await service._current_source_manifests(db, wiki=wiki))[2]
    assert service._digest(original) != service._digest(changed)
    setattr(alias, field, original_value)
    await db.flush()
    restored = (await service._current_source_manifests(db, wiki=wiki))[2]
    assert service._digest(original) == service._digest(restored)


@pytest.mark.parametrize("mutation", ["alias", "evidence", "source", "entity"])
@pytest.mark.parametrize("already_published", [False, True])
async def test_publish_rejects_changed_or_removed_authority(db, mutation, already_published):
    wiki_id = await seed(db)
    build = await service.build_wiki(db, wiki_id=wiki_id, tenant_id=7, actor_uid="owner")
    assert build["status"] == "COMPLETED"
    previous_publication_id = None
    if already_published:
        publication = await service.publish_wiki(
            db, wiki_id=wiki_id, tenant_id=7, actor_uid="publisher", build_id=build["build_id"]
        )
        previous_publication_id = publication["publication_id"]
    if mutation == "alias":
        (await db.execute(select(m.KnowledgeGraphEntityAlias))).scalar_one().alias = "Changed"
    elif mutation == "evidence":
        await db.delete((await db.execute(select(m.KnowledgeGraphRelationEvidence))).scalar_one())
    elif mutation == "entity":
        (await db.execute(select(m.KnowledgeGraphEntity).where(m.KnowledgeGraphEntity.entity_id == "a"))).scalar_one().name = "Changed"
    else:
        (await db.execute(select(m.WikiSourceBinding))).scalar_one().enabled = False
    await db.flush()
    with pytest.raises(service.WikiServiceError):
        await service.publish_wiki(db, wiki_id=wiki_id, tenant_id=7, actor_uid="publisher", build_id=build["build_id"])
    wiki = await service._get_wiki(db, wiki_id, 7)
    assert wiki.current_publication_id == (previous_publication_id if already_published else None)


async def test_compile_manifest_freezes_complete_navigation_and_claim_ids(db):
    wiki_id = await seed(db)
    build = await service.build_wiki(db, wiki_id=wiki_id, tenant_id=7, actor_uid="owner")
    artifact = (await db.execute(select(m.WikiBuildArtifact).where(m.WikiBuildArtifact.kind == "POSTGRES_CANONICAL_MANIFEST"))).scalar_one()
    manifest = artifact.metadata_json
    assert len(manifest["navigation_entries"]) == 2
    assert len(manifest["claim_revision_ids"]) == 1
    assert manifest["navigation_entries"][0]["aliases"] == ["AliasA"]
    assert manifest["dependencies_complete"] is True
    # A later build may update the shared WikiPage title; publication must use
    # the original build's navigation, not mutable page metadata.
    page = (await db.execute(select(m.WikiPage).where(m.WikiPage.page_key == "entity:a"))).scalar_one()
    page.title = "Later build title"
    await db.flush()
    publication = await service.publish_wiki(db, wiki_id=wiki_id, tenant_id=7, actor_uid="publisher", build_id=build["build_id"])
    assert publication["manifest"]["navigation_entries"] == manifest["navigation_entries"]
    assert publication["manifest"]["claim_revision_ids"] == manifest["claim_revision_ids"]


async def test_preflight_hides_denied_source_and_explains_pdf_no_claim(db):
    db.add(m.KnowledgeBase(kb_id="pdf", tenant_id=7, name="PDF", kb_type="milvus", contract_key="pdf_evidence", share_config={}))
    db.add(m.KnowledgeFile(file_id="f", kb_id="pdf", filename="paper.pdf", status="done", is_folder=False))
    await db.flush()
    result = await service.preflight_wiki(db, tenant_id=7, source_kb_ids=["pdf", "secret"], accessible_source_ids={"pdf"})
    assert result["can_create"] is False
    assert result["sources"][1] == {"kb_id": "secret", "accessible": False, "blocking_reasons": ["SOURCE_NOT_ACCESSIBLE"]}
    assert "PDF_NAVIGATION_ONLY_NO_CLAIMS" in result["sources"][0]["warnings"]
