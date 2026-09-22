"""run_artifacts 读时投影：history 按轮注入与 run result 产物数组。"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.services.agent_run_service import _load_run_artifacts
from yuxi.services.conversation_service import _inject_run_artifacts
from yuxi.storage.postgres.models_business import RunArtifact

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(RunArtifact.__table__.create)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as db:
        yield db
    await engine.dispose()


def _artifact(
    artifact_id: int,
    *,
    run_id: str,
    path: str,
    name: str | None = None,
    origin: dict | None = None,
) -> RunArtifact:
    return RunArtifact(
        id=artifact_id,
        tenant_id=1,
        uid="u1",
        run_id=run_id,
        thread_id="th-1",
        origin=origin or {"source": "mcp", "mcp_server": "ricekb", "mcp_tool": "search_genes"},
        name=name or path.rsplit("/", 1)[-1],
        virtual_path=path,
        sha256="a" * 64,
        size_bytes=128,
        media_type="application/json",
    )


async def test_history_projection_injects_per_run_artifacts(session):
    session.add_all(
        [
            _artifact(1, run_id="run-a", path="/home/gem/user-data/outputs/mcp_results/a.json"),
            _artifact(2, run_id="run-a", path="/home/gem/user-data/outputs/mcp_results/b.json"),
            _artifact(
                3,
                run_id="run-b",
                path="/home/gem/user-data/outputs/sequence_deliverables/x.fa",
                origin={"source": "sequence_deliverable", "sequence_id": "Os06t0133000-01"},
            ),
        ]
    )
    await session.commit()

    history = [
        {"id": 1, "type": "human", "content": "问题", "run_id": None},
        {"id": 2, "type": "ai", "content": "回答A", "run_id": "run-a"},
        {"id": 3, "type": "human", "content": "问题2", "run_id": None},
        {"id": 4, "type": "ai", "content": "回答B", "run_id": "run-b"},
        {"id": 5, "type": "ai", "content": "无 run 回答", "run_id": None},
        {"id": 6, "type": "ai", "content": "无产物 run", "run_id": "run-c"},
    ]
    await _inject_run_artifacts(history, session)

    by_id = {msg["id"]: msg for msg in history}
    assert [item["name"] for item in by_id[2]["run_artifacts"]] == ["a.json", "b.json"]
    assert len(by_id[4]["run_artifacts"]) == 1
    assert by_id[4]["run_artifacts"][0]["origin"]["source"] == "sequence_deliverable"
    assert "run_artifacts" not in by_id[1]
    assert "run_artifacts" not in by_id[5]
    # 键恒写（含空）：无产物的 run 得到权威空清单——"空就是空"，前端不再回退
    assert by_id[6]["run_artifacts"] == []
    # 序列化形状锁定（前端下载/保存卡片消费的字段）
    first = by_id[2]["run_artifacts"][0]
    assert set(first) >= {"run_id", "thread_id", "origin", "name", "virtual_path", "sha256", "size_bytes", "media_type"}


async def test_history_projection_skips_when_no_run_ids(session):
    history = [{"id": 1, "type": "ai", "content": "x", "run_id": None}]
    await _inject_run_artifacts(history, session)  # 不应触发查询，也不应报错
    assert "run_artifacts" not in history[0]


async def test_run_result_artifacts_array(session):
    session.add(_artifact(1, run_id="run-a", path="/home/gem/user-data/outputs/mcp_results/a.json"))
    await session.commit()

    artifacts = await _load_run_artifacts("run-a", session)
    assert [item["virtual_path"] for item in artifacts] == ["/home/gem/user-data/outputs/mcp_results/a.json"]

    assert await _load_run_artifacts("run-missing", session) == []


async def test_projection_degrades_gracefully_on_query_failure():
    # 无表环境（如 SQLite 内存库未建表）：投影降级为无产物，不抛异常
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as db:
        history = [{"id": 1, "type": "ai", "content": "x", "run_id": "run-a"}]
        await _inject_run_artifacts(history, db)
        assert "run_artifacts" not in history[0]
        assert await _load_run_artifacts("run-a", db) == []


# ---------------------------------------------------------------------------
# 写侧行为（register_run_artifact）：用 SQLite 替换 pg_manager 会话工厂真覆盖，
# 避免单测直连 Postgres（materializer/deliverable 的单测已把该函数 monkeypatch 掉）。
# ---------------------------------------------------------------------------


class _PgManagerStub:
    """替换 pg_manager：get_async_session_context 指向测试自建的 SQLite 会话工厂。"""

    def __init__(self, factory):
        self._factory = factory

    def get_async_session_context(self):
        return self._factory()


def _install_sqlite_manager(monkeypatch, engine, session_factory):
    from yuxi.storage.postgres import manager as pg_manager_module

    monkeypatch.setattr(pg_manager_module, "pg_manager", _PgManagerStub(session_factory))


def _write_entry(path: str, digest: str = "b" * 64) -> "object":
    from yuxi.agents.mcp.artifact_materializer import MaterializedArtifact

    return MaterializedArtifact(
        virtual_path=path,
        name=path.rsplit("/", 1)[-1],
        sha256=digest,
        size_bytes=32,
        media_type="application/json",
        origin={"source": "mcp", "mcp_server": "s", "mcp_tool": "t"},
    )


async def test_register_run_artifact_persists_row(monkeypatch):
    """有 run_id 时写入权威表：租户/uid/thread 与 entry 字段按上下文落库。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(RunArtifact.__table__.create)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    _install_sqlite_manager(monkeypatch, engine, session_factory)

    from yuxi.agents.mcp.artifact_materializer import register_run_artifact
    from yuxi.agents.mcp.execution import McpExecutionContext

    context = McpExecutionContext(tenant_id=3, uid="u9", thread_id="th-9", run_id="run-write")
    entry = _write_entry("/home/gem/user-data/outputs/mcp_results/x.json")
    try:
        assert await register_run_artifact(context=context, entry=entry) is True
        async with session_factory() as verify:
            row = (await verify.execute(select(RunArtifact).where(RunArtifact.run_id == "run-write"))).scalar_one()
        assert row.tenant_id == 3
        assert row.uid == "u9"
        assert row.thread_id == "th-9"
        assert row.virtual_path == entry.virtual_path
        assert row.sha256 == "b" * 64
        assert row.size_bytes == 32
        assert row.origin["source"] == "mcp"
    finally:
        await engine.dispose()


async def test_register_run_artifact_requires_run_id():
    """无 run_id（同步直连链路未建 AgentRun）：不登记，文件与 state 通道仍生效。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(RunArtifact.__table__.create)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    from yuxi.agents.mcp.artifact_materializer import register_run_artifact
    from yuxi.agents.mcp.execution import McpExecutionContext

    context = McpExecutionContext(tenant_id=1, uid="u1", thread_id="th-1", run_id=None)
    entry = _write_entry("/home/gem/user-data/outputs/mcp_results/y.json", digest="c" * 64)
    try:
        assert await register_run_artifact(context=context, entry=entry) is False
        async with session_factory() as verify:
            assert (await verify.execute(select(RunArtifact))).all() == []
    finally:
        await engine.dispose()


async def test_register_run_artifact_idempotent_on_duplicate(monkeypatch):
    """同 run 同 virtual_path 重复登记：唯一约束冲突被吞掉返回 False，不抛异常。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(RunArtifact.__table__.create)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    _install_sqlite_manager(monkeypatch, engine, session_factory)

    from yuxi.agents.mcp.artifact_materializer import register_run_artifact
    from yuxi.agents.mcp.execution import McpExecutionContext

    context = McpExecutionContext(tenant_id=1, uid="u1", thread_id="th-1", run_id="run-dup")
    entry = _write_entry("/home/gem/user-data/outputs/mcp_results/dup.json")
    try:
        assert await register_run_artifact(context=context, entry=entry) is True
        assert await register_run_artifact(context=context, entry=entry) is False
        async with session_factory() as verify:
            rows = (await verify.execute(select(RunArtifact))).all()
        assert len(rows) == 1
    finally:
        await engine.dispose()
    await engine.dispose()
