"""本轮产物清单块（artifacts_block）行为契约：join 语义、渲染形状、门禁时机。

实测缺陷修正版：
- join：run_id 第一维限定本轮；origin.source=="mcp" 再按 adopted 审计集合过滤；
  sequence_deliverable 的 origin **没有** mcp_call_audit_id，按 run_id 直收——
  按审计 id join 会恰好漏掉最关键的 .fa 文件；
- 时机：产物元数据（size 等）不是 manifest 事实——本块只允许在全门禁之后追加
  （缺陷2：进组合复跑必被 unsupported_numbers 打回，把整个投影打回原形）。
"""

from __future__ import annotations

from yuxi.knowledge.rendering.artifacts_block import (
    render_run_artifacts_block,
    select_publishable_artifacts,
)

_ROWS = [
    {
        "name": "Os06t0133000-01_cds.fa",
        "media_type": "text/plain",
        "size_bytes": 2051,
        "sha256": "cab8b7a461fb0a54561ba4b13fda07145c31cd395012e45bea4aa6669238fdde",
        "origin": {"source": "sequence_deliverable", "sequence_id": "Os06t0133000-01"},
    },
    {
        "name": "ricekb-profile_ricekb_gene_profile_1a2b3c4d.md",
        "media_type": "text/markdown",
        "size_bytes": 60123,
        "sha256": "3f5b2c1b9bd8dc0f5d63a94f78ee4b03ee91c4d5226f1a24c7a2f28c6dcaa1e9",
        "origin": {"source": "mcp", "mcp_server": "ricekb-profile", "mcp_call_audit_id": 268},
    },
    {
        "name": "unadopted_probe.json",
        "media_type": "application/json",
        "size_bytes": 10,
        "sha256": "a" * 64,
        "origin": {"source": "mcp", "mcp_call_audit_id": 266},
    },
    {
        "name": "agent_report.md",
        "media_type": "text/markdown",
        "size_bytes": 512,
        "sha256": "b" * 64,
        "origin": {"source": "agent_presented"},
    },
]


def test_select_filters_mcp_by_adopted_and_keeps_run_scoped_rows():
    selected = select_publishable_artifacts(_ROWS, adopted_audit_ids={268})
    names = [row["name"] for row in selected]
    # sequence_deliverable（origin 无 audit id）与 agent_presented 按 run_id 直收
    assert "Os06t0133000-01_cds.fa" in names
    assert "agent_report.md" in names
    # adopted 的 mcp 行保留审计语义
    assert "ricekb-profile_ricekb_gene_profile_1a2b3c4d.md" in names
    # 未 adopted 的 mcp 探查调用不进清单
    assert "unadopted_probe.json" not in names


def test_select_drops_mcp_rows_without_usable_audit_id():
    rows = [
        {"name": "x.json", "origin": {"source": "mcp"}},
        {"name": "y.json", "origin": {"source": "mcp", "mcp_call_audit_id": "NaN"}},
    ]
    assert select_publishable_artifacts(rows, adopted_audit_ids={268}) == []
    # adopted 集合为空/None 同样只保留非 mcp 行
    only_seq = [_ROWS[0]]
    assert [r["name"] for r in select_publishable_artifacts(only_seq, None)] == ["Os06t0133000-01_cds.fa"]


def test_render_block_shape_and_no_markers():
    block = render_run_artifacts_block(select_publishable_artifacts(_ROWS, {268}))
    assert block is not None
    assert block.startswith("**本轮产物**")
    assert "| 产物 | 类型 | 大小 | 完整性（sha256 前 8 位） |" in block
    assert "`Os06t0133000-01_cds.fa`" in block and "FASTA" in block
    assert "2.0 KB" in block and "58.7 KB" in block
    assert "`cab8b7a4`" in block
    assert "产物卡中下载或保存到工作区" in block
    # 清单块不携带 MCP-F marker（它在全门禁之后追加，不参与事实核验）
    assert "[MCP-F:" not in block
    # 空清单 → None（调用方跳过追加）
    assert render_run_artifacts_block([]) is None
    assert render_run_artifacts_block(None) is None


def _use(audit_id: int, operation: str, facts: list[dict]) -> dict:
    return {
        "source_use_id": f"mcp:{audit_id}",
        "provider_id": "ricekb",
        "operation": operation,
        "status": "SUCCESS",
        "adopted": True,
        "provenance": {"mcp_call_audit_id": audit_id, "fact_manifest": {"facts": facts}},
    }


_SEQ_FACTS = [
    {"id": "f_00000000000000a1", "path": "/data/sequence_id", "string_value": "Os06t0133000-01"},
    {"id": "f_00000000000000a2", "path": "/data/sequence_type", "string_value": "cds"},
    {"id": "f_00000000000000a3", "path": "/data/sequence_length", "numeric_value": 1830},
]

_PROFILE_FACTS_FOR_GROUNDING = [
    {"id": "f_00000000000000b1", "path": "/data/identity/entity_key", "string_value": "RAP:Os06g0133000"},
    {"id": "f_00000000000000b2", "path": "/data/identity/canonical_rap_id", "string_value": "Os06g0133000"},
    {"id": "f_00000000000000b3", "path": "/data/identity/matched_identifiers/0", "string_value": "Wx"},
    {"id": "f_00000000000000b4", "path": "/data/identity/matched_namespaces/0", "string_value": "SYMBOL"},
    {"id": "f_00000000000000b5", "path": "/data/identity/description", "string_value": "Granule-bound starch synthase"},
    {"id": "f_00000000000000b6", "path": "/data/identity/source_databases/0", "string_value": "MSU"},
    {"id": "f_00000000000000b7", "path": "/data/identity/source_databases/1", "string_value": "RAP_DB"},
    {"id": "f_00000000000000b8", "path": "/data/identity/source_record_count", "numeric_value": 12},
    {"id": "f_00000000000000b9", "path": "/data/locations/0/chromosome", "string_value": "Chr6"},
    {"id": "f_00000000000000ba", "path": "/data/locations/0/start", "numeric_value": 1765622},
    {"id": "f_00000000000000bb", "path": "/data/locations/0/end", "numeric_value": 1770656},
    {"id": "f_00000000000000bc", "path": "/data/locations/0/strand", "string_value": "+"},
    {"id": "f_00000000000000bd", "path": "/data/annotations/go", "string_value": "GO:0004373"},
]


def test_artifact_block_tokens_are_not_catalog_numbers():
    """缺陷2 风险存证：清单块的数字 token（大小/哈希前缀）不在事实目录——

    当前门禁放行是人类可读格式（"2.0 KB"）+ 表格结构豁免的偶然结果，不是
    契约保证；任何门禁收紧（表格行规则/原始字节数）都会把"早注入"打回。
    注入点契约因此收敛为：``_append_run_artifacts_footer`` 只在全部门禁之后
    追加（见下一用例）。
    """
    from yuxi.knowledge.rendering.source_output_guard import _fact_catalog

    uses = [
        _use(270, "ricekb_sequence", _SEQ_FACTS),
        _use(268, "ricekb_gene_profile", _PROFILE_FACTS_FOR_GROUNDING),
    ]
    catalog = _fact_catalog(uses)
    catalog_numbers = set()
    for fact in catalog.values():
        if "numeric_value" in fact:
            catalog_numbers.add(str(fact["numeric_value"]))
    block = render_run_artifacts_block(_ROWS)
    import re as _re

    block_numbers = set(_re.findall(r"\d+(?:\.\d+)?", block or ""))
    # 风险存证的核心：清单块的大小 token（2051B→"2.0"、60123B→"58.7"）不是任何
    # manifest 事实值——当前门禁放行是格式偶然，注入点契约必须锁在全门禁之后。
    size_tokens = {"2.0", "58.7"}
    assert size_tokens <= block_numbers
    assert not (size_tokens & catalog_numbers)


async def test_footer_helper_appends_after_content_and_filters_by_adoption(monkeypatch):
    """注入点契约（缺陷2/3 收口）：helper 在门禁链尾追加、原内容原样保留。

    join 语义集成：sequence_deliverable 直收、未 adopted 的 mcp 行剔除。
    """
    from langchain_core.messages import AIMessage

    from yuxi.services import agent_run_service
    from yuxi.services.chat_service import _append_run_artifacts_footer

    async def fake_load(run_id):
        assert run_id == "run-b079c160"
        return _ROWS

    monkeypatch.setattr(agent_run_service, "load_run_artifacts", fake_load)
    source_uses = [
        {"adopted": True, "provenance": {"mcp_call_audit_id": 268}},
        {"adopted": False, "provenance": {"mcp_call_audit_id": 266}},
    ]
    original = "叙述与投影表格（已过全部门禁）。"
    message = AIMessage(content=original)
    result = await _append_run_artifacts_footer(message, run_id="run-b079c160", source_uses=source_uses)
    assert result is message
    assert result.content.startswith(original)
    assert "**本轮产物**" in result.content
    assert "Os06t0133000-01_cds.fa" in result.content  # sequence_deliverable 无 audit id 也入清单
    assert "unadopted_probe.json" not in result.content  # 未 adopted 的 mcp 行剔除
    # 空消息 / 无 run_id / 查询失败：原样返回
    assert await _append_run_artifacts_footer(None, run_id="r", source_uses=[]) is None
    untouched = AIMessage(content="x")
    assert (await _append_run_artifacts_footer(untouched, run_id=None, source_uses=[])) is untouched

    async def boom(run_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(agent_run_service, "load_run_artifacts", boom)
    degraded = AIMessage(content="y")
    assert (await _append_run_artifacts_footer(degraded, run_id="r", source_uses=[])) is degraded
    assert degraded.content == "y"
