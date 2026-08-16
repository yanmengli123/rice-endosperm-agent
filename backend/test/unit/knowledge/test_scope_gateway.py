from yuxi.knowledge.scope_gateway import _deduplicate_and_rerank, classify_evidence_status


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
