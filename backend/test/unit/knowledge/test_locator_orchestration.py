from __future__ import annotations

import pytest

from yuxi.knowledge.evidence import quote_locator
from yuxi.knowledge.orchestration import retrieval_orchestrator
from yuxi.knowledge.rendering import citation_channel


@pytest.mark.asyncio
async def test_quote_locator_short_circuits_generative_retrieval(monkeypatch: pytest.MonkeyPatch):
    async def fake_locator(_db, *, question, kb_ids):
        assert "OsMYB73" in question
        assert kb_ids == ["kb-a"]
        return {
            "status": "VERIFIED",
            "locator_version": "test",
            "page": 3,
            "zone": "MAIN_TEXT",
            "anchor_id": "ea-3",
            "span_id": "es-3",
            "evidence_id": "ev-3",
            "parse_revision_id": "rev-3",
            "kb_id": "kb-a",
            "file_id": "file-a",
            "source_sha256": "a" * 64,
            "quote_head": "The structure of OsMYB73 protein was also predicted.",
            "filename": "paper.pdf",
        }

    async def fake_citations(_db, rows):
        assert rows[0]["parse_revision_id"] == "rev-3"
        return [{"ref": "E1", "page_numbers": [3], "locatable": True}]

    async def fake_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(quote_locator, "resolve_quote_locator", fake_locator)
    monkeypatch.setattr(citation_channel, "build_citations_for_contract", fake_citations)
    monkeypatch.setattr(retrieval_orchestrator, "_persist_audit", fake_audit)
    monkeypatch.setattr(retrieval_orchestrator, "_emit_knowledge_trace", lambda *_args, **_kwargs: None)

    contract = await retrieval_orchestrator.prepare_knowledge_context(
        object(),
        question="The structure of OsMYB73 protein was also predicted 这句原文在正文第几页？",
        scope_snapshot={
            "tenant_id": 1,
            "scope_version": 1,
            "members": [{"kb_id": "kb-a", "kb_name": "Paper", "kb_type": "milvus"}],
        },
        run_id="run-1",
        request_id="req-1",
    )

    assert contract["status"] == "COMPLETED"
    assert contract["retrieval_plan"]["answer_mode"] == "DETERMINISTIC_LOCATOR"
    assert contract["locator_resolution"]["page"] == 3
    assert contract["evidence"][0]["anchor_id"] == "ea-3"
    assert contract["citations"][0]["page_numbers"] == [3]


@pytest.mark.asyncio
async def test_compound_locator_question_does_not_short_circuit(monkeypatch: pytest.MonkeyPatch):
    """复合意图（定位+解释）：不进入确定性短路，locator 结果并入证据与引用池。"""

    async def fake_locator(_db, *, question, kb_ids):
        return {
            "status": "VERIFIED",
            "locator_version": "test",
            "page": 17,
            "zone": "SUPPORTING_INFO",
            "anchor_id": "ea-cap",
            "span_id": "es-cap",
            "evidence_id": "ev-cap",
            "parse_revision_id": "rev-3",
            "kb_id": "kb-a",
            "file_id": "file-a",
            "source_sha256": "a" * 64,
            "quote_head": "Figure S8 Rice grain starch physicochemical characteristics...",
            "quote": "Figure S8 Rice grain starch physicochemical characteristics comparison of wild-type.",
            "filename": "paper.pdf",
            "backlinks": [
                {
                    "anchor_id": "ea-main",
                    "parse_revision_id": "rev-3",
                    "kb_id": "kb-a",
                    "file_id": "file-a",
                    "filename": "paper.pdf",
                    "page": 6,
                    "zone": "MAIN_TEXT",
                    "quote": "The wild-type and the two mutants were having soft gel consistency.",
                    "quote_head": "The wild-type and the two mutants...",
                }
            ],
        }

    async def fake_citations(_db, rows):
        assert rows, "evidence rows must not be empty"
        return [{"ref": "E1", "page_numbers": [6], "locatable": True, "zone": "MAIN_TEXT",
                 "filename": "paper.pdf", "_quote_norm": "wild type mutants gel consistency"}]

    async def fake_gateway(*, query_text, scope_snapshot, top_k=12, verbatim=None):
        return {
            "evidence": [
                {
                    "evidence_id": "evdoc-1",
                    "source_type": "DOCUMENT",
                    "kb_id": "kb-a",
                    "file_id": "file-a",
                    "content": "The wild-type and the two mutants were having soft gel consistency.",
                }
            ],
            "warnings": [],
        }

    async def fake_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(quote_locator, "resolve_quote_locator", fake_locator)
    monkeypatch.setattr(citation_channel, "build_citations_for_contract", fake_citations)
    monkeypatch.setattr(retrieval_orchestrator, "_persist_audit", fake_audit)
    monkeypatch.setattr(retrieval_orchestrator, "_emit_knowledge_trace", lambda *_a, **_k: None)
    import yuxi.knowledge.scope_gateway as scope_gateway_module
    monkeypatch.setattr(scope_gateway_module, "query_knowledge_scope_gateway", fake_gateway)

    contract = await retrieval_orchestrator.prepare_knowledge_context(
        object(),
        question=(
            "Figure S8 Rice grain starch physicochemical characteristics comparison "
            "这句话在哪一页，具体是什么意思？"
        ),
        scope_snapshot={
            "tenant_id": 1,
            "scope_version": 1,
            "knowledge_strategy": "KNOWLEDGE_FIRST",
            "members": [{"kb_id": "kb-a", "kb_name": "Paper", "kb_type": "milvus"}],
        },
        run_id="run-2",
        request_id="req-2",
    )

    # 不短路：走正常检索流程，定位结果以附加形式并入
    assert contract["retrieval_plan"]["answer_mode"] == "LOCATOR_GROUNDED_ANSWER"
    assert contract["locator_resolution"]["page"] == 17
    assert contract["evidence"][0]["retrieval_channel"] == "QUOTE_LOCATOR"
    refs = [row["ref"] for row in contract["citations"]]
    # 基础引用 + 定位锚点 + 图注反链
    assert refs == ["E1", "E2", "E3"]
    assert contract["citations"][1]["page_numbers"] == [17]
    assert contract["citations"][2]["page_numbers"] == [6]
    assert "复合意图" in "".join(contract.get("warnings") or [])
