"""文献编译任务状态机测试（P2：幂等入列 / attempt 退避 → DEAD / L3 自动升级）。

SQLite 内存库（BigIntPk 不自增，显式给 id——AGENTS.md 硬规范）。
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.literature.notes import service as compile_service
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeParseArtifact,
    KnowledgeParseRevision,
    LiteratureCompileJob,
    LiteraturePaper,
    LiteratureReaderRender,
    LiteratureArtifactRevision,
)

pytestmark = [pytest.mark.unit]

REVISION_ID = "sir_compile_test_0001"
SOURCE_SHA = "c" * 64
TENANT = 1


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


class _FakeMinio:
    def __init__(self, document_json: bytes, l1_note: bytes | None = None):
        self.document_json = document_json
        self.l1_note = l1_note
        self.uploads: list[tuple[str, bytes]] = []

    async def adownload_file(self, bucket, object_name):
        assert bucket == compile_service.RENDER_BUCKET
        if object_name.endswith("document.json"):
            return self.document_json
        if object_name.endswith("L1_note.md"):
            if self.l1_note is None:
                raise FileNotFoundError(object_name)
            return self.l1_note
        raise FileNotFoundError(object_name)

    async def aupload_file(self, *, bucket_name, object_name, data, content_type):
        self.uploads.append((object_name, bytes(data)))


_DOCUMENT_JSON = json.dumps(
    {
        "metadata": {"title": "Starch synthesis", "journal": "Adv. Mater."},  # 无年份：year 分量不可用
        "paragraphs": [
            {"para_id": "P001", "text": "背景段落全文。"},
            {"para_id": "P002", "text": "方法段落全文。"},
        ],
    },
    ensure_ascii=False,
).encode("utf-8")

_COMPILE_RESULT = {
    "l1": {"one_liner": "一句话贡献 [P001]", "ai_value": 5, "topic_score": None, "tags": ["rice"]},
    "l2": {"wiki": "## 方法论批判\n……", "concepts": []},
    "ai_value": 5,
    "topic_score": None,
    "raw": "{}",
}


@pytest_asyncio.fixture
async def env(monkeypatch, tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for model in (
            LiteraturePaper,
            LiteratureCompileJob,
            LiteratureReaderRender,
            KnowledgeParseRevision,
            KnowledgeParseArtifact,
        ):
            await conn.run_sync(model.__table__.create)
        await conn.run_sync(LiteratureArtifactRevision.__table__.create)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(
            KnowledgeParseRevision(
                id=1,
                revision_id=REVISION_ID,
                tenant_id=TENANT,
                kb_id="kb_lit",
                file_id="kf_lit",
                source_sha256=SOURCE_SHA,
                parser_fingerprint="fp_x",
                pipeline_version="scientific_pdf_v3.2",
                status="ACTIVE",
            )
        )
        session.add(
            LiteratureReaderRender(
                id=1,
                revision_id=REVISION_ID,
                tenant_id=TENANT,
                status="SUCCEEDED",
                markdown_uri=f"kbasset://tenants/{TENANT}/documents/{SOURCE_SHA}/reader_render/{REVISION_ID}/render.md",
            )
        )
        await session.commit()

        monkeypatch.setattr(
            compile_service.pg_manager, "get_async_session_context", lambda: _AsyncSessionContext(session)
        )
        queue = _FakeQueue()

        async def _get_pool():
            return queue

        monkeypatch.setattr(compile_service, "get_arq_pool", _get_pool)
        minio = _FakeMinio(_DOCUMENT_JSON)
        monkeypatch.setattr(compile_service, "get_minio_client", lambda: minio)

        async def _fake_compile(meta, paragraphs, *, journal_meta=""):
            assert "[P001]" not in paragraphs  # 编译输入是纯段落列表
            assert paragraphs[0] == "背景段落全文。"
            return dict(_COMPILE_RESULT)

        monkeypatch.setattr(compile_service.compiler, "compile_l1_l2", _fake_compile)
        from yuxi.literature.notes import wiki_publish

        async def _publish(*args, **kwargs):
            return {"published": False}

        monkeypatch.setattr(wiki_publish, "publish_literature_notes", _publish)
        yield session, queue, minio

    await engine.dispose()


async def _paper(session) -> LiteraturePaper:
    return (await session.execute(select(LiteraturePaper))).scalar_one()


async def _job(session, level: str) -> LiteratureCompileJob:
    return (await session.execute(select(LiteratureCompileJob).where(LiteratureCompileJob.level == level))).scalar_one()


async def test_register_paper_is_idempotent_by_rid(env):
    session, _, _ = env
    first = await compile_service.register_literature_paper(
        tenant_id=TENANT, doi="10.1002/adma.202407106", title="T1", source_revision_id=REVISION_ID
    )
    second = await compile_service.register_literature_paper(tenant_id=TENANT, doi="10.1002/adma.202407106", title="T2")
    assert first.id == second.id
    assert first.rid == "doi-10.1002_adma.202407106"
    papers = (await session.execute(select(LiteraturePaper))).scalars().all()
    assert len(papers) == 1


async def test_compile_success_updates_paper_and_uploads_notes(env):
    session, queue, minio = env
    paper = await compile_service.register_literature_paper(
        tenant_id=TENANT, doi="10.1002/adma.202407106", title="T", source_revision_id=REVISION_ID
    )
    result = await compile_service.enqueue_literature_compile(paper_id=paper.id, level="L1")
    assert result["reused"] is False

    outcome = await compile_service.process_literature_compile({}, result["job_id"])
    assert outcome["status"] == "SUCCEEDED"
    job = await _job(session, "L1")
    assert job.status == "SUCCEEDED"
    prefix = f"tenants/{TENANT}/literature/notes/{paper.rid}/{REVISION_ID}/r1/literature-compile-v1"
    assert job.note_uri.startswith(f"kbasset://{prefix}/g") and job.note_uri.endswith("/L1_note.md")
    prefix = job.note_uri.removeprefix("kbasset://").removesuffix("/L1_note.md")
    l2_job = await _job(session, "L2")
    assert l2_job.status == "SUCCEEDED"
    assert l2_job.note_uri == f"kbasset://{prefix}/L2_wiki.md"
    # 评分回填（ai_value=5 唯一可用分量 → 归一化 5.0 ≥ L3 阈值 4.0）
    paper_row = await _paper(session)
    assert paper_row.ai_value_score == 5
    assert paper_row.value_score > 0
    # L3 自动升级（ai_value 可用且 score(=4 附近) ≥ 4.0）
    l3_jobs = [j for j in queue.jobs if j[2] and j[2] != f"lit-compile:{result['job_id']}"]
    assert l3_jobs, "score ≥ 4.0 必须自动排 L3"
    # 产物上传：L1 note（L2 wiki 因 fake l2 存在也应上传）
    names = [name for name, _ in minio.uploads]
    assert f"{prefix}/L1_note.md" in names
    assert f"{prefix}/L2_wiki.md" in names


async def test_enqueue_is_idempotent_and_failure_goes_dead(env, monkeypatch):
    session, queue, _ = env
    paper = await compile_service.register_literature_paper(
        tenant_id=TENANT, doi="10.1002/adma.202407106", title="T", source_revision_id=REVISION_ID
    )
    first = await compile_service.enqueue_literature_compile(paper_id=paper.id, level="L1")
    again = await compile_service.enqueue_literature_compile(paper_id=paper.id, level="L1")
    assert again["status"] == "PENDING" and again["reused"] is True
    forced_while_active = await compile_service.enqueue_literature_compile(paper_id=paper.id, level="L1", force=True)
    assert forced_while_active == {"job_id": first["job_id"], "status": "PENDING", "reused": True}
    assert len(queue.jobs) == 1

    active_job = await _job(session, "L1")
    active_job.updated_at = compile_service.utc_now() - timedelta(seconds=compile_service.PENDING_REQUEUE_SECONDS + 1)
    await session.commit()
    forced_after_expiry = await compile_service.enqueue_literature_compile(paper_id=paper.id, level="L1", force=True)
    assert forced_after_expiry["status"] == "PENDING" and forced_after_expiry["reused"] is False
    assert len(queue.jobs) == 2

    async def _boom(meta, paragraphs, *, journal_meta=""):
        raise RuntimeError("模拟 LLM 故障")

    monkeypatch.setattr(compile_service.compiler, "compile_l1_l2", _boom)
    for _ in range(compile_service.MAX_COMPILE_ATTEMPTS):
        outcome = await compile_service.process_literature_compile({}, first["job_id"])
    job = await _job(session, "L1")
    assert outcome["status"] == "DEAD"
    assert job.status == "DEAD"
    assert job.attempt == compile_service.MAX_COMPILE_ATTEMPTS
    assert "模拟 LLM 故障" in (job.error or "")

    # A user resubmission starts a new retry budget and cannot expose the stale
    # artifact/error state from the prior terminal execution.
    restarted = await compile_service.enqueue_literature_compile(paper_id=paper.id, level="L1")
    assert restarted["status"] == "PENDING" and restarted["reused"] is False
    restarted_job = await _job(session, "L1")
    assert restarted_job.attempt == 0
    assert restarted_job.error is None


async def test_compile_requires_linked_revision(env, monkeypatch):
    session, _, _ = env
    paper = await compile_service.register_literature_paper(
        tenant_id=TENANT, doi="10.1002/adma.202407106", title="T", source_revision_id=None
    )
    result = await compile_service.enqueue_literature_compile(paper_id=paper.id, level="L1")
    outcome = await compile_service.process_literature_compile({}, result["job_id"])
    assert outcome["status"] == "PENDING"  # attempt 1/3，回 PENDING 待重试
    job = await _job(session, "L1")
    assert "无任何正文产物" in (job.error or "")
