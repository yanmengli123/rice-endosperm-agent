"""索引血缘对账：inspect_file_index_lineage 的闸门口径与审计口径。"""

from yuxi.knowledge.implementations.milvus import MilvusKB
from yuxi.services.scientific_pdf_ingest_service import inspect_file_index_lineage


class _ChunkStub:
    def __init__(self, chunk_id: str, index_revision_id: str | None):
        self.chunk_id = chunk_id
        self.source_provenance = (
            {"index_revision_id": index_revision_id, "parse_revision_id": "spr_x"} if index_revision_id else None
        )


def _patch_chunks(monkeypatch, chunks):
    class _RepoStub:
        async def list_by_file_id(self, file_id):
            return chunks

    monkeypatch.setattr("yuxi.repositories.knowledge_chunk_repository.KnowledgeChunkRepository", _RepoStub)


def _versioned_id(file_id: str, revision_id: str, index: int) -> str:
    token = MilvusKB._index_revision_token(revision_id)
    return f"{file_id}_rev_{token}_chunk_{index}"


async def test_healthy_lineage_has_no_problems(monkeypatch):
    revision_id = "sir_healthy"
    chunks = [
        _ChunkStub(_versioned_id("file1", revision_id, i), revision_id) for i in range(3)
    ]
    _patch_chunks(monkeypatch, chunks)

    report = await inspect_file_index_lineage(
        file_id="file1", index_revision_id=revision_id, expected_chunk_count=3
    )

    assert report["problems"] == []
    assert report["matched_chunks"] == 3
    assert report["stale_chunks"] == 0


async def test_dangling_pointer_reports_no_versioned_chunks(monkeypatch):
    """KB11 现场：激活指针存在，chunk 全是无版本 legacy ID。"""
    revision_id = "sir_dangling"
    chunks = [_ChunkStub(f"file1_chunk_{i}", None) for i in range(21)]
    _patch_chunks(monkeypatch, chunks)

    report = await inspect_file_index_lineage(
        file_id="file1", index_revision_id=revision_id, expected_chunk_count=None
    )

    assert report["matched_chunks"] == 0
    assert report["stale_chunks"] == 21
    assert any("没有任何 chunk 携带索引版本标记" in problem for problem in report["problems"])


async def test_count_mismatch_is_a_gate_problem(monkeypatch):
    revision_id = "sir_partial"
    chunks = [
        _ChunkStub(_versioned_id("file1", revision_id, 0), revision_id),
        _ChunkStub(_versioned_id("file1", revision_id, 1), revision_id),
    ]
    _patch_chunks(monkeypatch, chunks)

    report = await inspect_file_index_lineage(
        file_id="file1", index_revision_id=revision_id, expected_chunk_count=3
    )

    assert any("chunk_count=3" in problem for problem in report["problems"])


async def test_provenance_mismatch_is_a_gate_problem(monkeypatch):
    revision_id = "sir_prov"
    chunks = [_ChunkStub(_versioned_id("file1", revision_id, 0), "sir_other")]
    _patch_chunks(monkeypatch, chunks)

    report = await inspect_file_index_lineage(
        file_id="file1", index_revision_id=revision_id, expected_chunk_count=1
    )

    assert any("source_provenance" in problem for problem in report["problems"])


async def test_stale_chunks_alone_are_not_gate_problems(monkeypatch):
    """激活闸只管候选版本完整性；旧版本残留由激活后 cleanup 清理。"""
    revision_id = "sir_mixed"
    chunks = [
        _ChunkStub(_versioned_id("file1", revision_id, 0), revision_id),
        _ChunkStub(_versioned_id("file1", revision_id, 1), revision_id),
        _ChunkStub("file1_chunk_99", None),
    ]
    _patch_chunks(monkeypatch, chunks)

    report = await inspect_file_index_lineage(
        file_id="file1", index_revision_id=revision_id, expected_chunk_count=2
    )

    assert report["problems"] == []
    assert report["stale_chunks"] == 1
