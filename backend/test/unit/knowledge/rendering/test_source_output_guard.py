from yuxi.knowledge.rendering.source_output_guard import (
    fact_catalog_summary,
    guard_answer_for_evidence_level,
    guard_glossary_answer,
    guard_non_document_source_answer,
    render_degraded_fact_sheet,
)


def test_mcp_only_answer_cannot_emit_document_evidence_affordances():
    guarded, audit = guard_non_document_source_answer(
        "Wx 的结构化记录如下 [E2]，位于第 17 页。"
        "〔证据E2｜补充材料·第17页｜paper.pdf〕\n\n"
        "【证据引用】（后端渲染）\n- E2｜正文·第17页｜paper.pdf｜ea_1234567890abcdef"
    )

    assert "[E2]" not in guarded
    assert "证据E2" not in guarded
    assert "【证据引用】" not in guarded
    assert "ea_123" not in guarded
    assert "第 17 页" not in guarded
    assert "当前数据来源不提供 PDF 物理页码" in guarded
    assert audit["evidence_refs_removed"] == 1
    assert audit["reference_blocks_removed"] == 1


def test_source_only_attestation_requires_adopted_successful_ricekb_call():
    guarded, audit = guard_answer_for_evidence_level(
        "数据模式：SOURCE-ONLY\nWx 位于第 3 页。",
        evidence_level="E1_DATA_PROVENANCE",
        source_uses=[],
        requires_mcp=True,
    )

    assert "Wx 位于" not in guarded
    assert "未通过 MCP 事实级核验" in guarded
    assert audit["status"] == "REJECTED"
    assert audit["source_only_verified"] is False


def test_source_only_attestation_accepts_fact_grounded_ricekb_source_and_removes_pdf_claims():
    guarded, audit = guard_answer_for_evidence_level(
        "数据模式：SOURCE-ONLY\nWx 的结构化记录已找到。[MCP-F:42:f_1234567890abcdef]",
        evidence_level="E1_DATA_PROVENANCE",
        source_uses=[
            {
                "source_use_id": "mcp:42",
                "provider_id": "ricekb",
                "status": "SUCCESS",
                "adopted": True,
                "provenance": {
                    "mcp_call_audit_id": 42,
                    "fact_manifest": {
                        "facts": [
                            {
                                "id": "f_1234567890abcdef",
                                "path": "/status",
                                "value_digest": "sha256:test",
                            }
                        ]
                    },
                },
            }
        ],
    )

    assert guarded.startswith("数据模式：SOURCE-ONLY")
    assert audit["status"] == "PASSED"
    assert audit["source_only_verified"] is True
    assert audit["fact_grounding"]["passed"] is True


def test_source_only_rejects_wrong_coordinate_arithmetic_even_with_valid_source_call():
    guarded, audit = guard_answer_for_evidence_level(
        "数据模式：SOURCE-ONLY\n区间长度是 3 bp。[MCP-F:42:f_1234567890abcdef]",
        evidence_level="E1_DATA_PROVENANCE",
        source_uses=[
            {
                "source_use_id": "mcp:42",
                "status": "SUCCESS",
                "adopted": True,
                "provenance": {
                    "mcp_call_audit_id": 42,
                    "fact_manifest": {
                        "facts": [
                            {
                                "id": "f_1234567890abcdef",
                                "path": "/data/start",
                                "numeric_value": 1770556,
                            }
                        ]
                    },
                },
            }
        ],
    )

    assert "区间长度是 3 bp" not in guarded
    assert audit["status"] == "REJECTED"
    assert audit["fact_grounding"]["unsupported_numbers"] == [{"line": 2, "value": "3"}]


def test_e4_keeps_verified_document_affordances():
    text = "原文位于第 8 页 [E1]。"
    guarded, audit = guard_answer_for_evidence_level(text, evidence_level="E4_VERBATIM_LOCATOR")

    assert guarded == text
    assert audit["document_affordance_guard"]["applied"] is False


def test_glossary_miss_replaces_model_definition_with_closed_world_disclosure():
    guarded, audit = guard_glossary_answer(
        "OASIS 是某个模型猜测的缩写。〔证据E1｜第3页〕",
        contract={
            "knowledge_scope_snapshot": {"kb_ids": ["kb_glossary"]},
            "authority_decision": {
                "authority_kind": "GLOSSARY",
                "outcome": "MISS",
                "lookup_terms": ["OASIS"],
            },
            "evidence": [],
        },
    )

    assert guarded == "词典核验：未收录\n\n当前运行范围内的活动术语词典版本未收录“OASIS”。"
    assert "某个模型猜测" not in guarded
    assert "证据" not in guarded
    assert audit["authority_outcome"] == "MISS"
    assert audit["model_text_replaced"] is True


def test_glossary_hit_publishes_only_in_scope_rows_with_data_provenance():
    guarded, audit = guard_glossary_answer(
        "invented",
        contract={
            "knowledge_scope_snapshot": {"kb_ids": ["kb_ok"]},
            "authority_decision": {"authority_kind": "GLOSSARY", "outcome": "HIT"},
            "evidence": [
                {
                    "evidence_id": "canonical:rev:rec",
                    "kb_id": "kb_ok",
                    "content": "term：PCR\ndefinition：Polymerase chain reaction",
                    "record_key": "PCR",
                    "row_number": 2,
                    "revision_id": "rev_1",
                },
                {
                    "evidence_id": "canonical:bad:rec",
                    "kb_id": "kb_deleted",
                    "content": "must not publish",
                },
            ],
        },
    )

    assert "Polymerase chain reaction" in guarded
    assert "kb_id=kb_ok" in guarded
    assert "revision=rev_1" in guarded
    assert "kb_deleted" not in guarded
    assert audit["out_of_scope_evidence_removed"] == 1


def _fact_source_uses(*facts: dict) -> list[dict]:
    return [
        {
            "source_use_id": "mcp:42",
            "provider_id": "ricekb",
            "operation": "ricekb_entity",
            "status": "SUCCESS",
            "adopted": True,
            "provenance": {
                "mcp_call_audit_id": 42,
                "fact_manifest": {"facts": list(facts)},
            },
        }
    ]


def test_attached_unit_and_cjk_adjacent_numbers_must_be_supported():
    uses = _fact_source_uses({"id": "f_1234567890abcdef", "path": "/data/start", "numeric_value": 1770556})
    for claim in ("区间长度是3bp。[MCP-F:42:f_1234567890abcdef]", "差值为3个碱基。[MCP-F:42:f_1234567890abcdef]"):
        guarded, audit = guard_answer_for_evidence_level(
            f"数据模式：SOURCE-ONLY\n{claim}",
            evidence_level="E1_DATA_PROVENANCE",
            source_uses=uses,
        )
        assert audit["fact_grounding"]["passed"] is False
        assert {"line": 2, "value": "3"} in audit["fact_grounding"]["unsupported_numbers"]


def test_supported_unit_number_and_cjk_adjacent_supported_value_pass():
    uses = _fact_source_uses({"id": "f_1234567890abcdef", "path": "/data/length", "numeric_value": 98})
    guarded, audit = guard_answer_for_evidence_level(
        "数据模式：SOURCE-ONLY\n区间长度是98bp。[MCP-F:42:f_1234567890abcdef]",
        evidence_level="E1_DATA_PROVENANCE",
        source_uses=uses,
    )
    assert audit["fact_grounding"]["passed"] is True
    guarded, audit = guard_answer_for_evidence_level(
        "数据模式：SOURCE-ONLY\n跨度为98个碱基。[MCP-F:42:f_1234567890abcdef]",
        evidence_level="E1_DATA_PROVENANCE",
        source_uses=uses,
    )
    assert audit["fact_grounding"]["passed"] is True


def test_datetime_and_heading_numbers_are_structural_not_claims():
    uses = _fact_source_uses(
        {"id": "f_1234567890abcdef", "path": "/retrieved_at", "string_value": "2026-09-21T07:12:43+00:00"}
    )
    guarded, audit = guard_answer_for_evidence_level(
        "数据模式：SOURCE-ONLY\n## 3. 序列核验\n检索时间：2026-09-21T07:12:43+00:00。[MCP-F:42:f_1234567890abcdef]",
        evidence_level="E1_DATA_PROVENANCE",
        source_uses=uses,
    )
    assert audit["fact_grounding"]["passed"] is True


def test_render_degraded_fact_sheet_is_deterministic_and_marker_bearing():
    uses = _fact_source_uses(
        {"id": "f_1234567890abcdef", "path": "/gene/start", "numeric_value": 1770556},
        {"id": "f_1234567890ffffff", "path": "/gene/symbol", "string_value": "Wx"},
    )
    sheet = render_degraded_fact_sheet(uses)
    assert sheet is not None
    assert sheet.startswith("数据模式：SOURCE-ONLY")
    assert "[MCP-F:42:f_1234567890abcdef]" in sheet
    assert "1770556" in sheet
    assert "Wx" in sheet
    assert render_degraded_fact_sheet([]) is None


def test_fact_catalog_summary_exposes_values_for_repair_prompt():
    uses = _fact_source_uses(
        {"id": "f_1234567890abcdef", "path": "/gene/start", "numeric_value": 1770556},
        {"id": "f_1234567890ffffff", "path": "/gene/symbol", "string_value": "Wx"},
    )
    summary = fact_catalog_summary(uses)
    assert {"marker": "[MCP-F:42:f_1234567890abcdef]", "path": "/gene/start", "numeric_value": 1770556} in summary
    assert {"marker": "[MCP-F:42:f_1234567890ffffff]", "path": "/gene/symbol", "string_value": "Wx"} in summary


def test_factless_adopted_sources_carry_no_fact_obligation():
    uses = [
        {
            "source_use_id": "mcp:7",
            "provider_id": "custom",
            "operation": "anything",
            "status": "SUCCESS",
            "adopted": True,
            "provenance": {"mcp_call_audit_id": 7},
        }
    ]
    guarded, audit = guard_answer_for_evidence_level(
        "数据模式：SOURCE-ONLY\n自定义工具返回了区间长度 3 bp。",
        evidence_level="E1_DATA_PROVENANCE",
        source_uses=uses,
    )
    assert audit["fact_grounding"]["required"] is False
    assert audit["fact_grounding"]["passed"] is True
    assert audit["source_only_verified"] is True


# ── AUTO 轮门禁分派（P0-B5）：叙述行豁免、数字行仍严格 ───────────


def test_auto_policy_exempts_narrative_lines_but_keeps_numeric_lines_strict():
    uses = _fact_source_uses({"id": "f_1234567890abcdef", "path": "/gene/symbol", "string_value": "Wx"})
    text = "数据模式：SOURCE-ONLY\n该基因在胚乳中高表达。[MCP-F:42:f_1234567890abcdef]\n基因全长 1860 bp。\n"
    # 无分派（默认严格口径）：叙述行+数字行都要求标记 → 拒绝
    _, strict = guard_answer_for_evidence_level(text, evidence_level="E1_DATA_PROVENANCE", source_uses=uses)
    assert strict["status"] == "REJECTED"
    # AUTO 分派：叙述行豁免，但数字行（1860 bp 无标记）仍必拒——数字口径不放松
    _, relaxed = guard_answer_for_evidence_level(
        text, evidence_level="E1_DATA_PROVENANCE", source_uses=uses, source_policy="AUTO"
    )
    assert relaxed["status"] == "REJECTED"
    assert relaxed["fact_grounding"]["relaxed"] is True
    assert 3 in relaxed["fact_grounding"]["ungrounded_lines"]
    assert 2 not in relaxed["fact_grounding"]["ungrounded_lines"]


def test_auto_policy_narrative_only_answer_passes():
    uses = _fact_source_uses({"id": "f_1234567890abcdef", "path": "/gene/symbol", "string_value": "Wx"})
    text = (
        "数据模式：SOURCE-ONLY\n"
        "该基因在胚乳中高表达，参与淀粉合成调控。[MCP-F:42:f_1234567890abcdef]\n"
        "该位点的命名历史与各来源收录情况见下方记录。\n"
    )
    _, relaxed = guard_answer_for_evidence_level(
        text, evidence_level="E1_DATA_PROVENANCE", source_uses=uses, source_policy="AUTO"
    )
    assert relaxed["status"] == "PASSED"
    assert relaxed["fact_grounding"]["relaxed"] is True
    # 同一答案在显式数据库轮（无分派）仍被逐行拒绝——AUTO 边界由测试锁定
    _, strict = guard_answer_for_evidence_level(text, evidence_level="E1_DATA_PROVENANCE", source_uses=uses)
    assert strict["status"] == "REJECTED"


def test_auto_relaxation_keeps_gene_identifier_lines_strict():
    uses = _fact_source_uses({"id": "f_1234567890abcdef", "path": "/gene/symbol", "string_value": "Wx"})
    text = "数据模式：SOURCE-ONLY\n该位点对应 LOC_Os06g0133000。\n"
    _, relaxed = guard_answer_for_evidence_level(
        text, evidence_level="E1_DATA_PROVENANCE", source_uses=uses, source_policy="AUTO"
    )
    # 基因标识符是可核验主张：叙述豁免不覆盖标识符行
    assert relaxed["status"] == "REJECTED"


# ── OFF 轮误声明剥标签（P0 残留 1）与冒号行核验收窄（P0 残留 2a）───────────


def test_off_turn_misdeclared_source_only_is_stripped_not_rejected():
    guarded, audit = guard_answer_for_evidence_level(
        "数据模式：SOURCE-ONLY\n当前可用技能：rice-source-agent、knowledge-base。[MCP-F:99:f_0000000000000000]",
        evidence_level="E3_CLAIM_EVIDENCE",
        source_uses=[],
        source_policy="AUTO",
        requires_mcp=False,
    )
    # OFF 轮（无 adopted 源、plan 未要求 MCP）：声明与必无效标记被剥除，内容照常发布
    assert "SOURCE-ONLY" not in guarded
    assert "[MCP-F:" not in guarded
    assert "rice-source-agent" in guarded
    assert audit["status"] == "PASSED"
    assert audit["source_only_stripped"] is True


def test_off_turn_without_declaration_is_untouched():
    text = "Oryza sativa 是栽培稻的学名。"
    guarded, audit = guard_answer_for_evidence_level(
        text, evidence_level="E3_CLAIM_EVIDENCE", source_uses=[], source_policy="AUTO"
    )
    assert guarded == text
    assert audit["status"] == "PASSED"
    assert audit["source_only_stripped"] is False


def test_strict_turn_misdeclared_source_only_still_rejected():
    guarded, audit = guard_answer_for_evidence_level(
        "数据模式：SOURCE-ONLY\nWx 的结构化记录。",
        evidence_level="E1_DATA_PROVENANCE",
        source_uses=[],
        source_policy="MCP_ONLY",
        requires_mcp=True,
    )
    # STRICT 轮的声明承载真实核验义务：剥标签等于放行未核验数据，必须保持整杀
    assert "未通过 MCP 事实级核验" in guarded
    assert audit["status"] == "REJECTED"
    assert audit["source_only_stripped"] is False


def test_off_turn_declaration_only_answer_falls_back_to_rejection():
    guarded, audit = guard_answer_for_evidence_level(
        "数据模式：SOURCE-ONLY",
        evidence_level="E1_DATA_PROVENANCE",
        source_uses=[],
        requires_mcp=False,
    )
    # 剥除后无任何实质内容：没有可发布的东西，退回拒绝文案而非发布空白
    assert "未通过 MCP 事实级核验" in guarded
    assert audit["status"] == "REJECTED"


def test_marker_bearing_label_line_loses_colon_exemption():
    uses = _fact_source_uses({"id": "f_1234567890abcdef", "path": "/data/length", "numeric_value": 98})
    _, audit = guard_answer_for_evidence_level(
        "数据模式：SOURCE-ONLY\n区间长度 3 bp 的来源见标记 [MCP-F:42:f_1234567890abcdef]：",
        evidence_level="E1_DATA_PROVENANCE",
        source_uses=uses,
        source_policy="MCP_ONLY",
        requires_mcp=True,
    )
    # 带标记的冒号结尾行不再走结构行豁免：行内数字 3 不在所引事实（98）内 → 必拒
    assert audit["fact_grounding"]["passed"] is False
    assert {"line": 2, "value": "3"} in audit["fact_grounding"]["unsupported_numbers"]


def test_markerless_label_line_keeps_colon_exemption():
    uses = _fact_source_uses({"id": "f_1234567890abcdef", "path": "/gene/symbol", "string_value": "Wx"})
    _, audit = guard_answer_for_evidence_level(
        "数据模式：SOURCE-ONLY\n基因符号：Wx [MCP-F:42:f_1234567890abcdef]\n各字段来源：",
        evidence_level="E1_DATA_PROVENANCE",
        source_uses=uses,
        source_policy="MCP_ONLY",
        requires_mcp=True,
    )
    assert audit["fact_grounding"]["passed"] is True
    assert audit["status"] == "PASSED"


def test_degraded_sheet_excludes_error_status_calls():
    uses = [
        {
            "source_use_id": "mcp:7",
            "provider_id": "ricekb",
            "operation": "ricekb_support",
            "status": "ERROR",
            "adopted": False,
            "provenance": {
                "mcp_call_audit_id": 7,
                "fact_manifest": {
                    "facts": [
                        {
                            "id": "f_2bad2a455e6d117a",
                            "path": "/0",
                            "string_value": "Error executing tool ricekb_support: gateway timed out after 20s",
                        }
                    ]
                },
            },
        }
    ]
    # RC5 回归锁：error 状态的调用不进 adopted 集，错误文本不得出现在降级事实清单
    assert render_degraded_fact_sheet(uses) is None
