"""F3' golden 数字核验口径测试：用实测被拒草稿的行形态锁现状行为。

取材：2026-09-22 run e772d26c/a7b982e3 的真实档案草稿（见
``test/fixtures/knowledge_rendering/archive_draft.txt``），覆盖当时被拒日志里
出现的数字类：坐标（1765622/1770656）、千分位（1,770,656）、表名内嵌日期
（irgsp_1_0_…_20260205:22049）、溯源行号（#row=33731）、正文日期（2026-02-05）。

结论契约（红线"没有实测失败用例就不改核验口径"）：
- 若下列现状用例全部通过 → F3 关闭，fact_ledger.py 的 mask 不做任何改动；
- 未来出现新的实测误拒，先在此固化用例，再补 mask 并配反向测试。
"""

from __future__ import annotations

from pathlib import Path

from yuxi.knowledge.rendering.source_output_guard import guard_answer_for_evidence_level

_FIXTURES = Path(__file__).parents[3] / "fixtures" / "knowledge_rendering"

_FACTS = [
    {"id": "f_00000000000000a1", "path": "/data/locations/0/start", "numeric_value": 1765622},
    {"id": "f_00000000000000a2", "path": "/data/locations/0/end", "numeric_value": 1770656},
    {"id": "f_00000000000000a3", "path": "/data/locations/0/chromosome", "string_value": "Chr6"},
    {
        "id": "f_00000000000000a4",
        "path": "/data/evidence_refs/provenance_ids/0",
        "string_value": "source_msu.msu_loci#row=33731",
        "numeric_tokens": ["33731"],
    },
    {
        "id": "f_00000000000000a5",
        "path": "/data/evidence_refs/provenance_ids/1",
        "string_value": "source_rapdb.irgsp_1_0_representative_annotation_20260205:22049",
    },
    {"id": "f_00000000000000a6", "path": "/data/identity/canonical_rap_id", "string_value": "Os06g0133000"},
]

_SOURCE_USES = [
    {
        "source_use_id": "mcp:42",
        "provider_id": "ricekb",
        "operation": "ricekb_gene_profile",
        "status": "SUCCESS",
        "adopted": True,
        "provenance": {
            "mcp_call_audit_id": 42,
            "fact_manifest": {"facts": _FACTS},
        },
    }
]


def _guard(draft: str) -> dict:
    _, audit = guard_answer_for_evidence_level(draft, evidence_level="E1_DATA_PROVENANCE", source_uses=_SOURCE_USES)
    return audit


def test_real_coordinate_row_with_provenance_ref_passes():
    """实测 L33 形态：坐标行 + #row=33731 溯源引用，全部有对应事实 → 通过。"""
    draft = (
        "数据模式：SOURCE-ONLY\n"
        "| MSU | Chr6 | LOC_Os06g04200 | 1765622 | 1770656 | + | "
        "source_msu.msu_loci#row=33731 "
        "[MCP-F:42:f_00000000000000a1][MCP-F:42:f_00000000000000a2]"
        "[MCP-F:42:f_00000000000000a3][MCP-F:42:f_00000000000000a4] |"
    )
    audit = _guard(draft)
    assert audit["fact_grounding"]["passed"] is True, audit["fact_grounding"]
    assert audit["fact_grounding"]["unsupported_numbers"] == []


def test_thousands_separator_coordinate_passes_when_cited():
    """千分位形态：1,770,656 引用 numeric 1770656 的事实 → 归一化后通过。"""
    draft = (
        "数据模式：SOURCE-ONLY\n"
        "Wx（Os06g0133000）在 MSU 注释中的终止坐标为 1,770,656。"
        "[MCP-F:42:f_00000000000000a2][MCP-F:42:f_00000000000000a6]"
    )
    audit = _guard(draft)
    assert audit["fact_grounding"]["passed"] is True, audit["fact_grounding"]


def test_table_name_embedded_date_digits_are_identifier_protected():
    """表名内嵌日期（…_20260205:22049）逐字照抄工具返回 → 下划线/冒号保护，不构成数值主张。"""
    draft = (
        "数据模式：SOURCE-ONLY\n"
        "| 溯源行 | source_rapdb.irgsp_1_0_representative_annotation_20260205:22049 "
        "[MCP-F:42:f_00000000000000a5] |"
    )
    audit = _guard(draft)
    assert audit["fact_grounding"]["passed"] is True, audit["fact_grounding"]
    assert audit["fact_grounding"]["unsupported_numbers"] == []


def test_prose_date_is_masked_as_structural_span():
    """正文里的快照日期（2026-02-05）是结构性片段，不要求事实覆盖。"""
    draft = "数据模式：SOURCE-ONLY\nRAP-DB 快照导入于 2026-02-05，Wx 记录核验通过。[MCP-F:42:f_00000000000000a6]"
    audit = _guard(draft)
    assert audit["fact_grounding"]["passed"] is True, audit["fact_grounding"]


def test_wrong_coordinate_is_still_rejected():
    """反向锁：错值坐标（1770657）不得因任何 mask 扩展而放过。"""
    draft = (
        "数据模式：SOURCE-ONLY\n"
        "| MSU | Chr6 | LOC_Os06g04200 | 1765622 | 1770657 | + "
        "[MCP-F:42:f_00000000000000a1][MCP-F:42:f_00000000000000a2] |"
    )
    audit = _guard(draft)
    assert audit["fact_grounding"]["passed"] is False
    assert any(item["value"] == "1770657" for item in audit["fact_grounding"]["unsupported_numbers"])


def test_golden_archive_draft_fixture_present():
    """golden 固件在位：真实档案草稿供渲染器保损回放套件消费。"""
    assert (_FIXTURES / "archive_draft.txt").exists()
    assert (_FIXTURES / "degraded_sheet.txt").exists()
    assert (_FIXTURES / "cds_answer.txt").exists()


def test_bare_time_mask_does_not_split_table_name_row_ref():
    r"""golden 实测误报（049）：裸时间模式不得匹配 …_20260205:22049 的中段。

    修复前 "\d{1,2}:\d{2}" 匹配了 "05:22"，切开的 "049" 被当成数值主张导致整答被拒；
    修复后加边界（前不得是标识符字符，后不得是字母数字），真时间仍被掩蔽。
    """
    from yuxi.agents.mcp.fact_ledger import extract_number_tokens, mask_structural_number_spans

    masked = mask_structural_number_spans("source_rapdb.irgsp_1_0_representative_annotation_20260205:22049")
    assert extract_number_tokens(masked) == []
    # 反向：正文里的真实时间仍是结构片段，不产生数值主张
    prose = mask_structural_number_spans("导入于 12:30 完成")
    assert extract_number_tokens(prose) == []
    # 反向：真实日期仍被掩蔽
    assert extract_number_tokens(mask_structural_number_spans("导入于 2026-02-05 完成")) == []
