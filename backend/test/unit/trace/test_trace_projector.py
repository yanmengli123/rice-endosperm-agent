"""Trace 投影纯函数单测：span 状态机、summary 计数与 replay 一致性。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from yuxi.trace import projector
from yuxi.trace.protocol import build_trace_event

pytestmark = [pytest.mark.unit]

TOOL_SPAN = "1111111111111111"
MODEL_SPAN = "2222222222222222"
KNOWLEDGE_SPAN = "3333333333333333"
RUN_SPAN = "4444444444444444"


def _event(**overrides):
    base = dict(
        run_id="r1",
        thread_id="t1",
        category="TOOL",
        operation="execution",
        event_type="tool.execution.started",
        sequence=1,
        span_id=TOOL_SPAN,
        occurred_at=datetime(2026, 9, 8, 6, 42, 0, tzinfo=UTC),
    )
    base.update(overrides)
    return build_trace_event(**base)


HEADER = {"run_id": "r1", "thread_id": "t1", "tenant_id": 1, "agent_slug": "chatbot"}


def test_span_lifecycle_started_completed():
    spans: dict = {}
    projector.apply_event_to_spans(spans, _event(title="Gene Lookup"))
    assert spans[TOOL_SPAN]["status"] == "RUNNING"
    assert spans[TOOL_SPAN]["title"] == "Gene Lookup"

    projector.apply_event_to_spans(
        spans,
        _event(
            event_type="tool.execution.completed",
            occurred_at=datetime(2026, 9, 8, 6, 42, 3, tzinfo=UTC),
            sequence=3,
            duration_ms=3000,
            summary="命中 18 条",
        ),
    )
    assert spans[TOOL_SPAN]["status"] == "COMPLETED"
    assert spans[TOOL_SPAN]["duration_ms"] == 3000
    assert spans[TOOL_SPAN]["summary"] == "命中 18 条"


def test_span_failed_carries_error_type():
    spans: dict = {}
    projector.apply_event_to_spans(spans, _event())
    projector.apply_event_to_spans(
        spans,
        _event(
            event_type="tool.execution.failed",
            sequence=2,
            occurred_at=datetime(2026, 9, 8, 6, 42, 2, tzinfo=UTC),
            duration_ms=2000,
            attributes={"error_type": "TimeoutError"},
        ),
    )
    assert spans[TOOL_SPAN]["status"] == "FAILED"
    assert spans[TOOL_SPAN]["error_type"] == "TimeoutError"


def test_events_without_span_id_are_summary_only():
    spans: dict = {}
    summary = projector.new_summary_state(HEADER)
    event = _event(span_id=None, category="MODEL", operation="generation", event_type="model.generation.first_token")
    projector.apply_event_to_spans(spans, event)
    projector.apply_event_to_summary(summary, event)
    assert spans == {}
    assert summary["first_token_at"] is not None


def test_summary_counts_and_tokens():
    summary = projector.new_summary_state(HEADER)
    events = [
        _event(category="RUN", operation="execution", event_type="run.execution.started", span_id=RUN_SPAN),
        _event(
            category="MODEL",
            operation="generation",
            event_type="model.generation.started",
            span_id=MODEL_SPAN,
            sequence=2,
        ),
        _event(
            category="MODEL",
            operation="generation",
            event_type="model.generation.first_token",
            span_id=None,
            sequence=3,
            occurred_at=datetime(2026, 9, 8, 6, 42, 0, tzinfo=UTC) + timedelta(milliseconds=841),
            attributes={"model_spec": "gpt-x"},
        ),
        _event(
            category="MODEL",
            operation="generation",
            event_type="model.generation.completed",
            span_id=MODEL_SPAN,
            sequence=4,
            attributes={"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500, "model_spec": "gpt-x"},
        ),
        _event(category="TOOL", operation="execution", event_type="tool.execution.started", sequence=5),
        _event(
            category="MCP",
            operation="execution",
            event_type="mcp.execution.started",
            sequence=6,
        ),
        _event(
            category="KNOWLEDGE",
            operation="search",
            event_type="knowledge.search.started",
            span_id=KNOWLEDGE_SPAN,
            sequence=7,
        ),
        _event(
            category="SKILL",
            operation="runtime",
            event_type="skill.runtime.resolved",
            sequence=8,
            attributes={"prompt_skills": ["rice-gene-analysis", "lit-review"], "skill_count": 2},
        ),
        _event(
            category="RUN",
            operation="execution",
            event_type="run.execution.completed",
            span_id=RUN_SPAN,
            sequence=9,
        ),
    ]
    for event in events:
        projector.apply_event_to_summary(summary, event)

    assert summary["status"] == "completed"
    assert summary["model_calls"] == 1
    assert summary["tool_calls"] == 1
    assert summary["mcp_calls"] == 1
    assert summary["knowledge_calls"] == 1
    assert summary["skill_count"] == 2
    assert summary["input_tokens"] is None
    assert summary["total_tokens"] is None
    assert summary["ttft_ms"] == 841
    assert summary["trace_event_count"] == 9
    assert summary["last_sequence"] == 9
    assert summary["attributes"]["model_spec"] == "gpt-x"


def test_replay_is_deterministic_and_idempotent():
    events = [
        _event(category="RUN", operation="execution", event_type="run.execution.started", span_id=RUN_SPAN),
        _event(
            category="MODEL",
            operation="generation",
            event_type="model.generation.started",
            span_id=MODEL_SPAN,
            sequence=2,
        ),
        _event(
            category="MODEL",
            operation="generation",
            event_type="model.generation.completed",
            span_id=MODEL_SPAN,
            sequence=3,
            duration_ms=3800,
            attributes={"total_tokens": 7231},
        ),
        _event(
            category="RUN",
            operation="execution",
            event_type="run.execution.completed",
            span_id=RUN_SPAN,
            sequence=4,
        ),
    ]
    spans_a, summary_a = projector.replay_events(events, HEADER)
    spans_b, summary_b = projector.replay_events(list(events), HEADER)
    assert spans_a == spans_b
    assert summary_a == summary_b
    assert spans_a[MODEL_SPAN]["status"] == "COMPLETED"
    assert summary_a["status"] == "completed"


def test_running_spans_left_open_after_interrupted_run():
    events = [
        _event(category="RUN", operation="execution", event_type="run.execution.started", span_id=RUN_SPAN),
        _event(
            category="MODEL",
            operation="generation",
            event_type="model.generation.started",
            span_id=MODEL_SPAN,
            sequence=2,
        ),
    ]
    spans, summary = projector.replay_events(events, HEADER)
    assert spans[MODEL_SPAN]["status"] == "RUNNING"  # 等 reconciler 闭合
    assert summary["status"] == "running"


def test_summary_facets_project_stage_bar_inputs():
    """阶段条 facets：plan/检索/守卫/投影事件进 summary.attributes（快照起步真源）。"""
    summary = projector.new_summary_state(HEADER)
    events = [
        _event(
            category="RUN",
            operation="plan",
            event_type="run.plan.resolved",
            span_id=None,
            attributes={
                "source_policy": "MCP_ONLY",
                "evidence_level": "E1_DATA_PROVENANCE",
                "retrieval_required": True,
                "requires_mcp": True,
                "satisfiable": False,
                "required_server": "bio-mcp",
                "required_server_missing": "bio-mcp",
                "error_code": "MCP_SERVER_NOT_CONFIGURED",
            },
        ),
        _event(
            category="KNOWLEDGE",
            operation="search",
            event_type="knowledge.search.completed",
            span_id=KNOWLEDGE_SPAN,
            sequence=2,
            attributes={
                "claim_count": 2,
                "evidence_count": 5,
                "verbatim_hit_count": 1,
                "wiki_navigation_hit_count": 0,
                "intent": "ENTITY_LOOKUP",
                "contract_status": "COMPLETED",
                "completeness_status": "PASS",
                "warning_count": 0,
                "knowledge_scope_version": 3,
            },
        ),
        _event(
            category="ANSWER",
            operation="source_guard",
            event_type="answer.source_guard.completed",
            span_id=None,
            sequence=3,
            attributes={"guard_status": "REJECTED", "evidence_level": "E1_DATA_PROVENANCE"},
        ),
        _event(
            category="ANSWER",
            operation="render",
            event_type="answer.render.applied",
            span_id=None,
            sequence=4,
            attributes={
                "renderer_version": "profile-projection/1.0",
                "boundary": "source_gate",
                "eligible": True,
                "applied": False,
                "fallback_reason": "not_eligible",
            },
        ),
    ]
    for event in events:
        projector.apply_event_to_summary(summary, event)

    facets = summary["attributes"]
    assert facets["plan"]["source_policy"] == "MCP_ONLY"
    assert facets["plan"]["satisfiable"] is False
    assert facets["plan"]["required_server_missing"] == "bio-mcp"
    assert facets["knowledge"]["status"] == "COMPLETED"
    assert facets["knowledge"]["evidence_count"] == 5
    assert facets["knowledge"]["verbatim_hit_count"] == 1
    assert facets["guard"] == {"guard_status": "REJECTED", "evidence_level": "E1_DATA_PROVENANCE"}
    assert facets["render"]["fallback_reason"] == "not_eligible"
    assert facets["artifact_count"] == 0


def test_knowledge_skipped_and_interrupted_facets():
    summary = projector.new_summary_state(HEADER)
    projector.apply_event_to_summary(
        summary,
        _event(
            category="KNOWLEDGE",
            operation="search",
            event_type="knowledge.search.skipped",
            span_id=None,
            attributes={"intent": "SOCIAL", "contract_status": "SKIPPED"},
        ),
    )
    assert summary["attributes"]["knowledge"]["status"] == "SKIPPED"

    projector.apply_event_to_summary(
        summary,
        _event(
            category="KNOWLEDGE",
            operation="search",
            event_type="knowledge.search.interrupted",
            span_id=KNOWLEDGE_SPAN,
            sequence=2,
            attributes={"error_type": "worker_lost"},
        ),
    )
    assert summary["attributes"]["knowledge"]["status"] == "INTERRUPTED"


def test_artifact_facet_bounded_and_counted():
    summary = projector.new_summary_state(HEADER)
    for index in range(25):
        projector.apply_event_to_summary(
            summary,
            _event(
                category="RUN",
                operation="artifact",
                event_type="run.artifact.materialized",
                span_id=None,
                sequence=index + 1,
                attributes={
                    "origin_source": "mcp",
                    "mcp_server": "ricekb",
                    "mcp_tool": "ricekb_gene_profile",
                    "size_bytes": 1024 + index,
                    "media_type": "application/json",
                    "name": f"artifact-{index}.json",
                    "sha256": f"{index:064x}",
                },
            ),
        )
    facets = summary["attributes"]
    assert facets["artifact_count"] == 25
    assert len(facets["artifacts"]) == 20
    assert facets["artifacts"][0]["name"] == "artifact-0.json"
    assert facets["artifacts"][-1]["name"] == "artifact-19.json"


def test_facets_survive_replay():
    """replay（reconciler 重建投影）必须复现同一份 facets，快照路径不脱节。"""
    events = [
        _event(
            category="RUN",
            operation="plan",
            event_type="run.plan.resolved",
            span_id=None,
            attributes={"source_policy": "AUTO", "evidence_level": "E2_WIKI_CONTEXT", "satisfiable": True},
        ),
        _event(
            category="RUN",
            operation="artifact",
            event_type="run.artifact.materialized",
            span_id=None,
            sequence=2,
            attributes={"name": "x.json", "origin_source": "model", "size_bytes": 3},
        ),
    ]
    _, summary = projector.replay_events(events, HEADER)
    assert summary["attributes"]["plan"]["source_policy"] == "AUTO"
    assert summary["attributes"]["artifact_count"] == 1
    assert summary["attributes"]["artifacts"] == [{"name": "x.json", "origin_source": "model", "size_bytes": 3}]
