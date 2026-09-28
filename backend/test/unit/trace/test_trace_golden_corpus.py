"""trace 事件 golden corpus：每个核心 USER 事件一条样例，经真实构建管线零漂移。

语料 ``fixtures/agent_run_contract/trace_events.jsonl`` 与桌面端共享目录但独立于
wire 契约 manifest（trace 协议当前不在 X-Yuxi-Protocol-Version 谈判范围内）。
本测试保证：样例事件全部能通过 ``build_trace_event`` 构建（事件名/类别/操作合法），
attributes 键零剥离（schema 白名单与发射端不漂移），wire 视角可 JSON 序列化。
新增 USER 级事件时应同步补语料行——漏登记时下方的覆盖断言会失败。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from yuxi.trace.protocol import EVENT_ATTRIBUTE_SCHEMAS, build_trace_event, wire_event

pytestmark = [pytest.mark.unit]

CORPUS_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "agent_run_contract" / "trace_events.jsonl"

#: 必须出现在语料中的核心事件（新增 USER 级事件时同步补行）。
REQUIRED_EVENT_TYPES = {
    "run.execution.started",
    "run.plan.resolved",
    "run.artifact.materialized",
    "model.generation.started",
    "model.generation.completed",
    "model.generation.first_visible_token",
    "tool.execution.started",
    "tool.execution.completed",
    "mcp.execution.started",
    "mcp.execution.completed",
    "mcp.audit.recorded",
    "subagent.execution.started",
    "skill.runtime.resolved",
    "knowledge.search.started",
    "knowledge.search.completed",
    "knowledge.search.skipped",
    "answer.source_guard.completed",
    "answer.render.applied",
    "answer.followup_suggestions.completed",
    "run.execution.completed",
}


def _load_corpus() -> list[dict]:
    lines = [line.strip() for line in CORPUS_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


def test_corpus_covers_required_user_events():
    covered = {sample["event_type"] for sample in _load_corpus()}
    missing = sorted(REQUIRED_EVENT_TYPES - covered)
    assert not missing, f"golden corpus 缺少核心事件样例（补 trace_events.jsonl 行）：{missing}"
    unknown = sorted(covered - set(EVENT_ATTRIBUTE_SCHEMAS))
    assert not unknown, f"golden corpus 含未登记事件：{unknown}"


def test_corpus_events_build_without_attribute_drift():
    dropped: list[str] = []
    for sample in _load_corpus():
        attributes = sample.get("attributes") or {}
        event = build_trace_event(
            run_id="r1",
            thread_id="t1",
            category=sample["category"],
            operation=sample["operation"],
            event_type=sample["event_type"],
            attributes=attributes,
            visibility=sample.get("visibility", "USER"),
        )
        missing_keys = sorted(set(attributes) - set(event["attributes"]))
        if missing_keys:
            dropped.append(f"{sample['event_type']}: 被白名单剥离的键 {missing_keys}")
        wire = wire_event(event)
        assert json.loads(json.dumps(wire))["event_type"] == sample["event_type"]
    assert not dropped, f"样例属性被 schema 静默剥离（发射端与 schema 漂移）：\n{dropped}"
