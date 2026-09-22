"""MCP 查询结果的确定性数据产物物化。

设计原则（与 sequence_deliverable 同源的尽力而为语义）：

1. **确定性优先**：产物是否出现不依赖模型是否调用 ``present_artifacts``。
   host 层在 MCP 工具成功返回后由程序把数据结果落盘成文件，按钮的出现
   由代码保证（R2）。
2. **两条出口一次登记**：落盘文件同时进入（a）LangGraph ``state.artifacts``
   ——经 run 级累积器 + ``ArtifactStateMiddleware.after_model`` 排空合并，
   SSE ``agent_state`` 与 ``/state`` 恒可见；（b）``run_artifacts`` 表——
   历史回看读时投影与 run result 的唯一真源。
3. **零新增鉴权面**：文件落在 ``outputs/mcp_results/`` 下，复用既有 thread
   artifacts / viewer 下载端点（归属与目录遏制在那两处校验）。
4. **尽力而为**：无执行上下文、超限、IO 错误只记日志返回 None，绝不阻断
   工具调用与回答流。
5. **账本时序红线**：物化取 ``result.text`` 必须发生在 ``append_model_ledger``
   之前（host.call_tool 调用方保证），否则 MCP-F 账本标记会混进交付物。

累积器可见性说明：ContextVar 在 run 边界（run_worker 任务 / chat_service
流式生成器消费任务）set，graph 内部的工具节点/模型节点子 Task 拷贝同一
Context、共享同一个可变 list 对象——与 ``McpExecutionContext`` 的跨 Task
传播论证同源（run_worker ``set_mcp_execution_context`` 处注释）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

from yuxi.utils import logger
from yuxi.utils.paths import VIRTUAL_PATH_OUTPUTS

MCP_RESULTS_DIR_NAME = "mcp_results"
#: 纯文本结果低于该字符数不物化（小结果留在回答正文即可，避免产物噪音）
_MATERIALIZED_MIN_TEXT_CHARS = 2000
#: 单个物化文件上限（结构化 JSON / 长文本）
_MAX_ARTIFACT_BYTES = 10 * 1024 * 1024
#: 单 run 物化文件数上限（护栏：异常工具循环不得刷爆线程 outputs）
_MAX_FILES_PER_RUN = 20
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class MaterializedArtifact:
    """一个已落盘产物的登记要素（内存流转，可 JSON 序列化）。"""

    virtual_path: str
    name: str
    sha256: str
    size_bytes: int
    media_type: str
    origin: dict[str, Any]


@dataclass
class ArtifactAccumulator:
    """Run 级物化累积器：跨 Task 共享的可变对象，由 ContextVar 持有引用。"""

    entries: list[MaterializedArtifact] = field(default_factory=list)
    materialized_count: int = 0

    def take_entries(self) -> list[MaterializedArtifact]:
        entries = self.entries
        self.entries = []
        return entries


_ARTIFACT_ACCUMULATOR: ContextVar[ArtifactAccumulator | None] = ContextVar(
    "yuxi_run_artifact_accumulator", default=None
)


def begin_artifact_accumulation() -> Token:
    """在 run 边界开启一次产物累积（与 set_mcp_execution_context 同一位置调用）。"""
    return _ARTIFACT_ACCUMULATOR.set(ArtifactAccumulator())


def end_artifact_accumulation(token) -> None:
    """跨异步上下文安全地结束累积（语义对齐 reset_mcp_execution_context）。"""
    if token is None:
        return
    try:
        _ARTIFACT_ACCUMULATOR.reset(token)
    except (ValueError, RuntimeError):
        # 跨 Context / 重复 reset：no-op，绝不污染已完成的结果流。
        return


def drain_materialized_artifacts() -> list[MaterializedArtifact]:
    """排空当前 run 尚未进入 state 的产物条目（供 after_model 中间件消费）。"""
    accumulator = _ARTIFACT_ACCUMULATOR.get()
    if accumulator is None:
        return []
    return accumulator.take_entries()


def _safe_filename_part(value: str, fallback: str) -> str:
    return _SAFE_FILENAME_RE.sub("-", str(value or "")).strip(".-")[:64] or fallback


def _decide_payload(
    *,
    result_text: str,
    structured_content: dict[str, Any] | None,
) -> tuple[bytes, str, str] | None:
    """选择物化负载：结构化内容优先（JSON），长文本次之（Markdown）。"""
    if isinstance(structured_content, dict) and structured_content:
        payload = json.dumps(structured_content, ensure_ascii=False, indent=2, default=str).encode("utf-8")
        return payload, "json", "application/json"
    if len(result_text or "") >= _MATERIALIZED_MIN_TEXT_CHARS:
        return str(result_text).encode("utf-8"), "md", "text/markdown"
    return None


async def register_run_artifact(*, context, entry: MaterializedArtifact) -> bool:
    """写 run_artifacts 权威记录；同 run 同路径重复登记幂等，失败只记日志。

    无 run_id（如同步直连链路未建 AgentRun）时不登记——文件与 state 通道
    仍然生效，只有历史回看投影拿不到该轮产物（边界见设计文档）。
    """
    run_id = getattr(context, "run_id", None)
    if not run_id:
        return False
    from yuxi.storage.postgres.manager import pg_manager
    from yuxi.storage.postgres.models_business import RunArtifact

    try:
        async with pg_manager.get_async_session_context() as session:
            session.add(
                RunArtifact(
                    tenant_id=getattr(context, "tenant_id", None),
                    uid=str(getattr(context, "uid", "")),
                    run_id=str(run_id),
                    thread_id=str(getattr(context, "thread_id", "") or ""),
                    origin=dict(entry.origin),
                    name=entry.name,
                    virtual_path=entry.virtual_path,
                    sha256=entry.sha256,
                    size_bytes=entry.size_bytes,
                    media_type=entry.media_type,
                )
            )
            await session.commit()
        return True
    except Exception as error:  # noqa: BLE001 —— 登记是增强通道；唯一约束冲突/库不可用都不阻断主流程
        logger.info(f"Run artifact registration skipped for {entry.virtual_path}: {type(error).__name__}")
        return False


async def note_delivered_artifact(entry: MaterializedArtifact) -> None:
    """登记一个已落盘产物：进 state 累积器 + 写权威表；失败只记日志。"""
    accumulator = _ARTIFACT_ACCUMULATOR.get()
    if accumulator is not None:
        accumulator.materialized_count += 1
        accumulator.entries.append(entry)
    from yuxi.agents.mcp.execution import get_mcp_execution_context

    context = get_mcp_execution_context()
    if context is not None:
        await register_run_artifact(context=context, entry=entry)


async def materialize_mcp_data_result(
    *,
    server_slug: str,
    tool_name: str,
    result_text: str,
    structured_content: dict[str, Any] | None,
    is_error: bool,
    mcp_call_audit_id: int | None,
) -> MaterializedArtifact | None:
    """把一次成功的 MCP 数据查询结果物化为线程 outputs 下的可下载产物。

    任何失败（无上下文、低于阈值、超限、IO 错误）都返回 None 且不抛异常。
    """
    if is_error:
        return None
    accumulator = _ARTIFACT_ACCUMULATOR.get()
    if accumulator is None:
        # 无 run 边界（离线评估等）：state 通道不可用，不物化。
        return None
    from yuxi.agents.mcp.execution import get_mcp_execution_context

    context = get_mcp_execution_context()
    if context is None or not context.thread_id:
        return None

    decided = _decide_payload(result_text=result_text, structured_content=structured_content)
    if decided is None:
        return None
    payload, extension, media_type = decided
    if len(payload) > _MAX_ARTIFACT_BYTES:
        logger.info(
            f"MCP result not materialized ({server_slug}/{tool_name}): "
            f"{len(payload)} bytes exceeds {_MAX_ARTIFACT_BYTES}"
        )
        return None
    if accumulator.materialized_count >= _MAX_FILES_PER_RUN:
        logger.warning(
            f"MCP result not materialized ({server_slug}/{tool_name}): "
            f"per-run artifact limit {_MAX_FILES_PER_RUN} reached"
        )
        return None

    digest = hashlib.sha256(payload).hexdigest()
    filename = (
        f"{_safe_filename_part(server_slug, 'mcp')}_{_safe_filename_part(tool_name, 'tool')}_{digest[:8]}.{extension}"
    )
    thread_id = str(context.thread_id)
    uid = str(context.uid)

    def _write_file() -> None:
        from yuxi.agents.backends.sandbox import ensure_thread_dirs, sandbox_outputs_dir

        ensure_thread_dirs(thread_id, uid)
        target_dir = sandbox_outputs_dir(thread_id) / MCP_RESULTS_DIR_NAME
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / filename).write_bytes(payload)

    try:
        await asyncio.to_thread(_write_file)
    except Exception as error:  # noqa: BLE001 —— 物化是增强通道，绝不阻断工具调用主流程
        logger.warning(f"MCP artifact materialization skipped: {type(error).__name__}: {error}")
        return None

    entry = MaterializedArtifact(
        virtual_path=f"{VIRTUAL_PATH_OUTPUTS}/{MCP_RESULTS_DIR_NAME}/{filename}",
        name=filename,
        sha256=digest,
        size_bytes=len(payload),
        media_type=media_type,
        origin={
            "source": "mcp",
            "mcp_server": server_slug,
            "mcp_tool": tool_name,
            "mcp_call_audit_id": mcp_call_audit_id,
        },
    )
    accumulator.materialized_count += 1
    accumulator.entries.append(entry)
    await register_run_artifact(context=context, entry=entry)
    logger.info(f"Materialized MCP data artifact: {entry.virtual_path} ({entry.size_bytes} bytes)")
    return entry


__all__ = [
    "MCP_RESULTS_DIR_NAME",
    "ArtifactAccumulator",
    "MaterializedArtifact",
    "begin_artifact_accumulation",
    "drain_materialized_artifacts",
    "end_artifact_accumulation",
    "materialize_mcp_data_result",
    "note_delivered_artifact",
    "register_run_artifact",
]
