"""未定位图片的输出授权治理金标（D1–D5 修复验收）。

2026-09 生产事故链：图片定位失败（指纹未命中 + 视觉未配置）→ 文本回退把
NOT_FOUND 覆盖成 NOT_APPLICABLE → 普通检索池照常开放 → MiniMax 编造
文献名/Figure 编号/页码 → citation_channel 给编造内容挂上看似正规的芯片。
本文件逐环验收修复。
"""

from __future__ import annotations

import re
import unicodedata

import pytest

import yuxi.knowledge.scope_gateway as scope_gateway_module
from yuxi.knowledge.evidence import quote_locator
from yuxi.knowledge.orchestration import retrieval_orchestrator
from yuxi.knowledge.rendering.answer_context_builder import build_answer_context
from yuxi.knowledge.rendering.citation_channel import apply_citation_channel
from yuxi.knowledge.vision import figure_image_locator as figure_module
from yuxi.knowledge.vision import provider as provider_module

pytestmark = [pytest.mark.unit]

_SCOPE = {
    "tenant_id": 1,
    "scope_version": 1,
    "knowledge_strategy": "KNOWLEDGE_FIRST",
    "members": [{"kb_id": "kb-a", "kb_name": "Paper", "kb_type": "milvus"}],
}


def _norm(text: str) -> str:
    value = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", " ", re.sub(r"[^0-9a-z一-鿿]+", " ", value.casefold())).strip()


def _citation(ref: str, page: int, quote: str, *, zone: str = "MAIN_TEXT") -> dict:
    return {
        "ref": ref,
        "evidence_id": f"ev-{ref}",
        "kb_id": "kb-a",
        "file_id": "file-a",
        "filename": "Plant Biotechnology Journal - 2024 - Liu - OsMYB73.pdf",
        "zone": zone,
        "page_numbers": [page],
        "primary_page": page,
        "quote_head": quote[:80],
        "anchor_ids": [f"ea-{ref}"],
        "locatable": True,
        "toc_line": False,
        "secondary_of": None,
        "_anchor_id": f"ea-{ref}",
        "_physical_evidence_id": f"ev-{ref}",
        "_retrieval_channel": "DOCUMENT",
        "_quote": quote,
        "_quote_norm": _norm(quote),
    }


def _patch_unlocated_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    *,
    citations: list[dict],
    image_result: dict,
    quote_result: dict | None = None,
):
    """图片定位失败 + 普通检索仍命中（不相关第 18 页内容）的最小现场。"""

    async def fake_gateway(*, query_text, scope_snapshot, top_k=12, verbatim=None):
        return {
            "evidence": [
                {
                    "evidence_id": "evdoc-18",
                    "source_type": "DOCUMENT",
                    "kb_id": "kb-a",
                    "file_id": "file-a",
                    "content": "Figure S18 Root hair density assay in hydroponic culture conditions.",
                }
            ],
            "warnings": [],
        }

    async def fake_citations(_db, rows):
        assert rows
        return citations

    async def fake_audit(*_args, **_kwargs):
        return None

    async def fake_image_locator(_db, *, kb_ids, image_bytes=None, observation=None):
        return dict(image_result)

    async def fake_quote_locator(_db, *, question, kb_ids):
        return dict(quote_result or {"status": "NOT_APPLICABLE", "locator_version": "test"})

    monkeypatch.setattr(scope_gateway_module, "query_knowledge_scope_gateway", fake_gateway)
    monkeypatch.setattr(figure_module, "resolve_figure_image_locator", fake_image_locator)
    monkeypatch.setattr(quote_locator, "resolve_quote_locator", fake_quote_locator)
    monkeypatch.setattr(retrieval_orchestrator, "_persist_audit", fake_audit)
    monkeypatch.setattr(retrieval_orchestrator, "_emit_knowledge_trace", lambda *_a, **_k: None)
    from yuxi.knowledge.rendering import citation_channel

    monkeypatch.setattr(citation_channel, "build_citations_for_contract", fake_citations)
    monkeypatch.setattr(provider_module, "get_vision_provider", lambda: provider_module.NullVisionProvider())
    monkeypatch.setattr(provider_module, "current_vision_status", lambda: {"status": "NOT_CONFIGURED"})


_VISION_UNAVAILABLE = {
    "status": "NOT_FOUND",
    "locator_version": "figure_image_locator_v3",
    "locator_kind": "FIGURE_IMAGE",
    "reason": "VISION_PROVIDER_UNAVAILABLE",
}


@pytest.mark.asyncio
async def test_d1_image_failure_not_overridden_by_text_fallback(monkeypatch: pytest.MonkeyPatch):
    """D1：纯图片问句（无可提取引文）→ 图片 NOT_FOUND 终局，不被 NOT_APPLICABLE 覆盖。"""
    unrelated = _citation("E1", 18, "Figure S18 Root hair density assay in hydroponic culture conditions.")
    _patch_unlocated_pipeline(monkeypatch, citations=[unrelated], image_result=_VISION_UNAVAILABLE)

    contract = await retrieval_orchestrator.prepare_knowledge_context(
        object(),
        question="这个图片在哪篇论文哪一页，是什么意思？",
        scope_snapshot=_SCOPE,
        run_id="run-d1",
        request_id="req-d1",
        image_bytes=b"unindexed-image",
    )
    resolution = contract["locator_resolution"]
    # 终局状态保留：NOT_FOUND + VISION_PROVIDER_UNAVAILABLE（而非 NOT_APPLICABLE）
    assert resolution["status"] == "NOT_FOUND"
    assert resolution["reason"] == "VISION_PROVIDER_UNAVAILABLE"
    ledger_stages = {entry["stage"]: entry["status"] for entry in resolution["attempt_ledger"]}
    assert ledger_stages["TEXT_FALLBACK"] == "NOT_APPLICABLE"
    assert resolution["vision_status"] == "NOT_CONFIGURED"
    # D3：未验证 → 引用池/上下文证据清空 + 回答证据计数显式 0
    assert contract["citations"] == []
    assert contract["completeness"]["returned_evidence_count"] == 0
    # D2：结构化策略
    policy = contract["answer_policy"]
    assert policy["mode"] == "UNLOCATED"
    assert policy["page_claim_allowed"] is False
    assert policy["document_citations_allowed"] is False
    assert policy["visual_explanation_allowed"] is False  # 视觉未配置 → 只允许保守说明
    assert policy["required_disclosure"]
    assert contract["locator_resolution"]["answer_policy"]["mode"] == "UNLOCATED"


@pytest.mark.asyncio
async def test_d1_text_verified_quote_still_wins_for_quote_bearing_question(monkeypatch: pytest.MonkeyPatch):
    """用户确实粘贴了引文文本时文本回退合法：文本 VERIFIED 覆盖图片失败。"""
    quote = "Two pairs of constructs, OsMYB73-VN173 and OsNF-YB1-VC155, were transformed into tobacco leaf cells."
    citation = _citation("E1", 15, quote)
    citation.update({"evidence_id": "ev-quote", "_physical_evidence_id": "ev-quote"})
    _patch_unlocated_pipeline(
        monkeypatch,
        citations=[citation],
        image_result=_VISION_UNAVAILABLE,
        quote_result={
            "status": "VERIFIED",
            "locator_version": "test",
            "page": 15,
            "zone": "MAIN_TEXT",
            "evidence_id": "ev-quote",
            "anchor_id": "ea-E1",
        },
    )

    contract = await retrieval_orchestrator.prepare_knowledge_context(
        object(),
        question=f"{quote} 这句话在哪个文献那一页？",
        scope_snapshot=_SCOPE,
        run_id="run-d1b",
        request_id="req-d1b",
        image_bytes=b"image-attached",
    )
    assert contract["locator_resolution"]["status"] == "VERIFIED"
    assert contract["locator_resolution"]["page"] == 15
    # VERIFIED → 引用保留（未被清空）
    assert contract["citations"]
    assert contract["answer_policy"]["mode"] == "LOCATOR_VERIFIED"


@pytest.mark.asyncio
async def test_d3_unlocated_generic_page18_never_leaks(monkeypatch: pytest.MonkeyPatch):
    """金标 #4：未定位 + 普通检索命中第 18 页 → 回答仍不得出现第 18 页/芯片/Figure 编号。"""
    unrelated = _citation("E1", 18, "Figure S18 Root hair density assay in hydroponic culture conditions.")
    _patch_unlocated_pipeline(monkeypatch, citations=[unrelated], image_result=_VISION_UNAVAILABLE)

    contract = await retrieval_orchestrator.prepare_knowledge_context(
        object(),
        question="这个图片在哪篇论文哪一页，是什么意思？",
        scope_snapshot=_SCOPE,
        run_id="run-d3",
        request_id="req-d3",
        image_bytes=b"unindexed-image",
    )
    policy = contract["answer_policy"]

    # 模型完全无视策略的输出：文献名 + Figure 编号 + 页码 + 自造芯片 + 定位行
    model_answer = (
        "已可靠定位到原文：〔引文定位｜正文·第18页｜Plant Biotechnology Journal - 2024 - Liu - OsMYB73.pdf〕\n\n"
        "该图出自 Liu 等 2024 年论文，是 Figure S18 第 18 页的根毛密度实验图。"
        "图片显示 hydroponic 条件下的表型。〔证据E1｜补充材料·第18页｜Plant Biotechnology Journal.pdf〕"
    )
    guarded, validation = apply_citation_channel(
        model_answer,
        contract.get("citations") or [unrelated],  # 即便调用方误传引用，守卫独立收权
        locator=contract.get("locator_resolution"),
        authority_policy=policy,
    )
    assert "第18页" not in guarded and "第 18 页" not in guarded
    assert "〔证据E1" not in guarded and "〔引文定位" not in guarded
    assert "Figure S18" not in guarded and "FigureS18" not in guarded
    assert "已可靠定位到原文" not in guarded
    assert policy["required_disclosure"] in guarded
    assert validation["answer_policy"]["citations_revoked"] is True
    assert validation["answer_policy"]["figure_label_claims_removed"] >= 1  # 芯片内编号随整块剥离，正文单独计数
    assert validation["answer_policy"]["disclosure_appended"] is True
    # G3 收紧：UNLOCATED（视觉未配置、无观察）→ 模型答案整体不可信，后端固定
    # 文案替换（编造的「根毛密度」描述与文献身份一并清除，零残留）
    assert validation["answer_policy"]["answer_replaced_by_policy"] is True
    assert "根毛密度" not in guarded and "Liu" not in guarded
    # 守卫幂等（双重应用字节稳定）
    twice, _ = apply_citation_channel(
        guarded, [unrelated], locator=contract.get("locator_resolution"), authority_policy=policy
    )
    assert twice == guarded


@pytest.mark.asyncio
async def test_policy_multiple_matches_mode(monkeypatch: pytest.MonkeyPatch):
    """MULTIPLE_MATCHES：不发布文献/页码，允许视觉解释，请求补充题注。"""
    unrelated = _citation("E1", 18, "Figure S18 Root hair density assay.")
    _patch_unlocated_pipeline(
        monkeypatch,
        citations=[unrelated],
        image_result={
            "status": "MULTIPLE_MATCHES",
            "locator_version": "figure_image_locator_v3",
            "locator_kind": "FIGURE_IMAGE",
            "match_count": 2,
            "reason": "figure_match_ambiguous",
        },
    )

    contract = await retrieval_orchestrator.prepare_knowledge_context(
        object(),
        question="这个图片在哪篇论文哪一页，是什么意思？",
        scope_snapshot=_SCOPE,
        run_id="run-mm",
        request_id="req-mm",
        image_bytes=b"ambiguous-image",
    )
    policy = contract["answer_policy"]
    assert policy["mode"] == "LOCATOR_AMBIGUOUS"
    assert policy["document_identity_allowed"] is False
    # G3 收紧：无可信视觉观察的歧义不作「图片内容解释」授权 → 守卫整体替换保守文案
    assert policy["visual_explanation_allowed"] is False
    assert "无法唯一确定" in (policy["required_disclosure"] or "")
    assert contract["citations"] == []
    assert contract["completeness"]["returned_evidence_count"] == 0


def test_d2_answer_policy_reaches_model_context():
    """D2：结构化策略真正进入模型上下文（而非只进审计 contract）。"""
    contract = {
        "retrieval_plan": {"intent": "GENERAL_KNOWLEDGE_QUERY", "query_mode": "BOUNDED", "answer_mode": "FREEFORM"},
        "evidence": [],
        "claims": [],
        "citations": [],
        "completeness": {},
        "locator_intent": {"sub_intents": []},
        "answer_policy": {
            "mode": "UNLOCATED",
            "document_identity_allowed": False,
            "figure_label_allowed": False,
            "page_claim_allowed": False,
            "document_citations_allowed": False,
            "visual_explanation_allowed": False,
            "required_disclosure": "当前无法对这张图片进行可靠定位，因此不能确定其来源文献与页码。",
        },
    }
    context = build_answer_context(contract)
    assert '"answer_policy":{"mode":"UNLOCATED"' in context
    assert "answer_policy 为本轮输出授权的硬约束" in context
    assert "required_disclosure" in context
    # 模型上下文不含任何可引用页码来源
    assert '"citations":[]' in context


@pytest.mark.asyncio
async def test_state_projection_exposes_vision_and_failure_stage():
    """状态模块：vision_status / answer_mode / failure_stage 从持久化账本投影。"""
    from types import SimpleNamespace

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from yuxi.storage.postgres.models_knowledge import (
        EvidenceAnchorRecord,
        EvidenceSpanRecord,
        KnowledgeChunk,
        KnowledgeFile,
        KnowledgeParseRevision,
    )

    from yuxi.knowledge.evidence.assembler import assemble_evidence_for_run
    from yuxi.repositories.knowledge_retrieval_repository import KnowledgeRetrievalRepository

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        for table in (
            KnowledgeFile.__table__,
            KnowledgeParseRevision.__table__,
            KnowledgeChunk.__table__,
            EvidenceAnchorRecord.__table__,
            EvidenceSpanRecord.__table__,
        ):
            await connection.run_sync(table.create)

    records = [
        SimpleNamespace(
            retrieval_id="kr_img",
            status="DEGRADED",
            intent="QUOTE_LOCATOR",
            chunk_ids_json=[],
            evidence_ids_json=[],
            locator_resolution_json={
                "status": "NOT_FOUND",
                "reason": "VISION_PROVIDER_UNAVAILABLE",
                "vision_status": "NOT_CONFIGURED",
                "answer_policy": {"mode": "UNLOCATED"},
                "attempt_ledger": [
                    {"stage": "ASSET_FINGERPRINT", "status": "NO_MATCH"},
                    {"stage": "VISION_OBSERVATION", "status": "NOT_CONFIGURED"},
                    {"stage": "CAPTION_BRIDGE", "status": "SKIPPED"},
                    {"stage": "TEXT_FALLBACK", "status": "NOT_APPLICABLE"},
                ],
            },
        )
    ]

    async def list_for_run(_repository, _run_id):
        return records

    KnowledgeRetrievalRepository.list_for_run = list_for_run
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        result = await assemble_evidence_for_run(session, "run_img", allowed_kb_ids={"kb-a"})
    await engine.dispose()

    assert result["locator_status"] == "NOT_FOUND"
    assert result["vision_status"] == "NOT_CONFIGURED"
    assert result["answer_mode"] == "UNLOCATED"
    assert result["failure_stage"] == "VISION_OBSERVATION"
    summary = result["summary"]
    assert summary["verified_binding_count"] == 0
    assert summary["answer_evidence_count"] == 0


def test_figure_label_regex_no_longer_eats_leading_english_word():
    """P1：'Figure 7 A schematic...' 不再被误归一成 figure 7a。"""
    from yuxi.knowledge.evidence.caption_locator import canonical_figure_label, extract_figure_label

    extracted = extract_figure_label("Figure 7 A schematic diagram of the CRISPR construct is shown.")
    assert extracted == "Figure 7"
    assert canonical_figure_label(extracted) == "figure 7"
    # 相邻 panel 字母仍正常识别
    assert canonical_figure_label("Figure 5A") == "figure 5a"
    assert canonical_figure_label("Figure S8") == "figure s8"
    assert extract_figure_label("见 Figure S8 如下") == "Figure S8"
