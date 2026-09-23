"""数据面投影（profile_projection）行为契约 + 组合复跑门禁 + 洁净度断言。

分工（呈现层 v3 定稿）：模型只写 ≤4 句叙述；档案表/序列摘要表由服务端从
source_manifest 的事实清单确定性构造（构造性 marker：值与 marker 同源于该
fact）。fixture 形态取自实测 run 34ab1514（gene_profile）与 31fce149
（sequence）的 manifest 投影。
"""

from __future__ import annotations

from yuxi.knowledge.rendering.profile_projection import project_data_plane
from yuxi.knowledge.rendering.source_output_guard import guard_answer_for_evidence_level

_PROFILE_FACTS = [
    {"id": "f_00000000000000e1", "path": "/data/identity/entity_key", "string_value": "RAP:Os06g0133000"},
    {"id": "f_00000000000000e2", "path": "/data/identity/canonical_rap_id", "string_value": "Os06g0133000"},
    {"id": "f_00000000000000e3", "path": "/data/identity/matched_identifiers/0", "string_value": "Wx"},
    {"id": "f_00000000000000e4", "path": "/data/identity/matched_namespaces/0", "string_value": "SYMBOL"},
    {"id": "f_00000000000000e5", "path": "/data/identity/description", "string_value": "Granule-bound starch synthase"},
    {
        "id": "f_00000000000000e6",
        "path": "/data/identity/source_databases/0",
        "string_value": "MSU",
    },
    {
        "id": "f_00000000000000e7",
        "path": "/data/identity/source_databases/1",
        "string_value": "RAP_DB",
    },
    {"id": "f_00000000000000e8", "path": "/data/identity/source_record_count", "numeric_value": 12},
    {"id": "f_00000000000000e9", "path": "/data/locations/0/chromosome", "string_value": "Chr6"},
    {"id": "f_00000000000000ea", "path": "/data/locations/0/start", "numeric_value": 1765622},
    {"id": "f_00000000000000eb", "path": "/data/locations/0/end", "numeric_value": 1770656},
    {"id": "f_00000000000000ec", "path": "/data/locations/0/strand", "string_value": "+"},
    {"id": "f_00000000000000ed", "path": "/data/locations/0/locus", "string_value": "LOC_Os06g04200"},
    {"id": "f_00000000000000ee", "path": "/data/annotations/go", "string_value": "GO:0004373"},
    {
        "id": "f_00000000000000ef",
        "path": "/data/contract_notes/0",
        "string_value": (
            "调控关系（regulators/targets）：当前 RiceKB 契约未提供该数据；"
            "本轮未执行该查询——不得写成任何工具返回 NO_EVIDENCE。"
        ),
    },
    {
        "id": "f_00000000000000f0",
        "path": "/data/contract_notes/1",
        "string_value": "坐标差与区间长度只引用 qc 记录的 value，禁止自行计算或改写口径。",
    },
    {"id": "f_00000000000000f1", "path": "/data/transcripts/transcript_id/0", "string_value": "Os06t0133000-01"},
    {"id": "f_00000000000000f2", "path": "/data/transcripts/transcript_id/1", "string_value": "Os06t0133000-02"},
]

_SEQUENCE_FACTS_A = [
    {"id": "f_00000000000000f3", "path": "/data/sequence_id", "string_value": "Os06t0133000-01"},
    {"id": "f_00000000000000f4", "path": "/data/sequence_type", "string_value": "cds"},
    {"id": "f_00000000000000f5", "path": "/data/sequence_length", "numeric_value": 1830},
    {
        "id": "f_00000000000000f6",
        "path": "/data/sequence_sha256",
        "string_value": "cab8b7a461fb0a54561ba4b13fda07145c31cd395012e45bea4aa6669238fdde",
    },
    {"id": "f_00000000000000f7", "path": "/data/source_table", "string_value": "irgsp_1_0_cds_20260205"},
    {"id": "f_00000000000000f8", "path": "/data/source_record_id", "numeric_value": 21013},
]


def _use(audit_id: int, operation: str, facts: list[dict], *, adopted: bool = True) -> dict:
    return {
        "source_use_id": f"mcp:{audit_id}",
        "provider_id": "ricekb",
        "operation": operation,
        "status": "SUCCESS",
        "adopted": adopted,
        "provenance": {"mcp_call_audit_id": audit_id, "fact_manifest": {"facts": facts}},
    }


def test_profile_projection_renders_labeled_table_with_constructive_markers():
    projection = project_data_plane([_use(205, "ricekb_gene_profile", _PROFILE_FACTS)])
    assert projection is not None and projection.kind == "profile"
    blocks = projection.blocks
    assert "| 项目 | 值 | 引用 |" in blocks
    assert "Os06g0133000" in blocks  # 值逐字节来自 manifest
    assert "[MCP-F:205:f_00000000000000e2]" in blocks  # 构造性 marker
    assert "Chr6 1765622–1770656（+）" in blocks  # 位置组合行（只连接不改值）
    assert "`LOC_Os06g04200`" in blocks
    # 洁净度（A8）：无路径字面量、无机器列
    assert "/data/" not in blocks
    assert "provenance_id" not in blocks and "content_hash" not in blocks


def test_profile_boundary_notes_use_canned_templates_only():
    projection = project_data_plane([_use(205, "ricekb_gene_profile", _PROFILE_FACTS)])
    blocks = projection.blocks
    # 罐头边界行：note 含"未提供"且命中类别 → 固定句式
    assert "调控关系与靶标数据 RiceKB 契约未提供。" in blocks
    # 非缺失类 note（指令型）不进正文，原样进折叠层
    assert "禁止自行计算" not in blocks.split("<details")[0]
    assert "禁止自行计算" in blocks  # 折叠层原样保留，一字不改
    assert '<details class="yuxi-contract-notes">' in blocks


def test_projection_triggers_and_fallbacks():
    # 两次 gene_profile → 不触发（多实体风险）
    assert (
        project_data_plane(
            [_use(205, "ricekb_gene_profile", _PROFILE_FACTS), _use(206, "ricekb_gene_profile", _PROFILE_FACTS)]
        )
        is None
    )
    # 未 adopted → 不触发
    assert project_data_plane([_use(205, "ricekb_gene_profile", _PROFILE_FACTS, adopted=False)]) is None
    # 事实过少（无值 manifest / 受限数据级）→ 不触发
    thin = [dict(_PROFILE_FACTS[0]), dict(_PROFILE_FACTS[1])]
    assert project_data_plane([_use(205, "ricekb_gene_profile", thin)]) is None
    # 无关工具 → 不触发
    assert project_data_plane([_use(207, "ricekb_resolve", _PROFILE_FACTS)]) is None


def test_sequence_projection_renders_summary_table():
    projection = project_data_plane([_use(235, "ricekb_sequence", _SEQUENCE_FACTS_A)])
    assert projection is not None and projection.kind == "sequence"
    blocks = projection.blocks
    main_view = blocks.split("<details", 1)[0]
    # v4 值视图：主表 = 序列/类型/长度/描述；sha256 与源表:行下沉折叠"核验明细"
    assert "`Os06t0133000-01`" in main_view
    assert "1830 bp" in main_view
    assert "cab8b7a461fb0a54561ba4b13fda07145c31cd395012e45bea4aa6669238fdde" in blocks
    assert "cab8b7a461fb0a54561ba4b13fda07145c31cd395012e45bea4aa6669238fdde" not in main_view
    assert "irgsp_1_0_cds_20260205:21013" in blocks
    assert "irgsp_1_0_cds_20260205:21013" not in main_view
    assert '<details class="yuxi-citations"><summary>核验明细' in blocks
    assert "[MCP-F:235:f_00000000000000f5]" in main_view
    # P5：散文指针（"完整 FASTA 文件见本消息产物区"）已删除——下载入口由
    # 全门禁之后追加的产物清单块（artifacts_block）承载，投影文本必须 100%
    # 过事实门禁，不得携带非 manifest 内容。
    assert "完整 FASTA 文件见本消息产物区" not in blocks
    assert "产物" not in blocks


def test_combined_prose_and_projection_passes_fact_grounding():
    """组合文本（叙述 + 服务端表格）整体复跑事实门禁——构造性 marker 必须通过。"""
    source_uses = [_use(205, "ricekb_gene_profile", _PROFILE_FACTS)]
    projection = project_data_plane(source_uses)
    assert projection is not None
    prose = (
        "数据模式：SOURCE-ONLY\n"
        "Wx（Os06g0133000）编码颗粒结合型淀粉合酶，负责胚乳直链淀粉合成。"
        "[MCP-F:205:f_00000000000000e2][MCP-F:205:f_00000000000000e5]\n"
        "基因位于 Chr6 1765622–1770656（+ 链），跨源坐标一致。"
        "[MCP-F:205:f_00000000000000e9][MCP-F:205:f_00000000000000ea]"
        "[MCP-F:205:f_00000000000000eb][MCP-F:205:f_00000000000000ec]"
    )
    combined = prose + "\n\n" + projection.blocks
    guarded, audit = guard_answer_for_evidence_level(
        combined, evidence_level="E1_DATA_PROVENANCE", source_uses=source_uses
    )
    grounding = audit["fact_grounding"]
    assert grounding["required"] is True
    assert grounding["passed"] is True, grounding
    assert grounding["ungrounded_lines"] == []
    assert grounding["unsupported_numbers"] == []
    assert audit["status"] == "PASSED"


def test_projection_does_not_fabricate_missing_fields():
    """缺字段的行不出现（绝不编造）：去掉 annotations/contract_notes 后不渲染对应行。"""
    facts = [f for f in _PROFILE_FACTS if not f["path"].startswith(("/data/annotations", "/data/contract_notes"))]
    blocks = project_data_plane([_use(205, "ricekb_gene_profile", facts)]).blocks
    assert "GO 注释" not in blocks
    assert "契约说明" not in blocks and "契约未提供" not in blocks


# ── P5/P6：合并渲染、域补齐、四桶覆盖 ────────────────────────────────────────

_PROFILE_GOLDEN_FACTS = [
    *_PROFILE_FACTS,
    # names（别名域，P6 新增主表行）
    {"id": "f_00000000000000g1", "path": "/data/names/RAP_DB/symbol", "string_value": "Wx"},
    {"id": "f_00000000000000g2", "path": "/data/names/RAP_DB/synonyms/0", "string_value": "WX1"},
    {"id": "f_00000000000000g3", "path": "/data/names/RAP_DB/synonyms/1", "string_value": "GBSS-I"},
    {
        "id": "f_00000000000000g4",
        "path": "/data/names/ORYZABASE/synonyms/0",
        "string_value": "granule-bound starch synthase 1",
    },
    # references（文献域，P6 新增折叠层）
    {"id": "f_00000000000000g5", "path": "/data/references/0/pubmed_id", "string_value": "41186968"},
    {"id": "f_00000000000000g6", "path": "/data/references/0/title", "string_value": "Waxy gene regulation"},
    {"id": "f_00000000000000g7", "path": "/data/references/0/journal", "string_value": "Plant Mol Biol"},
    {"id": "f_00000000000000g8", "path": "/data/references/0/year", "string_value": "2018"},
    # qc（跨源一致性核验，P6 新增折叠层）
    {"id": "f_00000000000000g9", "path": "/data/qc/0/rule", "string_value": "interval_length_v1"},
    {"id": "f_00000000000000ga", "path": "/data/qc/0/value", "numeric_value": 5035},
    {"id": "f_00000000000000gb", "path": "/data/qc/0/unit", "string_value": "bp"},
    {
        "id": "f_00000000000000gc",
        "path": "/data/qc/0/formula",
        "string_value": "end - start + 1 (one_based_inclusive)",
    },
    # evidence_refs / compare（显式跳过桶：内部标识/聚合中间态）
    {"id": "f_00000000000000gd", "path": "/data/evidence_refs/0", "string_value": "prv_2049_ab"},
    {"id": "f_00000000000000ge", "path": "/data/compare/agreements/0", "string_value": "Os06g0133000"},
    # 空值事实（digest-only → null_or_digest_only 桶）
    {"id": "f_00000000000000gf", "path": "/data/annotations/msu_cgsnl", "value_digest": "sha256:deadbeef"},
]


def _golden_profile_projection():
    projection = project_data_plane([_use(268, "ricekb_gene_profile", _PROFILE_GOLDEN_FACTS)])
    assert projection is not None
    return projection


def test_dual_tool_round_merges_sequence_then_profile():
    """P5：双工具轮两段都渲染，sequence 在前（原子请求形态）、profile 在后。"""
    uses = [_use(270, "ricekb_sequence", _SEQUENCE_FACTS_A), _use(268, "ricekb_gene_profile", _PROFILE_FACTS)]
    projection = project_data_plane(uses)
    assert projection is not None
    assert projection.kind == "sequence+profile"
    blocks = projection.blocks
    assert blocks.index("| 序列 | 类型 | 长度 | 描述 | 引用 |") < blocks.index("| 项目 | 值 | 引用 |")
    assert "`Os06t0133000-01`" in blocks and "Os06g0133000" in blocks
    # 兼容属性：复合段的 audit_id 取首段、used_fact_ids 为并集
    assert projection.audit_id == 270


def test_sequence_used_fact_ids_no_longer_empty():
    """P6 收紧细节 2：序列段 used_fact_ids 补齐（修 fact_count 观测恒空）。"""
    seq_only = project_data_plane([_use(235, "ricekb_sequence", _SEQUENCE_FACTS_A)])
    assert seq_only is not None
    assert set(seq_only.used_fact_ids) == {f["id"] for f in _SEQUENCE_FACTS_A}


def test_merged_projection_with_prose_passes_fact_grounding():
    """缺陷2 防护（主路径）：双工具合并投影 + 叙述整体复跑事实门禁必须通过。"""
    uses = [_use(270, "ricekb_sequence", _SEQUENCE_FACTS_A), _use(268, "ricekb_gene_profile", _PROFILE_FACTS)]
    projection = project_data_plane(uses)
    assert projection is not None
    prose = (
        "数据模式：SOURCE-ONLY\n"
        "Wx（Os06g0130300 笔误校正：Os06g0133000）编码颗粒结合型淀粉合酶。"
        "[MCP-F:268:f_00000000000000e2][MCP-F:268:f_00000000000000e5]\n"
        "CDS 全长 1830 bp，完整序列见产物区。[MCP-F:270:f_00000000000000f5]"
    )
    combined = prose + "\n\n" + projection.blocks
    _, audit = guard_answer_for_evidence_level(combined, evidence_level="E1_DATA_PROVENANCE", source_uses=uses)
    grounding = audit["fact_grounding"]
    assert grounding["required"] is True
    assert grounding["passed"] is True, grounding
    assert audit["status"] == "PASSED"
    # 产物清单块绝不在投影内（size 数字非 manifest 事实，进组合复跑必被打回）
    assert "本轮产物" not in projection.blocks


def test_profile_renders_aliases_references_and_qc():
    """P6：names → 主表别名行；references/qc → 折叠层（值逐字节、marker 照挂）。"""
    blocks = _golden_profile_projection().blocks
    # 别名主表行（含命名空间口径）
    assert "别名（RAP_DB）" in blocks
    assert "`WX1`、`GBSS-I`" in blocks
    assert "标准符号 `Wx`" in blocks
    assert "别名（ORYZABASE）" in blocks
    assert "[MCP-F:268:f_00000000000000g2]" in blocks
    # 参考文献折叠层
    assert '<details class="yuxi-references"><summary>参考文献（1 篇）' in blocks
    assert "PMID 41186968 — Waxy gene regulation（Plant Mol Biol, 2018）" in blocks
    # 跨源一致性核验折叠层
    assert '<details class="yuxi-qc"><summary>跨源一致性核验（1 条）' in blocks
    assert "interval_length_v1: 5035 bp（end - start + 1 (one_based_inclusive)）" in blocks
    # 洁净度：显式跳过域的内部标识不上屏
    assert "prv_2049_ab" not in blocks


def test_profile_coverage_four_buckets_account_for_every_fact():
    """P6 覆盖断言（golden）：四桶并集 == manifest 全集（含空值事实）。

    取全必显全的机械化保证——装配器新增域未登记时按
    no_render_sink_registered 计账，本断言直接显红。
    """
    profile_segment = _golden_profile_projection().segments[0]
    coverage = profile_segment.coverage
    bucket_ids = set(coverage["main"]) | set(coverage["boundary"]) | set(coverage["folded"])
    skipped_ids = {fact_id for fact_id, _reason in coverage["skipped"]}
    all_ids = {fact["id"] for fact in _PROFILE_GOLDEN_FACTS}
    assert bucket_ids | skipped_ids == all_ids

    skip_reasons = {fact_id: reason for fact_id, reason in coverage["skipped"]}
    assert skip_reasons["f_00000000000000gd"] == "internal_provenance_ids"  # evidence_refs
    assert skip_reasons["f_00000000000000ge"] == "aggregation_intermediate"  # compare
    assert skip_reasons["f_00000000000000gf"] == "null_or_digest_only"  # 空值事实
    # golden 域全部登记（不允许出现未注册域）
    assert not [reason for reason in skip_reasons.values() if reason == "no_render_sink_registered"]
    # 注册为主表但未渲染的只允许触发字段（entity_key 仅作单实体判定）
    unrendered = {fid for fid, r in skip_reasons.items() if r.startswith("registered_")}
    assert unrendered <= {"f_00000000000000e1"}
