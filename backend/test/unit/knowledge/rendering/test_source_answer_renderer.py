"""MCP 值答案公开投影：业务值保留，内部审计协议不进入用户视图。"""

from __future__ import annotations

from pathlib import Path

from yuxi.knowledge.rendering.source_answer_renderer import (
    _FASTA_LINE,
    label_for_path,
    has_renderable_content,
    render_report,
    render_source_answer,
)
from yuxi.knowledge.rendering.source_output_guard import render_degraded_fact_sheet

_FIXTURES = Path(__file__).parents[3] / "fixtures" / "knowledge_rendering"
_DETAILS_OPEN = '<details class="yuxi-citations">'


def _golden(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def test_value_only_projection_hides_audit_protocol_on_all_golden_fixtures():
    for name in ("archive_draft.txt", "degraded_sheet.txt", "cds_answer.txt"):
        original = _golden(name)
        rendered = render_source_answer(original)
        assert "SOURCE-ONLY" not in rendered, name
        assert "MCP-F:" not in rendered, name
        assert "yuxi-citations" not in rendered, name
        assert "| 引用 |" not in rendered, name
        assert rendered.strip(), name


def test_markers_and_source_mode_are_hidden_without_fact_notes():
    original = "数据模式：SOURCE-ONLY\nWx 已核验。[MCP-F:42:f_00000000000000a1]"
    rendered = render_source_answer(original)  # 无 L2 数据
    assert rendered == "Wx 已核验。"


def test_fact_notes_never_surface_in_public_answer():
    original = "数据模式：SOURCE-ONLY\nWx 已核验。[MCP-F:42:f_00000000000000a1]"
    notes = {(42, "f_00000000000000a1"): {"path": "/data/identity/canonical_rap_id", "value": "Os06g0133000"}}
    rendered = render_source_answer(original, fact_notes=notes)
    assert rendered == "Wx 已核验。"
    assert "canonical_rap_id" not in rendered


def test_unknown_path_never_leaks_from_fact_notes():
    assert label_for_path("/data/completely/unknown/field") is None
    notes = {(42, "f_00000000000000a1"): {"path": "/data/completely/unknown/field", "value": "x"}}
    rendered = render_source_answer("数据模式：SOURCE-ONLY\n事实。[MCP-F:42:f_00000000000000a1]", fact_notes=notes)
    assert rendered == "事实。"


def test_renderer_failure_falls_back_to_original(monkeypatch):
    import yuxi.knowledge.rendering.source_answer_renderer as renderer_module

    original = "数据模式：SOURCE-ONLY\nx [MCP-F:42:f_00000000000000a1]"
    monkeypatch.setattr(
        renderer_module, "_value_only_projection", lambda text: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    assert render_source_answer(original) == "x"


def test_declaration_is_removed_wherever_it_appears():
    text = "概述一句话。\n\n数据模式：SOURCE-ONLY\n\n| 字段 | 值 |\n| --- | --- |"
    rendered = render_source_answer(text)
    assert "SOURCE-ONLY" not in rendered
    assert "概述一句话。" in rendered


def test_fasta_block_folding_preserves_bytes():
    fasta_lines = ["A" * 60, "C" * 60, "G" * 60]
    text = "数据模式：SOURCE-ONLY\n序列如下：\n" + "\n".join(fasta_lines) + "\n"
    rendered = render_source_answer(text)
    assert '<details class="yuxi-fasta">' in rendered
    for line in fasta_lines:
        assert line in rendered  # 字节原样保留
    assert "展开序列（3 行）" in rendered


def test_provenance_tokens_are_not_wrapped_with_internal_css():
    sha = "a" * 64
    text = f"数据模式：SOURCE-ONLY\n哈希 {sha} 与代码内 `{sha}`。"
    rendered = render_source_answer(text)
    assert '<span class="yuxi-prov">' not in rendered
    assert f"`{sha}`" in rendered


def test_eligibility_and_report():
    plain = "普通回答，无标记。"
    assert has_renderable_content(plain) is False
    assert render_report(plain)["eligible"] is False
    marked = "数据模式：SOURCE-ONLY\nx [MCP-F:1:f_00000000000000a1]"
    report = render_report(marked)
    assert report["eligible"] is True and report["marker_count"] == 1


def test_renderable_detector_accepts_fasta_without_markers():
    assert any(_FASTA_LINE.match(line) for line in ["A" * 50])
    assert has_renderable_content("序列：\n" + "ACGT" * 15) is True


def test_degraded_sheet_groups_by_domain_with_labels():
    source_uses = [
        {
            "source_use_id": "mcp:42",
            "provider_id": "ricekb",
            "operation": "ricekb_gene_profile",
            "status": "SUCCESS",
            "adopted": True,
            "provenance": {
                "mcp_call_audit_id": 42,
                "fact_manifest": {
                    "facts": [
                        {
                            "id": "f_00000000000000b2",
                            "path": "/data/locations/0/end",
                            "numeric_value": 1770656,
                        },
                        {
                            "id": "f_00000000000000b1",
                            "path": "/data/identity/canonical_rap_id",
                            "string_value": "Os06g0133000",
                        },
                        {
                            "id": "f_00000000000000b3",
                            "path": "/data/annotations/go",
                            "string_value": "GO:0004373",
                        },
                    ]
                },
            },
        }
    ]
    sheet = render_degraded_fact_sheet(source_uses)
    assert sheet is not None
    # 业务域分组标题存在且 identity 在 locations 之前（不再按 path 字母序）
    assert "### 标识与收录" in sheet
    assert "### 坐标" in sheet
    assert "### 功能注释" in sheet
    assert sheet.index("标识与收录") < sheet.index("坐标") < sheet.index("功能注释")
    # v4 值化：字段名用人类标签、无路径列（路径在读取边界的折叠引用清单里可还原）
    assert "规范 RAP ID" in sheet
    main_view = sheet.split('<details class="yuxi-citations">')[0]
    assert "/data/identity/canonical_rap_id" not in main_view
    assert "| 字段 | 值 | 引用 |" in sheet


def test_history_boundary_eligibility_gate():
    from yuxi.services.conversation_service import _source_answer_renderable

    marked = "数据模式：SOURCE-ONLY\nx [MCP-F:1:f_00000000000000a1]"
    assert _source_answer_renderable(marked, {}) is True
    # 错误消息与 SKIPPED 门禁消息不渲染
    assert _source_answer_renderable(marked, {"error_type": "model_connection_error"}) is False
    assert _source_answer_renderable(marked, {"is_error": True}) is False
    assert _source_answer_renderable(marked, {"source_output_guard": {"status": "SKIPPED"}}) is False
    assert _source_answer_renderable("普通文本", {}) is False


# ---------- 渲染器 v2：围栏感知与折叠引用清单 ----------


def test_fenced_fasta_folds_at_fence_level():
    fence = ["```fasta", ">Os06t0133000-01 description", "A" * 60, "C" * 60, "G" * 60, "```"]
    text = "数据模式：SOURCE-ONLY\n序列如下：\n" + "\n".join(fence) + "\n完毕。[MCP-F:42:f_00000000000000a1]"
    rendered = render_source_answer(text)
    assert rendered.count("```fasta") == 1  # 不产生嵌套围栏（golden 实测 v1 缺陷）
    assert '<details class="yuxi-fasta">' in rendered
    for line in fence[1:-1]:
        assert line in rendered  # 围栏内容字节不动
    assert rendered.index("```") < rendered.index("</details>")


def test_no_internal_span_wrapping_inside_or_outside_fences():
    sha = "a" * 64
    text = f"数据模式：SOURCE-ONLY\n外部哈希 {sha}，代码内不变：\n```\nhash={sha}\n```\n[MCP-F:42:f_00000000000000a1]"
    rendered = render_source_answer(text)
    assert '<span class="yuxi-prov">' not in rendered
    assert f"hash={sha}" in rendered


def test_single_long_bare_sequence_line_folds():
    line = "ACGT" * 500
    rendered = render_source_answer(f"数据模式：SOURCE-ONLY\n{line}\n")
    assert '<details class="yuxi-fasta">' in rendered
    assert line in rendered


def test_citation_appendix_is_absent_from_public_answer():
    rendered = render_source_answer("数据模式：SOURCE-ONLY\nWx 已核验。[MCP-F:42:f_00000000000000a1]")
    assert _DETAILS_OPEN not in rendered
    assert "引用清单" not in rendered
    assert rendered == "Wx 已核验。"


# ---------- 降级表 v3 收尾：D1/D2/D3 ----------


def _degraded_use(audit_id: int, operation: str, facts: list[dict]) -> dict:
    return {
        "source_use_id": f"mcp:{audit_id}",
        "provider_id": "ricekb",
        "operation": operation,
        "status": "SUCCESS",
        "adopted": True,
        "provenance": {"mcp_call_audit_id": audit_id, "fact_manifest": {"facts": facts}},
    }


def test_degraded_sheet_plumbing_filtered_and_deduped_and_richest_first():
    resolve_facts = [
        {"id": "f_00000000000000c1", "path": "/entity/canonical_rap_id", "string_value": "Os06g0133000"},
        {"id": "f_00000000000000c2", "path": "/data/0/canonical_rap_id", "string_value": "Os06g0133000"},
        {"id": "f_00000000000000c3", "path": "/meta/service_version", "string_value": "2.2.0"},
        {"id": "f_00000000000000c4", "path": "/entity/score_version", "string_value": "SOURCE_COVERAGE_V1"},
    ]
    profile_facts = [
        {"id": "f_00000000000000d1", "path": "/data/identity/canonical_rap_id", "string_value": "Os06g0133000"},
        {"id": "f_00000000000000d2", "path": "/data/identity/description", "string_value": "GBSS"},
        {"id": "f_00000000000000d3", "path": "/data/locations/0/chromosome", "string_value": "Chr6"},
        {"id": "f_00000000000000d4", "path": "/data/locations/0/start", "numeric_value": 1765622},
        {"id": "f_00000000000000d5", "path": "/data/locations/0/end", "numeric_value": 1770656},
    ]
    sheet = render_degraded_fact_sheet(
        [
            _degraded_use(201, "ricekb_resolve", resolve_facts),
            _degraded_use(202, "ricekb_gene_profile", profile_facts),
        ]
    )
    assert sheet is not None
    # D1：信息量最大的调用（202，5 事实）排在 201（4 事实）之前
    assert sheet.index("调用 202") < sheet.index("调用 201")
    # D2：管线行不出现，且有从略计数说明
    assert "service_version" not in sheet
    assert "SOURCE_COVERAGE_V1" not in sheet
    assert "管线/元信息" in sheet
    # v4 值化：主视图无路径列、无"（非公开值）"文案
    assert "字段（事实路径）" not in sheet
    assert "（非公开值" not in sheet
    # D3：同值去重——canonical_rap_id 的四个来源里每个值只出现一次
    assert sheet.count("Os06g0133000") >= 1
    value_rows = [line for line in sheet.splitlines() if "Os06g0133000" in line and line.startswith("|")]
    values_in_202 = [line for line in value_rows if "f_00000000000000d1" in line or "f_00000000000000c1" in line]
    assert len(values_in_202) <= 1  # 去重键跨调用生效：c1 与 d1 同值只留一条
    assert not ("f_00000000000000c2" in sheet and "f_00000000000000c1" in sheet)


def test_degraded_sheet_v4_value_view_and_folded_audit():
    """v4 值化：审计字段（sha/溯源/查询状态）下沉折叠层，主视图只有业务字段名。"""
    facts = [
        {"id": "f_0000000000000051", "path": "/data/sequence_id", "string_value": "Os06t0133000-01"},
        {"id": "f_0000000000000052", "path": "/data/sequence_length", "numeric_value": 1830},
        {
            "id": "f_0000000000000053",
            "path": "/data/sequence_sha256",
            "string_value": "cab8b7a461fb0a54561ba4b13fda07145c31cd395012e45bea4aa6669238fdde",
        },
        {"id": "f_0000000000000054", "path": "/provenance/0/row_ref", "numeric_value": 21013},
        {"id": "f_0000000000000055", "path": "/query/type", "string_value": "source_sequence"},
        {"id": "f_0000000000000056", "path": "/status", "string_value": "FOUND"},
        {"id": "f_0000000000000057", "path": "/data/names/MSU/cgsnl_name"},  # digest-only（null）
    ]
    sheet = render_degraded_fact_sheet([_degraded_use(203, "ricekb_sequence", facts)])
    assert sheet is not None
    main_view = sheet.split('<details class="yuxi-citations">')[0]
    folded = sheet.split('<details class="yuxi-citations">', 1)[1] if "<details" in sheet else ""
    # 主视图：业务字段（序列 ID/长度）在，无哈希、无查询状态、无路径字面量
    assert "Os06t0133000-01" in main_view and "1830" in main_view
    assert "cab8b7a4" not in main_view
    assert "source_sequence" not in main_view and "FOUND" not in main_view
    assert "/data/" not in main_view and "/provenance/" not in main_view
    # 折叠层：审计行（含完整路径与 marker）保损保留
    assert "cab8b7a4" in folded and "/provenance/0/row_ref" in folded
    assert "[MCP-F:203:f_0000000000000053]" in folded
    # 空值行剔除并入从略计数
    assert "cgsnl_name" not in sheet
    assert "空值/未核验" in sheet


def test_degraded_sheet_prioritizes_official_gene_and_protein_identifiers():
    facts = [
        {"id": "f_0000000000000061", "path": "/answer_policy", "string_value": "do not infer"},
        {"id": "f_0000000000000062", "path": "/provider", "string_value": "UNIPROT"},
        {
            "id": "f_0000000000000063",
            "path": "/data/results/0/comments/0/commentType",
            "string_value": "FUNCTION",
        },
        {
            "id": "f_0000000000000064",
            "path": "/data/results/0/primaryAccession",
            "string_value": "P0C585",
        },
        {
            "id": "f_0000000000000065",
            "path": "/data/results/0/genes/0/geneName/value",
            "string_value": "WAXY",
        },
        {
            "id": "f_0000000000000066",
            "path": "/data/results/0/organism/scientificName",
            "string_value": "Oryza sativa",
        },
    ]
    sheet = render_degraded_fact_sheet([_degraded_use(204, "uniprot_search_rest", facts)], maximum_facts=3)
    assert sheet is not None
    assert "### 标识与收录" in sheet
    assert "UniProt accession" in sheet and "P0C585" in sheet
    assert "基因符号" in sheet and "WAXY" in sheet
    assert "物种" in sheet and "Oryza sativa" in sheet
    assert "do not infer" not in sheet and "UNIPROT" not in sheet
    assert "FUNCTION" not in sheet


def test_degraded_sheet_fair_budget_keeps_two_authority_sources_visible():
    uniprot = [
        {
            "id": f"f_{index:016x}",
            "path": f"/data/results/0/comments/{index}/value",
            "string_value": f"annotation-{index}",
        }
        for index in range(1, 31)
    ]
    uniprot.insert(
        0,
        {
            "id": "f_0000000000000200",
            "path": "/data/results/0/primaryAccession",
            "string_value": "P0C585",
        },
    )
    ncbi = [
        {
            "id": "f_0000000000000300",
            "path": "/data/reports/0/gene_id",
            "string_value": "4340018",
        },
        {
            "id": "f_0000000000000301",
            "path": "/data/reports/0/symbol",
            "string_value": "LOC4340018",
        },
    ]
    sheet = render_degraded_fact_sheet(
        [
            _degraded_use(205, "uniprot_search_rest", uniprot),
            _degraded_use(206, "ncbi_datasets_gene_summary_cli", ncbi),
        ],
        maximum_facts=10,
    )
    assert sheet is not None
    assert "P0C585" in sheet
    assert "NCBI Gene ID" in sheet and "4340018" in sheet
    assert "NCBI 基因符号" in sheet and "LOC4340018" in sheet
