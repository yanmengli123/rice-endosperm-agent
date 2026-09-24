"""P2 检索升级单测：题型检测、约束式 MMR、context_evidence 验证。"""

from __future__ import annotations

import pytest

from yuxi.knowledge.planning.task_classifier import TASK_CLASSIFIER_VERSION, detect_question_types
from yuxi.knowledge.planning.query_planner import PLANNER_VERSION, plan_knowledge_query
from yuxi.knowledge.validation.context_evidence_validator import validate_context_evidence

pytestmark = [pytest.mark.unit]


def test_detect_question_types_numeric_entity_multi_hop():
    types = detect_question_types("OsMYB73 含有几个 SANT 结构域？分别位于 115-164 和 167-215 氨基酸区间？")
    assert "ENTITY" in types and "NUMERIC" in types and "MULTI_HOP" in types


def test_detect_question_types_defaults_to_fact():
    assert detect_question_types("水稻胚乳如何发育") == ["FACT"]


def test_detect_question_types_figure_table_citation():
    assert "FIGURE" in detect_question_types("Figure 2 展示了什么？")
    assert "TABLE" in detect_question_types("Table 1 中有哪些数据？")
    assert "CITATION" in detect_question_types("doi:10.1111/pbi.14558 这篇文章说了什么")


def test_plan_carries_question_types_and_version():
    plan = plan_knowledge_query("OsMYB73 与 OsbZIP58 的调控关系？", strategy="KNOWLEDGE_FIRST", scope_nonempty=True)
    # 1.3：detect_question_types 新增 VERBATIM 题型（引号原文片段/逐字意图）
    assert PLANNER_VERSION == "1.4" and TASK_CLASSIFIER_VERSION == "1.3"
    assert "question_types" in plan
    assert "MULTI_HOP" in plan["question_types"]


def test_validate_context_evidence_pass_and_failures():
    evidence = [
        {"evidence_id": "ev_1", "content": "OsMYB73 regulates grain filling rate", "kb_id": "kb_a"},
        {"evidence_id": "ev_2", "content": "SANT domains located at 115-164", "kb_id": "kb_a"},
    ]
    validation, warnings = validate_context_evidence(evidence, required_identifiers=["OsMYB73", "SANT"])
    assert validation["status"] == "PASS"
    assert validation["identifier_coverage"] == 1.0
    assert not warnings

    duplicated = [*evidence, dict(evidence[0])]
    validation_dup, warnings_dup = validate_context_evidence(duplicated)
    assert validation_dup["status"] in {"PASS", "DEGRADED"}
    assert "ev_1" in validation_dup["invalid_ids"]
    assert warnings_dup

    with_derived = [
        {"evidence_id": "ev_3", "content": "wiki text", "kb_id": "kb_wiki"},
    ]
    validation_derived, _ = validate_context_evidence(with_derived, derived_kb_ids={"kb_wiki"})
    assert validation_derived["status"] == "FAIL"
    assert validation_derived["derived_source_ids"] == ["ev_3"]

    empty = [{"evidence_id": "ev_4", "content": "  ", "kb_id": "kb_a"}]
    validation_empty, _ = validate_context_evidence(empty)
    assert validation_empty["status"] == "DEGRADED"
    assert validation_empty["empty_content_ids"] == ["ev_4"]


def test_validate_context_evidence_partial_identifier_coverage():
    evidence = [{"evidence_id": "ev_1", "content": "OsMYB73 regulates filling", "kb_id": "kb_a"}]
    validation, warnings = validate_context_evidence(evidence, required_identifiers=["OsMYB73", "SANT"])
    assert validation["identifier_coverage"] == 0.5
    assert any("SANT" in warning for warning in warnings)
