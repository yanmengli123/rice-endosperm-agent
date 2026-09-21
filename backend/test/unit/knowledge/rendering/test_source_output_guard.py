from yuxi.knowledge.rendering.source_output_guard import (
    guard_answer_for_evidence_level,
    guard_glossary_answer,
    guard_non_document_source_answer,
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
