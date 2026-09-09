"""Trace 投影纯函数：账本事件 → Span/Summary 状态。

同时服务于两条路径：

- recorder 实时投影：flush 时把内存中的 span/summary 状态 upsert 进读模型表；
- replay 重建：DELETE 投影 → 按序重放账本事件 → 重新得到同样状态。

所有函数都是纯的（输入 dict，输出/变更副本），不触碰数据库。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

SPAN_STATUS_RUNNING = "RUNNING"
SPAN_STATUS_COMPLETED = "COMPLETED"
SPAN_STATUS_FAILED = "FAILED"
SPAN_STATUS_INTERRUPTED = "INTERRUPTED"
SPAN_STATUS_SKIPPED = "SKIPPED"

_SPAN_TERMINAL_SUFFIX = {
    "completed": SPAN_STATUS_COMPLETED,
    "failed": SPAN_STATUS_FAILED,
    "interrupted": SPAN_STATUS_INTERRUPTED,
    "cancelled": SPAN_STATUS_INTERRUPTED,
    "skipped": SPAN_STATUS_SKIPPED,
}

# 汇总计数按类别累加的列名（RUN 不计数，只驱动状态）
_CATEGORY_COUNT_FIELDS = {
    "MODEL": "model_calls",
    "TOOL": "tool_calls",
    "MCP": "mcp_calls",
    "KNOWLEDGE": "knowledge_calls",
    "SUBAGENT": "subagent_calls",
}


def _parse_occurred_at(event: dict[str, Any]) -> datetime | None:
    raw = event.get("occurred_at")
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    if isinstance(raw, str) and raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def new_summary_state(header: dict[str, Any]) -> dict[str, Any]:
    state = {
        "run_id": header.get("run_id"),
        "thread_id": header.get("thread_id"),
        "tenant_id": header.get("tenant_id"),
        "agent_slug": header.get("agent_slug"),
        "run_type": header.get("run_type"),
        "request_id": header.get("request_id"),
        "created_by_run_id": header.get("created_by_run_id"),
        "status": None,
        "started_at": None,
        "first_token_at": None,
        "finished_at": None,
        "duration_ms": None,
        "ttft_ms": None,
        # UsageLedger/AgentRun 是计费权威；不知道的值必须是 null，不能伪装成 0。
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "model_calls": 0,
        "tool_calls": 0,
        "mcp_calls": 0,
        "knowledge_calls": 0,
        "subagent_calls": 0,
        "skill_count": 0,
        "retry_count": 0,
        "error_count": 0,
        "trace_event_count": 0,
        "last_sequence": 0,
        "attributes": {},
    }
    return state


def apply_event_to_summary(state: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    """把一条事件投影进 run 汇总状态（原地变更并返回，便于增量使用）。"""
    category = str(event.get("category") or "")
    event_type = str(event.get("event_type") or "")
    occurred_at = _parse_occurred_at(event)
    attributes = event.get("attributes") if isinstance(event.get("attributes"), dict) else {}

    state["trace_event_count"] = int(state.get("trace_event_count") or 0) + 1
    state["last_sequence"] = max(int(state.get("last_sequence") or 0), int(event.get("sequence") or 0))

    if occurred_at is not None:
        if state.get("started_at") is None:
            state["started_at"] = occurred_at
        state["finished_at"] = occurred_at
        if state.get("started_at") is not None:
            delta = (occurred_at - state["started_at"]).total_seconds()
            state["duration_ms"] = int(max(0.0, delta) * 1000)

    count_field = _CATEGORY_COUNT_FIELDS.get(category)
    if count_field and event_type.endswith(".started"):
        state[count_field] = int(state.get(count_field) or 0) + 1

    if category == "SKILL" and event_type == "skill.runtime.resolved":
        state["skill_count"] = len(attributes.get("prompt_skills") or []) or int(attributes.get("skill_count") or 0)

    if event_type.endswith(".retrying"):
        state["retry_count"] = int(state.get("retry_count") or 0) + 1
    if event_type.endswith(".failed"):
        state["error_count"] = int(state.get("error_count") or 0) + 1

    if category == "MODEL":
        if (
            event_type in {"model.generation.first_token", "model.generation.first_visible_token"}
            and state.get("first_token_at") is None
        ):
            state["first_token_at"] = occurred_at
            if state.get("started_at") is not None and occurred_at is not None:
                state["ttft_ms"] = int(max(0.0, (occurred_at - state["started_at"]).total_seconds()) * 1000)
        # token 仅是事件遥测字段；run 汇总的计费值由读取层从 UsageLedger 注入。
        model_spec = attributes.get("model_spec")
        if model_spec and "model_spec" not in (state.get("attributes") or {}):
            state.setdefault("attributes", {})["model_spec"] = model_spec
        credential_source = attributes.get("credential_source")
        if credential_source and "credential_source" not in (state.get("attributes") or {}):
            state.setdefault("attributes", {})["credential_source"] = credential_source

    if category == "KNOWLEDGE":
        scope_version = attributes.get("knowledge_scope_version")
        if scope_version and "knowledge_scope_version" not in (state.get("attributes") or {}):
            state.setdefault("attributes", {})["knowledge_scope_version"] = scope_version

    if category == "RUN":
        if event_type == "run.execution.started":
            state["status"] = "running"
        elif event_type.startswith("run.execution.") and event_type.rsplit(".", 1)[-1] in {
            "completed",
            "failed",
            "cancelled",
            "interrupted",
        }:
            state["status"] = event_type.rsplit(".", 1)[-1]
            if event_type == "run.execution.failed":
                state.setdefault("attributes", {})["error_type"] = attributes.get("error_type")
    return state


def apply_event_to_spans(spans: dict[str, dict[str, Any]], event: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """把一条事件投影进 span 状态表（span_id → state）。返回同一字典。"""
    span_id = event.get("span_id")
    if not span_id:
        return spans
    category = str(event.get("category") or "")
    operation = str(event.get("operation") or "")
    event_type = str(event.get("event_type") or "")
    occurred_at = _parse_occurred_at(event)
    duration_ms = event.get("duration_ms")
    attributes = event.get("attributes") if isinstance(event.get("attributes"), dict) else {}
    suffix = event_type.rsplit(".", 1)[-1] if event_type.count(".") >= 2 else ""

    span = spans.get(span_id)
    if span is None:
        span = {
            "span_id": span_id,
            "parent_span_id": event.get("parent_span_id"),
            "category": category,
            "operation": operation,
            "title": event.get("title"),
            "summary": event.get("summary"),
            "message_key": event.get("message_key"),
            "display_args": event.get("display_args") or {},
            "visibility": event.get("visibility") or "USER",
            "status": SPAN_STATUS_RUNNING,
            "started_at": occurred_at,
            "finished_at": None,
            "duration_ms": None,
            "error_type": None,
            "retry_count": 0,
            "attributes_summary": {},
        }
        spans[span_id] = span

    # 事件携带的展示字段永远以最新非空值为准
    for field in ("title", "summary", "message_key", "display_args"):
        if event.get(field):
            span[field] = event.get(field)
    if event.get("parent_span_id") and not span.get("parent_span_id"):
        span["parent_span_id"] = event.get("parent_span_id")
    if event.get("category"):
        span["category"] = category
    if event.get("operation"):
        span["operation"] = operation
    # 最严格可见性获胜，避免 ADMIN 起始事件被 USER 结束事件意外公开。
    if event.get("visibility") == "ADMIN":
        span["visibility"] = "ADMIN"

    if occurred_at is not None:
        if span.get("started_at") is None:
            span["started_at"] = occurred_at
        if suffix in _SPAN_TERMINAL_SUFFIX or suffix == "completed":
            span["finished_at"] = occurred_at
            if duration_ms is not None:
                span["duration_ms"] = int(duration_ms)
            elif span.get("started_at") is not None:
                span["duration_ms"] = int(max(0.0, (occurred_at - span["started_at"]).total_seconds()) * 1000)

    if suffix in _SPAN_TERMINAL_SUFFIX:
        span["status"] = _SPAN_TERMINAL_SUFFIX[suffix]
        if suffix in {"failed", "interrupted"}:
            # failed 携带异常类型；interrupted（如 worker_lost 收敛）同样保留原因
            span["error_type"] = attributes.get("error_type") or attributes.get("error.type") or span.get("error_type")
    elif suffix == "retrying":
        span["retry_count"] = int(span.get("retry_count") or 0) + 1
        span["status"] = SPAN_STATUS_RUNNING

    merged = dict(span.get("attributes_summary") or {})
    merged.update(attributes)
    span["attributes_summary"] = merged
    return spans


def replay_events(
    events: list[dict[str, Any]], header: dict[str, Any]
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """按序重放账本事件，得到完整 span 状态表与 run 汇总状态。"""
    summary = new_summary_state(header)
    spans: dict[str, dict[str, Any]] = {}
    for event in events:
        apply_event_to_summary(summary, event)
        apply_event_to_spans(spans, event)
    return spans, summary
