"""Concurrent shadow builders must never consume or alter the serving pointer."""

from contextlib import asynccontextmanager
import hashlib
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from yuxi.knowledge.implementations import milvus
from yuxi.storage.postgres import models_knowledge as models


@pytest.mark.parametrize("tampered", [False, True])
async def test_shadow_index_reads_its_own_hashed_artifact(monkeypatch, tampered):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        for model in (models.KnowledgeParseRevision, models.KnowledgeParseArtifact, models.KnowledgeIndexRevision):
            await connection.run_sync(model.__table__.create)
    async with AsyncSession(engine, expire_on_commit=False) as db:
        content = b"Immutable rice starch evidence."
        db.add(
            models.KnowledgeParseRevision(
                id=1,
                revision_id="parse",
                kb_id="kb",
                file_id="file",
                tenant_id=1,
                source_sha256="a" * 64,
                parser_fingerprint="b" * 64,
                pipeline_version="test",
            )
        )
        db.add(
            models.KnowledgeParseArtifact(
                id=1,
                artifact_id="artifact",
                revision_id="parse",
                kind="indexed_markdown",
                object_uri="minio://kb/candidate.md",
                sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
            )
        )
        db.add(
            models.KnowledgeIndexRevision(
                id=1,
                revision_id="index",
                parse_revision_id="parse",
                kb_id="kb",
                file_id="file",
                tenant_id=1,
                chunker_fingerprint="c" * 64,
                status="BUILDING",
            )
        )
        await db.flush()

        @asynccontextmanager
        async def context():
            yield db

        monkeypatch.setattr(milvus.pg_manager, "get_async_session_context", context)

        class FileRepo:
            async def update_fields(self, **kwargs):
                pytest.fail("shadow build must not overwrite the serving file")

            async def update_fields_if_status(self, **kwargs):
                pytest.fail("shadow build must not claim the mutable file pointer")

        class ChunkRepo:
            async def delete_by_chunk_ids(self, ids):
                pass

        monkeypatch.setattr(milvus, "KnowledgeFileRepository", FileRepo)
        monkeypatch.setattr(milvus, "KnowledgeChunkRepository", ChunkRepo)
        kb = milvus.MilvusKB.__new__(milvus.MilvusKB)
        kb.databases_meta = {"kb": {"metadata": {}}}
        monkeypatch.setattr(
            kb,
            "_load_file_meta",
            AsyncMock(
                return_value={
                    "file_id": "file",
                    "kb_id": "kb",
                    "filename": "paper.pdf",
                    "status": "indexed",
                    "markdown_file": "minio://kb/newer-serving.md",
                    "processing_params": {},
                }
            ),
        )
        monkeypatch.setattr(kb, "_get_milvus_collection", AsyncMock(return_value=object()))
        monkeypatch.setattr(kb, "_get_embedding_function", lambda _: object())
        read = AsyncMock(return_value=b"tampered" if tampered else content)
        monkeypatch.setattr(kb, "_read_minio_bytes", read)
        monkeypatch.setattr(kb, "_split_text_into_chunks", lambda text, *args: [{"chunk_index": 0, "content": text}])
        monkeypatch.setattr(kb, "_calculate_chunk_stats", lambda chunks: {"chunk_count": len(chunks), "token_count": 5})
        store = AsyncMock()
        monkeypatch.setattr(kb, "_embed_and_store_chunks", store)
        monkeypatch.setattr(kb, "_delete_chunks_from_milvus", AsyncMock())
        params = {"_parse_revision_id": "parse", "_index_revision_id": "index"}
        if tampered:
            with pytest.raises(ValueError, match="HASH_MISMATCH"):
                await kb.index_file("kb", "file", params=params)
            store.assert_not_called()
        else:
            outcome = await kb.index_file("kb", "file", params=params)
            assert outcome["chunk_count"] == 1
            chunks = store.call_args.args[3]
            assert chunks[0]["content"] == content.decode()
            assert chunks[0]["source_provenance"] == {"parse_revision_id": "parse", "index_revision_id": "index"}
        read.assert_awaited_once_with("minio://kb/candidate.md")
    await engine.dispose()
