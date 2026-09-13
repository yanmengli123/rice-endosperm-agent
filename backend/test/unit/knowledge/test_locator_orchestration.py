"""单一证据集定位编排测试：定位器消费检索产出的引用池，与状态模块同源。"""

from __future__ import annotations

import pytest

import yuxi.knowledge.scope_gateway as scope_gateway_module
from yuxi.knowledge.evidence import quote_locator
from yuxi.knowledge.orchestration import retrieval_orchestrator


def _citation(ref: str, page: int, quote: str, *, zone: str = "MAIN_TEXT") -> dict:
    import re

    import unicodedata

    def _norm(text: str) -> str:
        value = unicodedata.normalize("NFKC", text)
        for dash in "‐‑‒–—―−":
            value = value.replace(dash, "-")
        return re.sub(r"\s+", " ", re.sub(r"[^0-9a-z一-鿿]+", " ", value.casefold())).strip()

    return {
        "ref": ref,
        "evidence_id": f"ev-{ref}",
        "kb_id": "kb-a",
        "file_id": "file-a",
        "filename": "paper.pdf",
        "zone": zone,
        "page_numbers": [page],
        "primary_page": page,
        "quote_head": quote[:80],
        "anchor_ids": [f"ea-{ref}"],
        "locatable": True,
        "toc_line": False,
        "secondary_of": None,
        "_anchor_id": f"ea-{ref}",
        "_physical_evidence_id": f"ev-physical-{ref}",
        "_retrieval_channel": "DOCUMENT",
        "_span_id": f"es-{ref}",
        "_span_evidence_id": f"evs-{ref}",
        "_parse_revision_id": "pr-active",
        "_index_revision_id": "ir-active",
        "_source_sha256": "a" * 64,
        "_quote": quote,
        "_quote_norm": _norm(quote),
    }


def _patch_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    *,
    citations: list[dict],
    direct_locator: dict | None = None,
):
    async def fake_gateway(*, query_text, scope_snapshot, top_k=12, verbatim=None):
        return {
            "evidence": [
                {
                    "evidence_id": "evdoc-1",
                    "source_type": "DOCUMENT",
                    "kb_id": "kb-a",
                    "file_id": "file-a",
                    "content": "The structure of OsMYB73 protein was also predicted.",
                }
            ],
            "warnings": [],
        }

    async def fake_citations(_db, rows):
        assert rows, "evidence rows must not be empty"
        return citations

    async def fake_audit(*_args, **_kwargs):
        return None

    async def fake_direct_locator(_db, *, question, kb_ids):
        assert kb_ids == ["kb-a"]
        return direct_locator or {
            "status": "NOT_FOUND",
            "locator_version": "test",
            "reason": "not_seeded_in_this_test",
        }

    monkeypatch.setattr(scope_gateway_module, "query_knowledge_scope_gateway", fake_gateway)
    monkeypatch.setattr(quote_locator, "resolve_quote_locator", fake_direct_locator)
    monkeypatch.setattr(retrieval_orchestrator, "_persist_audit", fake_audit)
    monkeypatch.setattr(
        retrieval_orchestrator, "_emit_knowledge_trace", lambda *_args, **_kwargs: None
    )
    from yuxi.knowledge.rendering import citation_channel

    monkeypatch.setattr(citation_channel, "build_citations_for_contract", fake_citations)


_SCOPE = {
    "tenant_id": 1,
    "scope_version": 1,
    "knowledge_strategy": "KNOWLEDGE_FIRST",
    "members": [{"kb_id": "kb-a", "kb_name": "Paper", "kb_type": "milvus"}],
}


@pytest.mark.asyncio
async def test_pure_locator_resolves_from_retrieval_citations(monkeypatch: pytest.MonkeyPatch):
    """纯定位：最终页码只从冻结引用池裁决，与状态模块绑定同一行。"""
    _patch_pipeline(
        monkeypatch,
        citations=[
            _citation("E1", 3, "The structure of OsMYB73 protein was also predicted SANT domains."),
            _citation("E2", 8, "Unrelated localization content GFP nucleus."),
        ],
    )
    contract = await retrieval_orchestrator.prepare_knowledge_context(
        object(),
        question="The structure of OsMYB73 protein was also predicted 这句原文在正文第几页？",
        scope_snapshot=_SCOPE,
        run_id="run-1",
        request_id="req-1",
    )
    assert contract["retrieval_plan"]["answer_mode"] == "DETERMINISTIC_LOCATOR"
    assert contract["locator_resolution"]["page"] == 3
    assert contract["locator_resolution"]["citation_ref"] == "E1"
    assert contract["evidence"][0]["evidence_id"] == "evdoc-1"  # 状态模块同源检索集


@pytest.mark.asyncio
async def test_pure_locator_fails_closed_when_quote_not_in_evidence_set(monkeypatch):
    """引用池不含目标句：失败关闭（检索召回缺口不上屏猜测页码）。"""
    _patch_pipeline(monkeypatch, citations=[_citation("E1", 8, "Unrelated GFP nucleus content.")])
    contract = await retrieval_orchestrator.prepare_knowledge_context(
        object(),
        question="The structure of OsMYB73 protein was also predicted 这句原文在正文第几页？",
        scope_snapshot=_SCOPE,
        run_id="run-2",
        request_id="req-2",
    )
    assert contract["retrieval_plan"]["answer_mode"] == "DETERMINISTIC_LOCATOR"
    assert contract["locator_resolution"]["status"] == "NOT_FOUND"
    assert contract["status"] == "DEGRADED"
    assert contract["error_code"] == "QUOTE_LOCATOR_NOT_FOUND"


@pytest.mark.asyncio
async def test_full_scope_duplicate_cannot_be_hidden_by_top_k(monkeypatch: pytest.MonkeyPatch):
    """全范围存在重复物理位置时，Top-K 只返回一个候选也不能宣称唯一页码。"""
    _patch_pipeline(
        monkeypatch,
        citations=[
            _citation("E1", 3, "The structure of OsMYB73 protein was also predicted SANT domains.")
        ],
        direct_locator={
            "status": "MULTIPLE_MATCHES",
            "locator_version": "test",
            "match_count": 2,
            "reason": "quote_hits_multiple_physical_locations",
        },
    )
    contract = await retrieval_orchestrator.prepare_knowledge_context(
        object(),
        question="The structure of OsMYB73 protein was also predicted 这句原文在正文第几页？",
        scope_snapshot=_SCOPE,
        run_id="run-duplicate",
        request_id="req-duplicate",
    )

    assert contract["locator_resolution"]["status"] == "MULTIPLE_MATCHES"
    assert "page" not in contract["locator_resolution"]
    assert contract["status"] == "DEGRADED"
    assert contract["error_code"] == "QUOTE_LOCATOR_MULTIPLE_MATCHES"


@pytest.mark.asyncio
async def test_compound_locator_does_not_short_circuit(monkeypatch: pytest.MonkeyPatch):
    """复合意图（定位+解释）：不短路 LLM，locator 结果并入证据与引用池。"""
    _patch_pipeline(
        monkeypatch,
        citations=[
            _citation("E1", 3, "The structure of OsMYB73 protein was also predicted SANT domains."),
            _citation(
                "E2",
                6,
                "The wild-type and the two mutants were having soft gel consistency.",
            ),
            _citation(
                "E3",
                17,
                "Figure S8 Rice grain starch physicochemical characteristics comparison.",
                zone="SUPPORTING_INFO",
            ),
        ],
        direct_locator={
            "status": "VERIFIED",
            "locator_version": "test",
            "page": 17,
            "zone": "SUPPORTING_INFO",
            "anchor_id": "ea-E3",
            "span_id": "es-E3",
            "evidence_id": "ev-physical-E3",
            "span_evidence_id": "evs-E3",
            "parse_revision_id": "pr-active",
            "index_revision_id": "ir-active",
            "kb_id": "kb-a",
            "file_id": "file-a",
            "source_sha256": "a" * 64,
            "quote": "Figure S8 Rice grain starch physicochemical characteristics comparison.",
            "quote_head": "Figure S8 Rice grain starch physicochemical characteristics comparison.",
            "filename": "paper.pdf",
            "backlinks": [
                {
                    "page": 6,
                    "zone": "MAIN_TEXT",
                    "anchor_id": "ea-E2",
                    "quote": "The wild-type and the two mutants were having soft gel consistency.",
                    "filename": "paper.pdf",
                    "file_id": "file-a",
                    "kb_id": "kb-a",
                }
            ],
        },
    )
    contract = await retrieval_orchestrator.prepare_knowledge_context(
        object(),
        question=(
            "Figure S8 Rice grain starch physicochemical characteristics comparison "
            "这句话在哪一页，具体是什么意思？"
        ),
        scope_snapshot=_SCOPE,
        run_id="run-3",
        request_id="req-3",
    )
    assert contract["retrieval_plan"]["answer_mode"] == "LOCATOR_GROUNDED_ANSWER"
    assert contract["locator_resolution"]["page"] == 17
    assert contract["locator_resolution"]["zone"] == "SUPPORTING_INFO"
    assert contract["locator_resolution"]["backlinks"][0]["page"] == 6
    # 目标锚点已在冻结引用池内，不重复追加另一条引用。
    refs = [row["ref"] for row in contract["citations"]]
    assert refs == ["E1", "E2", "E3"]
    assert contract["citations"][2]["page_numbers"] == [17]
    assert "复合意图" in "".join(contract.get("warnings") or [])


@pytest.mark.asyncio
async def test_bifc_exact_anchor_is_frozen_into_answer_evidence(monkeypatch: pytest.MonkeyPatch):
    """第 15 页状态证据必须进入同一 Contract，供模型解释与状态投影共同消费。"""
    quote = (
        "Two pairs of constructs, OsMYB73-VN173 and OsNF-YB1-VC155, were transformed into "
        "tobacco leaf cells. The mixture of modified pUC-SPYNE and pUC-SPYCE vector was used "
        "as a negative control."
    )
    direct = {
        "status": "VERIFIED",
        "locator_version": "test",
        "page": 15,
        "zone": "MAIN_TEXT",
        "anchor_id": "ea-page15",
        "span_id": "es-page15",
        "evidence_id": "ev-page15-physical",
        "span_evidence_id": "evs-page15",
        "parse_revision_id": "pr-active",
        "index_revision_id": "ir-active",
        "kb_id": "kb-a",
        "file_id": "file-a",
        "source_sha256": "a" * 64,
        "quote_head": quote[:80],
        "quote": quote,
        "filename": "paper.pdf",
    }
    _patch_pipeline(monkeypatch, citations=[], direct_locator=direct)

    async def citations_from_frozen_evidence(_db, rows):
        assert rows[0]["retrieval_channel"] == "QUOTE_LOCATOR"
        assert rows[0]["page_number"] == 15
        assert rows[0]["evidence_quote"] == quote
        citation = _citation("E1", 15, quote)
        citation.update(
            {
                "evidence_id": "ev-page15-physical",
                "_anchor_id": "ea-page15",
                "_physical_evidence_id": "ev-page15-physical",
                "_retrieval_channel": "QUOTE_LOCATOR",
                "_span_id": "es-page15",
                "_span_evidence_id": "evs-page15",
                "_parse_revision_id": "pr-active",
                "_index_revision_id": "ir-active",
                "_source_sha256": "a" * 64,
                "_quote": quote,
            }
        )
        return [citation]

    from yuxi.knowledge.rendering import citation_channel

    monkeypatch.setattr(citation_channel, "build_citations_for_contract", citations_from_frozen_evidence)
    audit: dict = {}

    async def capture_audit(*_args, **kwargs):
        audit.update(kwargs)

    monkeypatch.setattr(retrieval_orchestrator, "_persist_audit", capture_audit)
    contract = await retrieval_orchestrator.prepare_knowledge_context(
        object(),
        question=f"{quote} 这句话在哪个文献那一页，具体是什么意思？",
        scope_snapshot=_SCOPE,
        run_id="run-bifc",
        request_id="req-bifc",
    )

    assert contract["locator_resolution"]["page"] == 15
    assert contract["locator_resolution"]["evidence_id"] == "ev-page15-physical"
    assert contract["retrieval_plan"]["intent"] == "QUOTE_LOCATOR"
    assert contract["retrieval_plan"]["answer_mode"] == "LOCATOR_GROUNDED_ANSWER"
    assert contract["evidence"][0]["evidence_quote"] == quote
    assert audit["plan"]["intent"] == "QUOTE_LOCATOR"
    assert audit["contract"]["locator_resolution"]["page"] == 15

    from yuxi.knowledge.rendering.answer_context_builder import build_answer_context

    model_context = build_answer_context(contract)
    assert quote in model_context
    assert '"citation_refs":["E1"]' in model_context
