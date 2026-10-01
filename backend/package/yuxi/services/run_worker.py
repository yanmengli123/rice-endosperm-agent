"""ARQ worker for agent runs."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

from arq import cron
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from yuxi.agents.mcp.artifact_materializer import begin_artifact_accumulation, end_artifact_accumulation
from yuxi.agents.mcp.execution import (
    McpExecutionContext,
    reset_mcp_execution_context,
    set_mcp_execution_context,
)
from yuxi.agents.mcp.service import ensure_builtin_mcp_servers_in_db
from yuxi.agents.skills.service import init_builtin_skills
from yuxi.agents.toolkits.browser.gateway_client import (
    BrowserExecutionContext,
    end_browser_task_best_effort,
    reset_browser_execution_context,
    set_browser_execution_context,
)
from yuxi.config import config as sys_config
from yuxi.knowledge.graphs.doclex.service import prewarm_doclex_for_kb
from yuxi.repositories.agent_run_repository import TERMINAL_RUN_STATUSES, AgentRunRepository
from yuxi.services.agent_run_service import (
    acknowledge_agent_run_dispatch,
    dispatch_pending_agent_runs,
    reconcile_stale_agent_runs,
)
from yuxi.services.channel_service import (
    process_channel_message,
    purge_channel_history,
    relay_channel_outbox,
    relay_channel_outbox_now,
)
from yuxi.services.chat_service import stream_agent_chat, stream_agent_resume
from yuxi.services.input_message_service import restore_chat_input_message
from yuxi.services.mcp_canary_service import run_mcp_live_canary
from yuxi.services.run_queue_service import (
    append_run_stream_event,
    clear_cancel_signal,
    has_cancel_signal,
    wait_for_cancel_signal,
)
from yuxi.services.run_stream_errors import normalize_stream_error_chunk
from yuxi.services.scientific_pdf_ingest_service import (
    process_scientific_pdf_ingest,
    recover_stale_scientific_pdf_ingests,
)
from yuxi.services.trace_service import purge_expired_trace_runs, relay_trace_outbox
from yuxi.services.wiki_service import process_dynamic_wiki_build, reconcile_dynamic_wikis
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import AgentRun, Message, User
from yuxi.storage.redis import get_arq_redis_settings
from yuxi.trace import TraceRecorder
from yuxi.utils.logging_config import logger
from yuxi.utils.thread_utils import extract_thread_id

LOADING_FLUSH_INTERVAL_MS = 100
LOADING_FLUSH_MAX_CHARS = 512
TRACE_FLUSH_TIMEOUT_SECONDS = 5.0
RUN_CANCEL_POLL_SECONDS = 0.2
RUN_STREAM_PROGRESS_HEARTBEAT_SECONDS = 15.0
RUN_STREAM_IDLE_TIMEOUT_SECONDS = 180.0
RUN_STREAM_TOTAL_TIMEOUT_SECONDS = 300.0
SUPPORTED_RUN_TYPES = {"chat", "resume", "subagent"}
_STREAM_WAITING = object()


class RunStreamIdleTimeout(TimeoutError):
    """Raised when an agent stream produces no event for the configured deadline."""


class RunStreamTotalTimeout(TimeoutError):
    """Raised when a run exceeds its interactive wall-clock deadline."""


@dataclass
class RunContext:
    run_id: str
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    _watch_task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._watch_task is None:
            self._watch_task = asyncio.create_task(self._watch_cancel_signal())

    async def close(self) -> None:
        if self._watch_task:
            self._watch_task.cancel()
            await asyncio.gather(self._watch_task, return_exceptions=True)
            self._watch_task = None

    async def wait_cancelled(self) -> None:
        await self.cancel_event.wait()

    async def is_cancelled(self) -> bool:
        if self.cancel_event.is_set():
            return True
        if await has_cancel_signal(self.run_id):
            self.cancel_event.set()
            return True
        return False

    async def _watch_cancel_signal(self) -> None:
        # Redis 发布/订阅可能因瞬时故障丢信号；除 Redis key/pubsub 外，每 5 秒
        # 以 DB 的 cancel_requested 状态兜底核对一次，保证取消最终一定生效。
        db_check_interval_seconds = 5.0
        last_db_check = time.monotonic()
        while not self.cancel_event.is_set():
            cancelled = await wait_for_cancel_signal(
                self.run_id,
                poll_timeout_seconds=RUN_CANCEL_POLL_SECONDS,
            )
            if not cancelled and time.monotonic() - last_db_check >= db_check_interval_seconds:
                cancelled = await _is_cancel_requested(self.run_id)
                last_db_check = time.monotonic()
            if cancelled:
                self.cancel_event.set()
                return


_ALL_THREADS = object()


@dataclass
class _ThreadBuffer:
    items: list[dict] = field(default_factory=list)
    chars: int = 0
    last_flush: float = field(default_factory=time.monotonic)


class ChunkedEventWriter:
    def __init__(self, run_id: str, thread_id: str | None, interval_ms: int = 100, max_chars: int = 512):
        self.run_id = run_id
        self.thread_id = thread_id
        self.interval_seconds = interval_ms / 1000
        self.max_chars = max_chars
        self.thread_buffers: dict[str | None, _ThreadBuffer] = {}

    def _target_thread_id(self, thread_id: str | None = None) -> str | None:
        return thread_id or self.thread_id

    async def append(self, chunk: dict, *, thread_id: str | None = None):
        target_thread_id = self._target_thread_id(thread_id or extract_thread_id(chunk))
        buffer = self.thread_buffers.setdefault(target_thread_id, _ThreadBuffer())
        buffer.items.append(chunk)
        buffer.chars += _loading_chunk_size(chunk)

        if _flush_loading_chunk_immediately(chunk):
            await self.flush(target_thread_id)
            return

        if (time.monotonic() - buffer.last_flush) >= self.interval_seconds or buffer.chars >= self.max_chars:
            await self.flush(target_thread_id)

    async def flush(self, thread_id: str | None | object = _ALL_THREADS):
        if thread_id is _ALL_THREADS:
            for target_thread_id in list(self.thread_buffers):
                await self.flush(target_thread_id)
            return

        buffer = self.thread_buffers.get(thread_id)
        if not buffer or not buffer.items:
            return
        await append_run_event(self.run_id, "messages", {"items": buffer.items}, thread_id=thread_id)
        buffer.items = []
        buffer.chars = 0
        buffer.last_flush = time.monotonic()


async def _get_run(run_id: str):
    async with pg_manager.get_async_session_context() as db:
        repo = AgentRunRepository(db)
        return await repo.get_run(run_id)


async def append_run_event(run_id: str, event_type: str, payload: dict, *, thread_id: str | None = None):
    await append_run_stream_event(run_id, event_type, payload, thread_id=thread_id)


async def mark_run_running(run_id: str):
    async with pg_manager.get_async_session_context() as db:
        repo = AgentRunRepository(db)
        await repo.mark_running(run_id)


async def mark_run_terminal(
    run_id: str,
    status: str,
    error_type: str | None = None,
    error_message: str | None = None,
    *,
    db=None,
):
    if db is not None:
        await AgentRunRepository(db).set_terminal_status(
            run_id,
            status=status,
            error_type=error_type,
            error_message=error_message,
        )
        return
    async with pg_manager.get_async_session_context() as db:
        repo = AgentRunRepository(db)
        await repo.set_terminal_status(run_id, status=status, error_type=error_type, error_message=error_message)


async def _load_user(uid: str):
    async with pg_manager.get_async_session_context() as db:
        result = await db.execute(
            select(User).where(
                User.uid == uid,
                User.is_deleted == 0,
                User.is_disabled.is_(False),
                User.department_id.is_not(None),
            )
        )
        return result.scalar_one_or_none()


async def _is_cancel_requested(run_id: str) -> bool:
    run = await _get_run(run_id)
    return bool(run and run.status == "cancel_requested")


def _job_try(ctx) -> int:
    if isinstance(ctx, dict):
        try:
            return int(ctx.get("job_try") or 1)
        except Exception:
            return 1
    return 1


def _is_retryable_exception(exc: Exception) -> bool:
    return isinstance(exc, (OperationalError, ConnectionError, TimeoutError, asyncio.TimeoutError))


def _iter_json_chunks(chunk_bytes: bytes) -> list[dict]:
    text = chunk_bytes.decode("utf-8")
    chunks: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            chunks.append(json.loads(line))
        except Exception:
            logger.warning(f"Failed to parse run stream chunk: {line[:200]}")
    return chunks


def _loading_chunk_size(chunk: dict) -> int:
    response = chunk.get("response")
    total = len(response) if isinstance(response, str) else 0
    stream_event = chunk.get("stream_event")
    if not isinstance(stream_event, dict):
        return total

    for key in ("content", "reasoning_content", "additional_reasoning_content", "args_delta"):
        value = stream_event.get(key)
        if isinstance(value, str):
            total += len(value)
    return total


def _flush_loading_chunk_immediately(chunk: dict) -> bool:
    stream_event = chunk.get("stream_event")
    return isinstance(stream_event, dict) and stream_event.get("type") == "tool_call"


def _chunk_thread_id(chunk: dict, fallback: str | None) -> str | None:
    return extract_thread_id(chunk, fallback)


def _is_first_message_delta_chunk(chunk: dict) -> bool:
    stream_event = chunk.get("stream_event")
    return (
        isinstance(stream_event, dict)
        and stream_event.get("type") == "message_delta"
        and bool(stream_event.get("content"))
    )


def _map_chunk_to_run_event(chunk: dict) -> tuple[str, dict]:
    status = chunk.get("status") or "event"
    if status == "loading":
        return "messages", {"chunk": chunk}
    if status == "agent_state":
        return "custom", {"name": "yuxi.agent_state", "chunk": chunk, "agent_state": chunk.get("agent_state") or {}}
    if status in {"ask_user_question_required", "human_approval_required", "interrupted"}:
        reason = "human_approval" if status == "human_approval_required" else status
        return "interrupt", {"reason": reason, "chunk": chunk}
    if status == "warning":
        return "custom", {"name": "yuxi.warning", "chunk": chunk}
    if status == "error":
        # 防御归一：旁路生产者（resume/subagent 等）未分类的连接类错误统一
        # error_type/retryable；已细分类的 error_type 原样放行。
        chunk = normalize_stream_error_chunk(chunk)
        return "error", {"chunk": chunk, "retryable": bool(chunk.get("retryable"))}
    if status == "finished":
        return "end", {"status": "completed", "chunk": chunk}
    return "custom", {"name": f"yuxi.{status}", "chunk": chunk}


async def _append_end_event(run_id: str, status: str, *, thread_id: str | None, payload: dict | None = None):
    end_payload = {"status": status}
    if payload:
        end_payload.update(payload)
    await append_run_event(run_id, "end", end_payload, thread_id=thread_id)


async def _attach_run_artifacts_to_finished_chunk(run_id: str, chunk: dict) -> dict:
    """终态 finished chunk 附带 run 级产物清单（run_artifacts 权威投影）。

    产物卡以 run 为粒度渲染：该字段给"本轮产物"一个确定性信号，前端不再从
    线程级累积的 agent_state.artifacts 推断（那会把上一轮产物钉到本轮——
    跨轮泄漏根因）。仅在确有产物时附带（字段缺席 ⟺ 本轮无产物，与 figures
    发布口径一致）；查询失败降级为不附带，终态语义不被产物投影拖挂。
    """
    if not isinstance(chunk, dict) or chunk.get("status") != "finished":
        return chunk
    try:
        from yuxi.services.agent_run_service import load_run_artifacts

        artifacts = await load_run_artifacts(run_id)
    except Exception as error:  # noqa: BLE001 —— 附带字段尽力而为
        logger.warning(f"Run artifacts terminal attach skipped for {run_id}: {type(error).__name__}")
        return chunk
    if artifacts:
        return {**chunk, "artifacts": artifacts}
    return chunk


async def _persist_terminal_trace(
    recorder: TraceRecorder,
    status: str,
    *,
    error_type: str | None = None,
    error_message: str | None = None,
) -> bool:
    """先持久化终态轨迹，再允许 AgentRun/Redis end 对外可见。

    Trace 默认是 BEST_EFFORT：故障不能翻转业务结果，但 ``end.trace_status`` 会
    明确标记 DEGRADED，reconciler 可据此补偿，不制造“看似完整”的假象。
    """
    # asyncio cancellation bypasses middleware ``except Exception`` blocks.
    # Close every child still observed as RUNNING before the root terminal fact
    # so a completed/failed/cancelled run can never expose immortal child spans.
    recorder.close_running_spans(
        suffix="interrupted",
        error_type=error_type or f"run_{status}_with_open_span",
    )
    terminal_message_id = recorder.run_terminal_message_id
    # message_id 只在 completed 的 schema 里登记；其他终态携带会被协议层
    # 当作漂移剥离并告警，不如在发射端就不传。
    recorder.record_run_terminal(
        status,
        error_type=error_type,
        error_message=error_message,
        attributes=({"message_id": terminal_message_id} if status == "completed" and terminal_message_id else None),
    )

    async def mark_terminal_in_transaction(db) -> None:
        await mark_run_terminal(
            recorder.run_id,
            status,
            error_type=error_type,
            error_message=error_message,
            db=db,
        )

    try:
        await recorder.flush(transaction_action=mark_terminal_in_transaction)
        # Receipt ACK is attempted when the worker starts and again after the
        # durable terminal commit. The second attempt closes a transient DB
        # failure window without coupling business completion to bookkeeping.
        await acknowledge_agent_run_dispatch(recorder.run_id)
        return True
    except Exception as error:  # noqa: BLE001
        logger.error(f"terminal trace persistence degraded for run {recorder.run_id}: {type(error).__name__}")
        # BEST_EFFORT policy: preserve the business terminal state if trace
        # storage is unavailable.  The end frame exposes DEGRADED explicitly.
        await mark_run_terminal(
            recorder.run_id,
            status,
            error_type=error_type,
            error_message=error_message,
        )
        await acknowledge_agent_run_dispatch(recorder.run_id)
        return False


async def _finish_run_before_execution(
    run: AgentRun,
    status: str,
    *,
    error_type: str,
    error_message: str,
    end_reason: str | None = None,
) -> None:
    """Close a run rejected during worker preflight with an auditable terminal fact."""
    recorder = TraceRecorder(
        run_id=run.id,
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
        if status == "cancelled":
            chunk = {
                "status": "interrupted",
                "message": error_message,
                "request_id": run.request_id,
            }
            await append_run_event(
                run.id,
                "interrupt",
                {"reason": end_reason or error_type, "chunk": chunk},
                thread_id=run.conversation_thread_id,
            )
        else:
            chunk = {
                "status": "error",
                "error_type": error_type,
                "error_message": error_message,
                "message": error_message,
                "request_id": run.request_id,
                "retryable": False,
            }
            await append_run_event(
                run.id,
                "error",
                {"chunk": chunk, "retryable": False},
                thread_id=run.conversation_thread_id,
            )
        trace_committed = await _persist_terminal_trace(
            recorder,
            status,
            error_type=error_type,
            error_message=error_message,
        )
        end_payload = {
            "chunk": chunk,
            "trace_status": "COMMITTED" if trace_committed else "DEGRADED",
        }
        if end_reason:
            end_payload["reason"] = end_reason
        await _append_end_event(
            run.id,
            status,
            thread_id=run.conversation_thread_id,
            payload=end_payload,
        )
    finally:
        await recorder.finalize()


async def _publish_run_progress(
    *,
    run_id: str,
    thread_id: str | None,
    request_id: str,
    done_event: asyncio.Event,
) -> None:
    """Publish visible progress independently from internal LangGraph events."""

    try:
        while True:
            try:
                await asyncio.wait_for(
                    done_event.wait(),
                    timeout=RUN_STREAM_PROGRESS_HEARTBEAT_SECONDS,
                )
                return
            except TimeoutError:
                pass

            try:
                await append_run_event(
                    run_id,
                    "custom",
                    {
                        "name": "yuxi.progress",
                        "chunk": {
                            "status": "progress",
                            "stage": "agent_processing",
                            "message": "服务端正在检索知识并生成回复…",
                            "request_id": request_id,
                        },
                    },
                    thread_id=thread_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - telemetry must not fail the run
                logger.exception(f"Failed to publish progress for run {run_id}")
    except asyncio.CancelledError:
        return


async def _consume_stream_with_cancel(agen, run_ctx: RunContext):
    stream_started_at = time.monotonic()
    while True:
        if time.monotonic() - stream_started_at >= RUN_STREAM_TOTAL_TIMEOUT_SECONDS:
            raise RunStreamTotalTimeout(
                f"agent stream exceeded {RUN_STREAM_TOTAL_TIMEOUT_SECONDS:.0f} seconds total runtime"
            )
        next_task = asyncio.create_task(agen.__anext__())
        idle_started_at = time.monotonic()
        while True:
            cancel_task = asyncio.create_task(run_ctx.wait_cancelled())
            idle_remaining = RUN_STREAM_IDLE_TIMEOUT_SECONDS - (time.monotonic() - idle_started_at)
            total_remaining = RUN_STREAM_TOTAL_TIMEOUT_SECONDS - (time.monotonic() - stream_started_at)
            wait_seconds = max(
                0.0,
                min(RUN_STREAM_PROGRESS_HEARTBEAT_SECONDS, idle_remaining, total_remaining),
            )
            done, _ = await asyncio.wait(
                {next_task, cancel_task},
                timeout=wait_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )

            if cancel_task in done:
                next_task.cancel("cancel_requested")
                await asyncio.gather(next_task, return_exceptions=True)
                raise asyncio.CancelledError(f"run {run_ctx.run_id} cancelled")

            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)

            if next_task in done:
                try:
                    yield next_task.result()
                except StopAsyncIteration:
                    return
                break

            if time.monotonic() - idle_started_at >= RUN_STREAM_IDLE_TIMEOUT_SECONDS:
                next_task.cancel("run_idle_timeout")
                await asyncio.gather(next_task, return_exceptions=True)
                raise RunStreamIdleTimeout(
                    f"agent stream produced no event for {RUN_STREAM_IDLE_TIMEOUT_SECONDS:.0f} seconds"
                )

            if time.monotonic() - stream_started_at >= RUN_STREAM_TOTAL_TIMEOUT_SECONDS:
                next_task.cancel("run_total_timeout")
                await asyncio.gather(next_task, return_exceptions=True)
                raise RunStreamTotalTimeout(
                    f"agent stream exceeded {RUN_STREAM_TOTAL_TIMEOUT_SECONDS:.0f} seconds total runtime"
                )

            # Yield control to the worker loop while the authoritative background
            # progress publisher keeps clients informed.
            yield _STREAM_WAITING


async def _emit_browser_guard_trace(run_id: str) -> None:
    """浏览器诚实性门禁：browser_enabled 轮若全程零审计，发用户可见轨迹事件。

    模型可能在未实际调用浏览器工具的情况下声称已完成操作（幻觉式合规）；
    审计表是唯一权威。零审计时明示事实，把静默幻觉变成可见、可查询的信号
    （与 answer.source_guard.completed 同一轨迹模式）。事件绝不影响发布路径。
    """
    try:
        from yuxi.services.browser_gateway_service import count_run_commands
        from yuxi.trace import emit_trace

        async with pg_manager.get_async_session_context() as db:
            audit_count = await count_run_commands(db, run_id=run_id)
        emit_trace(
            category="ANSWER",
            operation="browser_guard",
            event_type="answer.browser_guard.completed",
            title="本机浏览器核验",
            summary=(
                f"本轮浏览器工具实际调用 {audit_count} 次"
                + ("——回答中关于浏览器操作的描述未经实际执行，请谨慎采信" if audit_count == 0 else "")
            ),
            attributes={"browser_audit_count": audit_count, "invoked": audit_count > 0},
            visibility="USER",
        )
    except Exception as error:  # noqa: BLE001 —— 门禁绝不影响 run 终态
        logger.warning(f"browser guard trace skipped: {type(error).__name__}")


async def process_agent_run(ctx, run_id: str):
    """执行队列中的 AgentRun，并只从 run 列和输入消息恢复运行参数。"""
    run = await _get_run(run_id)
    if not run:
        logger.warning(f"Run not found: {run_id}")
        return

    # Receipt is the authoritative proof that the durable dispatch intent was
    # fulfilled, including the enqueue-success/ack-commit failure window.
    await acknowledge_agent_run_dispatch(run_id)

    if run.status in TERMINAL_RUN_STATUSES:
        logger.info(f"Run already terminal, skip: {run_id}, status={run.status}")
        return
    if run.status == "cancel_requested":
        await _finish_run_before_execution(
            run,
            "cancelled",
            error_type="cancelled",
            error_message="账号停用或用户请求取消",
            end_reason="cancelled_before_start",
        )
        return

    if not isinstance(run.input_payload, dict):
        await _finish_run_before_execution(
            run,
            "failed",
            error_type="invalid_input_payload",
            error_message="run input_payload 必须是对象",
        )
        return
    payload = run.input_payload
    runtime = payload.get("runtime") or {}
    if not isinstance(runtime, dict):
        await _finish_run_before_execution(
            run,
            "failed",
            error_type="invalid_runtime_payload",
            error_message="run input_payload.runtime 必须是对象",
        )
        return

    input_message = await _load_input_message(run.input_message_id)
    if not input_message:
        await _finish_run_before_execution(
            run,
            "failed",
            error_type="input_message_not_found",
            error_message="运行任务缺少输入消息",
        )
        return
    if not isinstance(input_message.extra_metadata, dict):
        await _finish_run_before_execution(
            run,
            "failed",
            error_type="invalid_input_metadata",
            error_message="输入消息 metadata 必须是对象",
        )
        return

    run_type = run.run_type
    agent_slug = run.agent_slug
    uid = run.uid
    request_id = run.request_id
    thread_id = run.conversation_thread_id
    input_metadata = input_message.extra_metadata
    image_content = input_message.image_content

    if run_type not in SUPPORTED_RUN_TYPES:
        await _finish_run_before_execution(
            run,
            "failed",
            error_type="invalid_run_type",
            error_message=f"不支持的 run_type: {run_type}",
        )
        return

    user = await _load_user(uid)
    if not user:
        await _finish_run_before_execution(
            run,
            "failed",
            error_type="user_unavailable",
            error_message=f"user {uid} is unavailable",
        )
        return

    resume_input = None
    if run_type == "resume":
        resume_input = input_metadata.get("resume")
        if resume_input is None:
            await _finish_run_before_execution(
                run,
                "failed",
                error_type="resume_input_not_found",
                error_message="resume run 缺少 resume 输入",
            )
            return
    else:
        try:
            normalized_input_message = restore_chat_input_message(
                content=input_message.content,
                image_content=image_content,
                metadata=input_metadata,
            )
        except ValueError as exc:
            await _finish_run_before_execution(
                run,
                "failed",
                error_type="invalid_input_message",
                error_message=str(exc),
            )
            return

    meta = {
        "run_id": run_id,
        "request_id": request_id,
        "agent_slug": agent_slug,
        "thread_id": thread_id,
        "uid": user.uid,
        "has_image": bool(image_content),
        "attachment_file_ids": input_metadata.get("attachment_file_ids") or [],
        "model_spec": payload.get("model_spec"),
        "knowledge_scope_snapshot": payload.get("knowledge_scope_snapshot"),
        "mention_resolution": payload.get("mention_resolution"),
        "user_credential": payload.get("user_credential"),
        "policy_version": payload.get("policy_version"),
        "browser_enabled": bool(payload.get("browser_enabled")),
        "run_type": run_type,
        "created_by_run_id": run.created_by_run_id,
    }
    if run_type == "subagent":
        # 三个线程 ID 在 subagent_run_service 创建 run 时已写入 runtime，此处不再二次兜底；
        # 缺失会在 chat_service._apply_subagent_runtime_context 处直接报错。
        meta["parent_thread_id"] = runtime.get("parent_thread_id")
        meta["file_thread_id"] = runtime.get("file_thread_id")
        meta["skills_thread_id"] = runtime.get("skills_thread_id")
    if input_metadata.get("source"):
        meta["source"] = input_metadata.get("source")
    if isinstance(input_metadata.get("agent_invocation_meta"), dict):
        meta["agent_invocation_meta"] = input_metadata.get("agent_invocation_meta") or {}

    await mark_run_running(run_id)
    run_ctx = RunContext(run_id=run_id)
    writer = ChunkedEventWriter(
        run_id=run_id,
        thread_id=thread_id,
        interval_ms=LOADING_FLUSH_INTERVAL_MS,
        max_chars=LOADING_FLUSH_MAX_CHARS,
    )
    await run_ctx.start()
    # 执行轨迹：事实账本（agent_run_trace_events）+ Outbox + 投影，同事务落库；
    # activate 后中间件/知识编排器经 emit_trace 无感埋点（API 进程内 no-op）。
    recorder = TraceRecorder(
        run_id=run_id,
        thread_id=thread_id,
        tenant_id=run.tenant_id,
        uid=uid,
        agent_slug=agent_slug,
        run_type=run_type,
        request_id=request_id,
        created_by_run_id=run.created_by_run_id,
    )
    await recorder.seed()
    recorder.activate()
    recorder.start_span(
        category="RUN",
        operation="execution",
        span_id="run",
        title="本轮执行",
        attributes={"agent_slug": agent_slug, "run_type": run_type, "request_id": request_id},
    )
    metadata_event = {
        "request_id": request_id,
        "agent_slug": agent_slug,
        "uid": uid,
        "source": input_metadata.get("source"),
        "run_type": run_type,
        "created_by_run_id": run.created_by_run_id,
        "subagent_slug": agent_slug if run_type == "subagent" else None,
    }
    if isinstance(input_metadata.get("agent_invocation_meta"), dict):
        metadata_event["agent_invocation_meta"] = input_metadata.get("agent_invocation_meta") or {}

    await append_run_event(
        run_id,
        "metadata",
        metadata_event,
        thread_id=thread_id,
    )
    terminal_outcome: tuple[str, str | None, str | None, dict] | None = None
    model_first_token_emitted = False
    progress_done = asyncio.Event()
    progress_task = asyncio.create_task(
        _publish_run_progress(
            run_id=run_id,
            thread_id=thread_id,
            request_id=request_id,
            done_event=progress_done,
        )
    )

    # MCP 执行身份必须设置在消费 Task（本协程）上：_consume_stream_with_cancel
    # 用 create_task(agen.__anext__()) 分步消费生成器，chat_service 在生成器首次
    # 激活里 set 的上下文只存在于那一个 Task 的 Context；后续每次 __anext__ 恢复
    # 都运行在新 Task 的全新 Context 里，工具节点读到的执行身份为 None，
    # record_mcp_call 静默丢弃审计 → MCP_ONLY 轮次被终态校验门误判来源不可用。
    # 在 worker Task 上 set 后，之后创建的每个子 Task 都会拷贝继承该值。
    mcp_execution_token = None
    if run.tenant_id is None:
        # 历史/测试 Run 可能没有租户快照。不能从请求体或用户对象猜租户；
        # 这类轮次仍可执行，但 MCP 审计会显式报告上下文缺口并由来源门失败关闭。
        logger.warning(f"AgentRun {run_id} has no authoritative tenant_id; MCP audit context was not activated")
    else:
        mcp_execution_token = set_mcp_execution_context(
            McpExecutionContext(
                tenant_id=int(run.tenant_id),
                uid=str(user.uid),
                thread_id=thread_id,
                run_id=run_id,
                agent_slug=agent_slug,
            )
        )
    # 本机浏览器执行身份与 MCP 同边界设置（同一段消费语义，见上注释）：
    # chat_service 生成器内 set 的值只活在首次激活的那个 Task，工具节点读不到；
    # 仅 browser_enabled 轮激活；缺租户快照时跳过，工具以 BROWSER_CONTEXT_MISSING
    # 显式失败（fail-closed），与 MCP 审计缺口的处理一致。
    browser_execution_token = None
    if meta.get("browser_enabled") and run.tenant_id is not None:
        browser_execution_token = set_browser_execution_context(
            BrowserExecutionContext(
                tenant_id=int(run.tenant_id),
                uid=str(user.uid),
                thread_id=thread_id,
                run_id=run_id,
                agent_slug=agent_slug,
            )
        )
    # 产物累积与 MCP 执行身份同边界开启：host 层物化的产物经该共享累积器
    # 由 ArtifactStateMiddleware.after_model 排空进 state.artifacts。
    artifact_accumulation_token = begin_artifact_accumulation()

    try:
        async with pg_manager.get_async_session_context() as db:
            if run_type == "resume":
                stream = stream_agent_resume(
                    thread_id=thread_id,
                    resume_input=resume_input,
                    meta=meta,
                    current_user=user,
                    db=db,
                )
            elif run_type in {"chat", "subagent"}:
                stream = stream_agent_chat(
                    agent_slug=agent_slug,
                    thread_id=thread_id,
                    meta=meta,
                    input_message=normalized_input_message,
                    current_user=user,
                    db=db,
                    save_user_message=False,
                )
            else:
                raise RuntimeError(f"unsupported run_type after validation: {run_type}")

            draining_for_terminal = False
            async for chunk_bytes in _consume_stream_with_cancel(stream, run_ctx):
                if chunk_bytes is _STREAM_WAITING:
                    continue
                if draining_for_terminal:
                    # 已捕获终态 chunk：继续排空生成器直到自然结束，而不是提前 break。
                    # stream 的会话消息落库发生在生成器收尾（save_messages_from_langgraph_state），
                    # 提前放弃生成器会让 ask_user/human_approval 中断前的消息永久不落库。
                    continue
                stop_stream = False
                for chunk in _iter_json_chunks(chunk_bytes):
                    if stop_stream:
                        # 终态已捕获：跳过同批次剩余 chunk，但继续排空后续流。
                        continue
                    target_thread_id = _chunk_thread_id(chunk, thread_id)
                    if chunk.get("status") == "loading":
                        if not model_first_token_emitted and _is_first_message_delta_chunk(chunk):
                            model_first_token_emitted = True
                            recorder.emit(
                                category="MODEL",
                                operation="generation",
                                event_type="model.generation.first_visible_token",
                                title="模型开始输出",
                            )
                        await writer.append(chunk, thread_id=target_thread_id)
                        continue

                    await writer.flush(target_thread_id)
                    status = chunk.get("status") or "event"
                    event_type, event_payload = _map_chunk_to_run_event(chunk)
                    if event_type != "end":
                        await append_run_event(run_id, event_type, event_payload, thread_id=target_thread_id)

                    if target_thread_id != thread_id:
                        if await run_ctx.is_cancelled():
                            raise asyncio.CancelledError(f"run {run_id} cancelled")
                        continue

                    if status == "finished":
                        terminal_outcome = ("completed", None, None, chunk)
                        stop_stream = True
                    elif status == "error":
                        terminal_outcome = (
                            "failed",
                            chunk.get("error_type") or "stream_error",
                            chunk.get("error_message") or chunk.get("message"),
                            chunk,
                        )
                        stop_stream = True
                    elif status == "interrupted":
                        status_value = "cancelled" if await _is_cancel_requested(run_id) else "interrupted"
                        terminal_outcome = (
                            status_value,
                            status_value,
                            chunk.get("message"),
                            chunk,
                        )
                        stop_stream = True
                    elif status in {"ask_user_question_required", "human_approval_required"}:
                        questions = chunk.get("questions") if isinstance(chunk, dict) else None
                        first_question = ""
                        if isinstance(questions, list) and questions:
                            first = questions[0]
                            if isinstance(first, dict):
                                first_question = str(first.get("question") or "").strip()

                        terminal_outcome = (
                            "interrupted",
                            status,
                            first_question or "需要用户回答问题",
                            chunk,
                        )
                        stop_stream = True

                    # 终态 chunk 已捕获后不再响应取消信号：此刻输出已完整产生，
                    # 晚到的取消不应把 completed 翻转成 cancelled（桌面端/前端会出现
                    # "完整回答 + 已取消"的自相矛盾终态）。
                    if terminal_outcome is None and await run_ctx.is_cancelled():
                        raise asyncio.CancelledError(f"run {run_id} cancelled")
                # 循环内 flush 严禁阻塞业务生成器：即使未来出现意外锁等待，
                # 也以超时放弃（flush 幂等，由下轮 flush / 终态事务兜底）。
                try:
                    await asyncio.wait_for(recorder.flush(), timeout=TRACE_FLUSH_TIMEOUT_SECONDS)
                except TimeoutError:
                    logger.warning(f"trace mid-run flush timed out for run {run_id}; deferred")
                if stop_stream:
                    draining_for_terminal = True

        # The stream writes the final assistant message with the session above.  Only
        # expose a terminal run after that transaction has committed, otherwise a
        # result reader can observe completed + empty output.
        await writer.flush()
        if terminal_outcome is not None:
            terminal_status, error_type, error_message, terminal_chunk = terminal_outcome
            terminal_chunk = await _attach_run_artifacts_to_finished_chunk(run_id, terminal_chunk)
            trace_committed = await _persist_terminal_trace(
                recorder,
                terminal_status,
                error_type=error_type,
                error_message=error_message,
            )
            await _append_end_event(
                run_id,
                terminal_status,
                thread_id=thread_id,
                payload={"chunk": terminal_chunk, "trace_status": "COMMITTED" if trace_committed else "DEGRADED"},
            )
        else:
            finished_chunk = {"status": "finished", "request_id": request_id}
            finished_chunk = await _attach_run_artifacts_to_finished_chunk(run_id, finished_chunk)
            trace_committed = await _persist_terminal_trace(recorder, "completed")
            await _append_end_event(
                run_id,
                "completed",
                thread_id=thread_id,
                payload={"chunk": finished_chunk, "trace_status": "COMMITTED" if trace_committed else "DEGRADED"},
            )

    except (RunStreamIdleTimeout, RunStreamTotalTimeout) as e:
        await writer.flush()
        error_message = "服务端长时间未收到检索或模型输出，已安全结束本次任务，请重试。"
        error_type = "run_total_timeout" if isinstance(e, RunStreamTotalTimeout) else "run_idle_timeout"
        error_chunk = {
            "status": "error",
            "error_type": error_type,
            "error_message": error_message,
            "message": error_message,
            "request_id": request_id,
            "retryable": True,
        }
        await append_run_event(
            run_id,
            "error",
            {"chunk": error_chunk, "retryable": True},
            thread_id=thread_id,
        )
        trace_committed = await _persist_terminal_trace(
            recorder, "failed", error_type=error_type, error_message=error_message
        )
        await _append_end_event(
            run_id,
            "failed",
            thread_id=thread_id,
            payload={"chunk": error_chunk, "trace_status": "COMMITTED" if trace_committed else "DEGRADED"},
        )
        logger.error(f"Run timeout {run_id}: {e}")
        return
    except asyncio.CancelledError:
        await writer.flush()
        cancel_chunk = {"status": "interrupted", "message": "对话已取消", "request_id": request_id}
        await append_run_event(
            run_id,
            "interrupt",
            {"reason": "cancelled", "chunk": cancel_chunk},
            thread_id=thread_id,
        )
        trace_committed = await _persist_terminal_trace(
            recorder, "cancelled", error_type="cancelled", error_message="对话已取消"
        )
        await _append_end_event(
            run_id,
            "cancelled",
            thread_id=thread_id,
            payload={"chunk": cancel_chunk, "trace_status": "COMMITTED" if trace_committed else "DEGRADED"},
        )
        logger.info(f"Run cancelled: {run_id}")
    except Exception as e:
        await writer.flush()
        if _is_retryable_exception(e):
            job_try = _job_try(ctx)
            logger.warning(f"Run retryable failure {run_id} (try={job_try}): {e}")
            retryable_error_chunk = {
                "status": "error",
                "error_type": "retryable_worker_error",
                "error_message": str(e),
                "request_id": request_id,
                "retryable": True,
                "job_try": job_try,
            }
            await append_run_event(
                run_id,
                "error",
                {"chunk": retryable_error_chunk, "retryable": True},
                thread_id=thread_id,
            )
            # 不做自动重跑：重跑会从 checkpoint 重复注入本轮 human 输入与合成检索
            # 消息（上下文污染 + 业务消息表出现重复轮次）。可重试错误同样以明确
            # 错误终止，由用户重新发送产生新的干净 run。
            trace_committed = await _persist_terminal_trace(
                recorder, "failed", error_type="retryable_worker_error", error_message=None
            )
            await _append_end_event(
                run_id,
                "failed",
                thread_id=thread_id,
                payload={
                    "chunk": retryable_error_chunk,
                    "trace_status": "COMMITTED" if trace_committed else "DEGRADED",
                },
            )
            logger.error(f"Run failed with retryable error, no auto-retry {run_id}: {e}")
            return

        logger.error(f"Run failed {run_id}: {e}")
        error_chunk = {
            "status": "error",
            "error_type": "worker_error",
            "error_message": str(e),
            "request_id": request_id,
            "retryable": False,
        }
        await append_run_event(
            run_id,
            "error",
            {"chunk": error_chunk, "retryable": False},
            thread_id=thread_id,
        )
        trace_committed = await _persist_terminal_trace(
            recorder, "failed", error_type="worker_error", error_message=None
        )
        await _append_end_event(
            run_id,
            "failed",
            thread_id=thread_id,
            payload={"chunk": error_chunk, "trace_status": "COMMITTED" if trace_committed else "DEGRADED"},
        )
        return
    finally:
        # 浏览器任务会话回收：在 worker Task 边界执行（工具继承的同一实例）；
        # ended 标志与 chat_service 生成器内的 best-effort 清理幂等互斥。
        if browser_execution_token is not None:
            await end_browser_task_best_effort()
            await _emit_browser_guard_trace(run_id)
            reset_browser_execution_context(browser_execution_token)
        # 与上方 set_mcp_execution_context 配对；跨 Context 时安全降级为 no-op。
        reset_mcp_execution_context(mcp_execution_token)
        end_artifact_accumulation(artifact_accumulation_token)
        progress_done.set()
        progress_task.cancel()
        await asyncio.gather(progress_task, return_exceptions=True)
        await recorder.finalize()
        await run_ctx.close()
        await clear_cancel_signal(run_id)


async def _load_input_message(message_id: int | None) -> Message | None:
    """加载 run 绑定的输入消息；worker 从这里恢复 query、resume、图片和请求元数据。"""
    if not message_id:
        return None
    async with pg_manager.get_async_session_context() as db:
        result = await db.execute(select(Message).where(Message.id == message_id))
        return result.scalar_one_or_none()


async def worker_heartbeat(ctx: Any = None) -> None:
    """ARQ 循环心跳：每分钟在事件循环内刷新心跳文件。

    Docker 健康检查锚定该文件的新鲜度（容忍 200s）：进程活着但 ARQ 循环卡死/
    崩溃时心跳停更，容器正确转为 unhealthy——修复「假健康 + 任务无限 pending」
    陷阱（容器启动时 Milvus 未就绪可让 api/worker 同时启动失败）。
    """
    del ctx
    from pathlib import Path as _P

    marker = _P("/tmp/yuxi_worker_heartbeat")
    try:
        marker.write_text(str(time.time()), encoding="utf-8")
    except OSError as error:  # noqa: BLE001 —— /tmp 不可写属部署异常,交由健康检查暴露
        logger.warning(f"worker heartbeat write failed: {type(error).__name__}")


async def _worker_startup(ctx):
    del ctx
    # 启动即落一次心跳：避免冷启动后首个 cron 窗口内健康检查空档
    await worker_heartbeat()
    pg_manager.initialize()
    await pg_manager.create_business_tables()
    await pg_manager.ensure_business_schema()
    # 视觉科研定位能力 canary（D5）：worker 是图片定位的主要执行进程，进程内
    # 能力缓存与 API 不共享——必须在 worker 启动时独立探测并记录状态。
    try:
        from yuxi.knowledge.vision.provider import probe_vision_capability

        capability = await probe_vision_capability()
        logger.info(
            f"[worker] Vision scientific locator: model = {capability['model'] or '(none)'}"
            f", status = {capability['status']} ({capability['detail']})"
        )
    except Exception as exc:  # noqa: BLE001 - canary 失败不阻断 worker 启动
        logger.warning(f"[worker] Vision capability canary skipped: {exc}")
    from yuxi.knowledge.runtime import knowledge_base

    await knowledge_base.initialize()
    # 启动即清扫一次历史孤儿 run（进程崩溃/Redis 瞬断遗留的 pending/running），
    # 释放被唯一活跃索引锁住的线程。
    await reconcile_stale_agent_runs()
    await dispatch_pending_agent_runs()
    await recover_stale_scientific_pdf_ingests()
    await reconcile_dynamic_wikis()
    await relay_trace_outbox()
    await ensure_builtin_mcp_servers_in_db()
    async with pg_manager.get_async_session_context() as session:
        await init_builtin_skills(session)
    from yuxi.knowledge.parser.credential_cache import ocr_credential_cache
    from yuxi.services.ocr_provider_service import ensure_builtin_ocr_provider_in_db, get_all_ocr_providers

    async with pg_manager.get_async_session_context() as session:
        await ensure_builtin_ocr_provider_in_db(session)
    async with pg_manager.get_async_session_context() as session:
        ocr_credential_cache.rebuild(await get_all_ocr_providers(session))
    sys_config.start_runtime_sync()


async def _worker_shutdown(ctx):
    await pg_manager.close()


class WorkerSettings:
    functions = [
        process_agent_run,
        worker_heartbeat,
        process_scientific_pdf_ingest,
        process_dynamic_wiki_build,
        prewarm_doclex_for_kb,
        process_channel_message,
        relay_channel_outbox_now,
    ]
    # 每 5 分钟清扫一次孤儿 run；worker 启动时也会立即执行一次。
    cron_jobs = [
        # ARQ 循环心跳（compose 健康检查锚定 /tmp/yuxi_worker_heartbeat，200s 容忍）
        cron(worker_heartbeat, minute=set(range(0, 60))),
        cron(reconcile_stale_agent_runs, minute=set(range(0, 60, 5))),
        cron(dispatch_pending_agent_runs, minute=set(range(0, 60))),
        cron(recover_stale_scientific_pdf_ingests, minute=set(range(0, 60, 5))),
        cron(reconcile_dynamic_wikis, minute=set(range(0, 60))),
        # 轨迹 Outbox relay：fast-path 直发失败/worker 崩溃遗留的 PENDING 补发
        cron(relay_trace_outbox, minute=set(range(0, 60))),
        # 渠道出站 Outbox relay：claim/lease + 指数退避，5 次后 DEAD（管理页可 requeue）
        cron(relay_channel_outbox, minute=set(range(0, 60))),
        # 每日按有界多批追赶超过策略期限的 STANDARD 终态轨迹；数据库函数承担安全门。
        cron(purge_expired_trace_runs, hour={3}, minute={17}),
        # 渠道历史保留期（H）：超期流水/终态 outbox/过期绑定有界清理；PENDING 绝不删
        cron(purge_channel_history, hour={3}, minute={23}),
        # 每日 MCP live canary：离线契约证明不了远程持续可用——成功率/空结果率
        # /p95 延迟落轨迹事件（mcp.canary.*），监控侧按阈值决定升级。
        cron(run_mcp_live_canary, hour={2}, minute={30}),
    ]
    # 不做 ARQ 自动重试：重跑会从 checkpoint 重复注入本轮输入（见 process_agent_run
    # 的 except 分支说明）。max_tries 保留 1 仅作为兜底声明。
    max_tries = 1
    retry_jobs = False
    job_timeout = 3600
    keep_result = 60
    # 显式抬高并发槽位：subagent 的 task 工具在 job 内同步等待子 run 终结，
    # 父子共享同一槽位池，默认 10 个槽位在多用户并行 subagent 时会父子互等。
    # 这是结构性缓解（彻底解法需要独立队列）；数值需大于预期的并行等待数。
    max_jobs = 32
    on_startup = _worker_startup
    on_shutdown = _worker_shutdown
    try:
        redis_settings = get_arq_redis_settings()
    except Exception:
        redis_settings = None
