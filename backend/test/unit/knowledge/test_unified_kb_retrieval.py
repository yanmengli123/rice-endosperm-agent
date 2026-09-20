"""单库统一检索入口：通道选择与图谱命中的身份保留。"""

from unittest.mock import AsyncMock

from yuxi.knowledge.scope_gateway import query_single_kb_unified


async def test_managed_graph_contract_uses_graph_channel(monkeypatch):
    contract = type("Spec", (), {"contract_key": "managed_graph"})()
    monkeypatch.setattr(
        "yuxi.knowledge.source_contracts.gate.load_kb_contract", AsyncMock(return_value=contract)
    )

    graph_rows = [
        {
            "evidence_id": "ev_1",
            "source_type": "STRUCTURED",
            "raw_score": 0.9,
            "subject": {"id": "ent_a", "name": "GeneA"},
            "predicate": "positively_regulates",
            "object": {"id": "ent_b", "name": "GeneB"},
            "content": "GeneA 正向调控 GeneB",
            "evidence_quote": "overexpression of GeneA up-regulates GeneB",
            "evidence_status": "SUPPORTING",
            "pmid": "12345678",
            "doi": None,
            "provenance": [{"kb_id": "kb22", "source_type": "STRUCTURED", "triple_id": "tri_1"}],
        },
        {
            "evidence_id": "ev_2",
            "source_type": "GRAPH",
            "raw_score": 0.4,
            "subject": {"id": "ent_c", "name": "GeneC"},
            "predicate": "interacts_with",
            "object": {"id": "ent_d", "name": "GeneD"},
            "content": "GeneC 与 GeneD 互作",
            "evidence_quote": None,
            "evidence_status": "SUPPORTING",
            "pmid": None,
            "doi": None,
            "provenance": [{"kb_id": "kb22", "source_type": "GRAPH", "triple_id": "tri_2"}],
        },
    ]

    async def _fake_graph_source(member, query_text, *, limit):
        assert member["kb_id"] == "kb22"
        assert member["structured_enabled"] and member["graph_enabled"]
        return graph_rows, None

    monkeypatch.setattr("yuxi.knowledge.scope_gateway._query_managed_graph_source", _fake_graph_source)

    hits = await query_single_kb_unified(kb_id="kb22", query_text="GeneA 调控", top_k=10)

    assert [hit["metadata"]["triple_id"] for hit in hits] == ["tri_1", "tri_2"]
    assert hits[0]["metadata"]["retrieval_channel"] == "STRUCTURED"
    assert hits[0]["metadata"]["evidence_id"] == "ev_1"
    assert hits[0]["metadata"]["pmid"] == "12345678"
    # 转换为文本上下文的同时保留完整图谱身份
    assert hits[0]["raw_graph_hit"]["evidence_id"] == "ev_1"
    assert "GeneA" in hits[0]["content"] and "GeneB" in hits[0]["content"]


async def test_document_contract_passes_through_aquery(monkeypatch):
    contract = type("Spec", (), {"contract_key": "pdf_evidence"})()
    monkeypatch.setattr(
        "yuxi.knowledge.source_contracts.gate.load_kb_contract", AsyncMock(return_value=contract)
    )

    doc_hits = [
        {"content": "chunk 文本", "score": 0.8, "metadata": {"chunk_id": "c1", "file_id": "f1"}},
    ]
    aquery_mock = AsyncMock(return_value=doc_hits)
    monkeypatch.setattr("yuxi.knowledge.runtime.knowledge_base.aquery", aquery_mock)

    hits = await query_single_kb_unified(
        kb_id="kb11", query_text="问题", retrieval_params={"final_top_k": 5, "use_reranker": False}
    )

    assert hits == [
        {
            "content": "chunk 文本",
            "score": 0.8,
            "metadata": {"chunk_id": "c1", "file_id": "f1", "retrieval_channel": "DOCUMENT"},
        }
    ]
    aquery_mock.assert_awaited_once()
    assert aquery_mock.call_args.kwargs["final_top_k"] == 5
