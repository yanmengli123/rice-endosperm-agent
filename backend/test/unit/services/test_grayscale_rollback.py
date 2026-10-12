"""灰度回滚回归（ADR-0008 P1/P2）：关通道只降精度不撤芯片，历史已存载荷不受影响。

panel_locator_enabled / table_cell_enabled 是发布侧开关：
- 关表格通道 → tables[] 不发布、芯片 suppressed_reason=table_channel_disabled、
  visual_status 回落题注（有题注证据时）；
- 关 panel 精度 → panel ref 走整图回退（panel_status=WHOLE_FIGURE_FALLBACK）；
- SSE 压缩重放消费的是**已存载荷**，与当前开关状态完全无关。
"""

import pytest
import yuxi.services.chat_service as svc
from yuxi.services.agent_run_service import _compact_stream_chunk

pytestmark = pytest.mark.unit


def _table_chip_match(monkeypatch):
    """正文一颗 Table 芯片，注册表 join 成功（anchor/revision 血统齐全）。"""
    matched = {
        "refs": [
            {
                "ref": "F1",
                "kind": "table",
                "label": "Table 1",
                "source": "caption",
                "citation_ref": "E1",
                "evidence_id": "ev",
                "anchor_id": "a",
                "kb_id": "kb",
                "file_id": "file",
                "revision_id": "rev",
                "page": 4,
                "panel_label": "",
            }
        ],
        "stats": {"orphan": 0},
    }
    monkeypatch.setattr(
        "yuxi.knowledge.rendering.figure_ref_channel.match_chips_to_registry",
        lambda *_args, **_kwargs: matched,
    )
    monkeypatch.setattr("yuxi.trace.emit_trace", lambda **_event: None)


async def test_table_channel_disabled_suppresses_tables_but_keeps_chips(monkeypatch):
    _table_chip_match(monkeypatch)

    def must_not_project(*_args, **_kwargs):
        raise AssertionError("table_cell_enabled=False 时不得触发表格投影")

    monkeypatch.setattr(
        "yuxi.knowledge.contracts.table_asset_projection.project_publishable_table",
        must_not_project,
    )
    monkeypatch.setattr(svc.conf, "table_cell_enabled", False)

    refs, figures, tables = await svc._figure_refs_public_payload(None, text="x", contract={"status": "COMPLETED"})

    assert len(refs) == 1
    assert refs[0]["suppressed_reason"] == "table_channel_disabled"
    # 有题注证据 → 视觉状态回落题注卡，而非谎称表格已并列
    assert refs[0]["visual_status"] == "VERIFIED_CAPTION_ONLY"
    assert refs[0]["table_index"] is None
    assert tables == []


async def test_table_channel_enabled_publishes_table_and_verified_status(monkeypatch):
    _table_chip_match(monkeypatch)
    monkeypatch.setattr(svc.conf, "table_cell_enabled", True)

    class FakeTable:
        table_id = "tbl-1"

        def model_dump(self, **_kwargs):
            return {"table_id": "tbl-1", "rows": []}

    async def project(*_args, **_kwargs):
        return FakeTable(), None

    monkeypatch.setattr(
        "yuxi.knowledge.contracts.table_asset_projection.project_publishable_table",
        project,
    )

    refs, figures, tables = await svc._figure_refs_public_payload(None, text="x", contract={"status": "COMPLETED"})

    assert refs[0]["suppressed_reason"] is None
    assert refs[0]["visual_status"] == "VERIFIED_WITH_TABLE"
    assert tables and tables[0]["table_id"] == "tbl-1"


async def test_panel_locator_disabled_degrades_to_whole_figure(monkeypatch):
    async def project(*_args, **_kwargs):
        refs = [{"ref": "F1", "anchor_id": "a", "kind": "figure", "panel_label": "B"}]
        figures = [
            {
                "binding_id": "figref:a",
                "revision_id": "r",
                "asset_name": name,
                "source_selector": {"panel_label": label} if label else {},
            }
            for name, label in (("a.png", "A"), ("whole.png", ""), ("b.png", "B"))
        ]
        return refs, figures, []

    monkeypatch.setattr(svc, "_figure_refs_public_payload", project)
    monkeypatch.setattr(svc.conf, "panel_locator_enabled", False)
    monkeypatch.setattr("yuxi.trace.emit_trace", lambda **_event: None)

    payload = await svc._augmented_citation_ready(None, locator=None, contract={}, text="")

    ref = payload["figure_refs"][0]
    # panel 标签保留、芯片保留，但精度降级为整图帧（layout 同 panel 边界测试）
    assert ref["panel_label"] == "B"
    assert ref["figure_index"] == 1
    assert ref["panel_status"] == "WHOLE_FIGURE_FALLBACK"


async def test_panel_locator_enabled_keeps_verified_panel(monkeypatch):
    async def project(*_args, **_kwargs):
        refs = [{"ref": "F1", "anchor_id": "a", "kind": "figure", "panel_label": "B"}]
        figures = [
            {
                "binding_id": "figref:a",
                "revision_id": "r",
                "asset_name": name,
                "source_selector": {"panel_label": label} if label else {},
            }
            for name, label in (("a.png", "A"), ("whole.png", ""), ("b.png", "B"))
        ]
        return refs, figures, []

    monkeypatch.setattr(svc, "_figure_refs_public_payload", project)
    monkeypatch.setattr(svc.conf, "panel_locator_enabled", True)
    monkeypatch.setattr("yuxi.trace.emit_trace", lambda **_event: None)

    payload = await svc._augmented_citation_ready(None, locator=None, contract={}, text="")

    ref = payload["figure_refs"][0]
    assert ref["figure_index"] == 2
    assert ref["panel_status"] == "VERIFIED"


def test_stored_citation_ready_replays_unchanged_with_both_channels_off():
    """历史回放消费已存 JSON：开关只闸新发布的 run，绝不重算存量载荷。"""
    stored_refs = [
        {
            "ref": "F1",
            "kind": "table",
            "visual_status": "VERIFIED_WITH_TABLE",
            "table_index": 0,
            "suppressed_reason": None,
        },
        {"ref": "F2", "kind": "figure", "panel_label": "B", "panel_status": "VERIFIED", "figure_index": 0},
    ]
    stored_tables = [{"table_id": "tbl-1", "rows": [[" genotype", "seed setting"]]}]
    chunk = {
        "status": "citation_ready",
        "figure_refs": stored_refs,
        "tables": stored_tables,
    }
    compact = _compact_stream_chunk(chunk)
    assert compact["figure_refs"] == stored_refs
    assert compact["tables"] == stored_tables
