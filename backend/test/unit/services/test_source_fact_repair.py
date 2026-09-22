"""有界事实核验修复回路与确定性降级渲染的行为契约。"""

from __future__ import annotations

import asyncio
import time

import pytest

from yuxi.services import chat_service
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
    async def fake_repair(draft, validation, source_uses, **kwargs):
        return _GOOD_DRAFT

    monkeypatch.setattr("yuxi.services.chat_service._repair_source_fact_grounding", fake_repair)
    guarded, validation = await _finalize_guarded_source_text(
        _DRAFT, evidence_level="E1_DATA_PROVENANCE", source_uses=_SOURCE_USES
    )

    assert "98 bp" in guarded
    assert "3 bp" not in guarded
    assert validation["fact_grounding"]["passed"] is True
    attempts = validation["fact_repair_attempts"]
    assert len(attempts) == 1 and attempts[0]["repaired"] is True
    assert isinstance(attempts[0]["elapsed_ms"], int)


@pytest.mark.asyncio
async def test_exhausted_repairs_fall_back_to_degraded_fact_sheet(monkeypatch):
    async def bad_repair(draft, validation, source_uses, **kwargs):
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
async def test_factless_custom_mcp_sources_impose_no_fact_obligation(monkeypatch):
    """GENERIC_MCP 轮次只有非注册表来源时：无账本可核验 → 正常发布，不再必拒。"""

    async def unexpected_repair(draft, validation, source_uses, **kwargs):
        raise AssertionError("repair must not run without a fact ledger")

    monkeypatch.setattr("yuxi.services.chat_service._repair_source_fact_grounding", unexpected_repair)
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

    assert guarded == _DRAFT
    assert validation["fact_grounding"]["required"] is False
    assert validation["fact_grounding"]["passed"] is True
    assert "fact_repair_attempts" not in validation


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


# ---------- P0-B：修复轮时间预算契约 ----------


def test_fact_repair_budget_fits_run_stream_watchdog():
    """预算关系不变量：修复轮最坏总耗时 < run idle 阈值 < run total 阈值。

    跨文件的时间契约是"回答生成后静默挂死、被看门狗收尸"事故的根因形态；
    任何人调整这三个常量之一，本测试强制重新核算预算关系。
    """
    from yuxi.services import run_worker

    wall = chat_service._FACT_REPAIR_ROUND_WALL_BUDGET_SECONDS
    assert (
        _FACT_REPAIR_MAX_ATTEMPTS * wall
        < run_worker.RUN_STREAM_IDLE_TIMEOUT_SECONDS
        < run_worker.RUN_STREAM_TOTAL_TIMEOUT_SECONDS
    )
    # 轮内细粒度超时不得超出墙钟（静默等待段先于墙钟失效，墙钟只兜慢流）
    assert chat_service._FACT_REPAIR_REQUEST_TIMEOUT_SECONDS <= wall
    assert chat_service._FACT_REPAIR_STREAM_CHUNK_TIMEOUT_SECONDS <= wall


@pytest.mark.asyncio
async def test_slow_repair_stream_abandoned_by_wall_budget(monkeypatch):
    """慢而不断流的修复流必须被墙钟截断：request/stream_chunk 超时只封静默不封总时长。"""

    class SlowChunk:
        content = "数据模式：SOURCE-ONLY\n"

    class SlowStreamModel:
        async def astream(self, messages):
            for _ in range(200):  # 总时长远超墙钟，但 chunk 间隔远小于 chunk 超时
                await asyncio.sleep(0.01)
                yield SlowChunk()

    monkeypatch.setattr("yuxi.agents.models.load_chat_model", lambda *args, **kwargs: SlowStreamModel())
    monkeypatch.setattr(chat_service, "_FACT_REPAIR_ROUND_WALL_BUDGET_SECONDS", 0.05)
    validation = {
        "fact_grounding": {
            "required": True,
            "ungrounded_lines": [2],
            "unsupported_numbers": [],
            "invalid_markers": [],
        }
    }
    started = time.monotonic()
    assert await _repair_source_fact_grounding(_DRAFT, validation, _SOURCE_USES) is None
    assert time.monotonic() - started < 2.0
