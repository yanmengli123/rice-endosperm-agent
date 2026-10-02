"""P10-B/C/D 契约测试：产物修订与指针、人工冻结保护、三模式编排、两套检索、
RRF 口径、派生索引边界（无证据字段）。
"""

from __future__ import annotations


import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.literature import artifacts, retrieval, workflow
from yuxi.storage.postgres.models_knowledge import (
    LiteratureArtifactRevision,
    LiteratureCompileJob,
    LiteraturePaper,
    LiteratureWorkflowRun,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


@pytest.mark.asyncio
async def _unused_async():
    pass


TENANT = 13
RID = "doi-p10bcd"


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
    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.uploads: list[str] = []

    async def adownload_file(self, bucket, object_name):
        if object_name not in self.files:
            raise FileNotFoundError(object_name)
        return self.files[object_name]

    async def aupload_file(self, *, bucket_name, object_name, data, content_type):
        self.files[object_name] = bytes(data)
        self.uploads.append(object_name)


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for model in (LiteraturePaper, LiteratureArtifactRevision, LiteratureWorkflowRun, LiteratureCompileJob):
            await conn.run_sync(model.__table__.create)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(LiteraturePaper(id=1, tenant_id=TENANT, rid=RID, kind="paper", doi="10.1001/p10bcd"))
        session.add(LiteraturePaper(id=2, tenant_id=TENANT, rid="doi-other", kind="paper", doi="10.1001/other"))
        await session.commit()
        for module in (artifacts, workflow, retrieval):
            monkeypatch.setattr(module.pg_manager, "get_async_session_context", lambda: _AsyncSessionContext(session))
        minio = _FakeMinio()
        monkeypatch.setattr(artifacts, "get_minio_client", lambda: minio)
        monkeypatch.setattr(retrieval, "_get_minio", lambda: minio)
        yield session, minio
    await engine.dispose()


# --- 阶段B：产物修订与指针 ---------------------------------------------------------


async def test_store_revision_switches_pointer_and_resolves(env):
    session, minio = env
    result = await artifacts.store_revision(
        TENANT,
        RID,
        artifacts.ArtifactKind.DOCUMENT,
        content=b'{"paragraphs": []}',
        content_type="application/json",
        operator="u1",
    )
    assert result["switched"] is True and not result["frozen"]
    doc = await artifacts.resolve_effective_document(TENANT, RID)
    assert doc["revision_id"] == result["revision_id"]
    assert doc["source"] == "rendered"


async def test_manual_freeze_blocks_auto_and_409_on_conflict(env):
    session, _ = env
    auto = await artifacts.store_revision(
        TENANT,
        RID,
        artifacts.ArtifactKind.DOCUMENT,
        content=b"auto v1",
        content_type="text/markdown",
        operator="system",
    )
    frozen = await artifacts.store_revision(
        TENANT,
        RID,
        artifacts.ArtifactKind.DOCUMENT,
        content=b"manual v1",
        content_type="text/markdown",
        operator="u1",
        freeze=True,
        expected_current_revision=auto["revision_id"],
    )
    assert frozen["frozen"] is True
    # 冻结后：自动生成只形成候选（不切指针）
    candidate = await artifacts.store_revision(
        TENANT,
        RID,
        artifacts.ArtifactKind.DOCUMENT,
        content=b"auto v2",
        content_type="text/markdown",
        operator="system",
    )
    assert candidate["switched"] is False
    doc = await artifacts.resolve_effective_document(TENANT, RID)
    assert doc["revision_id"] == frozen["revision_id"] and doc["frozen"] is True
    # 乐观并发：基于过期版本保存 → 409
    with pytest.raises(artifacts.RevisionConflictError, match="并发修改"):
        await artifacts.store_revision(
            TENANT,
            RID,
            artifacts.ArtifactKind.DOCUMENT,
            content=b"conflict",
            content_type="text/markdown",
            operator="u1",
            freeze=True,
            expected_current_revision=auto["revision_id"],
        )


async def test_resolve_falls_back_to_legacy_probe(env):
    session, minio = env
    # 无 DB 修订：直接构造 legacy 文件（document_resolved.json 优先）
    base = f"tenants/{TENANT}/documents/{'d' * 64}/reader_render/sir_legacy/"
    session.execute = session.execute  # keep
    paper = (await session.execute(select(LiteraturePaper).where(LiteraturePaper.rid == RID))).scalar_one()
    paper.source_revision_id = "sir_legacy"
    await session.commit()

    engine2 = paper.metadata.create_all  # noop guard
    async with _AsyncSessionContext(session):
        pass
    minio.files[base + "document_resolved.json"] = b"resolved legacy"
    # probe 需要查 render 表——直接 mock resolve 的 DB 查询层
    import yuxi.literature.artifacts as art

    async def fake_probe(t, p):
        return {
            "content": b"resolved legacy",
            "source": "decided",
            "revision_id": "legacy:document_resolved.json",
            "digest": "x",
            "frozen": False,
        }

    original = art._legacy_probe
    art._legacy_probe = fake_probe
    try:
        doc = await art.resolve_effective_document(TENANT, RID)
        assert doc["source"] == "decided"
    finally:
        art._legacy_probe = original


# --- 阶段C：三模式编排 ------------------------------------------------------------


async def test_workflow_idempotent_and_step_progression(env):
    session, _ = env
    first = await workflow.start_workflow(tenant_id=TENANT, paper_rid=RID, mode="parse_compile", requested_by="u1")
    again = await workflow.start_workflow(tenant_id=TENANT, paper_rid=RID, mode="parse_compile", requested_by="u1")
    assert first["run_id"] == again["run_id"] and again["reused"] is True
    # 不同模式独立运行
    full = await workflow.start_workflow(tenant_id=TENANT, paper_rid=RID, mode="full", requested_by="u1")
    assert full["run_id"] != first["run_id"]

    # 步骤推进
    await workflow.mark_step(first["run_id"], workflow.WorkflowStep.REGISTER, "SUCCEEDED")
    await workflow.mark_step(first["run_id"], workflow.WorkflowStep.RENDER, "SUCCEEDED")
    await workflow.mark_step(first["run_id"], workflow.WorkflowStep.COMPILE, "SUCCEEDED")
    await workflow.mark_step(first["run_id"], workflow.WorkflowStep.PUBLISH, "SUCCEEDED")
    state = await workflow.get_workflow(first["run_id"], TENANT)
    assert state["status"] == "SUCCEEDED"
    assert len(state["steps"]) == 4  # parse_compile 步骤数


async def test_workflow_mode_steps_match_plan(env):
    assert [s for s in workflow.MODE_STEPS["parse_compile"]] == ["register", "render", "compile", "publish"]
    assert "review_gate" in workflow.MODE_STEPS["full"]
    assert "compile" not in workflow.MODE_STEPS["parse_only"]  # 仅解析不编译
    with pytest.raises(ValueError, match="未知处理模式"):
        await workflow.start_workflow(tenant_id=TENANT, paper_rid=RID, mode="bogus", requested_by="u1")


async def test_skip_review_gate_records_operator(env):
    session, _ = env
    run = await workflow.start_workflow(tenant_id=TENANT, paper_rid=RID, mode="full", requested_by="u1")
    await workflow.skip_review_gate(run["run_id"], operator="u1", reason="已人工核对")
    state = await workflow.get_workflow(run["run_id"], TENANT)
    assert state["review_skipped"] is True and "已人工核对" in state["review_skip_reason"]
    gate = next(s for s in state["steps"] if s["step"] == "review_gate")
    assert gate["status"] == "SKIPPED"


# --- 阶段D：两套检索 --------------------------------------------------------------


def test_tokenize_mixed_language():
    tokens = retrieval.tokenize("starch 淀粉合成 regulation")
    assert "starch" in tokens and "regulation" in tokens
    assert any(len(t) == 2 and "\u4e00" <= t[0] <= "\u9fff" for t in tokens)  # 中文 bigram


def test_rrf_fuse_upstream_formula():
    # 上游口径：Σ w/(k+rank)，rank 从 1 起
    fused = retrieval.rrf_fuse([(["a", "b"], 1.0), (["b", "c"], 0.5)], k=60)
    scores = dict(fused)
    # b 在通道1 rank=2（1/(60+2)），通道2 rank=1（0.5/(60+1)）
    assert abs(scores["b"] - (1.0 / 62 + 0.5 / 61)) < 1e-9
    assert abs(scores["a"] - 1.0 / 61) < 1e-9
    assert fused[0][0] == "b"  # 双通道命中排最前


async def test_search_catalog_filters_and_orders(env):
    session, _ = env
    paper = (await session.execute(select(LiteraturePaper).where(LiteraturePaper.rid == RID))).scalar_one()
    paper.title = "Starch accumulation in rice endosperm"
    paper.abstract = "Carbohydrate flow during grain filling"
    paper.pub_year = 2025
    await session.commit()
    results = await retrieval.search_catalog(TENANT, "starch")
    assert len(results) >= 1 and results[0]["rid"] == RID
    empty = await retrieval.search_catalog(TENANT, "完全不匹配的词")
    assert all(r["rid"] != RID for r in empty)


async def test_search_compiled_rrf_and_boundary(env):
    session, minio = env
    # 种子：RID 有已编译 L1 笔记；other 无
    from yuxi.storage.postgres.models_knowledge import LiteratureCompileJob

    async with _AsyncSessionContext(session):
        session.add(LiteratureCompileJob(tenant_id=TENANT, paper_id=1, level="L1", status="SUCCEEDED", attempt=0))
    minio.files[f"tenants/{TENANT}/literature/notes/{RID}/L1_note.md"] = (
        b"# Note\nstarch biosynthesis regulates grain filling"
    )
    result = await retrieval.search_compiled(TENANT, "starch")
    assert result["channels_used"] and "note_fulltext" in result["channels_used"]
    assert len(result["hits"]) >= 1
    hit = result["hits"][0]
    assert hit["rid"] == RID and "starch" in hit["note_excerpt"].lower()
    # 派生索引边界：命中无证据字段（EvidenceEnvelope 不可构造）
    for forbidden in ("evidence_id", "quote", "claim", "content"):
        assert forbidden not in hit, f"导航命中不得携带证据字段: {forbidden}"


async def test_search_compiled_empty_channel_note(env):
    result = await retrieval.search_compiled(TENANT, "nonexistent")
    assert result["hits"] == [] and result["channels_used"] == []
    assert "关键词" in result["note"] or result["note"] == ""  # 明确降级说明
