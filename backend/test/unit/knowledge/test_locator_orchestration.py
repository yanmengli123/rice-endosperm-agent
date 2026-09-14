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
        # 引用池由冻结证据行构建：每条 citation 的物理证据 id 必须能回指一条
        # 已冻结的 gateway 证据行（Locator Authority 出口不变量的前提）。
        return {
            "evidence": [
                {
                    "evidence_id": "evdoc-1",
                    "source_type": "DOCUMENT",
                    "kb_id": "kb-a",
                    "file_id": "file-a",
                    "content": "The structure of OsMYB73 protein was also predicted.",
                }
            ]
            + [
                {
                    "evidence_id": str(citation.get("_physical_evidence_id") or citation.get("evidence_id")),
                    "source_type": "DOCUMENT",
                    "kb_id": citation.get("kb_id") or "kb-a",
                    "file_id": citation.get("file_id") or "file-a",
                    "content": str(citation.get("_quote") or ""),
                }
                for citation in citations
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
    monkeypatch.setattr(retrieval_orchestrator, "_emit_knowledge_trace", lambda *_args, **_kwargs: None)
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
        citations=[_citation("E1", 3, "The structure of OsMYB73 protein was also predicted SANT domains.")],
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
async def test_figure_caption_direct_binding_cannot_be_overwritten_by_discussion_page(monkeypatch):
    """Figure 题注物理绑定是终局；正文复述不能用引用池把第 7 页改成第 5 页。"""
    caption = "Figure 3 Rice grain starch physicochemical characteristics in ZH11 and cr-myb73."
    direct = {
        "status": "VERIFIED",
        "locator_version": "caption_locator_v3",
        "locator_kind": "FIGURE_CAPTION",
        "match_tier": "T0_RAW_EXACT",
        "page": 7,
        "zone": "MAIN_TEXT",
        "anchor_id": "ea-caption",
        "span_id": "es-caption",
        "evidence_id": "ev-caption-physical",
        "span_evidence_id": "evs-caption",
        "parse_revision_id": "pr-active",
        "index_revision_id": "ir-active",
        "kb_id": "kb-a",
        "file_id": "file-a",
        "source_sha256": "a" * 64,
        "quote": caption,
        "quote_head": caption,
        "filename": "paper.pdf",
    }
    _patch_pipeline(
        monkeypatch,
        citations=[
            _citation(
                "E1",
                5,
                "Figure 3 showed chain length distributions of amylopectin in ZH11 and cr-myb73.",
            )
        ],
        direct_locator=direct,
    )

    contract = await retrieval_orchestrator.prepare_knowledge_context(
        object(),
        question=f"{caption} 这段题注在哪一页？",
        scope_snapshot=_SCOPE,
        run_id="run-figure-authority",
        request_id="req-figure-authority",
    )

    assert contract["locator_resolution"]["status"] == "VERIFIED"
    assert contract["locator_resolution"]["page"] == 7
    assert contract["locator_resolution"]["anchor_id"] == "ea-caption"
    assert contract["locator_resolution"]["locator_kind"] == "FIGURE_CAPTION"


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
            "Figure S8 Rice grain starch physicochemical characteristics comparison 这句话在哪一页，具体是什么意思？"
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
async def test_locator_authority_gate_fails_closed_when_binding_evidence_not_frozen(monkeypatch):
    """出口门禁回归：引用池裁决 VERIFIED 但物理证据未冻结 → ANSWER_VALIDATION_FAILED，不上屏页码。"""
    rogue = _citation("E1", 9, "The structure of OsMYB73 protein was also predicted SANT domains.")
    rogue["_physical_evidence_id"] = "ev_rogue_unfrozen"  # 不在冻结证据集内
    rogue["evidence_id"] = "ev_rogue_unfrozen"
    _patch_pipeline(monkeypatch, citations=[])
    from yuxi.knowledge.rendering import citation_channel

    monkeypatch.setattr(
        citation_channel,
        "build_citations_for_contract",
        lambda *_args, **_kwargs: _async_return([rogue]),
    )
    contract = await retrieval_orchestrator.prepare_knowledge_context(
        object(),
        question="The structure of OsMYB73 protein was also predicted 这句原文在正文第几页？",
        scope_snapshot=_SCOPE,
        run_id="run-gate",
        request_id="req-gate",
    )
    assert contract["locator_resolution"]["status"] == "NOT_FOUND"
    assert contract["locator_resolution"]["_authority_gate_audit"]["original_status"] == "VERIFIED"
    assert contract["status"] == "DEGRADED"
    assert contract["error_code"] == "ANSWER_VALIDATION_FAILED"
    assert contract["retrieval_plan"]["answer_mode"] != "LOCATOR_GROUNDED_ANSWER"


async def _async_return(value):
    return value


@pytest.mark.asyncio
async def test_verified_binding_is_persisted_with_locator_resolution(monkeypatch):
    """门禁通过时绑定对象随 locator_resolution 持久化（投影/渲染统一消费该对象）。"""
    _patch_pipeline(
        monkeypatch,
        citations=[
            _citation("E1", 3, "The structure of OsMYB73 protein was also predicted SANT domains."),
        ],
    )
    contract = await retrieval_orchestrator.prepare_knowledge_context(
        object(),
        question="The structure of OsMYB73 protein was also predicted 这句原文在正文第几页？",
        scope_snapshot=_SCOPE,
        run_id="run-binding",
        request_id="req-binding",
    )
    binding = contract["locator_resolution"]["binding"]
    assert binding["binding_id"].startswith("vlb_")
    assert binding["status"] == "VERIFIED"
    assert binding["page_number"] == 3
    assert binding["physical_evidence_id"] == "ev-physical-E1"
    assert binding["locator_kind"] == "QUOTE_LOCATOR"


@pytest.mark.asyncio
async def test_image_attachment_flow_resolves_and_freezes_figure_binding(monkeypatch: pytest.MonkeyPatch):
    """图片附件入口端到端：观察 → 确定性裁决 → 冻结 + 门禁 + binding（第 4 页）。"""
    from yuxi.knowledge.vision import figure_image_locator as figure_module
    from yuxi.knowledge.vision import provider as provider_module
    from yuxi.knowledge.vision.visual_observation import VisualObservationEnvelope

    class _StubProvider:
        available = True

        async def describe(self, _image_bytes):
            return VisualObservationEnvelope.model_validate(
                {
                    "schema_version": "visual-observation.v1",
                    "figure_label": "Figure 1",
                    "panel_labels": ["a", "b", "c"],
                    "visible_entities": ["OsMYB73-GFP"],
                    "visible_text": ["Relative expression levels"],
                    "caption_fragments": ["Rice OsMYB73 gene expression"],
                    "visual_structure": {"bar_chart": True},
                    "confidence": 0.9,
                }
            )

    async def fake_image_locator(_db, *, kb_ids, image_bytes=None, observation=None):
        assert kb_ids == ["kb-a"]
        assert image_bytes
        return {
            "status": "VERIFIED",
            "locator_version": "figure_image_locator_v2",
            "locator_kind": "FIGURE_IMAGE",
            "match_tier": "V0_EXACT_ASSET_SHA",
            "page": 4,
            "asset_page": 4,
            "caption_page": 4,
            "source_page_index": 3,
            "zone": "MAIN_TEXT",
            "anchor_id": "ea_fig1",
            "span_id": "es_fig1",
            "evidence_id": "ev_fig1_page4",
            "span_evidence_id": "evs_fig1",
            "evidence_type": "caption",
            "parse_revision_id": "pr-active",
            "kb_id": "kb-a",
            "file_id": "file-a",
            "source_sha256": "a" * 64,
            "index_revision_id": "ir-active",
            "quote": "Figure 1. Expression patterns of OsMYB73 in rice seeds.",
            "quote_head": "Figure 1. Expression patterns of OsMYB73 in rice seeds.",
            "filename": "paper.pdf",
        }

    monkeypatch.setattr(provider_module, "get_vision_provider", lambda: _StubProvider())
    monkeypatch.setattr(figure_module, "resolve_figure_image_locator", fake_image_locator)

    figure_citation = _citation("E1", 4, "Figure 1. Expression patterns of OsMYB73 in rice seeds measured by qRT-PCR.")
    figure_citation.update(
        {
            "evidence_id": "ev_fig1_page4",
            "_physical_evidence_id": "ev_fig1_page4",
            "_anchor_id": "ea_fig1",
        }
    )
    _patch_pipeline(monkeypatch, citations=[figure_citation])

    async def fake_quote_locator(_db, *, question, kb_ids):  # 图片裁决已终局，不应回退文本通道
        raise AssertionError("image flow must not fall back to quote locator")

    monkeypatch.setattr(quote_locator, "resolve_quote_locator", fake_quote_locator)

    contract = await retrieval_orchestrator.prepare_knowledge_context(
        object(),
        question="这个图片在哪篇论文哪一页，是什么意思？",
        scope_snapshot=_SCOPE,
        run_id="run-image",
        request_id="req-image",
        image_bytes=b"fake-image-bytes",
    )
    assert contract["locator_resolution"]["status"] == "VERIFIED"
    assert contract["locator_resolution"]["page"] == 4
    assert contract["locator_resolution"]["locator_kind"] == "FIGURE_IMAGE"
    assert contract["locator_resolution"]["binding"]["page_number"] == 4
    assert contract["locator_resolution"]["binding"]["locator_kind"] == "FIGURE_IMAGE"
    assert contract["locator_resolution"]["binding"]["asset_pdf_page_number"] == 4
    # V0 确定性命中：视觉观察未被调用（确定性层先行，VLM 最后）
    assert "figure_image_observation" not in contract
    # 图片锚点已冻结进证据契约（门禁通过的前提）
    assert any(row.get("evidence_id") == "ev_fig1_page4" for row in contract["evidence"])


@pytest.mark.asyncio
async def test_image_flow_fails_closed_when_vision_provider_unavailable(monkeypatch: pytest.MonkeyPatch):
    """视觉 provider 不可用 → 失败关闭；绝不回退到自由回答页码。"""
    from yuxi.knowledge.vision import figure_image_locator as figure_module
    from yuxi.knowledge.vision import provider as provider_module

    monkeypatch.setattr(provider_module, "get_vision_provider", lambda: provider_module.NullVisionProvider())

    async def fake_image_locator(_db, *, kb_ids, image_bytes=None, observation=None):
        assert observation is None
        return {
            "status": "NOT_FOUND",
            "locator_version": "figure_image_locator_v2",
            "locator_kind": "FIGURE_IMAGE",
            "reason": "VISION_PROVIDER_UNAVAILABLE",
        }

    monkeypatch.setattr(figure_module, "resolve_figure_image_locator", fake_image_locator)

    async def fake_quote_locator(_db, *, question, kb_ids):
        return {"status": "NOT_FOUND", "locator_version": "test", "reason": "no_text_quote"}

    _patch_pipeline(monkeypatch, citations=[])
    monkeypatch.setattr(quote_locator, "resolve_quote_locator", fake_quote_locator)

    contract = await retrieval_orchestrator.prepare_knowledge_context(
        object(),
        question="这个图片在哪篇论文哪一页？",
        scope_snapshot=_SCOPE,
        run_id="run-novision",
        request_id="req-novision",
        image_bytes=b"fake-image-bytes",
    )
    assert contract["locator_resolution"]["status"] == "NOT_FOUND"
    assert "page" not in contract["locator_resolution"]
    assert contract["status"] == "DEGRADED"
    assert contract["figure_image_observation"] == {"available": False}


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
