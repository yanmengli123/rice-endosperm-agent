"""主题导读（MOC）异步任务状态机：入队幂等、租约、幂等护栏与对账。"""

from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.literature import concepts
from yuxi.storage.postgres.models_business import User
from yuxi.storage.postgres.models_knowledge import LiteraturePaper, LiteratureReportJob
from yuxi.utils.datetime_utils import utc_now

pytestmark = pytest.mark.unit
TENANT = 23


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for model in (User, LiteraturePaper, LiteratureReportJob):
            await conn.run_sync(model.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:

        @asynccontextmanager
        async def context():
            try:
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

        monkeypatch.setattr(concepts.pg_manager, "get_async_session_context", context)
        from yuxi.literature import workflow

        monkeypatch.setattr(workflow.pg_manager, "get_async_session_context", context)

        # 请求者存在性/租户一致性在 workflow 自身测试覆盖；此处直接放行 holder。
        async def fake_request_user(run):
            return run

        monkeypatch.setattr(workflow, "_request_user", fake_request_user)
        db.add(User(id=1, uid="u1", username="owner", password_hash="fixture", role="user"))
        db.add(LiteraturePaper(id=1, tenant_id=TENANT, rid="doi-moc", title="MOC source"))
        await db.commit()
        yield db
    await engine.dispose()


async def test_enqueue_rejects_blank_topic(env):
    with pytest.raises(ValueError, match="主题不能为空"):
        await concepts.enqueue_topic_moc(TENANT, topic="   ", created_by="u1")


async def test_enqueue_creates_pending_and_reuses_active(env, monkeypatch):
    monkeypatch.setattr(concepts, "_dispatch_report_job", lambda job_id: _async_true())
    first = await concepts.enqueue_topic_moc(TENANT, topic="淀粉合成", created_by="u1")
    assert first["status"] == "PENDING" and first["reused"] is False and first["job_id"].startswith("lrj_")
    second = await concepts.enqueue_topic_moc(TENANT, topic="淀粉合成", created_by="u1")
    assert second["reused"] is True and second["job_id"] == first["job_id"]


async def _async_true():
    return True


async def test_worker_success_records_result(env, monkeypatch):
    async def fake_generate(tenant_id, *, topic, created_by):
        return {"report_id": 9, "topic": topic, "sources": 4, "content_uri": "kbasset://x"}

    monkeypatch.setattr(concepts, "generate_topic_moc", fake_generate)
    created = await concepts.enqueue_topic_moc(TENANT, topic="灌浆", created_by="u1")
    result = await concepts.process_literature_topic_moc({}, created["job_id"])
    assert result["status"] == "SUCCEEDED"
    row = (await env.execute(select(LiteratureReportJob))).scalar_one()
    assert row.status == "SUCCEEDED" and row.result_json["sources"] == 4 and row.error is None
    assert row.attempt == 1


async def test_worker_failure_keeps_error_without_retry(env, monkeypatch):
    async def fake_generate(tenant_id, *, topic, created_by):
        raise ValueError("主题「灌浆」零匹配（无概念/标题命中，或相关文献未编译 L1）")

    monkeypatch.setattr(concepts, "generate_topic_moc", fake_generate)
    created = await concepts.enqueue_topic_moc(TENANT, topic="灌浆", created_by="u1")
    result = await concepts.process_literature_topic_moc({}, created["job_id"])
    assert result["status"] == "FAILED"
    row = (await env.execute(select(LiteratureReportJob))).scalar_one()
    assert row.status == "FAILED" and "零匹配" in row.error


async def test_worker_skips_completed_job(env, monkeypatch):
    async def unexpected(*args, **kwargs):
        raise AssertionError("已完成任务不得重复执行")

    monkeypatch.setattr(concepts, "generate_topic_moc", unexpected)
    row = LiteratureReportJob(
        job_id="lrj_done", tenant_id=TENANT, requested_by="u1", topic="done",
        status="SUCCEEDED", result_json={"report_id": 1},
    )
    env.add(row)
    await env.commit()
    result = await concepts.process_literature_topic_moc({}, "lrj_done")
    assert result == {"job_id": "lrj_done", "status": "SUCCEEDED", "skipped": "already_completed"}


async def test_reconcile_redispatches_pending_and_deads_stale_lease(env, monkeypatch):
    dispatched: list[str] = []

    async def fake_dispatch(job_id):
        dispatched.append(job_id)
        return True

    monkeypatch.setattr(concepts, "_dispatch_report_job", fake_dispatch)
    pending = LiteratureReportJob(
        job_id="lrj_p", tenant_id=TENANT, requested_by="u1", topic="p", status="PENDING",
    )
    stale = LiteratureReportJob(
        job_id="lrj_s", tenant_id=TENANT, requested_by="u1", topic="s", status="RUNNING",
        lease_token="tok", lease_expires_at=utc_now() - timedelta(minutes=31),
    )
    env.add_all([pending, stale])
    await env.commit()
    redispatched = await concepts.reconcile_literature_topic_mocs({})
    assert redispatched == 1 and dispatched == ["lrj_p"]
    rows = {r.job_id: r for r in (await env.execute(select(LiteratureReportJob))).scalars()}
    assert rows["lrj_s"].status == "DEAD" and "不自动重跑" in rows["lrj_s"].error


async def test_get_topic_moc_job_scopes_to_tenant(env):
    created = await concepts.enqueue_topic_moc(TENANT, topic="可见", created_by="u1")
    job = await concepts.get_topic_moc_job(TENANT, created["job_id"])
    assert job["job_id"] == created["job_id"] and job["requested_by"] == "u1"
    assert await concepts.get_topic_moc_job(TENANT + 1, created["job_id"]) is None
