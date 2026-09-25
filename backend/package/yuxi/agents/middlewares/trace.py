"""执行轨迹中间件：MODEL/TOOL/MCP/SUBAGENT span 的统一埋点。

- 只依赖 ``yuxi.trace`` 的 contextvar recorder：API 进程或无 run 上下文时
  ``current_recorder()`` 为 None，全部 no-op，业务零开销零风险；
- 工具入参只记 sha256 摘要（digest），不落原文——与 mcp_call_audit 同策略；
- MCP 工具通过 StructuredTool.metadata（server/mcp_tool_name）识别，没有
  metadata 时按普通 TOOL 记录，不做猜测。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import AIMessage

from yuxi.trace.recorder import current_recorder
from yuxi.trace.redaction import digest_text

SUBAGENT_TOOL_NAME = "task"


def _extract_model_usage(response: Any) -> dict[str, int]:
    """从 ModelResponse / ExtendedModelResponse 提取 usage_metadata（可缺省）。"""
    results = getattr(response, "result", None)
    if results is None:
        inner = getattr(response, "model_response", None)
        results = getattr(inner, "result", None)
    if not isinstance(results, list):
        return {}
    for message in reversed(results):
        if not isinstance(message, AIMessage):
            continue
        usage = getattr(message, "usage_metadata", None)
        if isinstance(usage, Mapping):
            return {str(key): int(value) for key, value in usage.items() if isinstance(value, int)}
    return {}


def _model_display_name(request: ModelRequest) -> str | None:
    model = getattr(request, "model", None)
    for attr in ("fully_specified_name", "model_name", "model", "deployment_name"):
        value = getattr(model, attr, None)
        if isinstance(value, str) and value:
            return value
    # Model repr/str may contain provider clients, base URLs, or SecretStr
    # fields. A stable class name is enough when no explicit model id exists.
    return type(model).__name__ if model is not None else None


def _tool_metadata(request: ToolCallRequest) -> dict[str, Any]:
    tool = getattr(request, "tool", None)
    metadata = getattr(tool, "metadata", None)
    return metadata if isinstance(metadata, dict) else {}


def _tool_result_is_error(result: Any) -> bool:
    """Read structured tool error markers without guessing from result text."""
    if getattr(result, "status", None) == "error":
        return True
    artifact = getattr(result, "artifact", None)
    if isinstance(artifact, dict) and artifact.get("is_error") is True:
        return True
    if isinstance(result, tuple) and len(result) == 2:
        artifact = result[1]
        return isinstance(artifact, dict) and artifact.get("is_error") is True
    return False


def _tool_result_content(result: Any) -> Any:
    """提取工具结果的文本面（ToolMessage.content 或 content_and_artifact 元组首元）。"""
    content = getattr(result, "content", None)
    if content is None and isinstance(result, tuple) and len(result) == 2:
        content = result[0]
    return content


def _tool_result_digest(result: Any) -> str | None:
    try:
        return digest_text(_tool_result_content(result))
    except Exception:  # noqa: BLE001 —— 摘要失败不影响 span 收口
        return None


def _mcp_result_audit_id(result: Any) -> int | None:
    """从 MCP envelope artifact 里读回本次调用的审计 id（host 层写入 provenance）。"""
    artifact = getattr(result, "artifact", None)
    if artifact is None and isinstance(result, tuple) and len(result) == 2:
        artifact = result[1]
    if not isinstance(artifact, dict):
        return None
    payload = artifact.get("payload")
    if not isinstance(payload, dict):
        return None
    provenance = payload.get("provenance")
    if not isinstance(provenance, dict):
        return None
    audit_id = provenance.get("mcp_call_audit_id")
    try:
        return int(audit_id) if audit_id is not None else None
    except (TypeError, ValueError):
        return None


class TraceMiddleware(AgentMiddleware):
    """把模型调用与工具执行映射为 yuxi.run-trace.v1 的 span 事件。"""

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        recorder = current_recorder()
        if recorder is None:
            return await handler(request)

        span_id = recorder.next_span_id("model")
        model_name = _model_display_name(request)
        attributes: dict[str, Any] = {}
        if model_name:
            attributes["model_spec"] = model_name
        if recorder.model_credential_source:
            attributes["credential_source"] = recorder.model_credential_source
        recorder.emit(
            category="MODEL",
            operation="generation",
            event_type="model.generation.started",
            span_id=span_id,
            title="模型生成",
            attributes=attributes,
        )
        started = time.monotonic()
        try:
            response = await handler(request)
        except Exception as error:
            recorder.finish_span(
                span_id,
                suffix="failed",
                error_type=type(error).__name__,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            raise
        usage = _extract_model_usage(response)
        completed_attributes = dict(attributes)
        completed_attributes.update(
            {
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "total_tokens": usage.get("total_tokens"),
            }
        )
        recorder.finish_span(
            span_id,
            suffix="completed",
            summary="模型生成完成",
            duration_ms=int((time.monotonic() - started) * 1000),
            attributes=completed_attributes,
        )
        return response

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        recorder = current_recorder()
        if recorder is None:
            return await handler(request)

        tool_call = request.tool_call if isinstance(request.tool_call, dict) else {}
        tool_name = str(tool_call.get("name") or "unknown")
        tool_call_id = tool_call.get("id")
        span_id = str(tool_call_id) if tool_call_id else recorder.next_span_id("tool")

        metadata = _tool_metadata(request)
        mcp_server = metadata.get("server")
        if mcp_server:
            category = "MCP"
            attributes = {
                "tool": tool_name,
                "mcp_server": str(mcp_server),
                "mcp_tool": str(metadata.get("mcp_tool_name") or tool_name),
                "args_digest": digest_text(tool_call.get("args")),
            }
            title = f"MCP {tool_name}"
        elif tool_name == SUBAGENT_TOOL_NAME:
            category = "SUBAGENT"
            attributes = {"tool": tool_name, "args_digest": digest_text(tool_call.get("args"))}
            title = "子智能体任务"
        else:
            category = "TOOL"
            attributes = {"tool": tool_name, "args_digest": digest_text(tool_call.get("args"))}
            title = tool_name

        recorder.emit(
            category=category,
            operation="execution",
            event_type=f"{category.lower()}.execution.started",
            span_id=span_id,
            title=title,
            attributes=attributes,
        )
        started = time.monotonic()
        try:
            result = await handler(request)
        except Exception as error:
            recorder.finish_span(
                span_id,
                suffix="failed",
                error_type=type(error).__name__,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            raise
        duration_ms = int((time.monotonic() - started) * 1000)
        if _tool_result_is_error(result):
            failure_attributes: dict[str, Any] = {}
            if category == "MCP":
                audit_id = _mcp_result_audit_id(result)
                if audit_id is not None:
                    failure_attributes["mcp_audit_id"] = audit_id
            recorder.finish_span(
                span_id,
                suffix="failed",
                error_type="tool_result_error",
                duration_ms=duration_ms,
                attributes=failure_attributes or None,
            )
        else:
            completed_attributes: dict[str, Any] = {}
            if category == "MCP":
                audit_id = _mcp_result_audit_id(result)
                if audit_id is not None:
                    completed_attributes["mcp_audit_id"] = audit_id
            elif category == "TOOL":
                result_digest = _tool_result_digest(result)
                if result_digest:
                    completed_attributes["result_digest"] = result_digest
            recorder.finish_span(
                span_id,
                suffix="completed",
                duration_ms=duration_ms,
                attributes=completed_attributes or None,
            )
        return result
