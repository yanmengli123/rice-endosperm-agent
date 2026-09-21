"""有界事实核验修复回路与确定性降级渲染的行为契约。"""

from __future__ import annotations

import pytest

from yuxi.services.chat_service import (
    _FACT_REPAIR_MAX_ATTEMPTS,
    _finalize_guarded_source_text,
    _repair_source_fact_grounding,
)

_DRAFT = "数据模式：SOURCE-ONLY\n区间长度是 3 bp。"
_GOOD_DRAFT = "数据模式：SOURCE-ONLY\n区间长度是 98 bp。[MCP-F:42:f_1234567890abcdef]"

_SOURCE_USES = [
    {
        "source_use_id": "mcp:42",
        "provider_id": "ricekb",
        "operation": "ricekb_entity",
        "status": "SUCCESS",
        "adopted": True,
        "provenance": {
            "mcp_call_audit_id": 42,
            "fact_manifest": {
                "facts": [
                    {"id": "f_1234567890abcdef", "path": "/data/length", "numeric_value": 98},
                    {"id": "f_1234567890fffff", "path": "/gene/symbol", "string_value": "Wx"},
                ]
            },
        },
    }
]


@pytest.mark.asyncio
async def test_first_repair_round_success_publishes_repaired_text(monkeypatch):
    async def fake_repair(draft, validation, source_uses):
        return _GOOD_DRAFT

    monkeypatch.setattr("yuxi.services.chat_service._repair_source_fact_grounding", fake_repair)
    guarded, validation = await _finalize_guarded_source_text(
        _DRAFT, evidence_level="E1_DATA_PROVENANCE", source_uses=_SOURCE_USES
    )

    assert "98 bp" in guarded
    assert "3 bp" not in guarded
    assert validation["fact_grounding"]["passed"] is True
    assert validation["fact_repair_attempts"] == [{"attempt": 1, "repaired": True}]


@pytest.mark.asyncio
async def test_exhausted_repairs_fall_back_to_degraded_fact_sheet(monkeypatch):
    async def bad_repair(draft, validation, source_uses):
        return _DRAFT  # 修复失败：仍旧未通过

    monkeypatch.setattr("yuxi.services.chat_service._repair_source_fact_grounding", bad_repair)
    guarded, validation = await _finalize_guarded_source_text(
        _DRAFT, evidence_level="E1_DATA_PROVENANCE", source_uses=_SOURCE_USES
    )

    assert guarded.startswith("数据模式：SOURCE-ONLY")
    assert "降级渲染" in guarded
    assert "[MCP-F:42:f_1234567890abcdef]" in guarded
    assert validation["degraded_render"] is True
    assert validation["status"] == "DEGRADED"
    assert len(validation["fact_repair_attempts"]) == _FACT_REPAIR_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_repair_channel_unavailable_keeps_fail_closed_text(monkeypatch):
    async def no_repair(draft, validation, source_uses):
        return None

    monkeypatch.setattr("yuxi.services.chat_service._repair_source_fact_grounding", no_repair)
    factless_uses = [
        {
            "source_use_id": "mcp:7",
            "provider_id": "custom",
            "operation": "anything",
            "status": "SUCCESS",
            "adopted": True,
            "provenance": {"mcp_call_audit_id": 7},
        }
    ]
    guarded, validation = await _finalize_guarded_source_text(
        _DRAFT, evidence_level="E1_DATA_PROVENANCE", source_uses=factless_uses
    )

    assert "3 bp" not in guarded
    assert "未通过 MCP 事实级核验" in guarded
    assert validation["fact_repair_attempts"] == [{"attempt": 1, "repaired": False}]


@pytest.mark.asyncio
async def test_repair_prompt_rejects_model_answers_without_source_header(monkeypatch):
    class StubResponse:
        content = "这是修复后的回答（缺 SOURCE-ONLY 头）"

    class StubModel:
        async def ainvoke(self, messages):
            return StubResponse()

    # chat_service 在函数内延迟 import load_chat_model，patch 模块属性即可生效
    monkeypatch.setattr("yuxi.agents.models.load_chat_model", lambda *args, **kwargs: StubModel())
    draft = "数据模式：SOURCE-ONLY\n区间长度是 3 bp。"
    validation = {
        "fact_grounding": {
            "required": True,
            "ungrounded_lines": [],
            "unsupported_numbers": [{"line": 2, "value": "3"}],
            "invalid_markers": [],
        }
    }
    assert await _repair_source_fact_grounding(draft, validation, _SOURCE_USES) is None
    assert _FACT_REPAIR_MAX_ATTEMPTS == 2
