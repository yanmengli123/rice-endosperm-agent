"""执行轨迹用例层：快照/补拉/投影重建/Outbox relay/worker 失联收敛。

读取路径（快照 + after_sequence 补拉）与 SSE 实时通道共同构成
「At-least-once + 幂等消费 + 缺口检测」的恢复语义：客户端以
``snapshot_sequence`` 起步，SSE 重放按 sequence 去重，缺口走 HTTP 补拉。
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from yuxi.repositories.agent_run_repository import AgentRunRepository
from yuxi.repositories.trace_repository import (
    TraceRepository,
    serialize_trace_event,
    serialize_trace_span,
    serialize_trace_summary,
)
from yuxi.storage.postgres.models_business import UsageLedger
from yuxi.trace import projector
from yuxi.trace.recorder import TraceRecorder
from yuxi.utils.datetime_utils import utc_now
from yuxi.utils.logging_config import logger

TRACE_SNAPSHOT_DEFAULT_LIMIT = 500
TRACE_CATCHUP_MAX_LIMIT = 1000
TRACE_STREAM_POLL_SECONDS = 0.5
TRACE_STREAM_HEARTBEAT_SECONDS = 15
TRACE_RETENTION_DAYS = max(1, int(os.getenv("TRACE_RETENTION_DAYS", "90")))
TRACE_RETENTION_BATCH_RUNS = max(
    1,
    min(1000, int(os.getenv("TRACE_RETENTION_BATCH_RUNS", "100"))),
)
TRACE_RETENTION_MAX_BATCHES = max(
    1,
    min(100, int(os.getenv("TRACE_RETENTION_MAX_BATCHES", "20"))),
)


async def _require_run(run_id: str, current_uid: str, db: AsyncSession):
    run = await AgentRunRepository(db).get_run_for_user(run_id, str(current_uid))
    if not run:
        raise HTTPException(status_code=404, detail="运行任务不存在")
    return run


async def get_run_trace_snapshot(
    *, run_id: str, current_uid: str, db: AsyncSession, include_admin: bool = False
) -> dict[str, Any]:
    """一次返回 run 的轨迹快照：summary + span 列表 + snapshot_sequence。"""
    run = await _require_run(run_id, current_uid, db)
    repo = TraceRepository(db)
    # Flush allocates its ledger range and advances every projection while it
    # holds an exclusive lock on this row. Acquire the matching shared lock
    # first so summary/spans/head cannot be read across two flush commits and
    # expose a projection cursor newer than the returned projection itself.
    head = await repo.get_head(run_id, for_share=True)
    summary_row = await repo.get_summary(run_id)
    spans = await repo.list_spans(run_id, visibility=None if include_admin else "USER")

    snapshot_sequence = int(getattr(head, "last_sequence", 0) or 0)
    projection_sequence = int(getattr(head, "projection_sequence", 0) or 0)
    if head is None:
        snapshot_sequence = await repo.max_sequence(run_id)
        projection_sequence = int(getattr(summary_row, "last_sequence", 0) or 0)
    if summary_row is None:
        # 投影缺失（如 run 在投影上线前执行过）：直接以账本 MAX(sequence) 兜底
        snapshot_sequence = await repo.max_sequence(run_id)

    summary: dict[str, Any] | None = None
    if summary_row is not None:
        summary = serialize_trace_summary(summary_row)
    elif snapshot_sequence > 0:
        # 投影缺失但账本存在：就地重放一次，不落库（读取路径无写权限假设）
        events = []
        cursor = 0
        while True:
            page = await repo.list_events(run_id, after_sequence=cursor, limit=1000)
            if not page:
                break
            events.extend(page)
            cursor = int(page[-1].sequence)
            if len(page) < 1000:
                break
        header = {
            "run_id": run_id,
            "thread_id": getattr(events[0], "thread_id", None) if events else None,
        }
        _, summary_state = projector.replay_events([TraceRecorder.row_to_event(row) for row in events], header)
        summary = {key: value for key, value in summary_state.items() if key != "tenant_id"}

    if summary is not None:
        summary.pop("tenant_id", None)
        usage = (
            await db.execute(
                select(
                    func.count(UsageLedger.id),
                    func.sum(UsageLedger.input_tokens),
                    func.sum(UsageLedger.output_tokens),
                    func.sum(UsageLedger.total_tokens),
                ).where(
                    UsageLedger.run_id == run_id,
                    UsageLedger.tenant_id == run.tenant_id,
                )
            )
        ).one()
        if int(usage[0] or 0) > 0:
            summary["input_tokens"] = int(usage[1] or 0)
            summary["output_tokens"] = int(usage[2] or 0)
            summary["total_tokens"] = int(usage[3] or 0)
            summary.setdefault("attributes", {})["usage_source"] = "usage_ledger"
        else:
            summary["input_tokens"] = None
            summary["output_tokens"] = None
            summary["total_tokens"] = None

    span_items = [serialize_trace_span(row) for row in spans]
    return {
        "run_id": run_id,
        "summary": summary,
        "spans": span_items,
        "snapshot_sequence": snapshot_sequence,
        "projection_sequence": projection_sequence,
    }


async def list_run_trace_events(
    *,
    run_id: str,
    current_uid: str,
    db: AsyncSession,
    after_sequence: int = 0,
    limit: int = TRACE_SNAPSHOT_DEFAULT_LIMIT,
    include_admin: bool = False,
) -> dict[str, Any]:
    """缺口补拉：返回 sequence 严格大于 after_sequence 的事件，升序。"""
    await _require_run(run_id, current_uid, db)
    bounded_limit = max(1, min(int(limit or TRACE_SNAPSHOT_DEFAULT_LIMIT), TRACE_CATCHUP_MAX_LIMIT))
    repo = TraceRepository(db)
    rows = await repo.list_events(run_id, after_sequence=int(after_sequence or 0), limit=bounded_limit)
    events = [serialize_trace_event(row) for row in rows]
    if not include_admin:
        events = [event for event in events if event.get("visibility") != "ADMIN"]
    # 游标代表已扫描的账本位置，而非最后一个可见事件；这样 ADMIN-only 页不会
    # 让普通用户无限拉取，也不会因为 sequence 空洞误判丢帧。
    next_after = int(rows[-1].sequence) if rows else int(after_sequence or 0)
    return {
        "run_id": run_id,
        "events": events,
        "next_after_sequence": next_after,
        "scanned_through_sequence": next_after,
        "has_more": len(rows) >= bounded_limit,
    }


async def list_run_trace_spans(*, run_id: str, current_uid: str, db: AsyncSession) -> dict[str, Any]:
    await _require_run(run_id, current_uid, db)
    spans = await TraceRepository(db).list_spans(run_id, visibility="USER")
    return {"run_id": run_id, "spans": [serialize_trace_span(row) for row in spans]}


async def stream_run_trace_events(*, run_id: str, after_sequence: int = 0):
    """独立的 durable Trace SSE；游标是 PostgreSQL ledger sequence。"""
    from yuxi.repositories.agent_run_repository import TERMINAL_RUN_STATUSES
    from yuxi.storage.postgres.manager import pg_manager

    cursor = max(0, int(after_sequence or 0))
    heartbeat_elapsed = 0.0
    while True:
        async with pg_manager.get_async_session_context() as db:
            rows = await TraceRepository(db).list_events(run_id, after_sequence=cursor, limit=500)
            run = await AgentRunRepository(db).get_run(run_id)
        for row in rows:
            cursor = int(row.sequence)
            if row.visibility != "USER":
                continue
            payload = serialize_trace_event(row)
            yield (f"event: trace\nid: {cursor}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n")
        if run is None:
            return
        if run.status in TERMINAL_RUN_STATUSES and not rows:
            return
        if rows:
            heartbeat_elapsed = 0.0
            continue
        await asyncio.sleep(TRACE_STREAM_POLL_SECONDS)
        heartbeat_elapsed += TRACE_STREAM_POLL_SECONDS
        if heartbeat_elapsed >= TRACE_STREAM_HEARTBEAT_SECONDS:
            heartbeat_elapsed = 0.0
            # 心跳携带游标 id：客户端可据 lastEventId 停滞判定假死连接。
            yield f"id: {cursor}\n: heartbeat\n\n"


async def rebuild_run_trace_projection(*, run_id: str, db: AsyncSession) -> int:
    """DELETE 投影 → 重放账本 → 重建 span/summary。返回重放事件数。"""
    repo = TraceRepository(db)
    run = await AgentRunRepository(db).get_run(run_id)
    if run is None:
        return 0
    run_summary = await repo.get_summary(run_id)
    header = {
        "run_id": run_id,
        "thread_id": getattr(run_summary, "thread_id", None),
        "tenant_id": run.tenant_id,
        "agent_slug": getattr(run_summary, "agent_slug", None),
        "run_type": getattr(run_summary, "run_type", None),
        "request_id": getattr(run_summary, "request_id", None),
        "created_by_run_id": getattr(run_summary, "created_by_run_id", None),
    }
    events: list[dict[str, Any]] = []
    cursor = 0
    while True:
        rows = await repo.list_events(run_id, after_sequence=cursor, limit=1000)
        if not rows:
            break
        events.extend(TraceRecorder.row_to_event(row) for row in rows)
        cursor = int(rows[-1].sequence)
        if len(rows) < 1000:
            break
    spans, summary = projector.replay_events(events, header)
    await repo.delete_projection(run_id)
    await repo.upsert_spans(run_id, list(spans.values()), tenant_id=int(run.tenant_id))
    await repo.upsert_summary(run_id, summary)
    head = await repo.get_head(run_id)
    if head is not None:
        head.projection_sequence = int(summary.get("last_sequence") or 0)
    logger.info(f"Rebuilt trace projection for run {run_id} from {len(events)} events")
    return len(events)


async def relay_trace_outbox(ctx: Any = None) -> int:
    """Outbox relay：把待投递轨迹事件补发进 Redis Stream。

    fast-path（recorder commit 后直发）失败或 worker 崩溃时由此兜底，
    保证「实时性可降级、事实不丢」。
    """
    del ctx
    from yuxi.services.run_queue_service import append_run_stream_event
    from yuxi.storage.postgres.manager import pg_manager

    delivered = 0
    try:
        claimed: list[dict[str, Any]] = []
        async with pg_manager.get_async_session_context() as db:
            repo = TraceRepository(db)
            rows = await repo.claim_pending_outbox(limit=100)
            if not rows:
                return 0
            claimed = [
                {
                    "id": int(row.id),
                    "run_id": row.run_id,
                    "sequence": int(row.sequence),
                    "payload": row.payload if isinstance(row.payload, dict) else {},
                    "lease_id": str(row.lease_id),
                }
                for row in rows
            ]
        # claim 已提交后再访问 Redis；进程崩溃时租约会被下一轮回收。
        lease_id = claimed[0]["lease_id"]
        try:
            for item in sorted(claimed, key=lambda value: value["sequence"]):
                payload = item["payload"]
                if payload.get("visibility") != "USER":
                    continue
                await append_run_stream_event(
                    item["run_id"],
                    "trace",
                    {"trace": payload},
                    thread_id=payload.get("thread_id"),
                )
            async with pg_manager.get_async_session_context() as db:
                await TraceRepository(db).mark_outbox_processed([item["id"] for item in claimed], lease_id=lease_id)
            delivered = len(claimed)
        except Exception as error:  # noqa: BLE001
            logger.warning(f"trace outbox relay failed for run {claimed[0]['run_id']}: {type(error).__name__}")
            async with pg_manager.get_async_session_context() as db:
                await TraceRepository(db).fail_outbox_claim(
                    [item["id"] for item in claimed],
                    lease_id=lease_id,
                    error="redis_publish_failed",
                )
    except Exception as error:  # noqa: BLE001
        logger.warning(f"trace outbox relay iteration failed: {error}")
    return delivered


async def purge_expired_trace_runs(ctx: Any = None) -> int:
    """Purge one bounded batch through the database-owned retention boundary.

    Only complete STANDARD runs older than the configured cutoff are eligible;
    the database function rejects active runs, pending delivery, legal hold, and
    cutoffs newer than 24 hours.  No application path disables the append-only
    trigger.
    """
    del ctx
    from yuxi.storage.postgres.manager import pg_manager

    cutoff = utc_now() - timedelta(days=TRACE_RETENTION_DAYS)
    total_deleted = 0
    for _ in range(TRACE_RETENTION_MAX_BATCHES):
        try:
            # One transaction per batch releases row/advisory locks and drops
            # the function's temporary candidate table before the next pass.
            async with pg_manager.get_async_session_context() as db:
                deleted = (
                    await db.execute(
                        text("SELECT yuxi_purge_trace_runs(:cutoff, :batch_limit)"),
                        {"cutoff": cutoff, "batch_limit": TRACE_RETENTION_BATCH_RUNS},
                    )
                ).scalar_one()
            count = int(deleted or 0)
            total_deleted += count
            if count == 0:
                break
        except Exception as error:  # noqa: BLE001 - maintenance must not stop the worker
            logger.warning(f"trace retention iteration failed: {type(error).__name__}")
            break
    if total_deleted:
        logger.info(f"trace retention purged {total_deleted} STANDARD events")
    return total_deleted


async def record_run_lost(*, run_id: str) -> None:
    """worker 失联收敛：为孤儿 run 补发终态事件并闭合 RUNNING span。

    只新增事件（run.execution.failed / span.interrupted + error.type=worker_lost），
    绝不 UPDATE 历史——与账本 append-only 语义一致。由对账任务在
    置终态后调用。
    """
    from yuxi.storage.postgres.manager import pg_manager

    try:
        async with pg_manager.get_async_session_context() as db:
            run = await AgentRunRepository(db).get_run(run_id)
            if run is None:
                return
            recorder = TraceRecorder(
                run_id=run_id,
                thread_id=run.conversation_thread_id,
                tenant_id=run.tenant_id,
                uid=run.uid,
                agent_slug=run.agent_slug,
                run_type=run.run_type,
                request_id=run.request_id,
                created_by_run_id=run.created_by_run_id,
            )
        await recorder.seed()
        recorder.activate()
        try:
            # seed 重放后仍处于 RUNNING 的 span：worker 已失联，不可能自然闭合
            recorder.close_running_spans(suffix="interrupted", error_type="worker_lost")
            status_value = run.status if run.status in {"cancelled", "interrupted"} else "failed"
            recorder.record_run_terminal(
                status_value,
                error_type=getattr(run, "error_type", None) or "worker_lost",
                error_message=getattr(run, "error_message", None) or "执行中断（worker 失联），轨迹由对账任务收敛",
                attributes={"reconciled_at": utc_now().isoformat()},
            )
        finally:
            await recorder.finalize()
    except Exception as error:  # noqa: BLE001 - 收敛失败不影响对账主流程
        logger.warning(f"trace run-lost closure failed for run {run_id}: {error}")
