"""yuxi.run-trace.v1 产品级 Trace 语义协议。

设计原则（ADR-0002）：

- 事件是不可变事实；Span/UI 状态全部是事件的投影；
- 事件名 ``{category}.{operation}.{suffix}`` 必须低基数、命名空间化，
  动态值（id、名称、路径）一律进 attributes，禁止进入 event_type；
- attributes 走 schema allow-list：扁平标量键值，键名与值长度受限，
  未知形状在构建期拒绝或降级，防止自由 JSON 无限膨胀；
- 敏感内容在进入本协议之前 DROP（见 redaction.py），本层只做结构校验。
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

TRACE_SCHEMA_VERSION = "yuxi.run-trace.v1"

# v1 类别集合：新增类别先在此登记，避免事件名逃逸出受控命名空间
TRACE_CATEGORIES = frozenset(
    {
        "RUN",
        "MODEL",
        "TOOL",
        "MCP",
        "SKILL",
        "KNOWLEDGE",
        "SUBAGENT",
        "VALIDATION",
        "SYSTEM",
    }
)

VISIBILITY_USER = "USER"
VISIBILITY_ADMIN = "ADMIN"
TRACE_VISIBILITIES = frozenset({VISIBILITY_USER, VISIBILITY_ADMIN})

SENSITIVITY_PUBLIC = "PUBLIC"
SENSITIVITY_INTERNAL = "INTERNAL"
TRACE_SENSITIVITIES = frozenset({SENSITIVITY_PUBLIC, SENSITIVITY_INTERNAL})

RETENTION_STANDARD = "STANDARD"
RETENTION_EXTENDED = "EXTENDED"
RETENTION_LEGAL_HOLD = "LEGAL_HOLD"
TRACE_RETENTION_CLASSES = frozenset({RETENTION_STANDARD, RETENTION_EXTENDED, RETENTION_LEGAL_HOLD})

# 幂等状态事件：同一 run 只记录首次（重复触发来自每次模型请求的中间件重解析，
# 属于同一事实的重复观察，进账本只会制造噪声）。
TRACE_ONCE_PER_RUN_EVENT_TYPES = frozenset(
    {
        "skill.runtime.resolved",
    }
)

# 事件名后缀生命周期约定：started/retrying/completed/failed/interrupted
_EVENT_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){2,}$")
_ATTRIBUTE_KEY_PATTERN = re.compile(r"^[a-z0-9_.]+$")
_TRACE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID_PATTERN = re.compile(r"^[0-9a-f]{16}$")

ATTRIBUTE_VALUE_MAX_CHARS = 512
TITLE_MAX_CHARS = 256
SUMMARY_MAX_CHARS = 2000
ATTRIBUTE_LIST_MAX_ITEMS = 20

# 已知 resource_ref 类型（详情永远由专业审计表承担，trace 只存引用与摘要）
RESOURCE_REF_TYPES = frozenset(
    {
        "knowledge_retrieval",
        "knowledge_scope",
        "mcp_call_audit",
        "agent_run",
        "message",
        "artifact",
    }
)

# v1 是封闭协议。新增事件或字段必须先在这里登记并补协议测试，避免业务代码
# 把动态名称、自由 JSON 或用户输入悄悄扩散到所有存储与客户端。
_COMMON_ATTRIBUTES = frozenset({"error_type", "error_code"})
EVENT_ATTRIBUTE_SCHEMAS: dict[str, frozenset[str]] = {
    "run.execution.started": frozenset({"agent_slug", "run_type", "request_id", "trace_policy"}),
    "run.execution.completed": frozenset({"message_id"}),
    "run.execution.failed": _COMMON_ATTRIBUTES | {"reconciled_at"},
    "run.execution.cancelled": _COMMON_ATTRIBUTES,
    "run.execution.interrupted": _COMMON_ATTRIBUTES,
    "model.generation.started": frozenset({"model_spec", "credential_source"}),
    "model.generation.first_visible_token": frozenset({"model_spec"}),
    # 仅用于读取/兼容已写入的 v1 事件；新埋点使用 first_visible_token。
    "model.generation.first_token": frozenset({"model_spec"}),
    "model.generation.completed": frozenset(
        {"model_spec", "credential_source", "input_tokens", "output_tokens", "total_tokens"}
    ),
    "model.generation.failed": _COMMON_ATTRIBUTES,
    "model.generation.retrying": _COMMON_ATTRIBUTES,
    "model.generation.interrupted": _COMMON_ATTRIBUTES,
    "tool.execution.started": frozenset({"tool", "args_digest"}),
    "tool.execution.completed": frozenset({"tool", "result_digest"}),
    "tool.execution.failed": _COMMON_ATTRIBUTES,
    "tool.execution.retrying": _COMMON_ATTRIBUTES,
    "tool.execution.interrupted": _COMMON_ATTRIBUTES,
    "mcp.execution.started": frozenset({"tool", "mcp_server", "mcp_tool", "args_digest"}),
    "mcp.execution.completed": frozenset({"tool", "mcp_server", "mcp_tool", "mcp_audit_id"}),
    "mcp.execution.failed": _COMMON_ATTRIBUTES | {"mcp_audit_id"},
    "mcp.execution.retrying": _COMMON_ATTRIBUTES,
    "mcp.execution.interrupted": _COMMON_ATTRIBUTES,
    "mcp.audit.recorded": frozenset({"mcp_server", "mcp_tool", "mcp_audit_id", "audit_status"}),
    "subagent.execution.started": frozenset({"tool", "args_digest", "agent_slug", "child_run_id"}),
    "subagent.execution.completed": frozenset({"agent_slug", "child_run_id"}),
    "subagent.execution.failed": _COMMON_ATTRIBUTES | {"agent_slug", "child_run_id"},
    "subagent.execution.retrying": _COMMON_ATTRIBUTES,
    "subagent.execution.interrupted": _COMMON_ATTRIBUTES,
    "skill.runtime.resolved": frozenset({"prompt_skills", "skill_count"}),
    "skill.runtime.activated": frozenset({"skill_slug"}),
    "knowledge.search.started": frozenset({"knowledge_scope_version", "intent"}),
    "knowledge.search.completed": frozenset(
        {
            "claim_count",
            "evidence_count",
            "wiki_navigation_hit_count",
            "intent",
            "contract_status",
            "completeness_status",
            "warning_count",
            "knowledge_scope_version",
        }
    ),
    "knowledge.search.failed": _COMMON_ATTRIBUTES
    | {
        "claim_count",
        "evidence_count",
        "wiki_navigation_hit_count",
        "intent",
        "contract_status",
        "completeness_status",
        "warning_count",
        "knowledge_scope_version",
    },
    "knowledge.search.skipped": frozenset({"intent", "contract_status", "knowledge_scope_version"}),
    "knowledge.search.interrupted": _COMMON_ATTRIBUTES,
    # 图卡资产投影（ADR-0004 P0-4 SLA 采集点）：reason 为 9 种抑制原因闭合枚举之一
    "knowledge.figure_projection.attached": frozenset({"reason", "figure_count", "locator_kind"}),
    "knowledge.figure_projection.suppressed": frozenset({"reason", "figure_count", "locator_kind"}),
    # 文献作用域解析（"哪篇文献"）：SLA = 解析唯一率 / 跨文献歧义率
    "knowledge.document_scope.resolved": frozenset({"channel", "candidate_count", "file_count"}),
    "knowledge.document_scope.ambiguous": frozenset({"channel", "candidate_count", "file_count"}),
    "knowledge.document_scope.unresolved": frozenset({"channel", "candidate_count", "file_count"}),
    "validation.quality.passed": frozenset({"validator", "result_digest"}),
    "validation.quality.failed": _COMMON_ATTRIBUTES | {"validator", "result_digest"},
    "system.execution.started": frozenset(),
    "system.execution.completed": frozenset(),
    "system.execution.failed": _COMMON_ATTRIBUTES,
}


def new_event_id() -> str:
    return f"evt_{uuid.uuid4().hex}"


def _clean_scalar(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value
    if isinstance(value, str):
        return value[:ATTRIBUTE_VALUE_MAX_CHARS]
    # 嵌套结构和任意对象不进入 wire；尤其禁止通过 str(dict) 复活内部秘密。
    return None


def sanitize_attributes(attributes: dict[str, Any] | None) -> dict[str, Any]:
    """按 allow-list 归一化 attributes：扁平键值 + 少量短标量列表。"""
    if not isinstance(attributes, dict):
        return {}
    result: dict[str, Any] = {}
    for key, value in attributes.items():
        if not isinstance(key, str) or not _ATTRIBUTE_KEY_PATTERN.match(key) or len(key) > 64:
            continue
        if isinstance(value, list | tuple):
            if len(value) > ATTRIBUTE_LIST_MAX_ITEMS:
                value = list(value)[:ATTRIBUTE_LIST_MAX_ITEMS]
            result[key] = [cleaned for item in value if (cleaned := _clean_scalar(item)) is not None]
        else:
            cleaned = _clean_scalar(value)
            if cleaned is not None or value is None:
                result[key] = cleaned
    return result


def sanitize_resource_refs(refs: Any) -> list[dict[str, str]]:
    if not isinstance(refs, list):
        return []
    result: list[dict[str, str]] = []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        ref_type = ref.get("type")
        ref_id = ref.get("id")
        if ref_type not in RESOURCE_REF_TYPES or ref_id is None:
            continue
        result.append({"type": str(ref_type), "id": str(ref_id)[:128]})
    return result


def build_event_type(category: str, operation: str, suffix: str) -> str:
    return f"{category.lower()}.{operation}.{suffix}"


def build_trace_event(
    *,
    run_id: str,
    thread_id: str | None,
    category: str,
    operation: str,
    event_type: str | None = None,
    event_id: str | None = None,
    trace_id: str | None = None,
    sequence: int = 0,
    span_id: str | None = None,
    parent_span_id: str | None = None,
    occurred_at: str | None = None,
    duration_ms: int | None = None,
    title: str | None = None,
    summary: str | None = None,
    message_key: str | None = None,
    display_args: dict[str, Any] | None = None,
    attributes: dict[str, Any] | None = None,
    resource_refs: list[dict[str, Any]] | None = None,
    visibility: str = VISIBILITY_USER,
    sensitivity: str = SENSITIVITY_INTERNAL,
    retention_class: str = RETENTION_STANDARD,
) -> dict[str, Any]:
    """构建一条协议事件；类别/命名不合法时抛 ValueError（fail fast）。"""
    if category not in TRACE_CATEGORIES:
        raise ValueError(f"unknown trace category: {category}")
    if visibility not in TRACE_VISIBILITIES:
        raise ValueError(f"unknown trace visibility: {visibility}")
    if sensitivity not in TRACE_SENSITIVITIES:
        raise ValueError(f"unknown trace sensitivity: {sensitivity}")
    if retention_class not in TRACE_RETENTION_CLASSES:
        raise ValueError(f"unknown trace retention class: {retention_class}")

    final_event_type = event_type or build_event_type(category, operation, "started")
    if not _EVENT_TYPE_PATTERN.match(final_event_type):
        raise ValueError(f"invalid trace event_type: {final_event_type}")
    allowed_attributes = EVENT_ATTRIBUTE_SCHEMAS.get(final_event_type)
    if allowed_attributes is None:
        raise ValueError(f"unregistered trace event_type: {final_event_type}")
    expected_category, expected_operation, _ = final_event_type.split(".", 2)
    if expected_category != category.lower() or expected_operation != operation:
        raise ValueError(f"trace event category/operation mismatch: {final_event_type}")
    if trace_id is not None and not _TRACE_ID_PATTERN.fullmatch(trace_id):
        raise ValueError("trace_id must be 32 lowercase hexadecimal characters")
    if span_id is not None and not _SPAN_ID_PATTERN.fullmatch(span_id):
        raise ValueError("span_id must be 16 lowercase hexadecimal characters")
    if parent_span_id is not None and not _SPAN_ID_PATTERN.fullmatch(parent_span_id):
        raise ValueError("parent_span_id must be 16 lowercase hexadecimal characters")

    safe_attributes = sanitize_attributes(attributes)
    safe_attributes = {key: value for key, value in safe_attributes.items() if key in allowed_attributes}

    event: dict[str, Any] = {
        "schema_version": TRACE_SCHEMA_VERSION,
        "event_id": event_id or new_event_id(),
        "trace_id": trace_id,
        "sequence": int(sequence),
        "run_id": run_id,
        "thread_id": thread_id,
        "category": category,
        "operation": operation,
        "event_type": final_event_type,
        "span_id": span_id,
        "parent_span_id": parent_span_id,
        "occurred_at": occurred_at,
        "duration_ms": int(duration_ms) if duration_ms is not None else None,
        "title": (title or "")[:TITLE_MAX_CHARS] or None,
        "summary": (summary or "")[:SUMMARY_MAX_CHARS] or None,
        "message_key": (message_key or final_event_type)[:128],
        "display_args": sanitize_attributes(display_args),
        "attributes": safe_attributes,
        "resource_refs": sanitize_resource_refs(resource_refs),
        "visibility": visibility,
        "sensitivity": sensitivity,
        "retention_class": retention_class,
    }
    return event


def wire_event(event: dict[str, Any]) -> dict[str, Any]:
    """账本事件 → 传输/客户端视角（剥离租户与用户身份字段，时间统一 ISO）。"""
    occurred_at = event.get("occurred_at")
    if isinstance(occurred_at, datetime):
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=UTC)
        occurred_at = occurred_at.isoformat()
    return {
        key: (occurred_at if key == "occurred_at" else event.get(key))
        for key in (
            "schema_version",
            "event_id",
            "trace_id",
            "sequence",
            "run_id",
            "thread_id",
            "category",
            "operation",
            "event_type",
            "span_id",
            "parent_span_id",
            "occurred_at",
            "duration_ms",
            "title",
            "summary",
            "message_key",
            "display_args",
            "attributes",
            "resource_refs",
            "visibility",
        )
    }
