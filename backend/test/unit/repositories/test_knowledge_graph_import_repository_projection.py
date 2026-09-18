"""Neo4j 投影导出的规范层对账参照查询：只取 ID 列、按 kb 隔离、提及键与 graph_utils.mention_key 同构。"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.knowledge.graphs.graph_utils import mention_key
from yuxi.repositories import knowledge_graph_import_repository as repository_module
from yuxi.repositories.knowledge_graph_import_repository import KnowledgeGraphImportRepository
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeGraphEntity,
    KnowledgeGraphEntityMention,
    KnowledgeGraphTriple,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

KB = "kb_proj_ref"
OTHER_KB = "kb_proj_other"


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


@pytest_asyncio.fixture
async def graph_session(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for model in (
            KnowledgeBase,
            KnowledgeChunk,
            KnowledgeGraphEntity,
            KnowledgeGraphTriple,
            KnowledgeGraphEntityMention,
        ):
            await conn.run_sync(model.__table__.create)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        monkeypatch.setattr(
            repository_module.pg_manager,
            "get_async_session_context",
            lambda: _AsyncSessionContext(session),
        )
        yield session

    await engine.dispose()


def _entity(kb_id: str, entity_id: str) -> KnowledgeGraphEntity:
    return KnowledgeGraphEntity(
        entity_id=entity_id,
        kb_id=kb_id,
        canonical_identity=f"name:{entity_id}",
        normalized_name=entity_id,
        label="Gene",
        name=entity_id,
        attributes={},
    )


def _chunk(kb_id: str, chunk_id: str, *, graph_indexed: bool) -> KnowledgeChunk:
    return KnowledgeChunk(
        chunk_id=chunk_id,
        file_id="file_1",
        kb_id=kb_id,
        chunk_index=0,
        content="text",
        graph_indexed=graph_indexed,
    )


async def test_projection_reference_returns_kb_scoped_id_sets(graph_session):
    graph_session.add_all(
        [
            KnowledgeBase(kb_id=KB, name="对账库", kb_type="milvus"),
            KnowledgeBase(kb_id=OTHER_KB, name="其他库", kb_type="milvus"),
            _entity(KB, "ent_a"),
            _entity(KB, "ent_b"),
            _entity(OTHER_KB, "ent_other"),
            _chunk(KB, "chunk_1", graph_indexed=True),
            _chunk(KB, "chunk_pending", graph_indexed=False),
            _chunk(OTHER_KB, "chunk_9", graph_indexed=True),
            KnowledgeGraphTriple(
                triple_id="tri_ab",
                kb_id=KB,
                source_entity_id="ent_a",
                target_entity_id="ent_b",
                relation_type="REGULATES_PROCESS",
                content="a -> b",
            ),
            KnowledgeGraphTriple(
                triple_id="tri_other",
                kb_id=OTHER_KB,
                source_entity_id="ent_other",
                target_entity_id="ent_other",
                relation_type="COEXPRESSION",
                content="x",
            ),
            KnowledgeGraphEntityMention(entity_id="ent_a", kb_id=KB, file_id="file_1", chunk_id="chunk_1"),
            KnowledgeGraphEntityMention(entity_id="ent_b", kb_id=KB, file_id="file_1", chunk_id="chunk_1"),
            KnowledgeGraphEntityMention(entity_id="ent_other", kb_id=OTHER_KB, file_id="file_2", chunk_id="chunk_9"),
        ]
    )
    await graph_session.commit()

    reference = await KnowledgeGraphImportRepository().projection_reference(KB)

    assert reference["kb_name"] == "对账库"
    assert reference["entity_ids"] == {"ent_a", "ent_b"}
    assert reference["triple_ids"] == {"tri_ab"}
    assert reference["mention_keys"] == {mention_key("chunk_1", "ent_a"), mention_key("chunk_1", "ent_b")}
    assert reference["chunk_ids"] == {"chunk_1", "chunk_pending"}
    assert reference["graph_indexed_chunk_ids"] == {"chunk_1"}


async def test_projection_reference_for_unknown_kb_is_empty(graph_session):
    reference = await KnowledgeGraphImportRepository().projection_reference("kb_absent")

    assert reference == {
        "entity_ids": set(),
        "triple_ids": set(),
        "rejected_entity_ids": set(),
        "rejected_triple_ids": set(),
        "mention_keys": set(),
        "chunk_ids": set(),
        "graph_indexed_chunk_ids": set(),
        "kb_name": None,
    }
