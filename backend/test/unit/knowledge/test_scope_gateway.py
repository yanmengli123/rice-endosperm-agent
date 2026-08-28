import asyncio

import pytest

from yuxi.knowledge import scope_gateway
from yuxi.knowledge.scope_gateway import (
    _compact_evidence,
    _deduplicate_and_rerank,
    classify_evidence_status,
    query_knowledge_scope_gateway,
)


def _evidence(*, kb_id: str, direction: str = "POSITIVE", status: str = "STRICT") -> dict:
    return {
        "evidence_id": f"ev-{kb_id}",
        "source_type": "STRUCTURED",
        "evidence_status": status,
        "kb_id": kb_id,
        "found_in_kbs": [kb_id],
        "subject": {"name": "OsFIE1"},
        "predicate": "REGULATES",
        "object": {"name": "seed size"},
        "direction": direction,
        "doi": "10.1000/rice",
        "raw_score": 0.8,
        "priority": 100,
        "provenance": [{"kb_id": kb_id}],
    }


def test_evidence_classifier_does_not_promote_candidate_or_conflict():
    assert classify_evidence_status("candidate", "high", "ALIGNED") == "CANDIDATE"
    assert classify_evidence_status("asserted", "high", "ALIGNED", "candidate") == "CANDIDATE"
    assert classify_evidence_status("asserted", "high", "CONFLICT") == "SUPPORTING"
    assert classify_evidence_status("rejected", "high", "ALIGNED") == "REJECTED"
    assert classify_evidence_status("verified", "high", "ALIGNED") == "STRICT"


def test_deduplication_preserves_cross_kb_provenance():
    evidence, warnings = _deduplicate_and_rerank(
        [_evidence(kb_id="kb-a"), _evidence(kb_id="kb-b")],
        top_k=10,
    )

    assert len(evidence) == 1
    assert evidence[0]["found_in_kbs"] == ["kb-a", "kb-b"]
    assert len(evidence[0]["provenance"]) == 2
    assert warnings == []


def test_conflicting_directions_are_not_silently_merged():
    evidence, warnings = _deduplicate_and_rerank(
        [_evidence(kb_id="kb-a", direction="POSITIVE"), _evidence(kb_id="kb-b", direction="NEGATIVE")],
        top_k=10,
    )

    assert evidence[0]["conflict"] is True
    assert warnings and "证据冲突" in warnings[0]


def test_yield_reranking_preserves_requested_semantic_layers():
    rows = []
    for index in range(8):
        row = _evidence(kb_id=f"direct-{index}")
        row["subject"] = {"name": f"DirectGene{index}"}
        row["outcome_class"] = "DIRECT_YIELD"
        rows.append(row)
    for category in ("CONDITION_SPECIFIC_YIELD", "YIELD_COMPONENT", "GRAIN_FILLING"):
        row = _evidence(kb_id=category.lower())
        row["subject"] = {"name": category}
        row["outcome_class"] = category
        rows.append(row)

    evidence, _ = _deduplicate_and_rerank(rows, top_k=6, stratify_yield=True)

    categories = {item["outcome_class"] for item in evidence}
    assert {"DIRECT_YIELD", "CONDITION_SPECIFIC_YIELD", "YIELD_COMPONENT", "GRAIN_FILLING"} <= categories


def test_compact_evidence_removes_internal_fields_and_duplicate_content():
    row = _evidence(kb_id="kb-a")
    row.update(
        {
            "content": "A" * 600,
            "metadata": {"large": True},
            "priority": 50,
            "raw_score": 0.99,
            "claim_eligible": True,
            "conflict": False,
        }
    )

    compact = _compact_evidence(row)

    assert compact["claim_eligible"] is True
    assert compact["conflict"] is False
    assert compact["evidence_quote"].endswith("…")
    assert len(compact["evidence_quote"]) == 300
    assert "content" not in compact
    assert "metadata" not in compact
    assert "priority" not in compact
    assert "raw_score" not in compact


@pytest.mark.asyncio
async def test_scope_timeout_keeps_successful_graph_evidence(monkeypatch: pytest.MonkeyPatch):
    async def slow_document(member, query_text):
        del member, query_text
        await asyncio.sleep(60)
        return [], None

    async def available_graph(member, query_text, *, limit):
        del query_text, limit
        row = _evidence(kb_id=member["kb_id"])
        row["claim_eligible"] = True
        row["outcome_class"] = "OTHER"
        return [row], None

    monkeypatch.setattr(scope_gateway, "_query_document_source", slow_document)
    monkeypatch.setattr(scope_gateway, "_query_managed_graph_source", available_graph)
    monkeypatch.setattr(scope_gateway, "KNOWLEDGE_DOCUMENT_SOURCE_TIMEOUT_SECONDS", 0.01)

    result = await query_knowledge_scope_gateway(
        query_text="OsFIE1",
        scope_snapshot={
            "members": [
                {
                    "kb_id": "kb-a",
                    "kb_name": "Rice graph",
                    "priority": 100,
                    "document_enabled": True,
                    "graph_enabled": True,
                    "structured_enabled": True,
                    "evidence_strict": True,
                    "evidence_supporting": True,
                    "evidence_candidate": False,
                    "evidence_rejected": False,
                }
            ],
            "effective_kb_ids": ["kb-a"],
        },
        top_k=5,
    )

    assert [item["evidence_id"] for item in result["evidence"]] == ["ev-kb-a"]
    assert any("DOCUMENT_TIMEOUT" in warning for warning in result["warnings"])
