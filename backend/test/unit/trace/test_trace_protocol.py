"""yuxi.run-trace.v1 协议契约单测：事件命名、allow-list、wire 视角。"""

from __future__ import annotations

import pytest
from yuxi.trace.protocol import (
    TRACE_CATEGORIES,
    TRACE_RETENTION_CLASSES,
    build_event_type,
    build_trace_event,
    sanitize_attributes,
    sanitize_resource_refs,
    wire_event,
)
from yuxi.storage.postgres.models_trace import TRACE_EVENT_RETENTION_CLASSES

pytestmark = [pytest.mark.unit]


def test_build_event_rejects_unknown_category():
    with pytest.raises(ValueError):
        build_trace_event(
            run_id="r1",
            thread_id="t1",
            category="NOT_A_CATEGORY",
            operation="exec",
            event_type="not_a_category.exec.started",
        )


def test_build_event_rejects_malformed_event_type():
    with pytest.raises(ValueError):
        build_trace_event(
            run_id="r1",
            thread_id="t1",
            category="TOOL",
            operation="execution",
            event_type="tool.execution",  # 只有两段
        )
    with pytest.raises(ValueError):
        build_trace_event(
            run_id="r1",
            thread_id="t1",
            category="TOOL",
            operation="execution",
            event_type="tool.execution.started.<run_id>",  # 动态 ID 禁止入事件名
        )


def test_build_event_accepts_all_registered_categories():
    registered = {
        "RUN": ("execution", "run.execution.started"),
        "MODEL": ("generation", "model.generation.started"),
        "TOOL": ("execution", "tool.execution.started"),
        "MCP": ("execution", "mcp.execution.started"),
        "SKILL": ("runtime", "skill.runtime.resolved"),
        "KNOWLEDGE": ("search", "knowledge.search.started"),
        "SUBAGENT": ("execution", "subagent.execution.started"),
        "VALIDATION": ("quality", "validation.quality.passed"),
        "SYSTEM": ("execution", "system.execution.started"),
    }
    assert set(registered) == set(TRACE_CATEGORIES)
    for category, (operation, event_type) in registered.items():
        event = build_trace_event(
            run_id="r1",
            thread_id="t1",
            category=category,
            operation=operation,
            event_type=event_type,
        )
        assert event["category"] == category
        assert event["schema_version"] == "yuxi.run-trace.v1"
        assert event["event_id"].startswith("evt_")


def test_sanitize_attributes_drops_non_scalar_shape_and_caps_length():
    sanitized = sanitize_attributes(
        {
            "retrieval.vector_hits": 28,
            "flag": True,
            "skills": ["a", "b"],
            "nested": {"deep": "dropped-shape-but-values-coerced"},
            "Bad Key!": 1,
            "x" * 100: "value",  # 键超长
            "long": "v" * 1000,
            "too_many": list(range(50)),
        }
    )
    assert sanitized["retrieval.vector_hits"] == 28
    assert sanitized["flag"] is True
    assert sanitized["skills"] == ["a", "b"]
    assert len(sanitized["long"]) == 512
    assert len(sanitized["too_many"]) == 20
    # 非法键与嵌套 dict 均丢弃，禁止通过字符串化复活内部秘密。
    assert "Bad Key!" not in sanitized
    assert "nested" not in sanitized


def test_sanitize_resource_refs_keeps_known_types_only():
    refs = sanitize_resource_refs(
        [
            {"type": "knowledge_retrieval", "id": "kr_123"},
            {"type": "agent_run", "id": 456},
            {"type": "unknown_type", "id": "x"},
            "garbage",
            {"type": "knowledge_retrieval"},  # 缺 id
        ]
    )
    assert refs == [
        {"type": "knowledge_retrieval", "id": "kr_123"},
        {"type": "agent_run", "id": "456"},
    ]


def test_wire_event_strips_tenant_identity_fields():
    event = build_trace_event(
        run_id="r1",
        thread_id="t1",
        category="KNOWLEDGE",
        operation="search",
        event_type="knowledge.search.completed",
        attributes={"claim_count": 3},
    )
    wire = wire_event(event)
    assert "tenant_id" not in wire
    assert "uid" not in wire
    assert "sensitivity" not in wire
    assert wire["sequence"] == event["sequence"]
    assert wire["event_type"] == "knowledge.search.completed"


def test_build_event_type_namespacing():
    assert build_event_type("TOOL", "execution", "started") == "tool.execution.started"
    assert build_event_type("KNOWLEDGE", "search", "completed") == "knowledge.search.completed"


def test_protocol_and_storage_retention_classes_stay_aligned():
    assert set(TRACE_EVENT_RETENTION_CLASSES) == set(TRACE_RETENTION_CLASSES)
