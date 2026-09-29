"""文件夹模型（真实文件夹 + 混合格式）单元测试。

烟测脚本只在容器内跑一次，这里把关键路径固化成可回归的单测：用 SQLite 内存库替换
``pg_manager`` 会话工厂，真跑 ``KnowledgeFileRepository`` 与 ``KnowledgeBase`` 的
建夹/物化/移动/递归删除/树装配逻辑（不连 Milvus，也不依赖 MinIO）。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.knowledge.base import KnowledgeBase
from yuxi.repositories import knowledge_file_repository as repo_module
from yuxi.repositories.knowledge_file_repository import KnowledgeFileRepository
from yuxi.storage.postgres.models_knowledge import KnowledgeBase as KnowledgeBaseModel
from yuxi.storage.postgres.models_knowledge import KnowledgeFile

KB_ID = "kb_folder_unit"


class _PgManagerStub:
    """把仓库层的 pg_manager 会话工厂指向测试自建的 SQLite 会话工厂。

    提交/回滚语义与 ``PostgresManager.get_async_session_context`` 一致：正常退出提交、
    异常回滚、最后关闭会话——否则 ``flush()`` 后的行对后续查询不可见。
    """

    def __init__(self, factory):
        self._factory = factory

    @asynccontextmanager
    async def get_async_session_context(self):
        session = self._factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


class _FolderKB(KnowledgeBase):
    """最小可用的抽象实现：只关心文件夹元数据，不碰向量库。"""

    kb_type = "folder_unit"

    def __init__(self, work_dir, session_factory):
        super().__init__(work_dir)
        self._session_factory = session_factory

    async def delete_file(self, kb_id: str, file_id: str) -> None:
        await KnowledgeFileRepository().delete(file_id)

    async def _create_kb_instance(self, kb_id, config):
        return None

    async def _initialize_kb_instance(self, instance):
        return None

    async def _persist_kb(self, kb_id):
        return None

    async def _save_metadata(self):
        return None

    async def refresh_database_stats(self, kb_id):
        return {}

    async def index_file(self, kb_id, file_id, operator_id=None):
        return {}

    async def aquery(self, query_text, kb_id, **kwargs):
        return []

    async def get_file_basic_info(self, kb_id, file_id):
        return {}

    async def get_file_content(self, kb_id, file_id):
        return {}

    async def get_file_info(self, kb_id, file_id):
        return {}

    async def update_content(self, kb_id, file_ids, params=None):
        return []

    async def get_query_params_config(self, kb_id, **kwargs):
        return {"type": "folder_unit", "options": []}


@pytest_asyncio.fixture
async def folder_env(monkeypatch, tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(KnowledgeBaseModel.__table__.create)
        await conn.run_sync(KnowledgeFile.__table__.create)
    async with session_factory() as session:
        session.add(KnowledgeBaseModel(kb_id=KB_ID, name="文件夹单测库", kb_type="folder_unit"))
        await session.commit()

    # 仓库模块在导入期绑定了 pg_manager 名字，替换模块属性即可让全部仓库调用走 SQLite。
    monkeypatch.setattr(repo_module, "pg_manager", _PgManagerStub(session_factory))

    kb = _FolderKB(str(tmp_path / "kb"), session_factory)
    kb.databases_meta[KB_ID] = {"name": "文件夹单测库", "kb_type": "folder_unit", "metadata": {}}

    async def _add_file(kb_id: str, file_id: str, filename: str, *, parent_id: str | None = None) -> None:
        async with session_factory() as session:
            session.add(
                KnowledgeFile(
                    file_id=file_id,
                    kb_id=kb_id,
                    filename=filename,
                    file_type=filename.rsplit(".", 1)[-1] if "." in filename else "bin",
                    status="uploaded",
                    is_folder=False,
                    parent_id=parent_id,
                )
            )
            await session.commit()

    yield SimpleNamespace(
        kb=kb,
        repo=KnowledgeFileRepository(),
        kb_id=KB_ID,
        add_file=_add_file,
        session_factory=session_factory,
    )
    await engine.dispose()


# ---------------------------------------------------------------------------
# 建夹：名称校验、同级唯一（大小写不敏感）、文件/目录同名互斥
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_name", ["", "   ", "a/b", "a\\b", ".", "..", "x" * 256])
async def test_create_folder_rejects_invalid_names(folder_env, bad_name):
    with pytest.raises(ValueError):
        await folder_env.kb.create_folder(folder_env.kb_id, bad_name)
    assert await folder_env.repo.list_real_folders(kb_id=folder_env.kb_id) == []


async def test_create_folder_rejects_sibling_collisions_and_allows_other_parents(folder_env):
    kb, kb_id = folder_env.kb, folder_env.kb_id

    docs = await kb.create_folder(kb_id, "docs")
    assert docs["is_folder"] is True and docs["parent_id"] is None

    with pytest.raises(ValueError):
        await kb.create_folder(kb_id, "DOCS")

    # 已存在的文件夹在 allow_existing=True 时复用而不是报错（source_path 物化依赖此语义）
    reused = await kb.create_folder(kb_id, "docs", allow_existing=True)
    assert reused["file_id"] == docs["file_id"]

    await folder_env.add_file(kb_id, "file_budget", "budget", parent_id=docs["file_id"])
    with pytest.raises(ValueError):
        await kb.create_folder(kb_id, "budget", docs["file_id"])

    # 同名文件夹挂到别的父目录下不算冲突
    nested = await kb.create_folder(kb_id, "docs", docs["file_id"])
    assert nested["parent_id"] == docs["file_id"]


async def test_create_folder_requires_folder_parent(folder_env):
    kb, kb_id = folder_env.kb, folder_env.kb_id
    await folder_env.add_file(kb_id, "file_leaf", "leaf.csv")

    with pytest.raises(ValueError):
        await kb.create_folder(kb_id, "sub", "file_leaf")


# ---------------------------------------------------------------------------
# 物化：ensure_folder_path / add_file_record(source_path)
# ---------------------------------------------------------------------------


async def test_ensure_folder_path_reuses_existing_and_creates_missing_segments(folder_env):
    kb, kb_id = folder_env.kb, folder_env.kb_id
    docs = await kb.create_folder(kb_id, "docs")

    leaf = await kb.ensure_folder_path(kb_id, ["docs", "2026", "q1"])
    chain = []
    cursor = leaf
    while cursor:
        row = await folder_env.repo.get_by_file_id(cursor)
        chain.append((cursor, row.filename))
        cursor = row.parent_id
    chain.reverse()

    assert [name for _, name in chain] == ["docs", "2026", "q1"]
    assert chain[0][0] == docs["file_id"], "同名已存在目录必须复用而不是重复建"


async def test_add_file_record_materializes_source_path_and_keeps_lineage(folder_env):
    kb, kb_id = folder_env.kb, folder_env.kb_id
    item = "minio://documents/upload/deep_lab.csv"

    meta = await kb.add_file_record(
        kb_id,
        item,
        params={
            "content_type": "file",
            "source_path": "docs\\2026\\q1\\lab.csv",
            "content_hashes": {item: "hash-folder-unit"},
            "file_sizes": {item: 2048},
        },
        operator_id="unit",
    )

    leaf = await folder_env.repo.get_by_file_id(meta["file_id"])
    assert leaf.filename == "lab.csv", "目录层级进 parent_id，文件名只留 basename"
    assert (leaf.processing_params or {})["source_path"] == "docs/2026/q1/lab.csv"

    parent = await folder_env.repo.get_by_file_id(leaf.parent_id)
    assert parent.filename == "q1" and parent.is_folder is True


# ---------------------------------------------------------------------------
# 遗留虚拟目录行接管
# ---------------------------------------------------------------------------


async def test_adopt_virtual_children_rewrites_legacy_rows(folder_env):
    kb, kb_id, repo = folder_env.kb, folder_env.kb_id, folder_env.repo
    await folder_env.add_file(kb_id, "file_legacy", "docs/legacy.pdf")
    await folder_env.add_file(kb_id, "file_other", "other/keep.pdf")

    # 建夹时对同名前缀的遗留虚拟行自动接管（previous_parent_id = 当前父级）
    docs = await kb.create_folder(kb_id, "docs")

    legacy = await repo.get_by_file_id("file_legacy")
    untouched = await repo.get_by_file_id("file_other")
    assert legacy.parent_id == docs["file_id"] and legacy.filename == "legacy.pdf", "建夹时自动接管遗留虚拟行"
    assert untouched.parent_id is None and untouched.filename == "other/keep.pdf"

    # 幂等：行已挂到真实目录下，再次接管不会重复命中
    adopted_again = await repo.adopt_virtual_children(
        kb_id=kb_id, folder_id=docs["file_id"], folder_name="docs", previous_parent_id=None
    )
    assert adopted_again == 0
    assert (await repo.get_by_file_id("file_legacy")).filename == "legacy.pdf"


# ---------------------------------------------------------------------------
# 移动：同名冲突、环检测、虚拟名归一化
# ---------------------------------------------------------------------------


async def test_move_file_rejects_conflicts_and_cycles(folder_env):
    kb, kb_id = folder_env.kb, folder_env.kb_id
    archive = await kb.create_folder(kb_id, "archive")
    docs = await kb.create_folder(kb_id, "docs")
    nested = await kb.create_folder(kb_id, "q1", docs["file_id"])
    await folder_env.add_file(kb_id, "file_a", "lab.csv", parent_id=docs["file_id"])
    await folder_env.add_file(kb_id, "file_b", "lab.csv", parent_id=archive["file_id"])

    with pytest.raises(ValueError):
        await kb.move_file(kb_id, "file_a", archive["file_id"])

    with pytest.raises(ValueError):
        await kb.move_file(kb_id, "file_a", "file_b")

    with pytest.raises(ValueError):
        await kb.move_file(kb_id, docs["file_id"], docs["file_id"])

    with pytest.raises(ValueError):
        await kb.move_file(kb_id, docs["file_id"], nested["file_id"])

    moved = await kb.move_file(kb_id, "file_a", None)
    assert moved["parent_id"] is None

    renamed = await kb.move_file(kb_id, docs["file_id"], archive["file_id"])
    assert renamed["parent_id"] == archive["file_id"]


async def test_move_file_normalizes_virtual_filename_and_keeps_lineage(folder_env):
    kb, kb_id, repo = folder_env.kb, folder_env.kb_id, folder_env.repo
    archive = await kb.create_folder(kb_id, "archive")
    await folder_env.add_file(kb_id, "file_virtual", "old/nested.pdf")

    moved = await kb.move_file(kb_id, "file_virtual", archive["file_id"])
    row = await repo.get_by_file_id("file_virtual")

    assert moved["filename"] == "nested.pdf"
    assert (row.processing_params or {})["source_path"] == "old/nested.pdf"


# ---------------------------------------------------------------------------
# 递归删除与真实目录树
# ---------------------------------------------------------------------------


async def test_delete_folder_removes_entire_subtree(folder_env):
    kb, kb_id, repo = folder_env.kb, folder_env.kb_id, folder_env.repo
    archive = await kb.create_folder(kb_id, "archive")
    docs = await kb.create_folder(kb_id, "docs", archive["file_id"])
    deep = await kb.create_folder(kb_id, "q1", docs["file_id"])
    await folder_env.add_file(kb_id, "file_deep", "lab.csv", parent_id=deep["file_id"])
    await folder_env.add_file(kb_id, "file_root", "keep.csv")

    await kb.delete_folder(kb_id, archive["file_id"])

    remaining = {row.file_id for row in await repo.list_by_kb_id(kb_id)}
    assert remaining == {"file_root"}
    assert await repo.list_real_folders(kb_id=kb_id) == []


async def test_folder_tree_returns_nested_real_folders(monkeypatch, folder_env):
    from yuxi.knowledge.manager import KnowledgeBaseManager

    kb, kb_id = folder_env.kb, folder_env.kb_id
    docs = await kb.create_folder(kb_id, "docs")
    await kb.create_folder(kb_id, "2026", docs["file_id"])
    await kb.create_folder(kb_id, "archive")
    await folder_env.add_file(kb_id, "file_budget", "budget", parent_id=docs["file_id"])

    manager = KnowledgeBaseManager(folder_env.kb.work_dir + "/manager")

    async def _fake_get_kb(_self, _kb_id):
        return kb

    monkeypatch.setattr(KnowledgeBaseManager, "_get_kb_for_database", _fake_get_kb)

    tree = await manager.get_folder_tree(kb_id)
    assert [node["filename"] for node in tree] == ["archive", "docs"]
    docs_node = next(node for node in tree if node["filename"] == "docs")
    assert [child["filename"] for child in docs_node["children"]] == ["2026"], "文件不进入真实目录树"


async def test_folder_tree_empty_after_delete(folder_env):
    kb, kb_id = folder_env.kb, folder_env.kb_id
    docs = await kb.create_folder(kb_id, "docs")
    await kb.create_folder(kb_id, "q1", docs["file_id"])

    await kb.delete_folder(kb_id, docs["file_id"])

    assert await folder_env.repo.list_real_folders(kb_id=kb_id) == []
