"""阅读渲染服务状态机测试（ADR-0010 决议 7 + 不变量 L0-2）。

SQLite 内存库（模式对齐 test_scientific_pdf_task_projection）：钉死
PENDING → RUNNING → SUCCEEDED/FAILED 状态机、幂等入列、attempt 退避、
质量门禁，以及 L0-2（渲染不触碰解析权威层：parser_fingerprint 与工件行不变）。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.literature.reading import service as service_module
from yuxi.literature.reading.process import RenderProcessError
from yuxi.utils.datetime_utils import utc_now
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeParseArtifact,
    KnowledgeParseRevision,
    LiteratureReaderRender,
    LiteraturePaper,
    KnowledgeWiki,
    WikiSourceBinding,
    WikiPublication,
)

pytestmark = [pytest.mark.unit]

REVISION_ID = "sir_lit_test_0001"
SOURCE_SHA = "a" * 64
MARKDOWN_URI_PREFIX = f"kbasset://tenants/1/documents/{SOURCE_SHA}/reader_render/{REVISION_ID}"


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


class _FakeQueue:
    def __init__(self):
        self.jobs: list[tuple[str, tuple, str | None]] = []

    async def enqueue_job(self, name, *args, _job_id=None):
        self.jobs.append((name, args, _job_id))
        return object()


class _FakeMinio:
    def __init__(self):
        self.uploads: list[tuple[str, int, str]] = []

    async def adownload_file(self, bucket, object_name):
        # 服务端必须先经 parse_minio_url 拆出对象名；若直接把完整 URL 当对象名传进来，
        # 在这里显式失败，避免 fixture 再次掩盖该缺陷。
        assert bucket == service_module.RENDER_BUCKET
        assert not object_name.startswith(("http://", "https://", "minio://")), (
            f"渲染输入仍是完整 URL，未解析成对象名: {object_name}"
        )
        if object_name.endswith("original.pdf"):
            return b"%PDF-1.7 fake-bytes"
        return b"# Starch synthesis\n\nOsNF-YB1 regulates starch biosynthesis.\n"

    async def aupload_file(self, *, bucket_name, object_name, data, content_type):
        assert bucket_name == service_module.RENDER_BUCKET
        self.uploads.append((object_name, len(data), content_type))


def _render_result(markdown: str) -> dict:
    return {
        "ok": True,
        "markdown": markdown,
        "image_count": 0,
        "image_names": [],
        "latex_count": 0,
        "source_digest": "d" * 64,
    }


async def _get_row(session) -> LiteratureReaderRender | None:
    return (
        await session.execute(select(LiteratureReaderRender).where(LiteratureReaderRender.revision_id == REVISION_ID))
    ).scalar_one_or_none()


@pytest_asyncio.fixture
async def service_env(monkeypatch, tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(LiteratureReaderRender.__table__.create)
        await conn.run_sync(KnowledgeParseRevision.__table__.create)
        await conn.run_sync(KnowledgeParseArtifact.__table__.create)
        for model in (LiteraturePaper, KnowledgeWiki, WikiSourceBinding, WikiPublication):
            await conn.run_sync(model.__table__.create)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(
            KnowledgeParseRevision(
                id=1,
                revision_id=REVISION_ID,
                tenant_id=1,
                kb_id="kb_lit",
                file_id="kf_lit",
                source_sha256=SOURCE_SHA,
                parser_fingerprint="fp_abcdef",
                pipeline_version="scientific_pdf_v3.2",
                status="ACTIVE",
            )
        )
        for artifact_id, kind, suffix in (
            ("spa_md_1", "mineru_markdown", f"mineru/{REVISION_ID}/full.md"),
            ("spa_pdf_1", "original_pdf", "source/original.pdf"),
        ):
            session.add(
                KnowledgeParseArtifact(
                    # SQLite 测试方言：BIGINT 主键不自增，显式给 id（AGENTS.md 硬规范）
                    id={"spa_md_1": 101, "spa_pdf_1": 102}[artifact_id],
                    artifact_id=artifact_id,
                    revision_id=REVISION_ID,
                    kind=kind,
                    # 真实生产形态是完整 MinIO URL（PG 实测），不是裸对象名：
                    # 旧 fixture 用裸名，正好掩盖了渲染层把 URL 当对象名的缺陷。
                    object_uri=f"http://localhost:9000/{service_module.RENDER_BUCKET}"
                    f"/tenants/1/documents/{SOURCE_SHA}/{suffix}",
                    sha256="b" * 64,
                    size_bytes=1024,
                )
            )
        await session.commit()

        monkeypatch.setattr(
            service_module.pg_manager,
            "get_async_session_context",
            lambda: _AsyncSessionContext(session),
        )
        queue = _FakeQueue()

        async def _get_pool():
            return queue

        minio = _FakeMinio()
        monkeypatch.setattr(service_module, "get_arq_pool", _get_pool)
        monkeypatch.setattr(service_module, "get_minio_client", lambda: minio)
        yield session, queue, minio

    await engine.dispose()


async def test_enqueue_creates_pending_and_enqueues(service_env):
    session, queue, _ = service_env
    result = await service_module.enqueue_reader_render(revision_id=REVISION_ID)
    assert result["status"] == "PENDING"
    assert result["reused"] is False
    assert queue.jobs == [
        (
            service_module.RENDER_JOB_NAME,
            (REVISION_ID, "reader_render_v1", 0, 0),
            "lit-render:sir_lit_test_0001:reader_render_v1:g0:a0",
        )
    ]
    row = await _get_row(session)
    assert row is not None
    assert row.status == "PENDING"
    assert row.render_version == "reader_render_v1"


async def test_enqueue_rejects_unknown_revision(service_env):
    with pytest.raises(ValueError, match="不存在"):
        await service_module.enqueue_reader_render(revision_id="sir_missing")


async def test_force_rerun_uses_fresh_dispatch_and_retry_budget(service_env):
    session, queue, _ = service_env
    await service_module.enqueue_reader_render(revision_id=REVISION_ID)
    old_job = queue.jobs[-1]
    row = await _get_row(session)
    row.status, row.attempt = "FAILED", service_module.MAX_RENDER_ATTEMPTS
    await session.commit()
    await service_module.enqueue_reader_render(revision_id=REVISION_ID, force=True)
    assert queue.jobs[-1][2] != old_job[2], "不能撞 ARQ 的终态结果缓存"
    assert row.attempt == 0
    assert await service_module._claim_render(*old_job[1]) is None, "旧投递不能认领新请求"


async def test_expired_render_cannot_publish_or_fail_replacement(service_env):
    session, queue, _ = service_env
    await service_module.enqueue_reader_render(revision_id=REVISION_ID)
    old = await service_module._claim_render(*queue.jobs[-1][1])
    old_id = old.id
    old_attempt, old_generation = old.attempt, getattr(old, "generation", 0)
    old.updated_at = utc_now() - timedelta(seconds=service_module.RUNNING_LEASE_SECONDS + 1)
    await session.commit()
    assert await service_module.reconcile_stale_literature_renders() == 1
    current = await service_module._claim_render(*queue.jobs[-1][1])
    assert current.attempt == old_attempt + 1, "超时必须消耗重试预算"
    with pytest.raises(ValueError, match="租约已失效"):
        await service_module._finish_success(
            old_id,
            markdown_uri="kbasset://old/render.md",
            result={},
            expected_attempt=old_attempt,
            expected_generation=old_generation,
        )
    assert not await service_module._finish_failure(
        REVISION_ID,
        old_id,
        "expired worker",
        expected_attempt=old_attempt,
        expected_generation=old_generation,
    )
    assert current.status == "RUNNING" and current.error != "expired worker"


async def test_stale_running_recovery_has_finite_budget(service_env):
    session, queue, _ = service_env
    await service_module.enqueue_reader_render(revision_id=REVISION_ID)
    for attempt in range(service_module.MAX_RENDER_ATTEMPTS):
        row = await service_module._claim_render(*queue.jobs[-1][1])
        assert row.attempt == attempt
        row.updated_at = utc_now() - timedelta(seconds=service_module.RUNNING_LEASE_SECONDS + 1)
        await session.commit()
        requeued = await service_module.reconcile_stale_literature_renders()
        assert requeued == (1 if attempt + 1 < service_module.MAX_RENDER_ATTEMPTS else 0)
    assert row.status == "FAILED" and row.attempt == service_module.MAX_RENDER_ATTEMPTS


async def test_pending_recovery_rotates_cached_dispatch_without_charging_render_attempt(service_env, monkeypatch):
    session, queue, _ = service_env
    seen = set()
    enqueue = queue.enqueue_job

    async def cached(name, *args, _job_id=None):
        if _job_id in seen:
            return None
        seen.add(_job_id)
        return await enqueue(name, *args, _job_id=_job_id)

    monkeypatch.setattr(queue, "enqueue_job", cached)
    await service_module.enqueue_reader_render(revision_id=REVISION_ID)
    old_job = queue.jobs[-1]
    row = await _get_row(session)
    row.updated_at = utc_now() - timedelta(seconds=service_module.PENDING_REQUEUE_SECONDS + 1)
    await session.commit()
    assert await service_module.reconcile_stale_literature_renders() == 1
    assert len(queue.jobs) == 2 and queue.jobs[-1][2] != old_job[2]
    assert row.attempt == 0
    assert await service_module._claim_render(*old_job[1]) is None


async def test_queue_dedup_is_not_reported_as_successful_enqueue(service_env, monkeypatch):
    session, queue, _ = service_env

    async def deduplicated(*_args, **_kwargs):
        return None

    monkeypatch.setattr(queue, "enqueue_job", deduplicated)
    result = await service_module.enqueue_reader_render(revision_id=REVISION_ID)
    assert result["enqueue_deferred"] is True
    row = await _get_row(session)
    row.updated_at = utc_now() - timedelta(seconds=service_module.PENDING_REQUEUE_SECONDS + 1)
    await session.commit()
    assert await service_module.reconcile_stale_literature_renders() == 0


async def test_text_only_ingest_renders_indexed_markdown_offline_without_mineru(service_env, monkeypatch):
    session, queue, minio = service_env
    revision = await session.get(KnowledgeParseRevision, 1)
    revision.status = "INDEXED_TEXT_ONLY"
    markdown = await session.get(KnowledgeParseArtifact, 101)
    markdown.kind = "indexed_markdown"
    await session.commit()
    calls = []

    def rendered(**kwargs):
        from pathlib import Path

        assert kwargs["mode"] == "offline" and not kwargs["extra_env"]
        assert "OsNF-YB1" in Path(kwargs["md_path"]).read_text()
        calls.append(kwargs)
        return _render_result("# Starch synthesis\n\nReadable indexed source body.\n")

    monkeypatch.setattr(service_module, "render_in_subprocess", rendered)
    await service_module.enqueue_reader_render(revision_id=REVISION_ID)
    result = await service_module.process_literature_reader_render({}, *queue.jobs[-1][1])
    assert result["status"] == "SUCCEEDED" and len(calls) == 1
    assert (await _get_row(session)).status == "SUCCEEDED" and minio.uploads
    assert revision.status == "INDEXED_TEXT_ONLY" and markdown.kind == "indexed_markdown"
    with pytest.raises(ValueError, match="mineru_markdown"):
        await service_module._load_render_inputs(REVISION_ID, dual=True)


async def test_enqueue_is_idempotent_while_running_or_succeeded(service_env):
    session, _, _ = service_env
    await service_module.enqueue_reader_render(revision_id=REVISION_ID)

    row = await _get_row(session)
    row.status = "RUNNING"
    await session.commit()
    again = await service_module.enqueue_reader_render(revision_id=REVISION_ID)
    assert again == {"revision_id": REVISION_ID, "status": "RUNNING", "reused": True}

    row.status = "SUCCEEDED"
    await session.commit()
    done = await service_module.enqueue_reader_render(revision_id=REVISION_ID)
    assert done["status"] == "SUCCEEDED" and done["reused"] is True

    forced = await service_module.enqueue_reader_render(revision_id=REVISION_ID, force=True)
    assert forced["reused"] is False
    assert (await _get_row(session)).status == "PENDING"


async def test_render_success_uploads_and_preserves_authority_plane(service_env, monkeypatch):
    """L0-2 钉死：渲染成功只写 MinIO 派生物 + 渲染行；解析权威层（指纹/工件）不变。"""
    session, queue, minio = service_env
    await service_module.enqueue_reader_render(revision_id=REVISION_ID)

    def _fake_render(**_kwargs):
        return _render_result("# 干净阅读视图\n\nOsNF-YB1 regulates starch biosynthesis.\n")

    monkeypatch.setattr(service_module, "render_in_subprocess", _fake_render)
    outcome = await service_module.process_literature_reader_render({}, *queue.jobs[-1][1])

    assert outcome["status"] == "SUCCEEDED"
    assert outcome["markdown_uri"].startswith(f"{MARKDOWN_URI_PREFIX}/reader_render_v1/")
    assert outcome["markdown_uri"].endswith("/render.md")

    row = await _get_row(session)
    assert row.status == "SUCCEEDED"
    assert row.markdown_uri.startswith(MARKDOWN_URI_PREFIX)
    assert row.source_digest == "d" * 64

    # 上传只发生在 reader_render 命名空间（第 5 段路径）
    assert minio.uploads
    assert all(name.split("/")[4] == "reader_render" for name, _, _ in minio.uploads)
    assert any(name.endswith("/render.md") and ctype == "text/markdown" for name, _, ctype in minio.uploads)

    # 不变量 L0-2：解析权威层零改动（指纹不变、无新增工件行）
    revision = (
        await session.execute(select(KnowledgeParseRevision).where(KnowledgeParseRevision.revision_id == REVISION_ID))
    ).scalar_one()
    assert revision.parser_fingerprint == "fp_abcdef"
    artifacts = (await session.execute(select(KnowledgeParseArtifact))).scalars().all()
    assert len(artifacts) == 2


async def test_render_failure_requeues_then_dead(service_env, monkeypatch):
    session, queue, _ = service_env
    await service_module.enqueue_reader_render(revision_id=REVISION_ID)

    def _boom(**_kwargs):
        raise RenderProcessError("渲染子进程失败: 模拟崩溃")

    monkeypatch.setattr(service_module, "render_in_subprocess", _boom)
    first = await service_module.process_literature_reader_render({}, *queue.jobs[-1][1])
    assert first["status"] == "REQUEUED"
    row = await _get_row(session)
    assert row.status == "PENDING" and row.attempt == 1

    second = await service_module.process_literature_reader_render({}, *queue.jobs[-1][1])
    assert second["status"] == "REQUEUED"
    third = await service_module.process_literature_reader_render({}, *queue.jobs[-1][1])
    assert third["status"] == "FAILED"

    row = await _get_row(session)
    assert row.status == "FAILED"
    assert row.attempt == service_module.MAX_RENDER_ATTEMPTS
    assert "模拟崩溃" in (row.error or "")
    # 入列轨迹：初始 attempt=0 + 失败重排 attempt=1/2（第 3 次失败不再重排）
    assert [job[2] for job in queue.jobs] == [
        "lit-render:sir_lit_test_0001:reader_render_v1:g0:a0",
        "lit-render:sir_lit_test_0001:reader_render_v1:g1:a1",
        "lit-render:sir_lit_test_0001:reader_render_v1:g2:a2",
    ]


async def test_render_rejects_blank_markdown_at_quality_gate(service_env, monkeypatch):
    """质量门禁（AGENTS.md 硬规范）：空白产物拒绝入库，走失败路径。"""
    session, _, _ = service_env
    await service_module.enqueue_reader_render(revision_id=REVISION_ID)

    def _blank(**_kwargs):
        return _render_result("   ")

    monkeypatch.setattr(service_module, "render_in_subprocess", _blank)
    outcome = await service_module.process_literature_reader_render({}, REVISION_ID)

    assert outcome["status"] == "REQUEUED"
    row = await _get_row(session)
    assert "空白" in (row.error or "")


async def test_modes_publish_separate_immutable_runs_and_review(service_env, monkeypatch):
    session, queue, minio = service_env
    monkeypatch.setenv("YUXI_PADDLE_TOKEN", "test-only-token")
    modes = []

    def render(**kwargs):
        modes.append(kwargs["mode"])
        return {**_render_result("# Reading\n\nValid research text."), "review_json": '{"items":[]}'}

    monkeypatch.setattr(service_module, "render_in_subprocess", render)
    outcomes = []
    for dual, force in ((False, False), (True, False), (False, True)):
        await service_module.enqueue_reader_render(revision_id=REVISION_ID, dual=dual, force=force)
        outcomes.append(await service_module.process_literature_reader_render({}, *queue.jobs[-1][1]))
    assert modes == ["offline", "dual", "offline"]
    assert all(outcome["status"] == "SUCCEEDED" for outcome in outcomes)
    assert len({outcome["markdown_uri"] for outcome in outcomes}) == 3
    rows = (await session.execute(select(LiteratureReaderRender))).scalars().all()
    assert len(rows) == 2
    for outcome in outcomes:
        prefix = outcome["markdown_uri"].removeprefix("kbasset://").removesuffix("/render.md")
        assert any(name == f"{prefix}/work/review.json" for name, _, _ in minio.uploads)
    assert outcomes[0]["markdown_uri"] != outcomes[2]["markdown_uri"]
