"""批次 1：文献深读上下文装配（闸门 + kill switch + fail-open）的单元测试。"""

from __future__ import annotations

import pytest

from yuxi.literature.deepread_context import build_deepread_payload, extract_bound_file_id

pytestmark = [pytest.mark.unit]


class TestExtractBoundFileId:
    def test_present_returns_file_id(self) -> None:
        assert extract_bound_file_id({"literature_binding": {"file_id": "f1"}}) == "f1"

    def test_absent_returns_none(self) -> None:
        assert extract_bound_file_id({}) is None
        assert extract_bound_file_id(None) is None
        assert extract_bound_file_id({"literature_binding": {}}) is None
        assert extract_bound_file_id({"literature_binding": "not-a-dict"}) is None


@pytest.mark.asyncio
async def test_build_deepread_payload_gates_on_binding(monkeypatch):
    # 无 literature_binding（非文献会话）→ None（即使开关开启）
    async def _fake_map(*_a, **_k):  # pragma: no cover - 不应被调用
        raise AssertionError("非文献会话不应构建地图")

    monkeypatch.setattr("yuxi.literature.deepread_context.build_document_map", _fake_map)
    assert await build_deepread_payload(None, {}) is None


@pytest.mark.asyncio
async def test_build_deepread_payload_gates_on_kill_switch(monkeypatch):
    # literature_binding 存在但 kill switch 关闭 → None
    assert (
        await build_deepread_payload(
            None, {"literature_binding": {"file_id": "f1"}, "literature_enhancement": {"deepread_enabled": False}}
        )
        is None
    )


@pytest.mark.asyncio
async def test_build_deepread_payload_success(monkeypatch):
    async def _fake_map(_db, *, file_id, parse_revision_id=None, tenant_id=None, kb_id=None):
        assert file_id == "f1"
        assert parse_revision_id == "rev_pin"
        assert tenant_id == 1 and kb_id == "kb1"
        return {
            "revision_id": "rev_pin",
            "title": "T",
            "outline": ["Methods"],
            "sections": [{"partition": "MAIN_TEXT", "page_start": 1, "page_end": 5}],
            "figures": ["Figure 1"],
            "tables": ["Table 1"],
            "page_count": 5,
        }

    monkeypatch.setattr("yuxi.literature.deepread_context.build_document_map", _fake_map)
    payload = await build_deepread_payload(
        None,
        {
            "tenant_id": 1,
            "effective_kb_ids": ["kb1"],
            "document_scope_ceiling": {"file_ids": ["f1"]},
            "literature_enhancement": {"deepread_enabled": True},
            "literature_binding": {"file_id": "f1", "kb_id": "kb1", "parse_revision_id": "rev_pin"},
        },
    )
    assert payload is not None
    assert payload["text"].startswith("<LITERATURE_DOCUMENT_MAP>")
    assert payload["figure_count"] == 1
    assert payload["table_count"] == 1
    assert payload["revision_id"] == "rev_pin"
    assert payload["character_count"] == len(payload["text"])


@pytest.mark.parametrize("missing", ["tenant_id", "effective_kb_ids", "document_scope_ceiling", "parse_revision_id"])
@pytest.mark.asyncio
async def test_map_does_not_read_outside_frozen_scope(monkeypatch, missing):
    snapshot = {
        "tenant_id": 1,
        "effective_kb_ids": ["kb1"],
        "document_scope_ceiling": {"file_ids": ["f1"]},
        "literature_enhancement": {"deepread_enabled": True},
        "literature_binding": {"file_id": "f1", "kb_id": "kb1", "parse_revision_id": "rev_pin"},
    }
    if missing == "parse_revision_id":
        snapshot["literature_binding"].pop(missing)
    else:
        snapshot.pop(missing)

    async def boom(*args, **kwargs):
        raise AssertionError("out-of-scope or unpinned map must not query artifacts")

    monkeypatch.setattr("yuxi.literature.deepread_context.build_document_map", boom)
    assert await build_deepread_payload(None, snapshot) is None
