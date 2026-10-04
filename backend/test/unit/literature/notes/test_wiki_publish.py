"""文献笔记 Wiki 发布测试（P9 决议 15：claim-free 派生导航发布）。

锁定三件事：
1. 容器三件套（literature_notes KB + MANUAL 容器 + 权威库绑定）自动落位；
2. ACTIVE publication 的 manifest 携带 navigation_entries 且来源闭包指向权威库；
3. 内容不变时重复发布幂等复用，不产生第二条 ACTIVE 版本。

SQLite 内存库（wiki 表主键已统一 BigIntPk 方言变体，自增可用）。
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.literature.notes import wiki_publish
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeBase,
    KnowledgeFile,
    KnowledgeParseRevision,
    KnowledgeWiki,
    LiteratureCompileJob,
    LiteraturePaper,
    WikiAuditEvent,
    WikiBuildArtifact,
    WikiBuildRun,
    WikiBuildSnapshot,
    WikiOutboxEvent,
    WikiPage,
    WikiPageRevision,
    WikiPublication,
    WikiPublicationIndex,
    WikiPublicationPage,
    WikiSourceBinding,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

TENANT = 1
REVISION_ID = "sir_wikipub_test_0001"
SOURCE_SHA = "a" * 64
RID = f"nd-{SOURCE_SHA[:12]}"
NOTE_OBJECT = f"tenants/{TENANT}/literature/notes/{RID}/L1_note.md"


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
    async def adownload_file(self, bucket, object_name):
        assert bucket == wiki_publish.RENDER_BUCKET
        if object_name == NOTE_OBJECT:
            return "# Starch synthesis · L1\n\n贡献段落。".encode()
        if object_name.endswith("L1_data.json"):
            return json.dumps({"concepts": ["AGPase"], "tags": ["rice"]}).encode("utf-8")
        raise FileNotFoundError(object_name)


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for model in (
            KnowledgeParseRevision,
            KnowledgeBase,
            KnowledgeFile,
            KnowledgeWiki,
            LiteraturePaper,
            LiteratureCompileJob,
            WikiSourceBinding,
            WikiBuildSnapshot,
            WikiBuildRun,
            WikiBuildArtifact,
            WikiPage,
            WikiPageRevision,
            WikiPublication,
            WikiPublicationPage,
            WikiPublicationIndex,
            WikiAuditEvent,
            WikiOutboxEvent,
        ):
            await conn.run_sync(model.__table__.create)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(
            KnowledgeParseRevision(
                id=1,
                revision_id=REVISION_ID,
                tenant_id=TENANT,
                kb_id="kb_src_authority",
                file_id="kf_wikipub",
                source_sha256=SOURCE_SHA,
                parser_fingerprint="fp_x",
                pipeline_version="scientific_pdf_v3.2",
                status="ACTIVE",
            )
        )
        session.add(
            LiteraturePaper(
                id=1,
                tenant_id=TENANT,
                rid=RID,
                kind="paper",
                title="Starch synthesis",
                source_revision_id=REVISION_ID,
            )
        )
        session.add(
            KnowledgeBase(
                id=1,
                kb_id="kb_src_authority",
                tenant_id=TENANT,
                name="Authority",
                kb_type="milvus",
                created_by="owner",
                share_config={"access_level": "private"},
            )
        )
        session.add(
            KnowledgeFile(
                id=1,
                kb_id="kb_src_authority",
                file_id="kf_wikipub",
                filename="source.pdf",
                active_parse_revision_id=REVISION_ID,
            )
        )
        session.add(
            LiteratureCompileJob(
                id=1,
                tenant_id=TENANT,
                paper_id=1,
                level="L1",
                status="SUCCEEDED",
                source_revision_id=REVISION_ID,
                note_uri=f"kbasset://{NOTE_OBJECT}",
            )
        )
        await session.commit()

        monkeypatch.setattr(wiki_publish.pg_manager, "get_async_session_context", lambda: _AsyncSessionContext(session))
        monkeypatch.setattr(wiki_publish, "get_minio_client", lambda: _FakeMinio())
        yield session

    await engine.dispose()


async def test_publish_creates_active_claim_free_publication(env):
    first = await wiki_publish.publish_literature_notes(TENANT, actor_uid="u1")
    assert first["published"] is True and first["reused"] is False and first["page_count"] == 1

    session = env
    wiki = (await session.execute(select(KnowledgeWiki))).scalar_one()
    assert wiki.update_mode == "MANUAL"  # 防 reconcile_dynamic_wikis cron 覆盖（纪律 7）
    assert wiki.status == "PUBLISHED"

    kb = (await session.execute(select(KnowledgeBase).where(KnowledgeBase.kb_id == wiki.kb_id))).scalar_one()
    assert kb.kb_type == "literature_notes"

    binding = (await session.execute(select(WikiSourceBinding))).scalar_one()
    assert binding.source_kb_id == "kb_src_authority"  # 来源闭包指向权威库

    publication = (
        await session.execute(select(WikiPublication).where(WikiPublication.status == "ACTIVE"))
    ).scalar_one()
    assert wiki.current_publication_id == publication.publication_id
    manifest = publication.manifest_json
    assert manifest["schema_version"] == "wiki-publication/1.0"
    assert manifest["claim_revision_ids"] == []  # claim-free：派生发布不携带断言
    entry = manifest["navigation_entries"][0]
    assert entry["page_key"] == f"literature:{RID}:L1"
    assert entry["source_kb_ids"] == ["kb_src_authority"]
    assert "Starch synthesis" in entry["expansion_terms"]

    revision = (
        await session.execute(select(WikiPageRevision).where(WikiPageRevision.status == "PUBLISHED"))
    ).scalar_one()
    assert revision.page_revision_id == entry["page_revision_id"]
    assert (await session.execute(select(func.count(WikiPublicationIndex.id)))).scalar_one() == 1
    assert (await session.execute(select(func.count(WikiOutboxEvent.id)))).scalar_one() >= 1


async def test_publish_is_idempotent_on_unchanged_content(env):
    first = await wiki_publish.publish_literature_notes(TENANT, actor_uid="u1")
    second = await wiki_publish.publish_literature_notes(TENANT, actor_uid="u1")
    assert second["published"] is True and second["reused"] is True
    assert second["build_id"] == first["build_id"]
    assert second["publication_id"] == first["publication_id"]
    active_count = (
        await env.execute(select(func.count(WikiPublication.id)).where(WikiPublication.status == "ACTIVE"))
    ).scalar_one()
    assert active_count == 1


async def test_source_switch_during_artifact_read_cannot_publish_old_notes(env, monkeypatch):
    class SourceSwitchMinio(_FakeMinio):
        async def adownload_file(self, bucket, object_name):
            file = (await env.execute(select(KnowledgeFile))).scalar_one()
            file.active_parse_revision_id = "new-source-revision"
            await env.commit()
            return await super().adownload_file(bucket, object_name)

    monkeypatch.setattr(wiki_publish, "get_minio_client", SourceSwitchMinio)
    result = await wiki_publish.publish_literature_notes(TENANT, actor_uid="u1")
    assert result == {"published": False, "reason": "source_changed"}
    assert (await env.execute(select(func.count(WikiPublication.id)))).scalar_one() == 0


@pytest.mark.parametrize("change,reason", [("card", "source_changed"), ("note", "note_changed")])
async def test_annotation_or_recompile_during_read_cannot_activate_old_navigation(env, monkeypatch, change, reason):
    class ChangedMinio(_FakeMinio):
        async def adownload_file(self, bucket, object_name):
            if object_name == NOTE_OBJECT:
                if change == "card":
                    paper = (await env.execute(select(LiteraturePaper))).scalar_one()
                    paper.card_revision += 1
                else:
                    job = (await env.execute(select(LiteratureCompileJob))).scalar_one()
                    job.note_uri = "kbasset://replacement-generation/L1_note.md"
                await env.commit()
            return await super().adownload_file(bucket, object_name)

    monkeypatch.setattr(wiki_publish, "get_minio_client", ChangedMinio)
    result = await wiki_publish.publish_literature_notes(TENANT, actor_uid="u1")
    assert result == {"published": False, "reason": reason}
    assert (await env.execute(select(func.count(WikiPublication.id)))).scalar_one() == 0
