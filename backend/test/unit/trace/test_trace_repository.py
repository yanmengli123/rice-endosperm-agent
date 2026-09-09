"""Trace 仓储失败恢复与权限边界单测（SQLite 内存库）。

覆盖反馈阻断项的核心状态机：head 行锁序号分配、Outbox 租约认领/ACK/回收/
死信、可见性过滤。PostgreSQL 特有的行锁语义由烟测在真实库上验证，
本层只保证状态机流转正确。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from yuxi.repositories.trace_repository import TRACE_OUTBOX_LEASE_SECONDS, TraceRepository
from yuxi.storage.postgres.models_business import AgentRun
from yuxi.storage.postgres.models_trace import (
    AgentRunTraceEvent,
    AgentRunTraceHead,
    AgentRunTraceOutbox,
    AgentRunTraceSpan,
    AgentRunTraceSummary,
)
from yuxi.utils.datetime_utils import utc_now

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

TRACE_ID = "0123456789abcdef0123456789abcdef"
ROOT_SPAN = "0123456789abcdef"


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for table in (
            AgentRun.__table__,
            AgentRunTraceHead.__table__,
            AgentRunTraceEvent.__table__,
            AgentRunTraceOutbox.__table__,
            AgentRunTraceSpan.__table__,
            AgentRunTraceSummary.__table__,
        ):
            await conn.run_sync(table.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        yield db
    await engine.dispose()


def _add_run(db, run_id: str = "run-1") -> None:
    db.add(
        AgentRun(
            id=run_id,
            conversation_thread_id="thread-1",
            agent_slug="ChatbotAgent",
            uid="user-1",
            status="completed",
            request_id=f"req-{run_id}",
            input_payload={},
        )
    )


async def _add_outbox(db, run_id: str, sequence: int, *, status: str = "PENDING", attempts: int = 0) -> int:
    row = AgentRunTraceOutbox(
        id=sequence,
        tenant_id=1,
        run_id=run_id,
        event_id=f"evt-{sequence}",
        sequence=sequence,
        payload={"sequence": sequence},
        status=status,
        attempts=attempts,
        available_at=utc_now() - timedelta(seconds=1),
    )
    db.add(row)
    await db.flush()
    return row.id


async def test_lock_and_allocate_assigns_dense_contiguous_sequences(session):
    _add_run(session)
    repo = TraceRepository(session)

    start, head = await repo.lock_and_allocate_sequences(
        run_id="run-1", tenant_id=1, trace_id=TRACE_ID, root_span_id=ROOT_SPAN, count=3
    )
    assert start == 1
    assert head.last_sequence == 3

    start2, head2 = await repo.lock_and_allocate_sequences(
        run_id="run-1", tenant_id=1, trace_id=TRACE_ID, root_span_id=ROOT_SPAN, count=2
    )
    assert start2 == 4
    assert head2.last_sequence == 5
    assert head2.run_id == "run-1"


async def test_claim_pending_outbox_respects_head_lease_and_backoff(session):
    _add_run(session)
    repo = TraceRepository(session)
    await repo.lock_and_allocate_sequences(
        run_id="run-1", tenant_id=1, trace_id=TRACE_ID, root_span_id=ROOT_SPAN, count=2
    )
    await _add_outbox(session, "run-1", 1)
    await _add_outbox(session, "run-1", 2)
    await session.commit()

    claimed = await repo.claim_pending_outbox()
    assert [row.sequence for row in claimed] == [1, 2]
    assert all(row.status == "PROCESSING" for row in claimed)
    lease = claimed[0].lease_id
    assert lease
    await session.commit()

    # head 租约未释放：同一 run 的后续 PENDING 不被并发 relay 认领
    await _add_outbox(session, "run-1", 3)
    await session.commit()
    assert await repo.claim_pending_outbox() == []

    # 租约释放后，退避中的行（available_at 未来）仍不认领
    head = (await session.execute(select(AgentRunTraceHead))).scalar_one()
    head.relay_lease_id = None
    head.relay_leased_at = None
    backing_off = (
        await session.execute(select(AgentRunTraceOutbox).where(AgentRunTraceOutbox.sequence == 3))
    ).scalar_one()
    backing_off.available_at = utc_now() + timedelta(seconds=60)
    await session.commit()
    assert await repo.claim_pending_outbox() == []

    backing_off.available_at = utc_now() - timedelta(seconds=1)
    await session.commit()
    final_claim = await repo.claim_pending_outbox()
    assert [row.sequence for row in final_claim] == [3]
    await session.commit()


async def test_claim_reclaims_stale_processing_rows(session):
    """relay 认领后崩溃：租约过期的 PROCESSING 行被回收回 PENDING。"""
    _add_run(session)
    repo = TraceRepository(session)
    await repo.lock_and_allocate_sequences(
        run_id="run-1", tenant_id=1, trace_id=TRACE_ID, root_span_id=ROOT_SPAN, count=1
    )
    await _add_outbox(session, "run-1", 1)
    await session.commit()

    claimed = await repo.claim_pending_outbox()
    assert len(claimed) == 1
    old_lease = claimed[0].lease_id
    await session.commit()

    # 模拟 worker 崩溃：租约时间拨回过期前
    row = (await session.execute(select(AgentRunTraceOutbox))).scalar_one()
    row.leased_at = utc_now() - timedelta(seconds=TRACE_OUTBOX_LEASE_SECONDS + 60)
    head = (await session.execute(select(AgentRunTraceHead))).scalar_one()
    head.relay_leased_at = row.leased_at
    await session.commit()

    reclaimed = await repo.claim_pending_outbox()
    assert [r.sequence for r in reclaimed] == [1]
    assert reclaimed[0].status == "PROCESSING"
    assert reclaimed[0].lease_id != old_lease  # 新租约（旧租约被回收清除）
    await session.commit()


async def test_mark_processed_requires_matching_lease(session):
    """ACK 必须带正确租约：过期租约被回收后，旧 relay 的迟到 ACK 不得生效。"""
    _add_run(session)
    repo = TraceRepository(session)
    await repo.lock_and_allocate_sequences(
        run_id="run-1", tenant_id=1, trace_id=TRACE_ID, root_span_id=ROOT_SPAN, count=1
    )
    outbox_id = await _add_outbox(session, "run-1", 1)
    await session.commit()

    claimed = await repo.claim_pending_outbox()
    stale_lease = claimed[0].lease_id
    await session.commit()

    # 租约过期回收 + 二次认领
    row = (await session.execute(select(AgentRunTraceOutbox))).scalar_one()
    head = (await session.execute(select(AgentRunTraceHead))).scalar_one()
    row.leased_at = utc_now() - timedelta(seconds=TRACE_OUTBOX_LEASE_SECONDS + 60)
    head.relay_leased_at = row.leased_at
    await session.commit()
    reclaimed = await repo.claim_pending_outbox()
    current_lease = reclaimed[0].lease_id
    await session.commit()

    # 旧 relay 迟到 ACK：无效
    await repo.mark_outbox_processed([outbox_id], lease_id=stale_lease)
    await session.commit()
    row = (await session.execute(select(AgentRunTraceOutbox))).scalar_one()
    assert row.status == "PROCESSING"

    # 现任租约 ACK：生效
    await repo.mark_outbox_processed([outbox_id], lease_id=current_lease)
    await session.commit()
    row = (await session.execute(select(AgentRunTraceOutbox))).scalar_one()
    assert row.status == "PROCESSED"
    assert row.processed_at is not None


async def test_fail_outbox_claim_backs_off_and_dead_letters(session):
    _add_run(session)
    repo = TraceRepository(session)
    await repo.lock_and_allocate_sequences(
        run_id="run-1", tenant_id=1, trace_id=TRACE_ID, root_span_id=ROOT_SPAN, count=2
    )
    first = await _add_outbox(session, "run-1", 1, attempts=19)  # 已达上限-1
    second = await _add_outbox(session, "run-1", 2)
    await session.commit()

    claimed = await repo.claim_pending_outbox()
    lease = claimed[0].lease_id
    await session.commit()

    await repo.fail_outbox_claim([first, second], lease_id=lease, error="redis_down")
    await session.commit()
    rows = {row.sequence: row for row in (await session.execute(select(AgentRunTraceOutbox))).scalars().all()}
    assert rows[1].status == "EXPIRED"  # attempts 达到 20 上限 → 死信（账本事实仍在）
    assert rows[2].status == "PENDING"
    assert rows[2].attempts == 1
    assert rows[2].available_at > utc_now()
    assert "redis_down" in (rows[2].last_error or "")


async def test_list_spans_visibility_filter(session):
    _add_run(session)
    repo = TraceRepository(session)
    await repo.lock_and_allocate_sequences(
        run_id="run-1", tenant_id=1, trace_id=TRACE_ID, root_span_id=ROOT_SPAN, count=1
    )
    db_rows = [
        AgentRunTraceSpan(
            id=1,
            tenant_id=1,
            run_id="run-1",
            span_id="run",
            category="RUN",
            operation="execution",
            status="COMPLETED",
            visibility="USER",
        ),
        AgentRunTraceSpan(
            id=2,
            tenant_id=1,
            run_id="run-1",
            span_id="diag",
            category="SYSTEM",
            operation="diagnostics",
            status="COMPLETED",
            visibility="ADMIN",
        ),
    ]
    session.add_all(db_rows)
    await session.commit()

    user_spans = await repo.list_spans("run-1", visibility="USER")
    assert [span.span_id for span in user_spans] == ["run"]
    all_spans = await repo.list_spans("run-1", visibility=None)
    assert {span.span_id for span in all_spans} == {"run", "diag"}
