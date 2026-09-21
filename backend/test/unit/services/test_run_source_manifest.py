from __future__ import annotations

from yuxi.knowledge.planning.turn_execution_plan import (
    AuthorityOutcome,
    EvidenceLevel,
    SourceClass,
    TaskIntent,
    plan_turn,
)
from yuxi.services.chat_service import _append_knowledge_source_uses, _initial_source_manifest


def test_glossary_miss_is_recorded_as_a_canonical_authority_decision() -> None:
    plan = plan_turn("Wx 是什么？", has_knowledge_scope=True)
    manifest = _initial_source_manifest(plan)

    _append_knowledge_source_uses(
        manifest,
        plan=plan,
        contract={
            "retrieval_id": "kr-glossary-miss",
            "status": "COMPLETED",
            "evidence": [],
            "authority_decision": {
                "authority_kind": "GLOSSARY",
                "outcome": "MISS",
                "reason_code": "TERM_NOT_FOUND",
                "lookup_terms": ["Wx"],
                "revision_ids": ["dsrev-1"],
            },
        },
    )

    assert plan.task.primary_intent == TaskIntent.GLOSSARY_LOOKUP
    assert len(manifest.source_uses) == 1
    assert manifest.source_uses[0].source_class == SourceClass.CANONICAL_RECORD
    assert manifest.source_uses[0].evidence_level == EvidenceLevel.DATA_PROVENANCE
    assert manifest.source_uses[0].provenance["revision_ids"] == ["dsrev-1"]
    assert manifest.authority_outcomes[0].outcome == AuthorityOutcome.MISS
    assert manifest.authority_outcomes[0].evidence_ids == []


def test_hybrid_knowledge_evidence_is_ledgered_against_literature_claim() -> None:
    plan = plan_turn(
        "结合知识库和 MCP 验证 Os01g0100100 的功能与论文依据",
        has_knowledge_scope=True,
        configured_mcps=["ricekb"],
    )
    manifest = _initial_source_manifest(plan)

    _append_knowledge_source_uses(
        manifest,
        plan=plan,
        contract={
            "retrieval_id": "kr-hybrid",
            "status": "COMPLETED",
            "evidence": [
                {
                    "evidence_id": "ev-doc-1",
                    "source_type": "DOCUMENT",
                    "kb_id": "kb-paper",
                }
            ],
        },
    )

    assert plan.task.primary_intent == TaskIntent.HYBRID_VERIFICATION
    assert manifest.source_uses[0].source_class == SourceClass.LOCAL_DOCUMENT
    assert manifest.source_uses[0].evidence_ids == ["ev-doc-1"]
    assert manifest.authority_outcomes[0].claim_id == "claim:literature"
    assert manifest.authority_outcomes[0].outcome == AuthorityOutcome.HIT
