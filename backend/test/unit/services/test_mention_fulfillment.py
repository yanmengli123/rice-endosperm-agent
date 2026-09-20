"""mention.v2 运行后验：REQUIRED / PREFERRED 未兑现分级（缺口 B 语义）。

REQUIRED 未兑现 → amendment + manifest.status 降级 DEGRADED（run 级可观测）；
PREFERRED 未兑现 → 仅审计记账，不打扰用户；兑现 → MENTION_FULFILLED；
非 COMPLETED 终态不被覆写。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from yuxi.knowledge.planning.turn_execution_plan import RunSourceManifest
from yuxi.services.chat_service import (
    _finalize_mention_skills,
    _frozen_mention_resolution,
    _pinned_mention_items,
    _record_mention_fulfillment,
    _settle_source_manifest_status,
)

pytestmark = [pytest.mark.unit]


def _manifest(status: str = "COMPLETED") -> RunSourceManifest:
    return RunSourceManifest(
        plan_id="tp_test",
        source_policy="AUTO",
        document_evidence_requested=False,
        mcp_requested=False,
        status=status,
    )


def test_pinned_items_require_resolved_status_and_default_required():
    resolution = {
        "mentions": [
            {"type": "mcp", "resource_id": "ricekb", "status": "RESOLVED"},
            {"type": "mcp", "resource_id": "dead", "status": "MENTION_RESOURCE_UNAVAILABLE"},
            {"type": "skill", "resource_id": "writing", "status": "RESOLVED", "strength": "PREFERRED"},
            {"type": "subagent", "resource_id": "rev", "status": "RESOLVED", "strength": "required"},
        ]
    }
    assert _pinned_mention_items(resolution, "mcp") == [("ricekb", "REQUIRED")]
    assert _pinned_mention_items(resolution, "skill") == [("writing", "PREFERRED")]
    assert _pinned_mention_items(resolution, "subagent") == [("rev", "REQUIRED")]
    assert _pinned_mention_items(None, "mcp") == []
    assert _pinned_mention_items({"mentions": "not-a-list"}, "mcp") == []


def test_required_unfulfilled_downgrades_status_and_records_amendment():
    manifest = _manifest()
    _record_mention_fulfillment(
        manifest,
        mention_type="mcp",
        resource_id="ricekb",
        strength="REQUIRED",
        fulfilled=False,
        reason_code="MENTION_MCP_NOT_INVOKED",
    )
    assert manifest.status == "DEGRADED"
    assert manifest.amendments[-1]["type"] == "MENTION_UNFULFILLED"
    assert manifest.amendments[-1]["strength"] == "REQUIRED"
    assert manifest.amendments[-1]["reason_code"] == "MENTION_MCP_NOT_INVOKED"


def test_preferred_unfulfilled_records_without_downgrade():
    manifest = _manifest()
    _record_mention_fulfillment(
        manifest,
        mention_type="subagent",
        resource_id="rev",
        strength="PREFERRED",
        fulfilled=False,
        reason_code="MENTION_SUBAGENT_NOT_DELEGATED",
    )
    assert manifest.status == "COMPLETED"
    assert manifest.amendments[-1]["type"] == "MENTION_UNFULFILLED"


def test_fulfilled_keeps_status_and_terminal_status_not_overwritten():
    manifest = _manifest()
    _record_mention_fulfillment(
        manifest, mention_type="mcp", resource_id="ricekb", strength="REQUIRED", fulfilled=True, reason_code=None
    )
    assert manifest.status == "COMPLETED"
    assert manifest.amendments[-1]["type"] == "MENTION_FULFILLED"

    failed = _manifest(status="FAILED")
    _record_mention_fulfillment(
        failed, mention_type="mcp", resource_id="ricekb", strength="REQUIRED", fulfilled=False, reason_code="x"
    )
    assert failed.status == "FAILED"


# ---- P0-1：子 run 模型输入回落到委派任务正文 ----


def test_child_model_query_falls_back_to_delegated_description():
    """继承载荷无 clean_question 时，chat_service 的 model_query 表达式回落到 query。"""
    from yuxi.knowledge.planning.mention_protocol import scope_only_mention_resolution

    parent = {
        "protocol": "mention-protocol.v2",
        "status": "RESOLVED",
        "document_ids": ["file_a"],
        "knowledge_ids": [],
        "pages": [],
        "figure_labels": [],
        "table_labels": [],
        "file_paths": [],
        "mcp_slugs": ["ricekb"],
        "skill_slugs": [],
        "subagent_slugs": ["literature-reviewer"],
        "tool_names": [],
        "mentions": [
            {"type": "document", "resource_id": "file_a", "status": "RESOLVED"},
            {"type": "mcp", "resource_id": "ricekb", "status": "RESOLVED"},
        ],
        "clean_question": "帮我查 OsNAC6 在干旱下的表达",
        "query_raw": "@doc:file_a @mcp:ricekb 帮我查 OsNAC6 在干旱下的表达",
        "errors": [],
    }
    child = scope_only_mention_resolution(parent)
    assert child is not None
    delegated_description = "请检索文献并总结 OsNAC6 的干旱响应证据"
    meta = {"mention_resolution": child}
    # chat_service.stream_agent_chat 的同一表达式：clean_question 为空 → 委派正文
    model_query = str(_frozen_mention_resolution(meta).get("clean_question") or "").strip() or delegated_description
    assert model_query == delegated_description


# ---- P0-2：requires_mcp 成功路径不得覆写 mention 的 DEGRADED ----


def test_settle_status_preserves_mention_degraded_on_mcp_success():
    manifest = _manifest(status="DEGRADED")
    assert _settle_source_manifest_status(SimpleNamespace(requires_mcp=True), manifest, True) is False
    assert manifest.status == "DEGRADED"


def test_settle_status_completes_when_not_degraded():
    manifest = _manifest(status="PLANNED")
    assert _settle_source_manifest_status(SimpleNamespace(requires_mcp=True), manifest, True) is False
    assert manifest.status == "COMPLETED"


def test_settle_status_signals_failure_answer_without_touching_status():
    manifest = _manifest(status="SOURCE_UNAVAILABLE")
    assert _settle_source_manifest_status(SimpleNamespace(requires_mcp=True), manifest, False) is True
    assert manifest.status == "SOURCE_UNAVAILABLE"


def test_settle_status_ignores_non_mcp_turns():
    manifest = _manifest(status="PLANNED")
    assert _settle_source_manifest_status(SimpleNamespace(requires_mcp=False), manifest, True) is False
    assert manifest.status == "PLANNED"


# ---- skill 兑现回写 ----


def test_mention_skills_fulfilled_when_readable_or_unknown():
    resolution = {"mentions": [{"type": "skill", "resource_id": "alpha", "status": "RESOLVED"}]}
    manifest = _manifest()
    _finalize_mention_skills(manifest, mention_resolution=resolution, readable_skills=["alpha"])
    assert manifest.amendments[-1]["type"] == "MENTION_FULFILLED"
    assert manifest.amendments[-1]["reason_code"] == "SKILL_PREACTIVATED"
    assert manifest.status == "COMPLETED"

    unknown = _manifest()
    _finalize_mention_skills(unknown, mention_resolution=resolution, readable_skills=None)
    assert unknown.amendments[-1]["type"] == "MENTION_FULFILLED"


def test_mention_skills_unfulfilled_when_revoked_midrun():
    resolution = {"mentions": [{"type": "skill", "resource_id": "ghost", "status": "RESOLVED"}]}
    manifest = _manifest()
    _finalize_mention_skills(manifest, mention_resolution=resolution, readable_skills=["alpha"])
    assert manifest.amendments[-1]["type"] == "MENTION_UNFULFILLED"
    assert manifest.amendments[-1]["reason_code"] == "SKILL_REVOKED_MIDRUN"
    assert manifest.status == "DEGRADED"
