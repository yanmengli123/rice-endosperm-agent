"""TraceRecorder：worker 进程内的轨迹记录器。

职责边界：

- ``emit`` 是同步 O(1) 缓冲追加（埋点开销 p95 目标 < 20ms 的关键），
  中间件/编排器通过模块级 ``emit_trace`` 无感调用，无活跃 recorder 时 no-op；
- ``flush`` 是唯一异步落库点：同一 PostgreSQL 事务内写入
  账本事件 + Outbox + Span/Summary 投影，commit 后再尽力直发 Redis
  （fast-path），失败由 Outbox relay 兜底——PG 是事实，Redis 只是实时通道；
- sequence 为 per-run 稠密整数，seed 时从账本 MAX(sequence) 续起并把存量
  事件 replay 进本地投影状态，保证投影状态永远是账本的超集。
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from yuxi.trace import projector
from yuxi.trace.protocol import (
    SENSITIVITY_INTERNAL,
    TRACE_ONCE_PER_RUN_EVENT_TYPES,
    TRACE_SCHEMA_VERSION,
    VISIBILITY_USER,
    build_event_type,
    build_trace_event,
    wire_event,
)
from yuxi.trace.redaction import digest_text, redact_attributes, redact_text
from yuxi.utils.datetime_utils import utc_now
from yuxi.utils.logging_config import logger

_BUFFER_FLUSH_THRESHOLD = 64
_BUFFER_SAFETY_VALVE = 256

_current_recorder: contextvars.ContextVar[TraceRecorder | None] = contextvars.ContextVar(
    "yuxi_current_trace_recorder", default=None
)


def current_recorder() -> TraceRecorder | None:
    """返回当前异步上下文绑定的轨迹记录器（worker run 任务内才有值）。"""
    return _current_recorder.get()


def emit_trace(
    *,
    category: str,
    operation: str,
    event_type: str | None = None,
    **fields: Any,
) -> None:
    """向当前 run 的 recorder 发一条轨迹事件；无活跃 recorder 时 no-op。

    供中间件/知识编排器等深层代码使用——它们只需要 import 本函数，
    不需要知道 recorder 是否存在（API 进程内调用同样是 no-op）。
    """
    recorder = _current_recorder.get()
    if recorder is None:
        return
    try:
        recorder.emit(category=category, operation=operation, event_type=event_type, **fields)
    except Exception as error:  # noqa: BLE001 - 埋点失败绝不能影响业务执行
        logger.warning(f"trace emit failed for run {recorder.run_id}: {error}")


class TraceRecorder:
    def __init__(
        self,
        *,
        run_id: str,
        thread_id: str | None,
        tenant_id: int | None,
        uid: str | None,
        agent_slug: str | None = None,
        run_type: str | None = None,
        request_id: str | None = None,
        created_by_run_id: str | None = None,
        trace_id: str | None = None,
        root_span_id: str | None = None,
    ) -> None:
        self.run_id = run_id
        self._header: dict[str, Any] = {
            "run_id": run_id,
            "thread_id": thread_id,
            "tenant_id": tenant_id,
            "uid": uid,
            "agent_slug": agent_slug,
            "run_type": run_type,
            "request_id": request_id,
            "created_by_run_id": created_by_run_id,
        }
        self._sequence = 0
        self.trace_id = trace_id or secrets.token_hex(16)
        self.root_span_id = root_span_id or secrets.token_hex(8)
        self._seeded = False
        self._disabled = False
        self._buffer: list[dict[str, Any]] = []
        self._spans: dict[str, dict[str, Any]] = {}
        self._summary: dict[str, Any] = projector.new_summary_state(self._header)
        self._span_seq: dict[str, int] = {}
        self._span_aliases: dict[str, str] = {"run": self.root_span_id}
        self._emitted_once: set[str] = set()
        self._token: contextvars.Token | None = None
        self._flush_lock = asyncio.Lock()

    # ------------------------------------------------------------------ 激活

    def activate(self) -> None:
        self._token = _current_recorder.set(self)

    def deactivate(self) -> None:
        if self._token is not None:
            _current_recorder.reset(self._token)
            self._token = None

    # ------------------------------------------------------------------ 事件

    def next_span_id(self, prefix: str) -> str:
        count = self._span_seq.get(prefix, 0) + 1
        self._span_seq[prefix] = count
        return secrets.token_hex(8)

    def _normalize_span_id(self, span_id: str | None) -> str | None:
        if span_id is None:
            return None
        if span_id == "run":
            return self.root_span_id
        if len(span_id) == 16 and all(char in "0123456789abcdef" for char in span_id):
            return span_id
        return self._span_aliases.setdefault(span_id, secrets.token_hex(8))

    def latest_running_span_id(self, category: str) -> str | None:
        for span_id, span in reversed(list(self._spans.items())):
            if span.get("category") == category and span.get("status") == projector.SPAN_STATUS_RUNNING:
                return span_id
        return None

    def emit(
        self,
        *,
        category: str,
        operation: str,
        event_type: str | None = None,
        span_id: str | None = None,
        parent_span_id: str | None = None,
        title: str | None = None,
        summary: str | None = None,
        attributes: dict[str, Any] | None = None,
        resource_refs: list[dict[str, Any]] | None = None,
        duration_ms: int | None = None,
        visibility: str = VISIBILITY_USER,
        sensitivity: str = SENSITIVITY_INTERNAL,
        occurred_at: datetime | None = None,
    ) -> dict[str, Any] | None:
        if self._disabled or self._header.get("tenant_id") is None:
            return None
        resolved_event_type = event_type or build_event_type(category, operation, "occurred")
        if resolved_event_type in TRACE_ONCE_PER_RUN_EVENT_TYPES:
            if resolved_event_type in self._emitted_once:
                return None
            self._emitted_once.add(resolved_event_type)
        final_span_id = self._normalize_span_id(span_id)
        final_parent_span_id = self._normalize_span_id(parent_span_id)
        if final_span_id and final_span_id != self.root_span_id and final_parent_span_id is None:
            final_parent_span_id = self.root_span_id
        event = build_trace_event(
            run_id=self.run_id,
            thread_id=self._header.get("thread_id"),
            category=category,
            operation=operation,
            event_type=event_type,
            trace_id=self.trace_id,
            sequence=self._next_sequence(),
            span_id=final_span_id,
            parent_span_id=final_parent_span_id,
            occurred_at=occurred_at or utc_now(),
            duration_ms=duration_ms,
            title=redact_text(title),
            summary=redact_text(summary),
            attributes=redact_attributes(attributes),
            resource_refs=resource_refs,
            visibility=visibility,
            sensitivity=sensitivity,
        )
        projector.apply_event_to_summary(self._summary, event)
        projector.apply_event_to_spans(self._spans, event)
        self._buffer.append(event)
        self._maybe_schedule_safety_flush()
        return event

    def start_span(
        self,
        *,
        category: str,
        operation: str,
        span_id: str | None = None,
        **fields: Any,
    ) -> str:
        final_span_id = self._normalize_span_id(span_id or self.next_span_id(category.lower()))
        assert final_span_id is not None
        self.emit(
            category=category,
            operation=operation,
            event_type=f"{category.lower()}.{operation}.started",
            span_id=final_span_id,
            **fields,
        )
        return final_span_id

    def finish_span(
        self,
        span_id: str,
        *,
        suffix: str = "completed",
        error_type: str | None = None,
        **fields: Any,
    ) -> None:
        final_span_id = self._normalize_span_id(span_id)
        span = self._spans.get(final_span_id) or {}
        attributes = dict(fields.pop("attributes", None) or {})
        if error_type:
            attributes.setdefault("error_type", error_type)
        duration_ms = fields.pop("duration_ms", None)
        if duration_ms is None and span.get("started_at") is not None:
            delta = (utc_now() - span["started_at"]).total_seconds()
            duration_ms = int(max(0.0, delta) * 1000)
        category = span.get("category") or "SYSTEM"
        operation = span.get("operation") or "execution"
        self.emit(
            category=category,
            operation=operation,
            event_type=f"{category.lower()}.{operation}.{suffix}",
            span_id=final_span_id,
            duration_ms=duration_ms,
            attributes=attributes,
            **fields,
        )

    def record_run_terminal(
        self,
        status: str,
        *,
        error_type: str | None = None,
        error_message: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        run_span = self._spans.get(self.root_span_id)
        duration_ms = None
        if run_span and run_span.get("started_at") is not None:
            duration_ms = int(max(0.0, (utc_now() - run_span["started_at"]).total_seconds()) * 1000)
        merged_attributes = dict(attributes or {})
        if error_type:
            merged_attributes.setdefault("error_type", error_type)
        self.emit(
            category="RUN",
            operation="execution",
            event_type=f"run.execution.{status}",
            span_id=self.root_span_id,
            title={
                "completed": "本轮执行完成",
                "failed": "本轮执行失败",
                "cancelled": "本轮执行已取消",
                "interrupted": "本轮执行已中断",
            }.get(status, "本轮执行结束"),
            # 原始异常文本可能含参数、路径或供应商响应；Trace 只保存稳定 error_type。
            summary=None,
            duration_ms=duration_ms,
            attributes=merged_attributes,
        )

    def close_running_spans(
        self,
        *,
        suffix: str = "interrupted",
        error_type: str = "worker_lost",
        exclude_span_ids: tuple[str, ...] = ("run",),
    ) -> int:
        """把仍处于 RUNNING 的 span 以新增事件闭合（worker 失联收敛用）。"""
        closed = 0
        excluded = {self._normalize_span_id(span_id) for span_id in exclude_span_ids}
        for span_id, span in list(self._spans.items()):
            if span.get("status") != projector.SPAN_STATUS_RUNNING or span_id in excluded:
                continue
            self.finish_span(
                span_id,
                suffix=suffix,
                error_type=error_type,
                title=span.get("title"),
                attributes={"error.type": error_type},
            )
            closed += 1
        return closed

    # ------------------------------------------------------------------ 持久化

    async def seed(self) -> None:
        """分页重放完整账本；失败只关闭本次可观测性，不影响主业务。"""
        from yuxi.repositories.trace_repository import TraceRepository
        from yuxi.storage.postgres.manager import pg_manager

        try:
            cursor = 0
            while True:
                async with pg_manager.get_async_session_context() as db:
                    repo = TraceRepository(db)
                    head = await repo.get_head(self.run_id)
                    existing = await repo.list_events(self.run_id, after_sequence=cursor, limit=1000)
                if head is not None:
                    self.trace_id = head.trace_id
                    self.root_span_id = head.root_span_id
                    self._span_aliases["run"] = self.root_span_id
                if not existing:
                    break
                for row in existing:
                    event = self.row_to_event(row)
                    projector.apply_event_to_summary(self._summary, event)
                    projector.apply_event_to_spans(self._spans, event)
                    if event.get("event_type") in TRACE_ONCE_PER_RUN_EVENT_TYPES:
                        self._emitted_once.add(str(event["event_type"]))
                cursor = int(existing[-1].sequence)
                if len(existing) < 1000:
                    break
            self._sequence = int(self._summary.get("last_sequence") or 0)
            self._seeded = True
        except Exception as error:  # noqa: BLE001
            # Continuing from an incomplete replay could overwrite durable
            # projections with a partial in-memory view. Disable this recorder;
            # terminal persistence will then report DEGRADED and commit the
            # business outcome through its explicit fallback transaction.
            self._disabled = True
            self._buffer.clear()
            logger.warning(f"trace seed disabled for run {self.run_id}: {type(error).__name__}")

    async def flush(
        self,
        *,
        transaction_action: Callable[[Any], Awaitable[None]] | None = None,
    ) -> None:
        """Persist buffered trace facts and an optional business write atomically.

        ``transaction_action`` runs in the same PostgreSQL transaction after the
        ledger/outbox/projections have been staged.  The worker uses this for the
        AgentRun terminal transition so neither side can commit alone.  Realtime
        delivery remains post-commit and is recovered by the outbox.
        """
        async with self._flush_lock:
            if self._disabled:
                if transaction_action is not None:
                    raise RuntimeError("trace recorder disabled after seed failure")
                return
            if not self._buffer and transaction_action is None:
                return
            await self._flush_locked(transaction_action=transaction_action)

    async def finalize(self) -> None:
        try:
            await self.flush()
        except Exception as error:  # noqa: BLE001
            logger.error(f"trace finalize flush failed for run {self.run_id}: {error}")
        finally:
            self.deactivate()

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def _maybe_schedule_safety_flush(self) -> None:
        if len(self._buffer) < _BUFFER_SAFETY_VALVE:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._safe_flush())
        except RuntimeError:
            pass

    async def _safe_flush(self) -> None:
        try:
            await self.flush()
        except Exception as error:  # noqa: BLE001
            logger.warning(f"trace safety flush failed for run {self.run_id}: {error}")

    async def _flush_locked(
        self,
        *,
        transaction_action: Callable[[Any], Awaitable[None]] | None = None,
    ) -> None:
        from yuxi.repositories.trace_repository import TraceRepository
        from yuxi.storage.postgres.manager import pg_manager
        from yuxi.storage.postgres.models_trace import AgentRunTraceOutbox

        if self._header.get("tenant_id") is None:
            self._buffer.clear()
            if transaction_action is not None:
                async with pg_manager.get_async_session_context() as db:
                    await transaction_action(db)
            return
        events = list(self._buffer)
        # 同步区内复制，确保 flush 过程中后来 emit 的事件不会让投影跑到账本前面。
        spans_snapshot = copy.deepcopy(self._spans)
        summary_snapshot = copy.deepcopy(self._summary)
        async with pg_manager.get_async_session_context() as db:
            if events:
                repo = TraceRepository(db)
                first_sequence, head = await repo.lock_and_allocate_sequences(
                    run_id=self.run_id,
                    tenant_id=int(self._header["tenant_id"]),
                    trace_id=self.trace_id,
                    root_span_id=self.root_span_id,
                    count=len(events),
                )
                for offset, event in enumerate(events):
                    event["sequence"] = first_sequence + offset
                    event["trace_id"] = head.trace_id
                self.trace_id = head.trace_id
                self.root_span_id = head.root_span_id
                last_sequence = first_sequence + len(events) - 1
                summary_snapshot["last_sequence"] = last_sequence
                summary_snapshot["projection_sequence"] = last_sequence
                head.projection_sequence = last_sequence
                await repo.insert_events(self._event_rows(events))
                db.add_all(
                    [
                        AgentRunTraceOutbox(
                            tenant_id=self._header.get("tenant_id"),
                            run_id=self.run_id,
                            event_id=event["event_id"],
                            sequence=int(event["sequence"]),
                            payload=wire_event(event),
                        )
                        for event in events
                    ]
                )
                await repo.upsert_spans(
                    self.run_id,
                    list(spans_snapshot.values()),
                    tenant_id=int(self._header["tenant_id"]),
                )
                await repo.upsert_summary(self.run_id, summary_snapshot)
            if transaction_action is not None:
                await transaction_action(db)

        # 事务已提交：账本与投影持久。以下 fast-path 只影响实时性，失败可接受。
        if events:
            flushed_ids = {event["event_id"] for event in events}
            self._buffer = [event for event in self._buffer if event["event_id"] not in flushed_ids]
            self._sequence = max(self._sequence, int(events[-1]["sequence"]))
            await self._publish_realtime(events)

    async def _publish_realtime(self, events: list[dict[str, Any]]) -> None:
        from yuxi.repositories.trace_repository import TraceRepository
        from yuxi.services.run_queue_service import append_run_stream_event
        from yuxi.storage.postgres.manager import pg_manager

        user_events = [event for event in events if event.get("visibility") == "USER"]
        for event in user_events:
            try:
                await append_run_stream_event(
                    self.run_id,
                    "trace",
                    {"trace": wire_event(event)},
                    thread_id=self._header.get("thread_id"),
                )
            except Exception as error:  # noqa: BLE001 - Outbox relay 会兜底
                logger.warning(f"trace realtime publish failed for run {self.run_id}: {error}")
                return
        # 全部直发成功：标记 Outbox 完成，避免 relay 重复投递（客户端虽可按
        # sequence 幂等去重，但不付出双倍流量）。任一失败即整体留待 relay。
        try:
            async with pg_manager.get_async_session_context() as db:
                await TraceRepository(db).mark_outbox_processed_for_events(
                    self.run_id, [event["event_id"] for event in events]
                )
        except Exception as error:  # noqa: BLE001
            logger.warning(f"trace outbox fast-path mark failed for run {self.run_id}: {error}")

    def _event_rows(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ingested_at = utc_now()
        rows = []
        for event in events:
            occurred_at = event.get("occurred_at")
            if isinstance(occurred_at, str):
                try:
                    occurred_at = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
                except ValueError:
                    occurred_at = ingested_at
            if isinstance(occurred_at, datetime) and occurred_at.tzinfo is None:
                occurred_at = occurred_at.replace(tzinfo=UTC)
            rows.append(
                {
                    "tenant_id": self._header.get("tenant_id"),
                    "uid": self._header.get("uid"),
                    "run_id": self.run_id,
                    "thread_id": self._header.get("thread_id"),
                    "schema_version": TRACE_SCHEMA_VERSION,
                    "event_id": event["event_id"],
                    "trace_id": event["trace_id"],
                    "sequence": int(event["sequence"]),
                    "category": event["category"],
                    "operation": event["operation"],
                    "event_type": event["event_type"],
                    "span_id": event.get("span_id"),
                    "parent_span_id": event.get("parent_span_id"),
                    "occurred_at": occurred_at,
                    "ingested_at": ingested_at,
                    "duration_ms": event.get("duration_ms"),
                    "title": event.get("title"),
                    "summary": event.get("summary"),
                    "message_key": event.get("message_key"),
                    "display_args": event.get("display_args") or {},
                    "attributes": event.get("attributes") or {},
                    "resource_refs": event.get("resource_refs") or [],
                    "visibility": event.get("visibility"),
                    "sensitivity": event.get("sensitivity"),
                    "retention_class": event.get("retention_class"),
                }
            )
        return rows

    @staticmethod
    def row_to_event(row: Any) -> dict[str, Any]:
        occurred_at = row.occurred_at
        if isinstance(occurred_at, datetime):
            if occurred_at.tzinfo is None:
                occurred_at = occurred_at.replace(tzinfo=UTC)
            occurred_at = occurred_at.isoformat()
        return {
            "schema_version": row.schema_version,
            "event_id": row.event_id,
            "trace_id": row.trace_id,
            "sequence": int(row.sequence or 0),
            "run_id": row.run_id,
            "thread_id": row.thread_id,
            "category": row.category,
            "operation": row.operation,
            "event_type": row.event_type,
            "span_id": row.span_id,
            "parent_span_id": row.parent_span_id,
            "occurred_at": occurred_at,
            "duration_ms": row.duration_ms,
            "title": row.title,
            "summary": row.summary,
            "message_key": row.message_key,
            "display_args": row.display_args or {},
            "attributes": row.attributes or {},
            "resource_refs": row.resource_refs or [],
            "visibility": row.visibility,
        }


def digest_args(args: Any) -> str:
    """工具入参摘要（公开便捷函数，供中间件使用）。"""
    return digest_text(args)


def new_trace_span_id() -> str:
    return secrets.token_hex(8)
