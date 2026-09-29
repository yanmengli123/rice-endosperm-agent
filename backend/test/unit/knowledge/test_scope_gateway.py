import asyncio
from types import SimpleNamespace

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


def test_compact_evidence_extracts_query_relevant_window_and_keeps_chunk_provenance():
    row = _evidence(kb_id="kb-a", status="SUPPORTING")
    row.update(
        {
            "source_type": "DOCUMENT",
            "file_id": "file-a",
            "chunk_id": "chunk-a",
            "content": (
                "Background text about OsMYB73. "
                + "x" * 700
                + " The results revealed two typical SANT domains between "
                "115–164 and 167–215 amino acids."
            ),
        }
    )

    compact = _compact_evidence(
        row,
        query_text="OsMYB73 有几个 SANT 保守结构域，氨基酸位置是多少？",
    )

    assert "115–164" in compact["evidence_quote"]
    assert "167–215" in compact["evidence_quote"]
    assert compact["file_id"] == "file-a"
    assert compact["chunk_id"] == "chunk-a"


def test_normalize_document_results_preserves_top_level_rerank_score():
    rows = scope_gateway._normalize_document_results(
        "kb-a",
        "Rice PDF",
        {
            "results": [
                {
                    "id": "chunk-a",
                    "file_id": "file-a",
                    "content": "OsMYB73 contains two SANT domains.",
                    "score": 0.4,
                    "rerank_score": 0.91,
                    "metadata": {},
                }
            ]
        },
        100,
    )

    assert rows[0]["raw_score"] == 0.91


def test_normalize_document_results_calibrates_exact_identifiers_with_lexical_score():
    rows = scope_gateway._normalize_document_results(
        "kb-a",
        "Rice PDF",
        {
            "results": [
                {
                    "id": "chunk-a",
                    "content": "OsMYB73 has two typical SANT domains.",
                    "score": 0.01,
                    "metadata": {},
                }
            ]
        },
        100,
        query_text="OsMYB73 SANT",
    )

    assert rows[0]["raw_score"] == 1.0


def test_general_reranking_prefers_relevance_over_yield_category():
    document = scope_gateway._normalize_document_results(
        "kb-doc",
        "Rice PDF",
        [{"id": "chunk-a", "content": "OsMYB73 contains SANT domains.", "score": 0.9}],
        100,
    )[0]
    graph = _evidence(kb_id="kb-graph", status="SUPPORTING")
    graph.update(
        {
            "subject": {"name": "UnrelatedGene"},
            "raw_score": 0.2,
            "outcome_class": "DIRECT_YIELD",
            "evidence_level": "E1",
        }
    )

    evidence, _ = _deduplicate_and_rerank([graph, document], top_k=2)

    assert evidence[0]["source_type"] == "DOCUMENT"


@pytest.mark.asyncio
async def test_document_source_uses_hybrid_scientific_retrieval_for_milvus(monkeypatch):
    from yuxi.knowledge import runtime

    captured = {}

    async def retriever(query_text, **kwargs):
        captured.update({"query_text": query_text, "kwargs": kwargs})
        return {
            "results": [
                {
                    "id": "chunk-a",
                    "file_id": "file-a",
                    "content": "OsMYB73 contains two SANT domains.",
                    "score": 0.87,
                    "metadata": {},
                }
            ]
        }

    monkeypatch.setattr(
        runtime,
        "knowledge_base",
        SimpleNamespace(
            get_retrievers=lambda: {
                "kb-a": {
                    "name": "Rice PDF",
                    "metadata": {"kb_type": "milvus"},
                    "retriever": retriever,
                }
            }
        ),
    )

    rows, error = await scope_gateway._query_document_source(
        {
            "kb_id": "kb-a",
            "kb_name": "Rice PDF",
            "document_enabled": True,
            "evidence_supporting": True,
            "priority": 100,
        },
        "OsMYB73 SANT",
    )

    assert error is None
    assert captured == {
        "query_text": "OsMYB73 SANT",
        "kwargs": {"search_mode": "hybrid", "scientific_pdf_diversity": True},
    }
    assert rows[0]["raw_score"] == 1.0


@pytest.mark.asyncio
async def test_scope_timeout_keeps_successful_graph_evidence(monkeypatch: pytest.MonkeyPatch):
    async def slow_document(member, query_text, *, file_ids=None):
        del member, query_text, file_ids
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


@pytest.mark.asyncio
async def test_folder_scope_filters_documents_and_fails_closed_other_channels(monkeypatch):
    captured = {}

    async def document_source(member, query_text, *, file_ids=None):
        del query_text
        captured["document"] = (member["kb_id"], file_ids)
        return [], None

    async def forbidden_graph(*args, **kwargs):
        del args, kwargs
        raise AssertionError("folder scope must not query whole-KB graph channels")

    monkeypatch.setattr(scope_gateway, "_query_document_source", document_source)
    monkeypatch.setattr(scope_gateway, "_query_managed_graph_source", forbidden_graph)

    result = await query_knowledge_scope_gateway(
        query_text="OsFIE1",
        scope_snapshot={
            "members": [
                {
                    "kb_id": "kb-a",
                    "kb_name": "Folder scoped",
                    "priority": 100,
                    "document_enabled": True,
                    "graph_enabled": False,
                    "structured_enabled": False,
                    "folder_scope_restricted": True,
                    "folder_file_ids": ["file-a", "file-b"],
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

    assert captured["document"] == ("kb-a", ["file-a", "file-b"])
    assert result["knowledge_source_status"][0]["graph_status"] == "DOCUMENT_SCOPE_EXCLUDED"
    assert result["knowledge_source_status"][0]["structured_status"] == "DOCUMENT_SCOPE_EXCLUDED"


@pytest.mark.asyncio
async def test_folder_scope_with_deleted_folders_excludes_kb_instead_of_widening(monkeypatch):
    async def forbidden_document(*args, **kwargs):
        del args, kwargs
        raise AssertionError("empty folder scope must not query the whole KB")

    async def forbidden_graph(*args, **kwargs):
        del args, kwargs
        raise AssertionError("empty folder scope must not query whole-KB graph channels")

    monkeypatch.setattr(scope_gateway, "_query_document_source", forbidden_document)
    monkeypatch.setattr(scope_gateway, "_query_managed_graph_source", forbidden_graph)

    result = await query_knowledge_scope_gateway(
        query_text="OsFIE1",
        scope_snapshot={
            "members": [
                {
                    "kb_id": "kb-a",
                    "kb_name": "Folder scoped",
                    "priority": 100,
                    "document_enabled": True,
                    "graph_enabled": False,
                    "structured_enabled": False,
                    "folder_scope_restricted": True,
                    "folder_file_ids": [],
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

    assert result["evidence"] == []
    assert "kb-a" in result["document_scope_excluded_kbs"]
    status = result["knowledge_source_status"][0]
    assert status["document_status"] == "NO_MATCHING_DOCUMENT"
    assert status["graph_status"] == "DOCUMENT_SCOPE_EXCLUDED"
