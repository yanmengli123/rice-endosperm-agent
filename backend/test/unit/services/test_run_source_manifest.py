from __future__ import annotations

from types import SimpleNamespace

import pytest

from yuxi.knowledge.planning.turn_execution_plan import (
    AuthorityOutcome,
    EvidenceLevel,
    SourceClass,
    TaskIntent,
    plan_turn,
)
from yuxi.services.chat_service import (
    _append_knowledge_source_uses,
    _append_mcp_source_uses,
    _finalize_mcp_manifest,
    _initial_source_manifest,
)


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


def test_discovery_call_is_audited_but_never_adopted_as_answer_evidence() -> None:
    plan = plan_turn(
        "通过 MCP 查水稻胚乳磷酸化组数据集",
        has_knowledge_scope=False,
        configured_mcps=["data-aggregator"],
    )
    manifest = _initial_source_manifest(plan)
    audit = SimpleNamespace(
        id=42, server_slug="data-aggregator", capability_name="search", status="success",
        arguments_digest="sha256:request", result_digest="sha256:result", provenance={},
    )
    _append_mcp_source_uses(manifest, audits=[audit], matched_ids={42})

    assert manifest.source_uses[0].source_class == SourceClass.DISCOVERY
    assert manifest.source_uses[0].adopted is False
    assert manifest.source_uses[0].evidence_ids == []


@pytest.mark.asyncio
async def test_not_found_and_discovery_cannot_satisfy_dataset_authority() -> None:
    plan = plan_turn(
        "通过 MCP 查水稻胚乳磷酸化组数据集",
        has_knowledge_scope=False,
        configured_mcps=["gene-authority", "data-aggregator"],
    )
    manifest = _initial_source_manifest(plan)
    audits = [
        SimpleNamespace(
            id=1, server_slug="data-aggregator", capability_name="search", status="success",
            arguments_digest="q1", result_digest="r1", provenance={},
        ),
        SimpleNamespace(
            id=2, server_slug="gene-authority", capability_name="pride_project_rest", status="success",
            arguments_digest="q2", result_digest="r2", provenance={"provider_status": "NOT_FOUND"},
        ),
    ]

    class _Db:
        async def execute(self, _statement):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: audits))

    satisfied = await _finalize_mcp_manifest(
        _Db(), run_id="run-test", plan=plan, manifest=manifest,
    )
    assert satisfied is False
    assert manifest.successful_mcp_call_count == 0
    assert not any(use.adopted for use in manifest.source_uses)
