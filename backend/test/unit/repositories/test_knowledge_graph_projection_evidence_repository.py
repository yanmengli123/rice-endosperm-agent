"""投影导出证据源的流式读取：mention 键集分页（不整表入内存）、join chunk 全文与文件名、
审核态随行、托管导入证据同 schema、chunk 按引用去重分批取全文。"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.repositories import knowledge_graph_repository as repository_module
from yuxi.repositories.knowledge_graph_repository import KnowledgeGraphRepository
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeFile,
    KnowledgeGraphEntity,
    KnowledgeGraphEntityMention,
    KnowledgeGraphRelationEvidence,
    KnowledgeGraphTriple,
    KnowledgeGraphTripleMention,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

KB = "kb_evidence_ref"
OTHER_KB = "kb_evidence_other"
QUOTE = "OsNF-YB1 通过结合 OsDOG1L 启动子促进胚乳细胞增殖。"
CONTENT = f"【章节】3.2 胚乳发育调控\n【文献】OsNF-YB1 调控胚乳发育\n{QUOTE}"


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
            KnowledgeFile,
            KnowledgeChunk,
            KnowledgeGraphEntity,
            KnowledgeGraphTriple,
            KnowledgeGraphEntityMention,
            KnowledgeGraphTripleMention,
            KnowledgeGraphRelationEvidence,
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


def _file(kb_id: str, file_id: str, *, original_filename: str | None = "稻芯.pdf") -> KnowledgeFile:
    return KnowledgeFile(file_id=file_id, kb_id=kb_id, filename="rice.pdf", original_filename=original_filename)


def _chunk(kb_id: str, chunk_id: str, *, file_id: str = "file_1", content: str = CONTENT) -> KnowledgeChunk:
    return KnowledgeChunk(
        chunk_id=chunk_id,
        file_id=file_id,
        kb_id=kb_id,
        chunk_index=0,
        content=content,
        start_char_pos=0,
        end_char_pos=len(content),
        source_provenance={"page_numbers": [4]},
        graph_indexed=True,
    )


def _entity(kb_id: str, entity_id: str, *, review_status: str = "CANDIDATE") -> KnowledgeGraphEntity:
    return KnowledgeGraphEntity(
        entity_id=entity_id,
        kb_id=kb_id,
        canonical_identity=f"name:{entity_id}",
        normalized_name=entity_id,
        label="Gene",
        name=entity_id,
        attributes={},
        review_status=review_status,
    )


def _triple(kb_id: str, triple_id: str, *, review_status: str = "CANDIDATE") -> KnowledgeGraphTriple:
    return KnowledgeGraphTriple(
        triple_id=triple_id,
        kb_id=kb_id,
        source_entity_id="ent_a",
        target_entity_id="ent_b",
        relation_type="REGULATES_PROCESS",
        content="a -> b",
        review_status=review_status,
    )


async def _seed(session) -> None:
    session.add_all(
        [
            KnowledgeBase(kb_id=KB, name="证据库", kb_type="milvus"),
            KnowledgeBase(kb_id=OTHER_KB, name="别的库", kb_type="milvus"),
            _file(KB, "file_1"),
            _file(OTHER_KB, "file_9", original_filename=None),
            _chunk(KB, "chunk_1"),
            _chunk(KB, "chunk_2", content="第二段：无引文的老数据。"),
            _chunk(OTHER_KB, "chunk_9", file_id="file_9"),
            _entity(KB, "ent_a", review_status="APPROVED"),
            _entity(KB, "ent_b"),
            _entity(OTHER_KB, "ent_other"),
            _triple(KB, "tri_1", review_status="APPROVED"),
            _triple(OTHER_KB, "tri_other"),
            KnowledgeGraphTripleMention(
                triple_id="tri_1",
                kb_id=KB,
                file_id="file_1",
                chunk_id="chunk_1",
                text=QUOTE,
                extractor_type="llm_scientific",
                confidence=0.9,
                hedge=False,
                trigger_verified=True,
                trigger_term="促进",
                verifier_confirmed=True,
            ),
            KnowledgeGraphTripleMention(triple_id="tri_1", kb_id=KB, file_id="file_1", chunk_id="chunk_2", text=None),
            KnowledgeGraphEntityMention(entity_id="ent_a", kb_id=KB, file_id="file_1", chunk_id="chunk_1", text=QUOTE),
            KnowledgeGraphEntityMention(entity_id="ent_b", kb_id=KB, file_id="file_1", chunk_id="chunk_2", text=None),
            KnowledgeGraphEntityMention(
                entity_id="ent_other", kb_id=OTHER_KB, file_id="file_9", chunk_id="chunk_9", text="其他库"
            ),
            KnowledgeGraphRelationEvidence(
                evidence_id="ev_1",
                triple_id="tri_1",
                kb_id=KB,
                pmid="12345678",
                evidence_quote="PMID 12345678 报告该关系。",
                claim_eligible=True,
            ),
        ]
    )
    await session.commit()


async def test_iter_projection_evidence_pages_by_id_and_carries_quote_context(graph_session):
    await _seed(graph_session)
    repository = KnowledgeGraphRepository()

    # page_size=1 强制走多页键集分页：结果与不分页一致，且顺序稳定（三元组 mention → 实体 mention → 托管证据）
    rows = [row async for row in repository.iter_projection_evidence(KB, page_size=1)]

    assert [(row["kind"], row["source"], row["target_id"], row["chunk_id"]) for row in rows] == [
        ("triple", "chunk_mention", "tri_1", "chunk_1"),
        ("triple", "chunk_mention", "tri_1", "chunk_2"),
        ("entity", "chunk_mention", "ent_a", "chunk_1"),
        ("entity", "chunk_mention", "ent_b", "chunk_2"),
        ("triple", "relation_evidence", "tri_1", None),
    ]

    first = rows[0]
    assert first["quote"] == QUOTE and first["chunk_content"] == CONTENT
    assert first["filename"] == "稻芯.pdf"  # original_filename 优先
    assert first["review_status"] == "APPROVED"  # 审核态来自所属三元组，与面板同源
    assert first["edge_business_id"] == "tri_1"
    assert first["confidence"] == 0.9 and first["trigger_term"] == "促进"
    assert first["context"] is None  # context_json 未写时不给假上下文

    legacy = rows[1]
    assert legacy["quote"] is None and legacy["review_status"] == "APPROVED"

    entity_row = rows[2]
    # 实体 mention 的关联键就是 MENTIONS 边键 chunk_id->entity_id
    assert entity_row["edge_business_id"] == "chunk_1->ent_a"
    assert entity_row["review_status"] == "APPROVED"

    managed = rows[4]
    assert managed["quote"] == "PMID 12345678 报告该关系。"
    assert managed["pmid"] == "12345678" and managed["review_status"] == "CANONICAL"
    assert managed["chunk_content"] is None and managed["chunk_id"] is None


async def test_iter_projection_evidence_is_kb_scoped(graph_session):
    await _seed(graph_session)
    repository = KnowledgeGraphRepository()

    rows = [row async for row in repository.iter_projection_evidence(OTHER_KB)]

    assert {row["target_id"] for row in rows} == {"ent_other"}
    assert rows[0]["filename"] == "rice.pdf"  # 无 original_filename 时退回 filename
    assert rows[0]["edge_business_id"] == "chunk_9->ent_other"


async def test_list_projection_evidence_returns_in_memory_view(graph_session):
    await _seed(graph_session)
    repository = KnowledgeGraphRepository()

    rows = await repository.list_projection_evidence(KB)

    assert len(rows) == 5
    assert all(row["kind"] in {"triple", "entity"} for row in rows)


async def test_iter_projection_chunks_dedupes_sorts_and_batches(graph_session):
    await _seed(graph_session)
    repository = KnowledgeGraphRepository()

    rows = [
        row
        async for row in repository.iter_projection_chunks(
            KB, ["chunk_2", "chunk_1", "chunk_2", "chunk_missing"], batch_size=1
        )
    ]

    assert [row["chunk_id"] for row in rows] == ["chunk_1", "chunk_2"]
    assert rows[0]["content"] == CONTENT
    assert rows[0]["filename"] == "稻芯.pdf"
    assert rows[0]["end_char_pos"] == len(CONTENT)
    assert rows[0]["source_provenance"] == {"page_numbers": [4]}
    assert rows[1]["content"] == "第二段：无引文的老数据。"


async def test_iter_projection_chunks_is_kb_scoped_and_empty_safe(graph_session):
    await _seed(graph_session)
    repository = KnowledgeGraphRepository()

    assert [row async for row in repository.iter_projection_chunks(KB, [])] == []
    # 其他库的 chunk_id 不会被本项目误取
    assert [row async for row in repository.iter_projection_chunks(KB, ["chunk_9"])] == []
    assert [row async for row in repository.iter_projection_chunks(OTHER_KB, ["chunk_9"])] != []
    assert await repository.list_chunks_for_projection(KB, ["chunk_1"]) != []
