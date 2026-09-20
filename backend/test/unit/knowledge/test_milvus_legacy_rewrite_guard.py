"""legacy 全量重写守卫：带激活修订指针的文件禁止绕过影子索引机制。

覆盖两条写入路径：
- ``index_file`` 不带 ``_index_revision_id`` 的 legacy 分支（手动重建索引任务）；
- ``update_content`` 的重新解析分支（自行 split/embed/store，绕过 index_file）。
"""

from unittest.mock import AsyncMock

from yuxi.knowledge.implementations.milvus import MilvusKB


class _MilvusTouched(Exception):
    """守卫应在任何 Milvus 访问之前触发。"""


def _make_kb() -> MilvusKB:
    kb = MilvusKB.__new__(MilvusKB)
    kb.databases_meta = {"kb1": {"metadata": {}}}
    return kb


def _file_meta(active_parse=None, active_index=None) -> dict:
    return {
        "file_id": "file1",
        "kb_id": "kb1",
        "filename": "a.pdf",
        "path": "kbasset://kb1/file1/source.pdf",
        "markdown_file": "kbasset://kb1/file1/markdown.md",
        "status": "indexed",
        "processing_params": {},
        "active_parse_revision_id": active_parse,
        "active_index_revision_id": active_index,
        "chunk_count": 3,
        "token_count": 100,
    }


async def test_index_file_rejects_legacy_rewrite_for_revision_managed_file(monkeypatch):
    kb = _make_kb()
    monkeypatch.setattr(
        kb,
        "_load_file_meta",
        AsyncMock(return_value=_file_meta(active_parse="spr_x", active_index="sir_y")),
    )

    async def _no_milvus(*args, **kwargs):
        raise _MilvusTouched()

    monkeypatch.setattr(kb, "_get_milvus_collection", _no_milvus)

    try:
        await kb.index_file("kb1", "file1", operator_id="user-1")
    except ValueError as exc:
        message = str(exc)
        assert "active_index_revision_id=sir_y" in message
        assert "active_parse_revision_id=spr_x" in message
        assert "user-1" in message
    else:
        raise AssertionError("legacy rewrite of a revision-managed file must be rejected")


async def test_index_file_allows_shadow_revision_write(monkeypatch):
    kb = _make_kb()
    monkeypatch.setattr(
        kb,
        "_load_file_meta",
        AsyncMock(return_value=_file_meta(active_parse="spr_x", active_index="sir_y")),
    )

    async def _boom(*args, **kwargs):
        raise _MilvusTouched()

    monkeypatch.setattr(kb, "_get_milvus_collection", _boom)

    try:
        await kb.index_file("kb1", "file1", params={"_index_revision_id": "sir_new"})
    except _MilvusTouched:
        pass
    else:
        raise AssertionError("guard must not block shadow revision writes")


async def test_index_file_allows_legacy_write_without_revision_pointers(monkeypatch):
    kb = _make_kb()
    monkeypatch.setattr(kb, "_load_file_meta", AsyncMock(return_value=_file_meta()))

    async def _boom(*args, **kwargs):
        raise _MilvusTouched()

    monkeypatch.setattr(kb, "_get_milvus_collection", _boom)

    try:
        await kb.index_file("kb1", "file1")
    except _MilvusTouched:
        pass
    else:
        raise AssertionError("files without revision pointers keep legacy indexing semantics")


async def test_update_content_blocks_revision_managed_file(monkeypatch):
    kb = _make_kb()
    monkeypatch.setattr(kb, "_load_file_meta", AsyncMock(return_value=_file_meta(active_index="sir_y")))
    monkeypatch.setattr(kb, "_get_milvus_collection", AsyncMock(return_value=object()))
    monkeypatch.setattr(kb, "_get_embedding_function", lambda spec: "embedding-fn")

    update_calls = []

    class _RepoStub:
        async def update_fields(self, **kwargs):
            update_calls.append(kwargs)

    monkeypatch.setattr("yuxi.knowledge.implementations.milvus.KnowledgeFileRepository", _RepoStub)

    async def _must_not_rewrite(*args, **kwargs):
        raise AssertionError("update_content must not touch chunks of a revision-managed file")

    monkeypatch.setattr(kb, "delete_file_chunks_only", _must_not_rewrite)

    results = await kb.update_content("kb1", ["file1"], None)

    assert len(results) == 1
    assert "科研 PDF 证据流水线托管" in str(results[0].get("error"))
    assert results[0].get("status") == "error_indexing"
    assert update_calls and update_calls[0]["data"]["status"] == "error_indexing"
