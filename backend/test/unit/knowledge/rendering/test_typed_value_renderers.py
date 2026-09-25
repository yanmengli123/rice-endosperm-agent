"""类型化值渲染器（typed_value_renderers）行为契约。

零幻觉纪律：值逐字节来自 fact manifest、行携带构造性 marker、组合单元格只
连接不改值；发现级候选卡必须声明语义边界（引导官方核验）；不适用返回 None。
"""

from __future__ import annotations

import pytest

from yuxi.knowledge.rendering.typed_value_renderers import (
    render_for_task,
    render_typed_value_segment,
)

pytestmark = [pytest.mark.unit]


def _use(audit_id: int, operation: str, facts: list[dict], *, adopted: bool = True, provider: str = "gene-authority") -> dict:
    return {
        "source_use_id": f"mcp:{audit_id}",
        "provider_id": provider,
        "operation": operation,
        "status": "SUCCESS",
        "adopted": adopted,
        "provenance": {"mcp_call_audit_id": audit_id, "fact_manifest": {"facts": facts}},
    }


def _f(fid: str, path: str, value) -> dict:
    fact = {"id": fid, "path": path}
    if isinstance(value, (int, float)):
        fact["numeric_value"] = value
    else:
        fact["string_value"] = str(value)
    return fact


# ── PROTEIN_PROFILE ─────────────────────────────────────────────────────────

_UNIPROT_FACTS = [
    _f("f_0000000000000b01", "/data/results/0/primaryAccession", "Q0DEV5"),
    _f("f_0000000000000b02", "/data/results/0/entryType", "UniProtKB reviewed (Swiss-Prot)"),
    _f("f_0000000000000b03", "/data/results/0/organism/scientificName", "Oryza sativa"),
    _f("f_0000000000000b04", "/data/results/0/sequence/length", 609),
    _f("f_0000000000000b05", "/data/results/0/proteinDescription/recommendedName/fullName/value", "Granule-bound starch synthase 1"),
]


def test_protein_renders_value_card_with_markers():
    segment = render_typed_value_segment([_use(601, "uniprot_search_rest", _UNIPROT_FACTS)], kind="protein")
    assert segment is not None
    blocks = segment.blocks
    assert "`Q0DEV5`" in blocks and "609 aa" in blocks
    assert "UniProt accession" in blocks and "入库类型" in blocks and "物种" in blocks
    assert "[MCP-F:601:f_0000000000000b01]" in blocks  # 构造性 marker
    assert "/data/" not in blocks  # 洁净度：无路径字面量


def test_protein_without_accession_returns_none():
    facts = [_f("f_0000000000000b02", "/data/results/0/entryType", "reviewed")]
    assert render_typed_value_segment([_use(602, "uniprot_search_rest", facts)], kind="protein") is None


# ── LITERATURE_SEARCH ───────────────────────────────────────────────────────

_EPMC_FACTS = [
    _f("f_0000000000000c01", "/data/results/0/title", "Waxy rice endosperm characterization"),
    _f("f_0000000000000c02", "/data/results/0/pubYear", "2026"),
    _f("f_0000000000000c03", "/data/results/0/doi", "10.21203/rs.3.rs-9480499/v1"),
    _f("f_0000000000000c04", "/data/results/0/authorString", "Fang J, Wang H, et al."),
    _f("f_0000000000000c05", "/data/results/1/title", "Second paper on GBSSI"),
    _f("f_0000000000000c06", "/data/results/1/pubYear", "2018"),
]


def test_literature_renders_bibliography_card():
    segment = render_typed_value_segment([_use(603, "europe_pmc_search_rest", _EPMC_FACTS)], kind="literature")
    assert segment is not None
    blocks = segment.blocks
    assert "Waxy rice endosperm characterization" in blocks and "（2026）" in blocks
    assert "DOI：`10.21203" in blocks
    assert "[MCP-F:603:f_0000000000000c01]" in blocks
    assert "共命中 2 条" in blocks


# ── DATASET_DISCOVERY ───────────────────────────────────────────────────────

_AGG_FACTS = [
    _f("f_0000000000000d01", "/data/results/0/title", "Rice endosperm proteomics dataset"),
    _f("f_0000000000000d02", "/data/results/0/source", "PRIDE"),
    _f("f_0000000000000d03", "/data/results/0/accession", "PXD082271"),
    _f("f_0000000000000d04", "/data/results/0/publicationYear", "2025"),
]


def test_dataset_candidates_declare_discovery_boundary():
    segment = render_typed_value_segment(
        [_use(604, "search", _AGG_FACTS, provider="data-aggregator")], kind="dataset_candidates"
    )
    assert segment is not None
    blocks = segment.blocks
    assert "`PXD082271`" in blocks and "[PRIDE]" in blocks
    # 语义边界：候选不是核验事实，必须引导官方核验
    assert "未经官方接口核验" in blocks and "点名单一数据源" in blocks
    assert "[MCP-F:604:f_0000000000000d03]" in blocks


def test_task_mapping_and_non_applicability():
    uses = [_use(605, "europe_pmc_search_rest", _EPMC_FACTS)]
    assert render_for_task("LITERATURE_DISCOVERY", uses) is not None
    assert render_for_task("DATASET_DISCOVERY", uses) is None  # 工具不匹配
    # 未采纳调用不渲染（protein 通道）
    unadopted = [_use(606, "uniprot_search_rest", _UNIPROT_FACTS, adopted=False)]
    assert render_typed_value_segment(unadopted, kind="protein") is None
    # 未知任务类型
    assert render_for_task("UNKNOWN_TASK", uses) is None


def test_server_hint_filters_foreign_providers():
    uses = [_use(607, "europe_pmc_search_rest", _EPMC_FACTS, provider="some-other-server")]
    assert render_for_task("LITERATURE_DISCOVERY", uses, server_hint="gene-authority") is None
    assert render_for_task("LITERATURE_DISCOVERY", uses, server_hint="some-other-server") is not None
