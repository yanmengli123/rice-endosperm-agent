"""AgentRun 执行轨迹存储模型。

四张表对应「事实账本 + 传输 Outbox + 两个读模型投影」：

- ``agent_run_trace_events``：append-only 事实账本，只允许 INSERT
  （数据库 trigger 拒绝 UPDATE/DELETE，见迁移 0023）；
- ``agent_run_trace_outbox``：与账本同事务写入的传输任务，relay 异步投递
  到 Redis Stream，实时通道故障只损失实时性、不损失事实；
- ``agent_run_trace_spans`` / ``agent_run_trace_summaries``：可重建的读模型
  投影（可 UPDATE），随时可 DELETE 后由账本 replay 重建。

时间列一律 TIMESTAMPTZ + aware UTC（utc_now）。
"""

from sqlalchemy import JSON, BigInteger, Column, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from yuxi.storage.postgres.models_business import Base, BigIntPk
from yuxi.utils.datetime_utils import utc_now

JSONValue = JSON().with_variant(JSONB, "postgresql")

TRACE_EVENT_VISIBILITIES = ("USER", "ADMIN")
TRACE_EVENT_SENSITIVITIES = ("PUBLIC", "INTERNAL")
TRACE_EVENT_RETENTION_CLASSES = ("STANDARD", "EXTENDED", "LEGAL_HOLD")
TRACE_SPAN_STATUSES = ("RUNNING", "COMPLETED", "FAILED", "INTERRUPTED", "SKIPPED")
TRACE_OUTBOX_STATUSES = ("PENDING", "PROCESSING", "PROCESSED", "EXPIRED")


class AgentRunTraceEvent(Base):
    """执行轨迹事实事件：不可变审计账本，禁止 UPDATE/DELETE。"""

    __tablename__ = "agent_run_trace_events"

    id = Column(BigIntPk, primary_key=True, autoincrement=True)
    tenant_id = Column(BigInteger, ForeignKey("tenants.id"), nullable=False, index=True)
    uid = Column(String(64), nullable=True, index=True)
    # 账本不设外键：run 行删除时事件必须留档（run_id 保留字符串值供审计），
    # CASCADE 会被 append-only trigger 拒绝、SET NULL 会丢失审计标识。
    run_id = Column(String(64), nullable=False, index=True)
    thread_id = Column(String(64), nullable=True)
    schema_version = Column(String(32), nullable=False, default="yuxi.run-trace.v1")
    event_id = Column(String(64), nullable=False)
    sequence = Column(BigInteger, nullable=False)
    category = Column(String(32), nullable=False)
    operation = Column(String(64), nullable=False)
    event_type = Column(String(96), nullable=False)
    # W3C Trace Context：trace_id=32 hex，span_id=16 hex。
    trace_id = Column(String(32), nullable=False)
    span_id = Column(String(64), nullable=True)
    parent_span_id = Column(String(64), nullable=True)
    occurred_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    ingested_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    duration_ms = Column(BigInteger, nullable=True)
    title = Column(String(256), nullable=True)
    summary = Column(Text, nullable=True)
    message_key = Column(String(128), nullable=True)
    display_args = Column(JSONValue, nullable=True)
    attributes = Column(JSONValue, nullable=True)
    resource_refs = Column(JSONValue, nullable=True)
    visibility = Column(String(16), nullable=False, default="USER")
    sensitivity = Column(String(16), nullable=False, default="INTERNAL")
    retention_class = Column(String(16), nullable=False, default="STANDARD")

    __table_args__ = (
        UniqueConstraint("run_id", "event_id", name="uq_agent_run_trace_events_event_id"),
        UniqueConstraint("run_id", "sequence", name="uq_agent_run_trace_events_sequence"),
        Index("ix_agent_run_trace_events_run_occurred", "run_id", "occurred_at"),
        Index(
            "ix_agent_run_trace_events_retention",
            "retention_class",
            "occurred_at",
            "run_id",
        ),
    )


class AgentRunTraceOutbox(Base):
    """轨迹事件传输 Outbox：与账本同事务写入，relay 投递 Redis 后置 PROCESSED。"""

    __tablename__ = "agent_run_trace_outbox"

    id = Column(BigIntPk, primary_key=True, autoincrement=True)
    tenant_id = Column(BigInteger, ForeignKey("tenants.id"), nullable=False, index=True)
    run_id = Column(
        String(64),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_id = Column(String(64), nullable=False)
    sequence = Column(BigInteger, nullable=False)
    payload = Column(JSONValue, nullable=False)
    status = Column(String(16), nullable=False, default="PENDING", index=True)
    attempts = Column(Integer, nullable=False, default=0)
    available_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    last_error = Column(Text, nullable=True)
    lease_id = Column(String(64), nullable=True, index=True)
    leased_at = Column(DateTime(timezone=True), nullable=True)
    processed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)

    __table_args__ = (
        UniqueConstraint("run_id", "event_id", name="uq_agent_run_trace_outbox_event"),
        Index("ix_agent_run_trace_outbox_pending", "status", "available_at"),
    )


class AgentRunTraceSpan(Base):
    """Span 投影（读模型）：由账本事件确定性推导，可随时 replay 重建。"""

    __tablename__ = "agent_run_trace_spans"

    id = Column(BigIntPk, primary_key=True, autoincrement=True)
    tenant_id = Column(BigInteger, ForeignKey("tenants.id"), nullable=False, index=True)
    run_id = Column(
        String(64),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    thread_id = Column(String(64), nullable=True)
    span_id = Column(String(64), nullable=False)
    parent_span_id = Column(String(64), nullable=True)
    category = Column(String(32), nullable=False)
    operation = Column(String(64), nullable=False)
    title = Column(String(256), nullable=True)
    summary = Column(Text, nullable=True)
    message_key = Column(String(128), nullable=True)
    display_args = Column(JSONValue, nullable=True)
    visibility = Column(String(16), nullable=False, default="USER")
    status = Column(String(16), nullable=False, default="RUNNING")
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    duration_ms = Column(BigInteger, nullable=True)
    error_type = Column(String(64), nullable=True)
    retry_count = Column(Integer, nullable=False, default=0)
    attributes_summary = Column(JSONValue, nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)

    __table_args__ = (
        UniqueConstraint("run_id", "span_id", name="uq_agent_run_trace_spans_span"),
        Index("ix_agent_run_trace_spans_run_started", "run_id", "started_at"),
    )


class AgentRunTraceSummary(Base):
    """Run 级汇总投影（读模型）：打开历史会话无需重放事件。"""

    __tablename__ = "agent_run_trace_summaries"

    id = Column(BigIntPk, primary_key=True, autoincrement=True)
    tenant_id = Column(BigInteger, ForeignKey("tenants.id"), nullable=False, index=True)
    run_id = Column(
        String(64),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    thread_id = Column(String(64), nullable=True, index=True)
    status = Column(String(32), nullable=True)
    agent_slug = Column(String(64), nullable=True)
    run_type = Column(String(32), nullable=True)
    request_id = Column(String(64), nullable=True)
    created_by_run_id = Column(String(64), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    first_token_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    duration_ms = Column(BigInteger, nullable=True)
    ttft_ms = Column(BigInteger, nullable=True)
    input_tokens = Column(BigInteger, nullable=True)
    output_tokens = Column(BigInteger, nullable=True)
    total_tokens = Column(BigInteger, nullable=True)
    model_calls = Column(Integer, nullable=False, default=0)
    tool_calls = Column(Integer, nullable=False, default=0)
    mcp_calls = Column(Integer, nullable=False, default=0)
    knowledge_calls = Column(Integer, nullable=False, default=0)
    subagent_calls = Column(Integer, nullable=False, default=0)
    skill_count = Column(Integer, nullable=False, default=0)
    retry_count = Column(Integer, nullable=False, default=0)
    error_count = Column(Integer, nullable=False, default=0)
    trace_event_count = Column(Integer, nullable=False, default=0)
    last_sequence = Column(BigInteger, nullable=False, default=0)
    projection_sequence = Column(BigInteger, nullable=False, default=0)
    attributes = Column(JSONValue, nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)


class AgentRunTraceHead(Base):
    """每个 run 的序号与 relay 租约权威行；写入时通过 ``FOR UPDATE`` 串行化。"""

    __tablename__ = "agent_run_trace_heads"

    run_id = Column(
        String(64),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tenant_id = Column(BigInteger, ForeignKey("tenants.id"), nullable=False, index=True)
    trace_id = Column(String(32), nullable=False, unique=True)
    root_span_id = Column(String(16), nullable=False)
    last_sequence = Column(BigInteger, nullable=False, default=0)
    projection_sequence = Column(BigInteger, nullable=False, default=0)
    relay_lease_id = Column(String(64), nullable=True)
    relay_leased_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)
