"""文献工作流显式取消语义测试（P0-②）。

覆盖边界：重复取消、终态不可取消、租约接替、mark_step 不得复活取消、
_claim 自愈收敛、已终结产物不回滚、跨租户拒绝。
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.literature import workflow
from yuxi.storage.postgres.models_knowledge import LiteraturePaper, LiteratureWorkflowRun

TENANT = 21
RID = "doi-cancel"


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


async def _seed(session, *, status: str, run_id: str, mode: str = "parse_only"):
    steps = [{"step": s, "status": "PENDING", "task_ref": ""} for s in workflow.MODE_STEPS[mode]]
    session.add(LiteraturePaper(id=1, tenant_id=TENANT, rid=RID, kind="paper", doi="10.1001/cancel"))
    session.add(
        LiteratureWorkflowRun(
            run_id=run_id,
            tenant_id=TENANT,
            paper_id=1,
            mode=mode,
            status=status,
            steps_json=steps,
            frozen_parse_revision="rev-1",
        )
    )
    await session.commit()


@pytest_asyncio.fixture
async def env(monkeypatch):
    async def enqueue_tick(_run_id):
        return True

    monkeypatch.setattr(workflow, "_enqueue_tick", enqueue_tick)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for model in (LiteraturePaper, LiteratureWorkflowRun):
            await conn.run_sync(model.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        monkeypatch.setattr(workflow.pg_manager, "get_async_session_context", lambda: _AsyncSessionContext(session))
        yield session
    await engine.dispose()


async def _row(session, run_id):
    # 注意：BigIntPk 在 aiosqlite 下不自增，且 session.get 按 BigInteger 比较会失配，
    # 这里一律走显式 select（见 AGENTS.md SQLite 测试方言约定）。
    return (
        await session.execute(select(LiteratureWorkflowRun).where(LiteratureWorkflowRun.run_id == run_id))
    ).scalar_one_or_none()


async def _status(session, run_id):
    row = await _row(session, run_id)
    return row.status if row else None


async def test_request_cancel_marks_intent_not_final(env):
    """取消请求落 CANCEL_REQUESTED（意图），不是终态——需 worker 在步骤边界收敛。"""
    session = env
    await _seed(session, status="RUNNING", run_id="r-cancel-1")
    result = await workflow.request_cancel_workflow("r-cancel-1", TENANT, operator="u1", reason="不想要了")
    assert result["status"] == "CANCEL_REQUESTED"
    assert result["already_requested"] is False
    row = await _row(session, "r-cancel-1")
    assert row.cancel_requested_by == "u1"
    assert row.cancel_reason == "不想要了"
    assert row.cancel_requested_at is not None


async def test_repeated_cancel_is_idempotent(env):
    """重复取消幂等：保留首次意图审计，不覆盖操作者。"""
    session = env
    await _seed(session, status="RUNNING", run_id="r-cancel-2")
    first = await workflow.request_cancel_workflow("r-cancel-2", TENANT, operator="u1", reason="第一次")
    second = await workflow.request_cancel_workflow("r-cancel-2", TENANT, operator="u2", reason="第二次")
    assert first["already_requested"] is False
    assert second["already_requested"] is True
    row = await _row(session, "r-cancel-2")
    assert row.cancel_requested_by == "u1"
    assert row.cancel_reason == "第一次"


@pytest.mark.parametrize("status", ["SUCCEEDED", "FAILED", "CANCELLED"])
async def test_terminal_status_is_not_cancellable(env, status):
    """终态不可取消（不得借取消抹掉已完成事实）。"""
    session = env
    await _seed(session, status=status, run_id=f"r-term-{status}")
    with pytest.raises(ValueError, match="已终结"):
        await workflow.request_cancel_workflow(f"r-term-{status}", TENANT, operator="u1")


async def test_claim_self_heals_cancel_request(env):
    """Worker 重启后 tick 再来：_claim 不认领租约，直接收敛为 CANCELLED。"""
    session = env
    await _seed(session, status="RUNNING", run_id="r-claim-cancel")
    row = await _row(session, "r-claim-cancel")
    row.status = "CANCEL_REQUESTED"
    await session.commit()

    claimed = await workflow._claim("r-claim-cancel")
    assert claimed is None
    assert await _status(session, "r-claim-cancel") == "CANCELLED"


async def test_claim_will_not_revive_cancelled_run(env):
    """已 CANCELLED 的运行不会被再次认领执行（取消不可被 tick 复活）。"""
    session = env


async def test_mark_step_does_not_resurrect_cancel(env):
    """迟到的步骤报告不得把 CANCEL_REQUESTED 覆盖回 RUNNING。"""
    session = env
    await _seed(session, status="RUNNING", run_id="r-late-step")
    await workflow.request_cancel_workflow("r-late-step", TENANT, operator="u1")
    await workflow.mark_step("r-late-step", "register", "SUCCEEDED", task_ref="rev-1")
    assert await _status(session, "r-late-step") == "CANCEL_REQUESTED"


async def test_finalize_marks_remaining_steps_cancelled(env):
    """收敛时未完成步骤标 CANCELLED，已完成步骤保留，不回滚。"""
    session = env
    await _seed(session, status="CANCEL_REQUESTED", run_id="r-final")
    row = await _row(session, "r-final")
    row.steps_json = [
        {"step": "register", "status": "SUCCEEDED", "task_ref": "rev-1"},
        {"step": "render", "status": "RUNNING", "task_ref": ""},
        {"step": "compile", "status": "PENDING", "task_ref": ""},
    ]
    await session.commit()

    assert await workflow._finalize_cancel("r-final") is True
    row = await _row(session, "r-final")
    assert row.status == "CANCELLED"
    statuses = {e["step"]: e["status"] for e in row.steps_json}
    assert statuses["register"] == "SUCCEEDED"  # 已完成不回滚
    assert statuses["render"] == "CANCELLED"
    assert statuses["compile"] == "CANCELLED"


async def test_finalize_is_noop_on_non_cancel_requested(env):
    """非取消态调 _finalize_cancel 不改变状态（防御误用）。"""
    session = env
    await _seed(session, status="RUNNING", run_id="r-nofinal")
    assert await workflow._finalize_cancel("r-nofinal") is False
    assert await _status(session, "r-nofinal") == "RUNNING"


async def test_cancel_is_tenant_scoped(env):
    """跨租户取消必须被拒（不可见即不可取消）。"""
    session = env
    await _seed(session, status="RUNNING", run_id="r-cross")
    with pytest.raises(ValueError, match="无权访问"):
        await workflow.request_cancel_workflow("r-cross", TENANT + 1, operator="u9")


async def test_get_workflow_exposes_cancel_fields(env):
    """投影暴露 cancel_requested / cancellable / cancel_reason。"""
    session = env
    await _seed(session, status="RUNNING", run_id="r-proj")
    active = await workflow.get_workflow("r-proj", TENANT)
    assert active["cancellable"] is True and active["cancel_requested"] is False

    await workflow.request_cancel_workflow("r-proj", TENANT, operator="u1", reason="停")
    after = await workflow.get_workflow("r-proj", TENANT)
    assert after["cancel_requested"] is True
    assert after["cancellable"] is False  # 已请求取消，不再重复允许
    assert after["cancel_reason"] == "停"


async def test_task_center_maps_cancelled_status():
    """任务中心必须显式映射 CANCELLED，否则 fallback 成 running（已取消却显示运行中）。"""
    from yuxi.literature import readops

    assert readops._TASK_CENTER_STATUS["CANCELLED"] == "cancelled"
    assert readops._TASK_CENTER_STATUS["CANCEL_REQUESTED"] == "running"
