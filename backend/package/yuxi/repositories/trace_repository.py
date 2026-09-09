"""执行轨迹仓储：账本/Outbox/投影的数据库访问边界。"""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.models_trace import (
    AgentRunTraceEvent,
    AgentRunTraceHead,
    AgentRunTraceOutbox,
    AgentRunTraceSpan,
    AgentRunTraceSummary,
)
from yuxi.utils.datetime_utils import utc_now

TRACE_OUTBOX_MAX_ATTEMPTS = 20
TRACE_OUTBOX_BACKOFF_BASE_SECONDS = 1.0
TRACE_OUTBOX_LEASE_SECONDS = 120


def serialize_trace_event(row: AgentRunTraceEvent) -> dict[str, Any]:
    occurred_at = row.occurred_at
    if occurred_at is not None and occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=utc_now().tzinfo)
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
        "occurred_at": occurred_at.isoformat() if occurred_at else None,
        "duration_ms": row.duration_ms,
        "title": row.title,
        "summary": row.summary,
        "message_key": row.message_key,
        "display_args": row.display_args or {},
        "attributes": row.attributes or {},
        "resource_refs": row.resource_refs or [],
        "visibility": row.visibility,
    }


def serialize_trace_span(row: AgentRunTraceSpan) -> dict[str, Any]:
    def _iso(value):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=utc_now().tzinfo)
        return value.isoformat()

    return {
        "span_id": row.span_id,
        "parent_span_id": row.parent_span_id,
        "category": row.category,
        "operation": row.operation,
        "title": row.title,
        "summary": row.summary,
        "message_key": row.message_key,
        "display_args": row.display_args or {},
        "visibility": row.visibility,
        "status": row.status,
        "started_at": _iso(row.started_at),
        "finished_at": _iso(row.finished_at),
        "duration_ms": row.duration_ms,
        "error_type": row.error_type,
        "retry_count": int(row.retry_count or 0),
        "attributes": row.attributes_summary or {},
    }


def serialize_trace_summary(row: AgentRunTraceSummary) -> dict[str, Any]:
    def _iso(value):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=utc_now().tzinfo)
        return value.isoformat()

    return {
        "run_id": row.run_id,
        "thread_id": row.thread_id,
        "status": row.status,
        "agent_slug": row.agent_slug,
        "run_type": row.run_type,
        "request_id": row.request_id,
        "started_at": _iso(row.started_at),
        "first_token_at": _iso(row.first_token_at),
        "finished_at": _iso(row.finished_at),
        "duration_ms": row.duration_ms,
        "ttft_ms": row.ttft_ms,
        "input_tokens": row.input_tokens,
        "output_tokens": row.output_tokens,
        "total_tokens": row.total_tokens,
        "model_calls": row.model_calls,
        "tool_calls": row.tool_calls,
        "mcp_calls": row.mcp_calls,
        "knowledge_calls": row.knowledge_calls,
        "subagent_calls": row.subagent_calls,
        "skill_count": row.skill_count,
        "retry_count": row.retry_count,
        "error_count": row.error_count,
        "trace_event_count": row.trace_event_count,
        "last_sequence": row.last_sequence,
        "projection_sequence": row.projection_sequence,
        "attributes": row.attributes or {},
    }


class TraceRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ------------------------------------------------------------------ 账本

    async def max_sequence(self, run_id: str) -> int:
        result = await self.db.execute(
            select(func.max(AgentRunTraceEvent.sequence)).where(AgentRunTraceEvent.run_id == run_id)
        )
        return int(result.scalar() or 0)

    async def lock_and_allocate_sequences(
        self,
        *,
        run_id: str,
        tenant_id: int,
        trace_id: str,
        root_span_id: str,
        count: int,
    ) -> tuple[int, AgentRunTraceHead]:
        """在 head 行锁内分配连续序号；并发 recorder 不再依赖 MAX+1/重试。

        锁序约束：head 已存在时只锁 head 行（Trace 域，业务 session 不持有），
        **禁止**无条件 FOR UPDATE agent_runs——run 的业务 session 收尾会
        UPDATE agent_runs.total_tokens 并持锁到生成器消费完毕；chunk 循环内的
        trace flush 若去锁该行，会与业务 session 互相等待形成死锁
        （worker 等锁、生成器等 worker 消费）。仅在 head 不存在的首次创建
        竞争窗口才锁 AgentRun 权威行。
        """
        from yuxi.storage.postgres.models_business import AgentRun

        # First read must itself acquire the row lock.  A preceding unlocked ORM
        # load would populate the session identity map; after waiting for another
        # allocator, SQLAlchemy could then reuse that stale ``last_sequence`` even
        # though PostgreSQL granted the lock on the newer row.
        head = (
            await self.db.execute(select(AgentRunTraceHead).where(AgentRunTraceHead.run_id == run_id).with_for_update())
        ).scalar_one_or_none()
        if head is None:
            await self.db.execute(select(AgentRun.id).where(AgentRun.id == run_id).with_for_update())
            # 双检：锁住 agent_runs 后重读，收敛并发创建竞争
            head = (
                await self.db.execute(
                    select(AgentRunTraceHead).where(AgentRunTraceHead.run_id == run_id).with_for_update()
                )
            ).scalar_one_or_none()
        if head is None:
            head = AgentRunTraceHead(
                run_id=run_id,
                tenant_id=tenant_id,
                trace_id=trace_id,
                root_span_id=root_span_id,
                last_sequence=0,
                projection_sequence=0,
            )
            self.db.add(head)
            await self.db.flush()
        start = int(head.last_sequence or 0) + 1
        head.last_sequence = int(head.last_sequence or 0) + max(0, count)
        return start, head

    async def get_head(self, run_id: str, *, for_share: bool = False) -> AgentRunTraceHead | None:
        stmt = select(AgentRunTraceHead).where(AgentRunTraceHead.run_id == run_id)
        if for_share:
            # A snapshot is assembled from head + summary + spans. Holding a
            # shared head lock makes those separate SELECTs one coherent
            # projection boundary while flush waits for an exclusive head lock.
            stmt = stmt.with_for_update(read=True)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def insert_events(self, event_rows: list[dict[str, Any]]) -> None:
        self.db.add_all([AgentRunTraceEvent(**row) for row in event_rows])

    async def list_events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 500,
        max_sequence: int | None = None,
    ) -> list[AgentRunTraceEvent]:
        stmt = (
            select(AgentRunTraceEvent)
            .where(
                AgentRunTraceEvent.run_id == run_id,
                AgentRunTraceEvent.sequence > after_sequence,
            )
            .order_by(AgentRunTraceEvent.sequence.asc())
            .limit(limit)
        )
        if max_sequence is not None:
            stmt = stmt.where(AgentRunTraceEvent.sequence <= max_sequence)
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def count_events(self, run_id: str) -> int:
        result = await self.db.execute(
            select(func.count(AgentRunTraceEvent.id)).where(AgentRunTraceEvent.run_id == run_id)
        )
        return int(result.scalar() or 0)

    # ------------------------------------------------------------------ 投影

    async def upsert_spans(self, run_id: str, span_states: list[dict[str, Any]], *, tenant_id: int) -> None:
        if not span_states:
            return
        span_ids = [state["span_id"] for state in span_states]
        result = await self.db.execute(
            select(AgentRunTraceSpan).where(
                AgentRunTraceSpan.run_id == run_id,
                AgentRunTraceSpan.span_id.in_(span_ids),
            )
        )
        existing = {row.span_id: row for row in result.scalars().all()}
        for state in span_states:
            row = existing.get(state["span_id"])
            if row is None:
                self.db.add(AgentRunTraceSpan(run_id=run_id, tenant_id=tenant_id, **self._span_row(state)))
                continue
            for key, value in self._span_row(state).items():
                setattr(row, key, value)

    @staticmethod
    def _span_row(state: dict[str, Any]) -> dict[str, Any]:
        return {
            "span_id": state["span_id"],
            "thread_id": state.get("thread_id"),
            "parent_span_id": state.get("parent_span_id"),
            "category": state.get("category") or "SYSTEM",
            "operation": state.get("operation") or "execution",
            "title": state.get("title"),
            "summary": state.get("summary"),
            "message_key": state.get("message_key"),
            "display_args": state.get("display_args") or {},
            "visibility": state.get("visibility") or "USER",
            "status": state.get("status") or "RUNNING",
            "started_at": state.get("started_at"),
            "finished_at": state.get("finished_at"),
            "duration_ms": state.get("duration_ms"),
            "error_type": state.get("error_type"),
            "retry_count": int(state.get("retry_count") or 0),
            "attributes_summary": state.get("attributes_summary") or {},
        }

    async def upsert_summary(self, run_id: str, state: dict[str, Any]) -> None:
        result = await self.db.execute(select(AgentRunTraceSummary).where(AgentRunTraceSummary.run_id == run_id))
        row = result.scalar_one_or_none()
        values = {
            "thread_id": state.get("thread_id"),
            "tenant_id": state.get("tenant_id"),
            "status": state.get("status"),
            "agent_slug": state.get("agent_slug"),
            "run_type": state.get("run_type"),
            "request_id": state.get("request_id"),
            "created_by_run_id": state.get("created_by_run_id"),
            "started_at": state.get("started_at"),
            "first_token_at": state.get("first_token_at"),
            "finished_at": state.get("finished_at"),
            "duration_ms": state.get("duration_ms"),
            "ttft_ms": state.get("ttft_ms"),
            "input_tokens": state.get("input_tokens"),
            "output_tokens": state.get("output_tokens"),
            "total_tokens": state.get("total_tokens"),
            "model_calls": state.get("model_calls") or 0,
            "tool_calls": state.get("tool_calls") or 0,
            "mcp_calls": state.get("mcp_calls") or 0,
            "knowledge_calls": state.get("knowledge_calls") or 0,
            "subagent_calls": state.get("subagent_calls") or 0,
            "skill_count": state.get("skill_count") or 0,
            "retry_count": state.get("retry_count") or 0,
            "error_count": state.get("error_count") or 0,
            "trace_event_count": state.get("trace_event_count") or 0,
            "last_sequence": state.get("last_sequence") or 0,
            "projection_sequence": state.get("projection_sequence") or state.get("last_sequence") or 0,
            "attributes": state.get("attributes") or {},
        }
        if row is None:
            self.db.add(AgentRunTraceSummary(run_id=run_id, **values))
        else:
            for key, value in values.items():
                setattr(row, key, value)

    async def list_spans(self, run_id: str, *, visibility: str | None = None) -> list[AgentRunTraceSpan]:
        stmt = select(AgentRunTraceSpan).where(AgentRunTraceSpan.run_id == run_id)
        if visibility is not None:
            stmt = stmt.where(AgentRunTraceSpan.visibility == visibility)
        result = await self.db.execute(stmt.order_by(AgentRunTraceSpan.started_at.asc(), AgentRunTraceSpan.id.asc()))
        return list(result.scalars().all())

    async def get_summary(self, run_id: str) -> AgentRunTraceSummary | None:
        result = await self.db.execute(select(AgentRunTraceSummary).where(AgentRunTraceSummary.run_id == run_id))
        return result.scalar_one_or_none()

    async def delete_projection(self, run_id: str) -> None:
        await self.db.execute(delete(AgentRunTraceSpan).where(AgentRunTraceSpan.run_id == run_id))
        await self.db.execute(delete(AgentRunTraceSummary).where(AgentRunTraceSummary.run_id == run_id))

    async def list_running_span_states(self, run_id: str) -> list[AgentRunTraceSpan]:
        result = await self.db.execute(
            select(AgentRunTraceSpan).where(
                AgentRunTraceSpan.run_id == run_id,
                AgentRunTraceSpan.status == "RUNNING",
            )
        )
        return list(result.scalars().all())

    # ------------------------------------------------------------------ Outbox

    async def claim_pending_outbox(self, *, limit: int = 100) -> list[AgentRunTraceOutbox]:
        """按 run 认领有序批次；head 租约阻止并发 relay 拆分同一 run。"""
        now = utc_now()
        stale_before = now - timedelta(seconds=TRACE_OUTBOX_LEASE_SECONDS)
        await self.db.execute(
            update(AgentRunTraceOutbox)
            .where(
                AgentRunTraceOutbox.status == "PROCESSING",
                AgentRunTraceOutbox.leased_at < stale_before,
            )
            .values(status="PENDING", lease_id=None, leased_at=None, available_at=now)
        )
        await self.db.execute(
            update(AgentRunTraceHead)
            .where(AgentRunTraceHead.relay_leased_at < stale_before)
            .values(relay_lease_id=None, relay_leased_at=None)
        )
        head_result = await self.db.execute(
            select(AgentRunTraceHead)
            .where(
                AgentRunTraceHead.relay_lease_id.is_(None),
                AgentRunTraceHead.run_id.in_(
                    select(AgentRunTraceOutbox.run_id).where(
                        AgentRunTraceOutbox.status == "PENDING",
                        AgentRunTraceOutbox.available_at <= now,
                    )
                ),
            )
            .order_by(AgentRunTraceHead.updated_at.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        head = head_result.scalar_one_or_none()
        if head is None:
            return []
        lease_id = secrets.token_hex(16)
        head.relay_lease_id = lease_id
        head.relay_leased_at = now
        result = await self.db.execute(
            select(AgentRunTraceOutbox)
            .where(
                AgentRunTraceOutbox.run_id == head.run_id,
                AgentRunTraceOutbox.status == "PENDING",
                AgentRunTraceOutbox.available_at <= now,
            )
            .order_by(AgentRunTraceOutbox.sequence.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        rows = list(result.scalars().all())
        for row in rows:
            row.status = "PROCESSING"
            row.lease_id = lease_id
            row.leased_at = now
        return rows

    async def mark_outbox_processed(self, outbox_ids: list[int], *, lease_id: str | None = None) -> None:
        if not outbox_ids:
            return
        stmt = update(AgentRunTraceOutbox).where(AgentRunTraceOutbox.id.in_(outbox_ids))
        if lease_id is not None:
            stmt = stmt.where(AgentRunTraceOutbox.lease_id == lease_id)
        await self.db.execute(
            stmt.values(
                status="PROCESSED",
                last_error=None,
                processed_at=utc_now(),
                lease_id=None,
                leased_at=None,
            )
        )
        if lease_id is not None:
            await self.db.execute(
                update(AgentRunTraceHead)
                .where(AgentRunTraceHead.relay_lease_id == lease_id)
                .values(relay_lease_id=None, relay_leased_at=None)
            )

    async def mark_outbox_processed_for_events(self, run_id: str, event_ids: list[str]) -> None:
        """fast-path 直发成功后按事件 ID 标记完成，避免 relay 重复投递。"""
        if not event_ids:
            return
        await self.db.execute(
            update(AgentRunTraceOutbox)
            .where(
                AgentRunTraceOutbox.run_id == run_id,
                AgentRunTraceOutbox.event_id.in_(event_ids),
                AgentRunTraceOutbox.status == "PENDING",
            )
            .values(status="PROCESSED", last_error=None, processed_at=utc_now())
        )

    async def fail_outbox_rows(self, rows: list[AgentRunTraceOutbox], error: str) -> None:
        """投递失败：退避重试；超过上限置 EXPIRED 死信（账本事实仍在）。"""
        now = utc_now()
        lease_ids = {row.lease_id for row in rows if row.lease_id}
        for row in rows:
            row.attempts = int(row.attempts or 0) + 1
            row.last_error = (error or "")[:2000]
            if row.attempts >= TRACE_OUTBOX_MAX_ATTEMPTS:
                row.status = "EXPIRED"
            else:
                row.status = "PENDING"
                backoff = min(60.0, TRACE_OUTBOX_BACKOFF_BASE_SECONDS * (2 ** min(row.attempts, 6)))
                row.available_at = now + timedelta(seconds=backoff)
            row.lease_id = None
            row.leased_at = None
        if lease_ids:
            await self.db.execute(
                update(AgentRunTraceHead)
                .where(AgentRunTraceHead.relay_lease_id.in_(lease_ids))
                .values(relay_lease_id=None, relay_leased_at=None)
            )

    async def fail_outbox_claim(self, outbox_ids: list[int], *, lease_id: str, error: str) -> None:
        if not outbox_ids:
            return
        result = await self.db.execute(
            select(AgentRunTraceOutbox)
            .where(
                AgentRunTraceOutbox.id.in_(outbox_ids),
                AgentRunTraceOutbox.lease_id == lease_id,
            )
            .with_for_update()
        )
        await self.fail_outbox_rows(list(result.scalars().all()), error)
