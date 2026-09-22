"""run_worker 终态 finished chunk 附带 run 级产物清单的行为契约。

- 只对 status == "finished" 的 chunk 附带；错误/中断 chunk 原样返回；
- 仅在确有产物时附带（字段缺席 ⟺ 本轮无产物，与 figures 发布口径一致）；
- 产物查询失败降级为不附带，终态语义不被产物投影拖挂。
"""

from __future__ import annotations

import pytest

from yuxi.services.run_worker import _attach_run_artifacts_to_finished_chunk

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def _patch_loader(monkeypatch, value_or_exception):
    async def fake_load(run_id):
        if isinstance(value_or_exception, Exception):
            raise value_or_exception
        return value_or_exception

    monkeypatch.setattr("yuxi.services.agent_run_service.load_run_artifacts", fake_load)


async def test_finished_chunk_gets_artifacts_when_present(monkeypatch):
    _patch_loader(
        monkeypatch,
        [{"virtual_path": "/home/gem/user-data/outputs/mcp_results/a.json", "name": "a.json"}],
    )
    chunk = await _attach_run_artifacts_to_finished_chunk("run-1", {"status": "finished", "request_id": "req"})
    assert chunk["status"] == "finished"
    assert [item["virtual_path"] for item in chunk["artifacts"]] == [
        "/home/gem/user-data/outputs/mcp_results/a.json"
    ]
    # 原 chunk 不被原地修改（调用方持有的 finished chunk 保持可复用）
    original = {"status": "finished"}
    enriched = await _attach_run_artifacts_to_finished_chunk("run-1", original)
    assert "artifacts" not in original and "artifacts" in enriched


async def test_finished_chunk_without_artifacts_stays_unchanged(monkeypatch):
    _patch_loader(monkeypatch, [])
    chunk = {"status": "finished", "request_id": "req"}
    result = await _attach_run_artifacts_to_finished_chunk("run-1", chunk)
    assert result is chunk
    assert "artifacts" not in result


async def test_error_and_interrupted_chunks_are_untouched(monkeypatch):
    _patch_loader(
        monkeypatch,
        [{"virtual_path": "/home/gem/user-data/outputs/mcp_results/a.json", "name": "a.json"}],
    )
    error_chunk = {"status": "error", "error_type": "model_error"}
    assert await _attach_run_artifacts_to_finished_chunk("run-1", error_chunk) is error_chunk
    interrupted_chunk = {"status": "interrupted", "message": "对话已取消"}
    assert await _attach_run_artifacts_to_finished_chunk("run-1", interrupted_chunk) is interrupted_chunk


async def test_loader_failure_degrades_to_unchanged_chunk(monkeypatch):
    _patch_loader(monkeypatch, RuntimeError("db unavailable"))
    chunk = {"status": "finished"}
    result = await _attach_run_artifacts_to_finished_chunk("run-1", chunk)
    assert result is chunk
    assert "artifacts" not in result


def test_compact_chunk_fields_whitelists_artifacts():
    """红线：终态附带的 artifacts 必须在压缩白名单内，否则被静默剥离（figures 事故）。"""
    from yuxi.services.agent_run_service import COMPACT_CHUNK_FIELDS, _compact_stream_chunk

    assert "artifacts" in COMPACT_CHUNK_FIELDS
    compact = _compact_stream_chunk(
        {"status": "finished", "artifacts": [{"virtual_path": "/x/a.json"}], "dropped": "field"}
    )
    assert compact["artifacts"] == [{"virtual_path": "/x/a.json"}]
    assert "dropped" not in compact
