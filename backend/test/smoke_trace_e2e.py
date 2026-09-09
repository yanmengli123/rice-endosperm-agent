"""执行轨迹全链路冒烟（真实 PG + Redis，容器内一次性运行）。

覆盖：recorder 生命周期 → 账本/Outbox/投影同事务落库 → fast-path 直发 Redis
→ Outbox 标记 PROCESSED → 快照/补拉查询 → append-only trigger → 脱敏。
运行：docker compose exec api uv run python test/smoke_trace_e2e.py
"""

import asyncio
import uuid
from datetime import timedelta

from sqlalchemy import delete, select, text, update
from yuxi.repositories.trace_repository import TraceRepository
from yuxi.services.agent_run_service import acknowledge_agent_run_dispatch, dispatch_pending_agent_runs
from yuxi.services.run_queue_service import list_run_stream_events
from yuxi.services.run_worker import _persist_terminal_trace
from yuxi.services.trace_service import (
    get_run_trace_snapshot,
    list_run_trace_events,
    rebuild_run_trace_projection,
    relay_trace_outbox,
)
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import AgentRun, AgentRunDispatchOutbox
from yuxi.storage.postgres.models_trace import (
    AgentRunTraceEvent,
    AgentRunTraceHead,
    AgentRunTraceOutbox,
    AgentRunTraceSpan,
    AgentRunTraceSummary,
)
from yuxi.trace import TraceRecorder
from yuxi.utils.datetime_utils import utc_now

RUN_ID = f"smoke-trace-{uuid.uuid4().hex[:8]}"
THREAD_ID = f"smoke-thread-{RUN_ID}"
UID = "smoke-user"


async def _verify_concurrent_head_allocation() -> bool:
    """真实 PG 多 session 必须由 head 行锁分配无重复、无空洞序号。"""
    lock_run_id = f"smoke-lock-{uuid.uuid4().hex[:8]}"
    async with pg_manager.get_async_session_context() as db:
        db.add(
            AgentRun(
                id=lock_run_id,
                conversation_thread_id=f"thread-{lock_run_id}",
                agent_slug="ChatbotAgent",
                uid=UID,
                tenant_id=1,
                status="completed",
                request_id=f"req-{lock_run_id}",
                input_payload={},
            )
        )
        db.add(
            AgentRunTraceHead(
                run_id=lock_run_id,
                tenant_id=1,
                trace_id=uuid.uuid4().hex,
                root_span_id=uuid.uuid4().hex[:16],
                last_sequence=0,
                projection_sequence=0,
            )
        )

    async def allocate_one() -> int:
        async with pg_manager.get_async_session_context() as db:
            start, _ = await TraceRepository(db).lock_and_allocate_sequences(
                run_id=lock_run_id,
                tenant_id=1,
                trace_id=uuid.uuid4().hex,
                root_span_id=uuid.uuid4().hex[:16],
                count=1,
            )
            return start

    starts = await asyncio.wait_for(
        asyncio.gather(*(allocate_one() for _ in range(32))),
        timeout=15,
    )
    async with pg_manager.get_async_session_context() as db:
        head = await TraceRepository(db).get_head(lock_run_id)
        await db.execute(delete(AgentRun).where(AgentRun.id == lock_run_id))
    return sorted(starts) == list(range(1, 33)) and head.last_sequence == 32


async def _verify_retention_purge() -> bool:
    """仅删除专用、过期、STANDARD、终态测试 run 的整套 Trace 数据。"""
    retention_run_id = f"smoke-retention-{uuid.uuid4().hex[:8]}"
    trace_id = uuid.uuid4().hex
    root_span_id = uuid.uuid4().hex[:16]
    async with pg_manager.get_async_session_context() as db:
        db.add(
            AgentRun(
                id=retention_run_id,
                conversation_thread_id=f"thread-{retention_run_id}",
                agent_slug="ChatbotAgent",
                uid=UID,
                tenant_id=1,
                status="completed",
                request_id=f"req-{retention_run_id}",
                input_payload={},
            )
        )
        db.add(
            AgentRunTraceHead(
                run_id=retention_run_id,
                tenant_id=1,
                trace_id=trace_id,
                root_span_id=root_span_id,
                last_sequence=1,
                projection_sequence=0,
            )
        )
        db.add(
            AgentRunTraceEvent(
                tenant_id=1,
                uid=UID,
                run_id=retention_run_id,
                thread_id=f"thread-{retention_run_id}",
                event_id=f"evt_{uuid.uuid4().hex}",
                trace_id=trace_id,
                sequence=1,
                category="RUN",
                operation="execution",
                event_type="run.execution.completed",
                span_id=root_span_id,
                occurred_at=utc_now() - timedelta(days=3),
                visibility="USER",
                sensitivity="INTERNAL",
                retention_class="STANDARD",
            )
        )

    async with pg_manager.get_async_session_context() as db:
        deleted = (
            await db.execute(
                text("SELECT yuxi_purge_trace_runs(:cutoff, 1)"),
                {"cutoff": utc_now() - timedelta(days=2)},
            )
        ).scalar_one()
        remaining = (
            await db.execute(select(AgentRunTraceEvent).where(AgentRunTraceEvent.run_id == retention_run_id))
        ).scalar_one_or_none()
        await db.execute(delete(AgentRun).where(AgentRun.id == retention_run_id))
    return int(deleted or 0) == 1 and remaining is None


async def _verify_worker_dispatch_ack() -> bool:
    """ARQ 已接单时必须自愈 enqueue 成功、DB ack 失败留下的 PENDING 行。"""
    dispatch_run_id = f"smoke-dispatch-{uuid.uuid4().hex[:8]}"
    async with pg_manager.get_async_session_context() as db:
        db.add(
            AgentRun(
                id=dispatch_run_id,
                conversation_thread_id=f"thread-{dispatch_run_id}",
                agent_slug="ChatbotAgent",
                uid=UID,
                tenant_id=1,
                status="pending",
                request_id=f"req-{dispatch_run_id}",
                input_payload={},
            )
        )
        db.add(
            AgentRunDispatchOutbox(
                tenant_id=1,
                run_id=dispatch_run_id,
                status="PENDING",
            )
        )

    await acknowledge_agent_run_dispatch(dispatch_run_id)
    async with pg_manager.get_async_session_context() as db:
        row = (
            await db.execute(select(AgentRunDispatchOutbox).where(AgentRunDispatchOutbox.run_id == dispatch_run_id))
        ).scalar_one()
        acknowledged = row.status == "DISPATCHED" and row.dispatched_at is not None
        await db.execute(delete(AgentRun).where(AgentRun.id == dispatch_run_id))
    return acknowledged


async def _verify_terminal_dispatch_settlement() -> bool:
    """已终态且未获回执的投递意图必须收敛，不能永久伪装成待派发。"""
    dispatch_run_id = f"smoke-settled-dispatch-{uuid.uuid4().hex[:8]}"
    async with pg_manager.get_async_session_context() as db:
        db.add(
            AgentRun(
                id=dispatch_run_id,
                conversation_thread_id=f"thread-{dispatch_run_id}",
                agent_slug="ChatbotAgent",
                uid=UID,
                tenant_id=1,
                status="failed",
                request_id=f"req-{dispatch_run_id}",
                input_payload={},
            )
        )
        db.add(
            AgentRunDispatchOutbox(
                tenant_id=1,
                run_id=dispatch_run_id,
                status="PENDING",
            )
        )

    await dispatch_pending_agent_runs(run_id=dispatch_run_id)
    async with pg_manager.get_async_session_context() as db:
        row = (
            await db.execute(select(AgentRunDispatchOutbox).where(AgentRunDispatchOutbox.run_id == dispatch_run_id))
        ).scalar_one()
        settled = row.status == "SETTLED" and row.dispatched_at is None and row.last_error == "run_already_terminal"
        await db.execute(delete(AgentRun).where(AgentRun.id == dispatch_run_id))
    return settled


async def _verify_snapshot_projection_boundary() -> bool:
    """A snapshot must wait for an in-flight projection commit at the trace head."""
    snapshot_run_id = f"smoke-snapshot-{uuid.uuid4().hex[:8]}"
    snapshot_thread_id = f"thread-{snapshot_run_id}"
    trace_id = uuid.uuid4().hex
    root_span_id = uuid.uuid4().hex[:16]
    async with pg_manager.get_async_session_context() as db:
        db.add(
            AgentRun(
                id=snapshot_run_id,
                conversation_thread_id=snapshot_thread_id,
                agent_slug="ChatbotAgent",
                uid=UID,
                tenant_id=1,
                status="completed",
                request_id=f"req-{snapshot_run_id}",
                input_payload={},
            )
        )
        db.add(
            AgentRunTraceHead(
                run_id=snapshot_run_id,
                tenant_id=1,
                trace_id=trace_id,
                root_span_id=root_span_id,
                last_sequence=0,
                projection_sequence=0,
            )
        )

    locked = asyncio.Event()
    release = asyncio.Event()

    async def write_projection() -> None:
        async with pg_manager.get_async_session_context() as db:
            head = (
                await db.execute(
                    select(AgentRunTraceHead).where(AgentRunTraceHead.run_id == snapshot_run_id).with_for_update()
                )
            ).scalar_one()
            occurred_at = utc_now()
            head.last_sequence = 1
            head.projection_sequence = 1
            db.add(
                AgentRunTraceEvent(
                    tenant_id=1,
                    uid=UID,
                    run_id=snapshot_run_id,
                    thread_id=snapshot_thread_id,
                    event_id=f"evt_{uuid.uuid4().hex}",
                    trace_id=trace_id,
                    sequence=1,
                    category="RUN",
                    operation="execution",
                    event_type="run.execution.completed",
                    span_id=root_span_id,
                    occurred_at=occurred_at,
                    visibility="USER",
                    sensitivity="INTERNAL",
                    retention_class="STANDARD",
                )
            )
            db.add(
                AgentRunTraceSpan(
                    tenant_id=1,
                    run_id=snapshot_run_id,
                    thread_id=snapshot_thread_id,
                    span_id=root_span_id,
                    category="RUN",
                    operation="execution",
                    status="COMPLETED",
                    started_at=occurred_at,
                    finished_at=occurred_at,
                    duration_ms=0,
                )
            )
            db.add(
                AgentRunTraceSummary(
                    tenant_id=1,
                    run_id=snapshot_run_id,
                    thread_id=snapshot_thread_id,
                    status="completed",
                    trace_event_count=1,
                    last_sequence=1,
                    projection_sequence=1,
                )
            )
            await db.flush()
            locked.set()
            await release.wait()

    async def read_snapshot() -> dict:
        async with pg_manager.get_async_session_context() as db:
            return await get_run_trace_snapshot(run_id=snapshot_run_id, current_uid=UID, db=db)

    writer = asyncio.create_task(write_projection())
    await asyncio.wait_for(locked.wait(), timeout=2)
    reader = asyncio.create_task(read_snapshot())
    await asyncio.sleep(0.1)
    waited_for_writer = not reader.done()
    release.set()
    try:
        snapshot = await asyncio.wait_for(reader, timeout=2)
        await asyncio.wait_for(writer, timeout=2)
    finally:
        release.set()
        if not writer.done():
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)
        if not reader.done():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

    async with pg_manager.get_async_session_context() as db:
        await db.execute(delete(AgentRun).where(AgentRun.id == snapshot_run_id))
    return (
        waited_for_writer
        and snapshot["snapshot_sequence"] == 1
        and snapshot["projection_sequence"] == 1
        and len(snapshot["spans"]) == 1
        and (snapshot["summary"] or {}).get("last_sequence") == 1
    )


async def _verify_terminal_closes_open_child_span() -> bool:
    cancel_run_id = f"smoke-cancel-{uuid.uuid4().hex[:8]}"
    cancel_thread_id = f"thread-{cancel_run_id}"
    async with pg_manager.get_async_session_context() as db:
        db.add(
            AgentRun(
                id=cancel_run_id,
                conversation_thread_id=cancel_thread_id,
                agent_slug="ChatbotAgent",
                uid=UID,
                tenant_id=1,
                status="running",
                request_id=f"req-{cancel_run_id}",
                input_payload={},
            )
        )

    recorder = TraceRecorder(
        run_id=cancel_run_id,
        thread_id=cancel_thread_id,
        tenant_id=1,
        uid=UID,
        agent_slug="ChatbotAgent",
        run_type="chat",
        request_id=f"req-{cancel_run_id}",
    )
    await recorder.seed()
    recorder.activate()
    try:
        recorder.start_span(category="RUN", operation="execution", span_id="run")
        model_span_id = recorder.start_span(category="MODEL", operation="generation")
        committed = await _persist_terminal_trace(
            recorder,
            "cancelled",
            error_type="cancelled",
        )
    finally:
        await recorder.finalize()

    async with pg_manager.get_async_session_context() as db:
        spans = await TraceRepository(db).list_spans(cancel_run_id)
        run = (await db.execute(select(AgentRun).where(AgentRun.id == cancel_run_id))).scalar_one()
        model_span = next((span for span in spans if span.span_id == model_span_id), None)
        result = bool(
            committed
            and run.status == "cancelled"
            and model_span is not None
            and model_span.status == "INTERRUPTED"
            and model_span.error_type == "cancelled"
        )
        await db.execute(delete(AgentRun).where(AgentRun.id == cancel_run_id))
    return result


async def main() -> None:
    pg_manager.initialize()
    await pg_manager.create_business_tables()
    await pg_manager.ensure_business_schema()

    checks: list[tuple[str, bool]] = []
    checks.append(("真实 PG 并发 head 分配 1..32", await _verify_concurrent_head_allocation()))
    checks.append(("快照在 head 共享锁下读取一致投影", await _verify_snapshot_projection_boundary()))
    checks.append(("run 终态会关闭残留 RUNNING 子 span", await _verify_terminal_closes_open_child_span()))
    checks.append(("retention 受控清理过期 STANDARD 终态 run", await _verify_retention_purge()))
    checks.append(("worker 接单自愈残留 PENDING dispatch", await _verify_worker_dispatch_ack()))
    checks.append(("已终态的孤立 dispatch 收敛为 SETTLED", await _verify_terminal_dispatch_settlement()))

    # 1. 造真实 AgentRun 行（trace 表 FK 依赖）
    async with pg_manager.get_async_session_context() as db:
        db.add(
            AgentRun(
                id=RUN_ID,
                conversation_thread_id=THREAD_ID,
                agent_slug="ChatbotAgent",
                uid=UID,
                tenant_id=1,
                status="running",
                request_id=f"req-{RUN_ID}",
                input_payload={},
            )
        )

    # 2. recorder 完整生命周期（埋点混合敏感字段验证脱敏）
    recorder = TraceRecorder(
        run_id=RUN_ID,
        thread_id=THREAD_ID,
        tenant_id=1,
        uid=UID,
        agent_slug="ChatbotAgent",
        run_type="chat",
        request_id=f"req-{RUN_ID}",
    )
    await recorder.seed()
    recorder.activate()
    try:
        run_span_id = recorder.start_span(
            category="RUN",
            operation="execution",
            span_id="run",
            title="本轮执行",
            attributes={"agent_slug": "ChatbotAgent", "run_type": "chat"},
        )
        model_span = recorder.next_span_id("model")
        recorder.emit(
            category="MODEL",
            operation="generation",
            event_type="model.generation.started",
            span_id=model_span,
            title="模型生成",
            attributes={"model_spec": "provider:test-model", "api_key": "sk-should-be-dropped1234"},
        )
        recorder.emit(
            category="MODEL",
            operation="generation",
            event_type="model.generation.first_visible_token",
        )
        recorder.finish_span(
            model_span,
            suffix="completed",
            duration_ms=3800,
            attributes={"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500},
        )
        tool_span = recorder.start_span(
            category="TOOL",
            operation="execution",
            span_id="tool-smoke-1",
            title="query_knowledge_scope",
            attributes={"tool": "query_knowledge_scope", "args_digest": "sha256:abcd1234"},
        )
        checks_w3c = all(
            len(value) == 16 and all(char in "0123456789abcdef" for char in value)
            for value in (run_span_id, model_span, tool_span)
        )
        assert checks_w3c and tool_span != "tool-smoke-1"
        recorder.finish_span(tool_span, suffix="completed", duration_ms=812)
        knowledge_span = recorder.start_span(
            category="KNOWLEDGE",
            operation="search",
            span_id="kr_smoke",
            title="知识检索",
        )
        recorder.finish_span(
            knowledge_span,
            duration_ms=1400,
            summary="召回 12 条证据、3 条 Claim",
            attributes={"claim_count": 3, "evidence_count": 12, "intent": "ENTITY_LOOKUP"},
            resource_refs=[{"type": "knowledge_retrieval", "id": "kr_smoke"}],
        )
        trace_committed = await _persist_terminal_trace(recorder, "completed")
    finally:
        await recorder.finalize()

    checks.append(("终态 Trace 与 AgentRun 同事务提交", trace_committed is True))

    # 3. 账本 + 投影 + Outbox 状态
    async with pg_manager.get_async_session_context() as db:
        repo = TraceRepository(db)
        events = await repo.list_events(RUN_ID, limit=100)
        checks.append(("账本事件数 = 9", len(events) == 9))
        sequences = [event.sequence for event in events]
        checks.append(("sequence 稠密单调 1..9", sequences == list(range(1, 10))))
        model_started = next(e for e in events if e.event_type == "model.generation.started")
        checks.append(
            ("敏感属性已 DROP(api_key)", "api_key" not in (model_started.attributes or {})),
        )
        checks.append(
            ("model_spec 保留", (model_started.attributes or {}).get("model_spec") == "provider:test-model"),
        )
        spans = await repo.list_spans(RUN_ID)
        checks.append(("投影 span 数 = 4(run/model/tool/knowledge)", len(spans) == 4))
        run_span = next(s for s in spans if s.span_id == run_span_id)
        checks.append(("run span 终态 COMPLETED", run_span.status == "COMPLETED"))
        kr_span = next(s for s in spans if s.span_id == knowledge_span)
        checks.append(("knowledge span 带摘要", kr_span.summary and "12" in kr_span.summary))
        summary = await repo.get_summary(RUN_ID)
        checks.append(("summary 状态 completed", summary.status == "completed"))
        checks.append(("summary 不把 trace token 当作计费权威", summary.total_tokens is None))
        checks.append(
            (
                "summary model_calls=1 tool_calls=1 knowledge_calls=1",
                summary.model_calls == 1 and summary.tool_calls == 1 and summary.knowledge_calls == 1,
            )
        )
        checks.append(("TTFT 已记录", summary.ttft_ms is not None and summary.ttft_ms >= 0))
        outbox_rows = (
            (await db.execute(select(AgentRunTraceOutbox).where(AgentRunTraceOutbox.run_id == RUN_ID))).scalars().all()
        )
        checks.append(
            (
                "outbox 全部 PROCESSED(fast-path 成功后标记)",
                len(outbox_rows) == 9 and all(r.status == "PROCESSED" for r in outbox_rows),
            )
        )
        run_row = (await db.execute(select(AgentRun).where(AgentRun.id == RUN_ID))).scalar_one()
        checks.append(("AgentRun 终态 completed", run_row.status == "completed"))

    # 4. Redis 内部传输流包含 USER trace 帧；公共消息 SSE 会在服务层过滤它们。
    stream_events = await list_run_stream_events(RUN_ID, limit=100)
    trace_frames = [e for e in stream_events if e["event_type"] == "trace"]
    checks.append(("Redis 流含 9 条 trace 帧", len(trace_frames) == 9))
    wire_sequences = [
        wire["sequence"]
        for frame in trace_frames
        if isinstance((wire := ((frame["payload"].get("payload") or {}).get("trace"))), dict)
    ]
    checks.append(("wire sequence 完整 1..9", wire_sequences == list(range(1, 10))))

    # 5. 快照 + 补拉（service 层，带归属校验路径）
    async with pg_manager.get_async_session_context() as db:
        snapshot = await get_run_trace_snapshot(run_id=RUN_ID, current_uid=UID, db=db)
        checks.append(("快照 run_id 正确", snapshot["run_id"] == RUN_ID))
        checks.append(("快照 snapshot_sequence = 9", snapshot["snapshot_sequence"] == 9))
        checks.append(("快照 spans = 4", len(snapshot["spans"]) == 4))
        checks.append(("快照 summary 不含租户字段", "tenant_id" not in (snapshot["summary"] or {})))
        catchup = await list_run_trace_events(run_id=RUN_ID, current_uid=UID, db=db, after_sequence=7, limit=5)
        checks.append(("补拉 after=7 返回 8..9", [e["sequence"] for e in catchup["events"]] == [8, 9]))
        catchup_all = await list_run_trace_events(run_id=RUN_ID, current_uid=UID, db=db, after_sequence=0, limit=3)
        checks.append(
            ("补拉分页 has_more", catchup_all["has_more"] is True and catchup_all["next_after_sequence"] == 3)
        )

    # 6. 投影重建（DELETE → replay → 一致）
    async with pg_manager.get_async_session_context() as db:
        await db.execute(delete(AgentRunTraceSpan).where(AgentRunTraceSpan.run_id == RUN_ID))
        await db.execute(delete(AgentRunTraceSummary).where(AgentRunTraceSummary.run_id == RUN_ID))
    async with pg_manager.get_async_session_context() as db:
        replayed = await rebuild_run_trace_projection(run_id=RUN_ID, db=db)
        checks.append(("replay 事件数 = 9", replayed == 9))
        repo = TraceRepository(db)
        summary = await repo.get_summary(RUN_ID)
        checks.append(("重建后 summary 一致", summary.status == "completed" and summary.total_tokens is None))

    # 7. append-only trigger：UPDATE/DELETE 被数据库拒绝
    async with pg_manager.get_async_session_context() as db:
        try:
            await db.execute(
                update(AgentRunTraceEvent).where(AgentRunTraceEvent.run_id == RUN_ID).values(title="tampered")
            )
            await db.commit()
            checks.append(("账本 UPDATE 被 trigger 拒绝", False))
        except Exception as error:
            await db.rollback()
            checks.append(("账本 UPDATE 被 trigger 拒绝", "append-only" in str(error)))

    # 8. relay：无 PENDING 时空转
    delivered = await relay_trace_outbox()
    checks.append(("relay 空转返回 0", delivered == 0))

    # 9. 死锁回归：业务 session 持有 AgentRun 行锁时，已有 head 的 Trace 分配
    # 只能锁 Trace 域，必须立即完成，不能等待业务行。
    async with pg_manager.get_async_session_context() as business_db:
        await business_db.execute(update(AgentRun).where(AgentRun.id == RUN_ID).values(total_tokens=38_562))
        async with pg_manager.get_async_session_context() as trace_db:
            start, head = await asyncio.wait_for(
                TraceRepository(trace_db).lock_and_allocate_sequences(
                    run_id=RUN_ID,
                    tenant_id=1,
                    trace_id=uuid.uuid4().hex,
                    root_span_id=uuid.uuid4().hex[:16],
                    count=0,
                ),
                timeout=2,
            )
            checks.append(("持有 AgentRun 行锁时 Trace head 不阻塞", start == 10 and head.last_sequence == 9))

    # 10. retention 数据库安全门：过新的 cutoff 必须在任何 DELETE 前拒绝。
    async with pg_manager.get_async_session_context() as db:
        try:
            await db.execute(text("SELECT yuxi_purge_trace_runs(NOW(), 1)"))
            checks.append(("retention 拒绝小于 24h 的 cutoff", False))
        except Exception as error:
            await db.rollback()
            checks.append(("retention 拒绝小于 24h 的 cutoff", "24 hours" in str(error)))

    # 11. 清理可变投影。事实账本没有 AgentRun 外键，按 append-only 审计策略留存；
    # 每次冒烟使用随机 run_id，因此不需要、也不允许从应用连接禁用触发器。
    async with pg_manager.get_async_session_context() as db:
        await db.execute(delete(AgentRun).where(AgentRun.id == RUN_ID))

    failed = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if failed:
        raise SystemExit(f"smoke failed: {failed}")
    print(f"\nALL {len(checks)} CHECKS PASSED (run_id={RUN_ID})")


asyncio.run(main())
