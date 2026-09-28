"""图表引用锚点通道单测（ADR-0008 P1）。

覆盖：多提及提取、注册表构建（题注血统/Binding 权威/跨文献歧义）、签发即替代、
幂等、结构感知、伪造芯片（确定性等价判据）、历史回流折叠、步骤 9 marker-aware
保护（本轮设计核验出的"不修就出事故"回归锁）、开关关闭零变化、芯片回读 join。
"""

from __future__ import annotations

import pytest

from yuxi.knowledge.evidence.caption_locator import iter_figure_labels
from yuxi.knowledge.rendering.authority_markers import (
    AuthorityMarkerKind,
    count_authority_markers,
    parse_authority_markers,
)
from yuxi.knowledge.rendering.citation_channel import (
    apply_citation_channel,
    sanitize_history_text,
)
from yuxi.knowledge.rendering.figure_ref_channel import (
    MAX_FIGURE_REF_KEYS,
    build_caption_registry,
    match_chips_to_registry,
    parse_figure_ref_chips,
    render_figure_ref_chip,
    resolve_figure_refs,
)

pytestmark = [pytest.mark.unit]


def _caption_citation(
    ref: str,
    label: str,
    *,
    file_id: str = "file_a",
    filename: str = "paper_a.pdf",
    page: int = 8,
    anchor: str = "ea_anchor_a",
    revision: str = "rev_1",
) -> dict:
    quote = f"{label}. Phenotype description of the cited figure."
    return {
        "ref": ref,
        "evidence_id": f"ev_{ref}",
        "kb_id": "kb_1",
        "file_id": file_id,
        "filename": filename,
        "zone": "MAIN_TEXT",
        "page_numbers": [page],
        "primary_page": page,
        "quote_head": quote[:80],
        "anchor_ids": [anchor],
        "locatable": True,
        "toc_line": False,
        "secondary_of": None,
        "_quote": quote,
        "_quote_norm": quote.lower(),
        "_anchor_id": anchor,
        "_parse_revision_id": revision,
        "_evidence_type": "caption",
    }


# ---- 多提及提取（设计验收清单：Figures 2A–2C / 并列 / 中文 / 限定词）----


def test_iter_figure_labels_multi_mention_shapes():
    labels = iter_figure_labels(
        "A（Figure 2 and Figure 4）；Figures 2A–2C；[Figure 2, Table 1]；"
        "图3A、3B以及表2；Supplementary Figure 4；Extended Data Fig. 2"
    )
    keys = [item["base_key"] for item in labels]
    assert keys == [
        "figure 2",
        "figure 4",
        "figure 2",
        "figure 2",
        "table 1",
        "figure 3",
        "table 2",
        "figure s4",
        "figure ed2",
    ]
    panels = {item["raw"]: item["panel"] for item in labels}
    assert panels["图3A"] == "a"  # panel 字母与绑定键分离（P3 panel 联动用）


def test_verified_supplementary_range_expands_and_signs_every_member():
    citations = [
        _caption_citation(f"E{index}", f"Figure S{number}", anchor=f"ea_s{number}")
        for index, number in enumerate(range(17, 21), start=1)
    ]

    rendered, validation = apply_citation_channel(
        "编号靠后的补充图包括 Figure S17–S20。",
        citations,
        figure_refs_enabled=True,
    )

    assert validation["figure_ranges_expanded"] == 1
    assert [ref["label"] for ref in validation["figure_refs"]] == [
        "Figure S17",
        "Figure S18",
        "Figure S19",
        "Figure S20",
    ]
    assert rendered.count("〔图表F") == 4


def test_unique_supplement_inventory_gap_is_repaired_from_frozen_captions():
    citations = [
        _caption_citation(f"E{index}", f"Figure S{number}", anchor=f"ea_s{number}")
        for index, number in enumerate(range(17, 21), start=1)
    ]
    draft = (
        "- Figure S18：Phenotype description of the cited figure.\n"
        "- Figure S19：Phenotype description of the cited figure.\n"
        "- Figure S20：Phenotype description of the cited figure.\n\n"
        "这四张补充图共同支持主结论。"
    )

    rendered, validation = apply_citation_channel(
        draft,
        citations,
        figure_refs_enabled=True,
        semantic_question="编号靠后的补充图有什么作用？请指明是哪几张图。",
    )

    repair = validation["supplement_inventory_repair"]
    assert repair["status"] == "REPAIRED"
    assert repair["added_labels"] == ["Figure S17"]
    assert [ref["label"] for ref in validation["figure_refs"]] == [
        "Figure S17",
        "Figure S18",
        "Figure S19",
        "Figure S20",
    ]
    assert rendered.count("〔图表F") == 4


def test_supplement_inventory_repair_fails_closed_when_two_ranges_fit():
    citations = [
        _caption_citation(f"E{index}", f"Figure S{number}", anchor=f"ea_s{number}")
        for index, number in enumerate(range(17, 22), start=1)
    ]
    draft = "Figure S18、Figure S19、Figure S20。这四张补充图共同支持主结论。"

    rendered, validation = apply_citation_channel(
        draft,
        citations,
        figure_refs_enabled=True,
        semantic_question="请指明是哪几张补充图。",
    )

    assert validation["supplement_inventory_repair"]["status"] == "AMBIGUOUS"
    assert "Figure S17" not in rendered
    assert "Figure S21" not in rendered


def test_aggregate_only_supplement_list_gets_verified_caption_inventory():
    citations = [
        _caption_citation(f"E{index}", f"Figure S{number}", anchor=f"ea_s{number}")
        for index, number in enumerate(range(17, 21), start=1)
    ]
    draft = "综上，Figure S17、Figure S18、Figure S19、Figure S20 这四张补充图共同支持主结论。"

    rendered, validation = apply_citation_channel(
        draft,
        citations,
        figure_refs_enabled=True,
        semantic_question="请指明是哪几张补充图。",
    )

    repair = validation["supplement_inventory_repair"]
    assert repair["status"] == "ENRICHED"
    assert repair["inventory_labels"] == ["Figure S17", "Figure S18", "Figure S19", "Figure S20"]
    assert "### 已核验图注清单" in rendered
    assert rendered.count("Phenotype description of the cited figure.") == 4


def test_parse_authority_markers_figure_reference_kind():
    text = "见〔图表F1｜Figure 2〕与〔图表F12｜表S1〕"
    markers = parse_authority_markers(text)
    assert [m["kind"] for m in markers] == [AuthorityMarkerKind.FIGURE_REFERENCE] * 2
    assert [m["ref"] for m in markers] == ["F1", "F12"]
    assert count_authority_markers(text)["FIGURE_REFERENCE"] == 2


# ---- 注册表：可信作用域 ----


def test_registry_caption_rows_and_midtext_rejection():
    caption = _caption_citation("E1", "Figure 2")
    midtext = _caption_citation("E2", "Figure 3")
    midtext["_quote"] = "The expression analysis (see Figure 3) shows significant changes."
    midtext["_evidence_type"] = None
    registry = build_caption_registry([caption, midtext])
    assert "figure 2" in registry
    assert "figure 3" not in registry  # 句中提及不是题注，不构成锚定来源


def test_registry_toc_line_excluded():
    toc = _caption_citation("E1", "Figure 2")
    toc["toc_line"] = True
    assert build_caption_registry([toc]) == {}


def test_registry_cross_file_same_label_is_ambiguous():
    a = _caption_citation("E1", "Figure 2", file_id="file_a", anchor="ea_a")
    b = _caption_citation("E2", "Figure 2", file_id="file_b", anchor="ea_b")
    registry = build_caption_registry([a, b])
    assert registry["figure 2"]["ambiguous"] is True
    stats = resolve_figure_refs("见 Figure 2", [a, b])["stats"]
    assert stats["unresolved"]["ambiguous_scope"] == 1


def test_registry_same_scope_duplicate_keeps_informative_locatable_caption():
    weak = _caption_citation("E1", "Figure 2", anchor="")
    weak["_quote"] = "Figure 2. Overview."
    weak["quote_head"] = weak["_quote"]
    strong = _caption_citation("E2", "Figure 2", anchor="ea_strong")
    strong["_quote"] = "Figure 2. Detailed CRISPR mutant grain phenotype and chalkiness evaluation."
    strong["quote_head"] = strong["_quote"]
    registry = build_caption_registry([weak, strong])
    assert registry["figure 2"]["anchor_id"] == "ea_strong"
    assert "chalkiness" in registry["figure 2"]["_caption_quote"]
    assert registry["figure 2"]["ambiguous"] is False


def test_registry_splits_two_captions_merged_in_one_physical_anchor():
    merged = _caption_citation("E1", "Figure S5", anchor="ea_merged")
    merged["_quote"] = (
        "Figure S5. SEM and TEM observations of mature endosperm. "
        "Figure S6. Rice starch particles of WT and cr-myb73 mutants."
    )
    merged["quote_head"] = merged["_quote"]
    registry = build_caption_registry([merged])
    assert set(registry) == {"figure s5", "figure s6"}
    assert registry["figure s5"]["anchor_id"] == registry["figure s6"]["anchor_id"] == "ea_merged"
    assert "starch particles" in registry["figure s6"]["_caption_quote"]


def test_registry_binding_authority_overrides_ambiguity():
    a = _caption_citation("E1", "Figure 2", file_id="file_a", anchor="ea_a")
    b = _caption_citation("E2", "Figure 2", file_id="file_b", anchor="ea_b")
    binding = {
        "binding": {
            "binding_id": "vlb_" + "1" * 20,
            "status": "VERIFIED",
            "locator_kind": "FIGURE_CAPTION",
            "physical_evidence_id": "ev_binding",
            "anchor_id": "ea_c",
            "file_id": "file_c",
            "filename": "paper_c.pdf",
            "parse_revision_id": "rev_2",
            "kb_id": "kb_1",
            "page_number": 8,
            "page_binding": "VERIFIED",
            "figure_identity_binding": "VERIFIED",
            "quote_head": "Figure 2. Phenotype description of the cited figure.",
        }
    }
    registry = build_caption_registry([a, b], locator=binding)
    assert registry["figure 2"]["binding_authority"] is True
    assert registry["figure 2"]["ambiguous"] is False
    assert registry["figure 2"]["source"] == "binding"


# ---- 解析与预算 ----


def test_resolve_budget_caps_distinct_keys():
    citations = [_caption_citation(f"E{i}", f"Figure {i}", anchor=f"ea_{i}") for i in range(1, 12)]
    text = "、".join(f"Figure {i}" for i in range(1, 12))
    plan = resolve_figure_refs(text, citations)
    assert len(plan["refs"]) == MAX_FIGURE_REF_KEYS
    assert plan["stats"]["unresolved"]["budget_exceeded"] == 3


def test_resolve_no_registry_mentions_stay_unresolved():
    plan = resolve_figure_refs("见 Figure 9", [_caption_citation("E1", "Figure 2")])
    assert plan["refs"] == []
    assert plan["stats"]["unresolved"]["no_registry_match"] == 1


# ---- 签发（apply_citation_channel 集成）----


_CITATIONS = [
    _caption_citation("E1", "Figure 2", page=8, anchor="ea_fig2"),
    _caption_citation("E2", "Table 1", page=11, anchor="ea_tab1"),
]


def test_sign_replaces_mentions_in_place_with_run_local_refs():
    text = "表型变化见 Figure 2，理化性质见 Table 1，另见 Figure 9。"
    signed, validation = apply_citation_channel(text, _CITATIONS, figure_refs_enabled=True)
    assert "〔图表F1｜Figure 2〕" in signed
    assert "〔图表F2｜Table 1〕" in signed
    assert "Figure 9" in signed  # 未解析提及不签发，保持纯文本
    refs = validation["figure_refs"]
    assert [(r["ref"], r["key"], r["kind"], r["source"], r["page"]) for r in refs] == [
        ("F1", "figure 2", "figure", "caption", 8),
        ("F2", "table 1", "table", "caption", 11),
    ]
    assert validation["figure_ref_stats"]["signed"] == 2


def test_switch_off_signing_adds_only_chips():
    text = "表型变化见 Figure 2，理化性质见 Table 1。"
    # 开关关闭：签发步骤零参与（reverse_bind 的未定位提示是既有行为，与本特性无关）
    off_text, off_validation = apply_citation_channel(text, _CITATIONS, figure_refs_enabled=False)
    assert off_validation["figure_refs"] == []
    assert "〔图表" not in off_text
    # 开关开启：与关闭版本的差异**仅限**签发芯片本身（暗发布纪律：关=历史行为）
    on_text, _ = apply_citation_channel(text, _CITATIONS, figure_refs_enabled=True)
    stripped = on_text.replace("〔图表F1｜Figure 2〕", "Figure 2").replace("〔图表F2｜Table 1〕", "Table 1")
    # D3 未核验标记是**另一条正交特性**（2026-09-26 段内下沉）：开关关闭时
    # 裸标签无权威绑定 → 该段被标（未核验）；开启后同段拿到芯片 → 不标。
    # 归一掉这一项后仍必须逐字节一致，即"除芯片与该标记外无任何内容漂移"。
    assert stripped.replace("（未核验）", "") == off_text.replace("（未核验）", "")
    # 顺带锁定该交互：关闭态确实带标记、开启态不带
    assert "（未核验）表型变化见 Figure 2" in off_text
    assert "（未核验）" not in on_text


def test_repeated_mentions_share_one_ref_number():
    signed, validation = apply_citation_channel("先见图 Figure 2，再见 Figure 2", _CITATIONS, figure_refs_enabled=True)
    assert signed.count("〔图表F1｜Figure 2〕") == 2
    assert "F2" not in signed
    assert validation["figure_refs"][0]["occurrences"] == 2


def test_guard_double_application_is_idempotent():
    signed, _ = apply_citation_channel("表型变化见 Figure 2。", _CITATIONS, figure_refs_enabled=True)
    twice, _ = apply_citation_channel(signed, _CITATIONS, figure_refs_enabled=True)
    assert twice == signed


def test_structure_aware_no_chips_in_tables_or_codefences():
    text = "段中 Figure 2。\n\n| 项目 | Figure 2 |\n|---|---|\n| a | b |\n\n```\nFigure 2\n```"
    signed, _ = apply_citation_channel(text, _CITATIONS, figure_refs_enabled=True)
    assert signed.count("〔图表") == 1


def test_fabricated_figure_chip_unresolvable_is_stripped():
    signed, validation = apply_citation_channel("见〔图表F1｜Figure 9〕所示。", _CITATIONS, figure_refs_enabled=True)
    assert "〔图表" not in signed
    assert "Figure 9" in signed
    assert validation["fabrication"]["fabricated_figure_removed"] == 1


def test_fabricated_figure_chip_resolvable_is_reissued_by_backend():
    # 模型无权选择 F#：即便标签可解析，也必须先剥壳再由后端从 F1 重签。
    signed, validation = apply_citation_channel("见〔图表F9｜Figure 2〕所示。", _CITATIONS, figure_refs_enabled=True)
    assert "〔图表F1｜Figure 2〕" in signed
    assert "F9" not in signed
    assert validation["fabrication"]["fabricated_figure_removed"] == 1


def test_citations_revoked_policy_signs_nothing():
    policy = {"document_citations_allowed": False, "figure_label_allowed": False}
    signed, validation = apply_citation_channel(
        "见 Figure 2", _CITATIONS, authority_policy=policy, figure_refs_enabled=True
    )
    assert "〔图表" not in signed
    assert validation["figure_refs"] == []


def test_step9_strip_is_marker_aware_chips_survive():
    # 回归锁：figure_label_allowed=False 时裸提及剥离，但签发芯片载荷里的
    # "Figure 2" 绝不能被误删成〔图表F1｜〕（瞬态保护救不了步骤 9）
    policy = {"figure_label_allowed": False}
    signed, _ = apply_citation_channel(
        "见图〔图表F1｜Figure 2〕与裸的 Figure 9", _CITATIONS, authority_policy=policy, figure_refs_enabled=True
    )
    assert "〔图表F1｜Figure 2〕" in signed
    assert "Figure 9" not in signed


def test_sanitize_history_folds_figure_chips():
    signed, _ = apply_citation_channel("表型变化见 Figure 2。", _CITATIONS, figure_refs_enabled=True)
    sanitized = sanitize_history_text(signed)
    assert "〔图表" not in sanitized
    assert "[citation omitted]" in sanitized


# ---- 芯片回读（chat 层 figure_refs 载荷的入口）----


def test_parse_and_match_chips_roundtrip():
    signed, _ = apply_citation_channel("表型变化见 Figure 2，见 Table 1。", _CITATIONS, figure_refs_enabled=True)
    chips = parse_figure_ref_chips(signed)
    assert [(c["ref"], c["label"]) for c in chips] == [("F1", "Figure 2"), ("F2", "Table 1")]
    matched = match_chips_to_registry(signed, _CITATIONS, None)
    assert matched["stats"]["matched"] == 2
    assert matched["stats"]["orphan"] == 0
    assert {r["ref"]: r["page"] for r in matched["refs"]} == {"F1": 8, "F2": 11}


def test_match_chips_orphan_chip_not_published():
    matched = match_chips_to_registry("见〔图表F1｜Figure 9〕", _CITATIONS, None)
    assert matched["refs"] == []
    assert matched["stats"]["orphan"] == 1


def test_ref_returns_are_json_serializable():
    """回归锁：解析/回读返回值不得携带内部字段（scopes 是 set，整包 json.dumps 会炸）。"""
    import json

    signed, _ = apply_citation_channel("表型变化见 Figure 2，见 Table 1。", _CITATIONS, figure_refs_enabled=True)
    plan = resolve_figure_refs("见 Figure 2 与 Table 1", _CITATIONS)
    json.dumps(plan["refs"])  # 不抛即通过
    for ref in plan["refs"]:
        assert "scopes" not in ref and "ambiguous" not in ref and "binding_authority" not in ref
    matched = match_chips_to_registry(signed, _CITATIONS, None)
    json.dumps(matched["refs"])
    assert "scopes" not in matched["refs"][0]


def test_match_chips_dedupes_shared_ref_number():
    """E2E 实测回归锁：同键多次提及共享同一芯片编号（正文两枚 F1），载荷必须一条。"""
    signed, _ = apply_citation_channel("先见图 Figure 2，再见 Figure 2 一次。", _CITATIONS, figure_refs_enabled=True)
    assert signed.count("〔图表F1｜Figure 2〕") == 2
    matched = match_chips_to_registry(signed, _CITATIONS, None)
    refs = [ref["ref"] for ref in matched["refs"]]
    assert refs == ["F1"]  # 不重复
    assert matched["stats"]["matched"] == 1


def test_render_chip_form_matches_authority_pattern():
    chip = render_figure_ref_chip("F1", "Figure 2")
    assert chip == "〔图表F1｜Figure 2〕"
    markers = parse_authority_markers(chip)
    assert len(markers) == 1 and markers[0]["kind"] == AuthorityMarkerKind.FIGURE_REFERENCE


def test_structured_table_gate_reconstructs_source_line_from_supported_table():
    table = {
        "label": "Table 1",
        "header_rows": 2,
        "rows": [
            [
                {"text": "Genotype", "rowspan": 2},
                {"text": "Length (mm)", "colspan": 2},
            ],
            [{"text": "Control"}, {"text": "Heat"}],
            [{"text": "WT (ZH11)"}, {"text": "5.12"}, {"text": "4.71"}],
        ],
    }
    signed, validation = apply_citation_channel(
        "WT 在热胁迫下粒长为 4.71 mm。",
        _CITATIONS,
        figure_refs_enabled=True,
        semantic_tables=[table],
        semantic_question="解释热胁迫下的粒长差异",
        table_semantic_ready=True,
    )
    assert "> 数据出处：〔图表F1｜Table 1〕。" in signed
    assert validation["table_claim_validation"]["supporting_table_labels"] == ["Table 1"]


# ---- 协议与白名单（figures 事故红线同款锁）----


def test_compact_whitelist_contains_figure_refs():
    from yuxi.services.agent_run_service import COMPACT_CHUNK_FIELDS

    assert "figure_refs" in COMPACT_CHUNK_FIELDS


def test_protocol_capability_and_version():
    from yuxi.services.agent_protocol import AGENT_RUN_CAPABILITIES, AGENT_RUN_PROTOCOL_VERSION

    assert "figure_refs" in AGENT_RUN_CAPABILITIES
    assert "table_cards" in AGENT_RUN_CAPABILITIES
    assert AGENT_RUN_PROTOCOL_VERSION == "1.8"


def test_config_switch_defaults_off_and_independent():
    from yuxi.config.app import Config

    assert Config.model_fields["figure_ref_anchor_enabled"].default is False
    assert Config.model_fields["figure_card_enabled"].default is False


def test_trace_events_registered():
    from yuxi.trace.protocol import EVENT_ATTRIBUTE_SCHEMAS, EVENT_EMITTER_INDEX

    for event_type in ("knowledge.figure_ref.resolved", "knowledge.figure_ref.unresolved"):
        assert event_type in EVENT_ATTRIBUTE_SCHEMAS
        assert event_type in EVENT_EMITTER_INDEX
