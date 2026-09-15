"""企业级收口金标（G1–G7）：footer-caption、全意图收权、零泄漏、三元组、同页双图。"""

from __future__ import annotations

import re
import unicodedata

import pytest

import yuxi.knowledge.scope_gateway as scope_gateway_module
from yuxi.knowledge.contracts.locator_binding import binding_from_locator_resolution
from yuxi.knowledge.evidence import quote_locator
from yuxi.knowledge.orchestration import retrieval_orchestrator
from yuxi.knowledge.rendering.answer_draft import render_answer_draft
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

_FIG4_CAPTION = (
    "Figure 4. Phenotypic characterization of cr-myb73 and OsMYB73-overexpression lines in T1 generation. "
    "(a) Grain length and width measurements. (b) Chalkiness degree comparison. Bar, 1.0 cm."
)


def _norm(text: str) -> str:
    value = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", " ", re.sub(r"[^0-9a-z一-鿿]+", " ", value.casefold())).strip()


def _citation(ref: str, page: int, quote: str) -> dict:
    return {
        "ref": ref,
        "evidence_id": f"ev-{ref}",
        "kb_id": "kb-a",
        "file_id": "file-a",
        "filename": "Plant Biotechnology Journal - 2024 - Liu - OsMYB73.pdf",
        "zone": "MAIN_TEXT",
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
        "_span_id": f"es-{ref}",
        "_span_evidence_id": f"evs-{ref}",
        "_parse_revision_id": "pr-active",
        "_index_revision_id": "ir-active",
        "_source_sha256": "a" * 64,
        "_quote": quote,
        "_quote_norm": _norm(quote),
    }


# ---- G1：footer-caption 存量兼容放行（Figure 4 第 8 页事故） ----


@pytest.fixture
async def footer_caption_session():
    import hashlib

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from yuxi.storage.postgres.models_knowledge import (
        EvidenceAnchorRecord,
        EvidenceSpanRecord,
        KnowledgeFile,
        KnowledgeParseRevision,
    )

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        for table in (
            KnowledgeFile.__table__,
            KnowledgeParseRevision.__table__,
            EvidenceAnchorRecord.__table__,
            EvidenceSpanRecord.__table__,
        ):
            await connection.run_sync(table.create)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(
            KnowledgeParseRevision(
                id=1,
                revision_id="spr_fig4",
                tenant_id=1,
                kb_id="kb-a",
                file_id="file_fig4",
                source_sha256="a" * 64,
                parser_fingerprint="f" * 64,
                pipeline_version="scientific_pdf_v3.0",
                status="INDEXED_FULL",
            )
        )
        session.add(
            KnowledgeFile(
                id=1,
                file_id="file_fig4",
                kb_id="kb-a",
                filename="liu-2024-pbj.pdf",
                active_parse_revision_id="spr_fig4",
                active_index_revision_id="sir_fig4",
            )
        )
        quote_hash = hashlib.sha256(_FIG4_CAPTION.encode()).hexdigest()
        # 生产形态：题注位于页面底部被 MinerU 归为 footer（anchor_type=footer），
        # 但 span 语义角色是 caption 且 span/anchor 页码与文本一致
        session.add(
            EvidenceAnchorRecord(
                id=1,
                anchor_id="ea_fig4_footer",
                parse_revision_id="spr_fig4",
                page=8,
                bbox=[40.0, 800.0, 280.0, 880.0],
                word_start=0,
                word_end=10,
                quote_hash=quote_hash,
                prefix_hash="p",
                suffix_hash="s",
                quote=_FIG4_CAPTION,
                fragments=[{"page_index": 7, "bbox": [40.0, 800.0, 280.0, 880.0], "coordinate_space": "pdf_points"}],
                anchor_type="footer",  # ← 误分类：布局在页脚
                locator_quality="HIGH",
                confidence=1.0,
                locatable=True,
                source="mineru",
                document_partition="MAIN_TEXT",
            )
        )
        session.add(
            EvidenceSpanRecord(
                id=1,
                tenant_id=1,
                parse_revision_id="spr_fig4",
                kb_id="kb-a",
                file_id="file_fig4",
                span_id="es_fig4",
                anchor_id="ea_fig4_footer",
                sentence_index=0,
                quote=_FIG4_CAPTION,
                quote_hash=quote_hash,
                page_number=8,
                evidence_type="caption",  # ← 语义角色：caption
                document_partition="MAIN_TEXT",
                partition_confidence=1.0,
                evidence_id="evs_fig4",
                container_label="Figure 4",
            )
        )
        await session.commit()
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_g1_footer_misclassified_caption_locates_page8(footer_caption_session):
    """金标：anchor_type=footer 的真实题注 → 正确正文第 8 页（不再 NOT_FOUND）。"""
    from yuxi.knowledge.evidence.caption_locator import resolve_figure_caption_locator

    resolution = await resolve_figure_caption_locator(
        footer_caption_session,
        figure_label="Figure 4",
        kb_ids=["kb-a"],
    )
    assert resolution is not None
    assert resolution["status"] == "VERIFIED"
    assert resolution["page"] == 8
    assert resolution["zone"] == "MAIN_TEXT"


@pytest.mark.asyncio
async def test_g1_running_head_footer_never_passes(footer_caption_session):
    """放行例外不给 running head/页码行开口子：形态判定与 anchor_type 无关。"""
    import hashlib

    from yuxi.storage.postgres.models_knowledge import EvidenceAnchorRecord, EvidenceSpanRecord

    running_head = "Plant Biotechnology Journal (2025) 23, pp. 1021–1038"
    quote_hash = hashlib.sha256(running_head.encode()).hexdigest()
    footer_caption_session.add(
        EvidenceAnchorRecord(
            id=2,
            anchor_id="ea_head_footer",
            parse_revision_id="spr_fig4",
            page=8,
            bbox=[40.0, 10.0, 280.0, 40.0],
            word_start=0,
            word_end=5,
            quote_hash=quote_hash,
            prefix_hash="p",
            suffix_hash="s",
            quote=running_head,
            anchor_type="footer",
            locator_quality="HIGH",
            confidence=1.0,
            locatable=True,
            source="mineru",
            document_partition="MAIN_TEXT",
        )
    )
    footer_caption_session.add(
        EvidenceSpanRecord(
            id=2,
            tenant_id=1,
            parse_revision_id="spr_fig4",
            kb_id="kb-a",
            file_id="file_fig4",
            span_id="es_head",
            anchor_id="ea_head_footer",
            sentence_index=1,
            quote=running_head,
            quote_hash=quote_hash,
            page_number=8,
            evidence_type="caption",
            document_partition="MAIN_TEXT",
            partition_confidence=1.0,
            evidence_id="evs_head",
            container_label="Figure 4",  # 伪装成 Figure 4 的页眉
        )
    )
    await footer_caption_session.commit()
    from yuxi.knowledge.evidence.caption_locator import resolve_figure_caption_locator

    # 真实题注（第 8 页）被放行的同一扫描中，running head 形态不放行；
    # 唯一题注仍定位第 8 页，页眉行不产生第二个候选
    resolution = await resolve_figure_caption_locator(footer_caption_session, figure_label="Figure 4", kb_ids=["kb-a"])
    assert resolution is not None
    assert resolution["status"] == "VERIFIED"
    assert resolution["page"] == 8
    assert resolution["anchor_id"] == "ea_fig4_footer"


# ---- G2/G3：全意图收权 + 未定位整体替换（Figure 4 题注 NOT_FOUND 生产事故） ----


def _patch_text_locator_failure(monkeypatch: pytest.MonkeyPatch, *, citations: list[dict]):
    async def fake_gateway(*, query_text, scope_snapshot, top_k=12, verbatim=None):
        return {
            "evidence": [
                {
                    "evidence_id": "evdoc-8",
                    "source_type": "DOCUMENT",
                    "kb_id": "kb-a",
                    "file_id": "file-a",
                    "content": "Phenotypic characterization across T1 generation lines on page 8 and 13.",
                }
            ],
            "warnings": [],
        }

    async def fake_citations(_db, rows):
        return citations

    async def fake_quote_locator(_db, *, question, kb_ids):
        return {"status": "NOT_FOUND", "locator_version": "test", "reason": "no_normalized_match"}

    monkeypatch.setattr(scope_gateway_module, "query_knowledge_scope_gateway", fake_gateway)
    monkeypatch.setattr(quote_locator, "resolve_quote_locator", fake_quote_locator)
    monkeypatch.setattr(retrieval_orchestrator, "_persist_audit", lambda *a, **k: _async_none())
    monkeypatch.setattr(retrieval_orchestrator, "_emit_knowledge_trace", lambda *a, **k: None)
    from yuxi.knowledge.rendering import citation_channel

    monkeypatch.setattr(citation_channel, "build_citations_for_contract", fake_citations)


async def _async_none():
    return None


@pytest.mark.asyncio
async def test_g2_text_caption_failure_revokes_citations_and_identity(monkeypatch: pytest.MonkeyPatch):
    """G2：题注定位失败（无图片）同样收权——引用池清空、policy UNLOCATED、计数 0。"""
    unrelated = _citation("E1", 8, "Phenotypic characterization across T1 generation lines at page 8.")
    _patch_text_locator_failure(monkeypatch, citations=[unrelated])

    contract = await retrieval_orchestrator.prepare_knowledge_context(
        object(),
        question=f"{_FIG4_CAPTION} 这句话在哪篇文献哪一页，是什么意思？",
        scope_snapshot=_SCOPE,
        run_id="run-g2",
        request_id="req-g2",
    )
    assert contract["locator_resolution"]["status"] == "NOT_FOUND"
    assert contract["citations"] == []
    assert contract["context_evidence"] == []
    assert contract["completeness"]["returned_evidence_count"] == 0
    policy = contract["answer_policy"]
    assert policy["mode"] == "UNLOCATED"
    assert policy["document_identity_allowed"] is False
    assert policy["page_claim_allowed"] is False

    # G3：模型宣称文献身份 + 页码 → 守卫整体替换为固定文案（无观察不可信）
    model_answer = (
        "该图注出自 Liu 等 2024 年发表在 Plant Biotechnology Journal 的论文，"
        "对应 Figure 4，位于正文第 8 页。〔证据E1｜正文·第8页｜paper.pdf〕"
    )
    guarded, validation = apply_citation_channel(
        model_answer,
        [unrelated],
        authority_policy=policy,  # 调用方误传引用也无效
    )
    assert validation["answer_policy"]["answer_replaced_by_policy"] is True
    assert "Liu" not in guarded
    assert "第8页" not in guarded and "第 8 页" not in guarded
    assert "〔证据E1" not in guarded
    assert policy["required_disclosure"] in guarded
    # 幂等
    twice, _ = apply_citation_channel(guarded, [unrelated], authority_policy=policy)
    assert twice == guarded


@pytest.mark.asyncio
async def test_g2_verified_text_locator_keeps_citations(monkeypatch: pytest.MonkeyPatch):
    """定位 VERIFIED 的文本流不受影响：引用保留、policy VERIFIED。"""
    quote = "OsMYB73 protein contains two SANT domains spanning residues 115-164 aa."
    citation = _citation("E1", 15, quote)
    citation.update({"evidence_id": "ev-quote", "_physical_evidence_id": "ev-quote"})

    async def fake_quote_locator(_db, *, question, kb_ids):
        return {
            "status": "VERIFIED",
            "locator_version": "test",
            "page": 15,
            "zone": "MAIN_TEXT",
            "evidence_id": "ev-quote",
            "anchor_id": "ea-E1",
            "container_label": None,
        }

    async def fake_gateway(*, query_text, scope_snapshot, top_k=12, verbatim=None):
        return {"evidence": [{"evidence_id": "ev-quote", "kb_id": "kb-a", "file_id": "file-a"}], "warnings": []}

    async def fake_citations(_db, rows):
        return [citation]

    monkeypatch.setattr(scope_gateway_module, "query_knowledge_scope_gateway", fake_gateway)
    monkeypatch.setattr(quote_locator, "resolve_quote_locator", fake_quote_locator)
    monkeypatch.setattr(retrieval_orchestrator, "_persist_audit", lambda *a, **k: _async_none())
    monkeypatch.setattr(retrieval_orchestrator, "_emit_knowledge_trace", lambda *a, **k: None)
    from yuxi.knowledge.rendering import citation_channel

    monkeypatch.setattr(citation_channel, "build_citations_for_contract", fake_citations)

    contract = await retrieval_orchestrator.prepare_knowledge_context(
        object(),
        question=f"{quote} 这句话在哪个文献那一页？",
        scope_snapshot=_SCOPE,
        run_id="run-g2b",
        request_id="req-g2b",
    )
    assert contract["locator_resolution"]["status"] == "VERIFIED"
    assert contract["citations"]
    policy = contract["answer_policy"]
    assert policy["mode"] == "LOCATOR_VERIFIED"
    # G7：纯文本定位无 Figure 编号 → figure_identity_binding UNRESOLVED → 编号不可发
    assert policy["figure_label_allowed"] is False
    assert policy["page_claim_allowed"] is True


# ---- G4：结构化草稿非法时零泄漏 ----


def test_g4_broken_draft_protocol_never_leaks():
    """生产形态：草案 JSON 截断 + 闭合标签泄漏 → 受限 repair 只回收纯文本。"""
    broken = (
        '<YUXI_ANSWER_DRAFT>{"schema_version":"answer-draft.v1","blocks":['
        '{"type":"paragraph","text":"OsMYB73 在灌浆期籽粒中高表达，定位于细胞核。"},'
        '{"type":"paragraph","text":"敲除后籽粒出现垩白表型。"},'
        '{"type":"hea'
        '},{"type":"paragraph","text":"残缺块'
    )
    rendered, meta = render_answer_draft(broken + "</YUXI_ANSWER_DRAFT>")
    assert meta["status"] == "ANSWER_DRAFT_REPAIRED"
    assert "OsMYB73" in rendered and "垩白" in rendered
    # 零协议泄漏
    assert "<YUXI_ANSWER_DRAFT>" not in rendered and "</YUXI_ANSWER_DRAFT>" not in rendered
    assert '{"type"' not in rendered and '"schema_version"' not in rendered


def test_g4_unrepairable_draft_replaced_by_safe_answer():
    """repair 也失败 → 后端确定性安全文案，永不原样返回 JSON/标签。"""
    garbage = '<YUXI_ANSWER_DRAFT>{"schema_version":"answer-draft.v1","blocks":[{"type":"head'
    rendered, meta = render_answer_draft(garbage + "</YUXI_ANSWER_DRAFT>")
    assert meta["status"] == "ANSWER_DRAFT_SCHEMA_INVALID"
    assert "YUXI_ANSWER_DRAFT" not in rendered
    assert "{" not in rendered
    assert "重新提问" in rendered


def test_g4_plain_bad_json_without_protocol_keeps_legacy():
    """无协议标签的普通坏 JSON：维持 Legacy Markdown 兼容（不误伤普通回答）。"""
    text = "普通回答文本，含一个孤立的 { 坏 JSON 片段。"
    rendered, meta = render_answer_draft(text)
    assert meta["status"] == "LEGACY_MARKDOWN"
    assert rendered == text


# ---- G5：ready 门禁 ----


def test_g5_provider_ready_requires_canary_ready(monkeypatch):
    provider = provider_module.ChatModelVisionProvider("minimax-cn:MiniMax-M3")
    monkeypatch.setattr(
        provider_module,
        "current_vision_status",
        lambda: {"status": provider_module.VISION_CONFIGURED},
    )
    assert provider.available is True  # spec 非空
    assert provider.ready is False  # canary 未确认 → 运行门禁不放行
    monkeypatch.setattr(
        provider_module,
        "current_vision_status",
        lambda: {"status": provider_module.VISION_READY},
    )
    assert provider.ready is True
    monkeypatch.setattr(
        provider_module,
        "current_vision_status",
        lambda: {"status": provider_module.VISION_PROVIDER_FAILED},
    )
    assert provider.ready is False


@pytest.mark.asyncio
async def test_g5_orchestrator_skips_provider_when_not_ready(monkeypatch: pytest.MonkeyPatch):
    """canary PROVIDER_FAILED → 运行时不调用 provider，账本记 PROVIDER_NOT_READY。"""
    call_state = {"describe_called": False}

    class _FailedCanaryProvider:
        available = True
        ready = False
        model_spec = "minimax-cn:MiniMax-M3"

        async def describe(self, _image_bytes):
            call_state["describe_called"] = True
            return None

    async def fake_image_locator(_db, *, kb_ids, image_bytes=None, observation=None):
        if observation is None:
            return {
                "status": "NOT_FOUND",
                "locator_version": "figure_image_locator_v3",
                "locator_kind": "FIGURE_IMAGE",
                "reason": "VISION_PROVIDER_UNAVAILABLE",
            }
        return {"status": "NOT_FOUND", "locator_version": "test", "reason": "two_signal"}

    monkeypatch.setattr(provider_module, "get_vision_provider", lambda: _FailedCanaryProvider())
    monkeypatch.setattr(figure_module, "resolve_figure_image_locator", fake_image_locator)
    unrelated = _citation("E1", 18, "Figure S18 root hair density assay.")
    _patch_unlocated(monkeypatch, [unrelated])

    contract = await retrieval_orchestrator.prepare_knowledge_context(
        object(),
        question="这个图片在哪篇论文哪一页，是什么意思？",
        scope_snapshot=_SCOPE,
        run_id="run-g5",
        request_id="req-g5",
        image_bytes=b"image",
    )
    assert call_state["describe_called"] is False  # canary FAILED → 不调用
    stages = {entry["stage"]: entry["status"] for entry in contract["locator_resolution"]["attempt_ledger"]}
    assert stages["VISION_OBSERVATION"] == "PROVIDER_NOT_READY"


def _patch_unlocated(monkeypatch, citations):
    async def fake_gateway(*, query_text, scope_snapshot, top_k=12, verbatim=None):
        return {"evidence": [{"evidence_id": "evdoc", "kb_id": "kb-a", "file_id": "file-a"}], "warnings": []}

    async def fake_citations(_db, rows):
        return citations

    async def fake_quote(_db, *, question, kb_ids):
        return {"status": "NOT_APPLICABLE", "locator_version": "test"}

    monkeypatch.setattr(scope_gateway_module, "query_knowledge_scope_gateway", fake_gateway)
    monkeypatch.setattr(retrieval_orchestrator, "_persist_audit", lambda *a, **k: _async_none())
    monkeypatch.setattr(retrieval_orchestrator, "_emit_knowledge_trace", lambda *a, **k: None)
    from yuxi.knowledge.rendering import citation_channel

    monkeypatch.setattr(citation_channel, "build_citations_for_contract", fake_citations)
    monkeypatch.setattr(quote_locator, "resolve_quote_locator", fake_quote)
    monkeypatch.setattr(provider_module, "current_vision_status", lambda: {"status": "PROVIDER_FAILED"})


# ---- G7：Binding 三元组 + 同页双 Figure ----


def test_g7_binding_triple_split():
    """页码已验证但编号未确认 → page_binding VERIFIED / figure_identity UNRESOLVED。"""
    binding = binding_from_locator_resolution(
        {
            "status": "VERIFIED",
            "page": 10,
            "zone": "UNKNOWN",
            "container_label": None,  # 生产形态：图片匹配第 10 页但编号未确认
            "evidence_id": "ev_fig5",
            "anchor_id": "ea_fig5",
            "evidence_type": "caption",
            "backlinks": [{"page": 6, "zone": "MAIN_TEXT"}],
        },
        retrieval_id="kr_1",
        locator_kind="FIGURE_IMAGE",
    )
    assert binding.page_binding == "VERIFIED"
    assert binding.figure_identity_binding == "UNRESOLVED"
    assert binding.explanation_grounding == "VERIFIED"  # 有正文回链

    identity_binding = binding_from_locator_resolution(
        {
            "status": "VERIFIED",
            "page": 4,
            "container_label": "Figure 1",
            "evidence_id": "ev_fig1",
            "anchor_id": "ea_fig1",
            "evidence_type": "caption",
        },
        retrieval_id="kr_2",
    )
    assert identity_binding.figure_identity_binding == "VERIFIED"
    assert identity_binding.explanation_grounding == "PARTIAL"  # 仅题注，无正文回链


def test_g7_same_page_two_figures_are_distinct_locations():
    """同页两个不同 Figure：物理键含 anchor_id → 不被静默合并为唯一位置。"""
    from yuxi.knowledge.vision.figure_image_locator import (
        TIER_V0_EXACT_ASSET_SHA,
        adjudicate_deterministic_match,
    )

    digest_a, digest_b = "a" * 64, "b" * 64

    def _entity(label, anchor_id, asset_digest):
        caption = f"{label}. Content of this specific figure on the same page."
        return {
            "entity_key": label,
            "container_label": label,
            "caption": caption,
            "caption_norm": _norm(caption),
            "caption_page": 9,
            "kb_id": "kb-a",
            "file_id": "file-a",
            "filename": "paper.pdf",
            "source_sha256": "a" * 64,
            "parse_revision_id": "pr_1",
            "index_revision_id": "ir_1",
            "span_id": "es_1",
            "span_evidence_id": "evs_1",
            "zone": "MAIN_TEXT",
            "assets": [
                {
                    "anchor_id": anchor_id,
                    "page": 9,
                    "bbox": [40.0, 100.0, 280.0, 180.0],
                    "asset_digest": asset_digest,
                    "asset_phash": None,
                    "panel_phashes": {},
                    "img_path": "",
                    "width": 0,
                    "height": 0,
                }
            ],
        }

    # 两图同页（page=9）：上传图 A → 只命中 A，页码唯一可发布（identity=A）
    resolution = adjudicate_deterministic_match(
        [_entity("Figure 6", "ea_fig6", digest_a), _entity("Figure 7", "ea_fig7", digest_b)],
        image_asset_digest=digest_a,
    )
    assert resolution["status"] == "VERIFIED"
    assert resolution["tier"] == TIER_V0_EXACT_ASSET_SHA
    assert resolution["asset"]["anchor_id"] == "ea_fig6"


# ---- H1：VISUAL_ONLY_UNLOCATED 文献身份零泄漏（评审原例） ----


def test_h1_visual_only_unlocated_document_identity_never_leaks():
    """评审原例：页码/Figure 已剥离但「该图出自 Liu 2024 年 … 论文」必须一并清零。"""
    policy = {
        "mode": "VISUAL_ONLY_UNLOCATED",
        "locator_kind": "FIGURE_IMAGE",
        "document_identity_allowed": False,
        "figure_label_allowed": False,
        "page_claim_allowed": False,
        "document_citations_allowed": False,
        "visual_explanation_allowed": True,
        "required_disclosure": "可以解释图片可见内容，但无法可靠确定来源文献和页码。",
    }
    text = "图片显示绿色荧光。该图出自 Liu 2024 年的 Plant Biotechnology Journal 论文。Figure 9，第12页。"
    guarded, validation = apply_citation_channel(text, [], authority_policy=policy)
    assert "Liu" not in guarded
    assert "Plant Biotechnology Journal" not in guarded
    assert "论文" not in guarded
    assert "Figure 9" not in guarded and "第12页" not in guarded
    assert "绿色荧光" in guarded  # 视觉描述保留
    assert validation["answer_policy"]["document_identity_claims_removed"] == 1
    assert validation["answer_policy"]["answer_replaced_by_policy"] is False  # 有观察：不做整体替换
    assert policy["required_disclosure"] in guarded
    # 幂等
    twice, _ = apply_citation_channel(guarded, [], authority_policy=policy)
    assert twice == guarded


def test_h1_identity_strip_keeps_normal_sentences():
    """只剥离身份断言句：普通功能描述（不含出处形态）不误伤。"""
    policy = {
        "mode": "VISUAL_ONLY_UNLOCATED",
        "document_identity_allowed": False,
        "figure_label_allowed": False,
        "page_claim_allowed": False,
        "document_citations_allowed": False,
        "visual_explanation_allowed": True,
        "required_disclosure": "可以解释图片可见内容，但无法可靠确定来源文献和页码。",
    }
    text = "图中可见三个 panel 的柱状图，绿色信号集中在细胞核区域。作者在 2024 年使用了 GUS 染色方法。"
    guarded, validation = apply_citation_channel(text, [], authority_policy=policy)
    assert "柱状图" in guarded and "GUS 染色" in guarded
    # 「作者在 2024 年使用…」无出处动词 + 无论文/期刊尾词 → 不是身份断言，保留
    assert validation["answer_policy"]["document_identity_claims_removed"] == 0


# ---- H2：Binding 三元组为唯一授权源 ----


def test_h2_policy_consumes_binding_triple():
    """页码 VERIFIED + 编号 UNRESOLVED + 无正文回链 → 页码可发/编号不可发/机制不可归因。"""
    from yuxi.knowledge.orchestration.retrieval_orchestrator import _build_answer_policy

    policy = _build_answer_policy(
        status="VERIFIED",
        observation_available=False,
        vision_status="NOT_CONFIGURED",
        locator_kind="FIGURE_IMAGE",
        binding={
            "binding_id": "vlb-h2-unresolved",
            "status": "VERIFIED",
            "page_number": 10,
            "physical_evidence_id": "ev-h2-unresolved",
            "page_binding": "VERIFIED",
            "figure_identity_binding": "UNRESOLVED",
            "explanation_grounding": "UNRESOLVED",
        },
    )
    assert policy["page_claim_allowed"] is True
    assert policy["figure_label_allowed"] is False  # Binding 覆盖顶层 container_label
    assert policy["mechanism_attribution_allowed"] is False
    assert policy["caption_fact_allowed"] is False
    assert policy["explanation_grounding"] == "UNRESOLVED"

    grounded = _build_answer_policy(
        status="VERIFIED",
        observation_available=False,
        vision_status="READY",
        locator_kind="FIGURE_CAPTION",
        binding={
            "binding_id": "vlb-h2-grounded",
            "status": "VERIFIED",
            "page_number": 10,
            "physical_evidence_id": "ev-h2-grounded",
            "page_binding": "VERIFIED",
            "figure_identity_binding": "VERIFIED",
            "explanation_grounding": "VERIFIED",
        },
    )
    assert grounded["figure_label_allowed"] is True
    assert grounded["mechanism_attribution_allowed"] is True
    assert grounded["caption_fact_allowed"] is True

    partial = _build_answer_policy(
        status="VERIFIED",
        observation_available=False,
        vision_status="READY",
        locator_kind="FIGURE_CAPTION",
        binding={
            "binding_id": "vlb-h2-partial",
            "status": "VERIFIED",
            "page_number": 10,
            "physical_evidence_id": "ev-h2-partial",
            "page_binding": "VERIFIED",
            "figure_identity_binding": "VERIFIED",
            "explanation_grounding": "PARTIAL",
        },
    )
    assert partial["mechanism_attribution_allowed"] is False  # 仅题注，无正文回链
    assert partial["caption_fact_allowed"] is True


# ---- H2b：机制归因执行（删除而非仅审计） ----


def test_h2b_mechanism_claims_removed_when_grounding_unverified():
    from yuxi.knowledge.rendering.explanation_claims import enforce_explanation_grounding

    policy = {"mechanism_attribution_allowed": False}
    text = (
        "图中可见 a/b/c 三个 panel。\n"
        "这说明 OsMYB73 调控淀粉合成通路。\n"
        "据此认为该基因参与籽粒发育调控。\n"
        "绿色荧光信号集中在细胞核区域。"
    )
    enforced, removed = enforce_explanation_grounding(text, policy=policy)
    assert removed == 2
    assert "调控" not in enforced and "据此认为" not in enforced
    assert "panel" in enforced and "绿色荧光" in enforced  # 视觉描述保留
    # 幂等 + 未授权时原样返回
    again, again_removed = enforce_explanation_grounding(enforced, policy=policy)
    assert again == enforced and again_removed == 0
    untouched, _ = enforce_explanation_grounding(text, policy=None)
    assert untouched == text


# ---- I2：身份收权升级（评审实测绕过形态逐条封堵） ----


def _unlocated_policy() -> dict:
    return {
        "mode": "VISUAL_ONLY_UNLOCATED",
        "document_identity_allowed": False,
        "figure_label_allowed": False,
        "page_claim_allowed": False,
        "document_citations_allowed": False,
        "visual_explanation_allowed": True,
        "required_disclosure": "可以解释图片可见内容，但无法可靠确定来源文献和页码。",
    }


def test_i2_english_source_line_bypass_closed():
    """评审原例：'Source: Liu et al. 2024, Plant Biotechnology Journal.' 不再穿透。"""
    guarded, validation = apply_citation_channel(
        "Visible green signal.\nSource: Liu et al. 2024, Plant Biotechnology Journal.",
        [],
        authority_policy=_unlocated_policy(),
    )
    assert "Liu" not in guarded and "Plant Biotechnology Journal" not in guarded
    assert "Source" not in guarded
    assert "green signal" in guarded
    assert validation["answer_policy"]["document_identity_claims_removed"] == 1


def test_i2_chinese_identity_variants_closed():
    """评审原例：来源为/依据…的研究/《标题》三种中文变体全部封堵。"""
    text = (
        "图中可见绿色荧光信号。\n"
        "来源为 Liu 等（2024），Plant Biotechnology Journal。\n"
        "依据 Liu 2024 的研究。\n"
        "对应《A novel transcription factor OsMYB73 affects grain size and chalkiness》。\n"
        "荧光集中在细胞核区域。"
    )
    guarded, validation = apply_citation_channel(text, [], authority_policy=_unlocated_policy())
    assert validation["answer_policy"]["document_identity_claims_removed"] == 3
    assert "Liu" not in guarded and "Plant Biotechnology" not in guarded
    assert "A novel transcription factor" not in guarded and "《" not in guarded
    assert "绿色荧光" in guarded and "细胞核" in guarded


def test_i2_normal_sentences_still_not_stripped():
    text = "图中可见三个 panel 的柱状图。作者在 2024 年使用了 GUS 染色方法。数据来源为三个生物学重复。"
    guarded, validation = apply_citation_channel(text, [], authority_policy=_unlocated_policy())
    assert validation["answer_policy"]["document_identity_claims_removed"] == 0
    assert "GUS" in guarded
    assert "三个生物学重复" in guarded


def test_i2_markdown_source_prefix_is_removed_without_losing_visual_sentence():
    text = "图中可见绿色信号。\n- **Source:** Liu et al. 2024, Plant Biotechnology Journal."

    guarded, validation = apply_citation_channel(text, [], authority_policy=_unlocated_policy())

    assert "绿色信号" in guarded
    assert "Source" not in guarded and "Liu" not in guarded
    assert validation["answer_policy"]["document_identity_claims_removed"] == 1


# ---- I1：Binding 否决不再走顶层 VERIFIED 回退 ----


def test_i1_binding_veto_overrides_top_level_verified():
    """评审构造：顶层 VERIFIED + Binding.page_binding=UNRESOLVED → 全部拒绝。"""
    from yuxi.knowledge.orchestration.retrieval_orchestrator import _build_answer_policy

    policy = _build_answer_policy(
        status="VERIFIED",
        observation_available=False,
        vision_status="READY",
        locator_kind="FIGURE_IMAGE",
        binding={
            "binding_id": "vlb-i1-veto",
            "status": "VERIFIED",
            "page_number": None,
            "page_binding": "UNRESOLVED",
            "figure_identity_binding": "UNRESOLVED",
            "explanation_grounding": "UNRESOLVED",
        },
    )
    assert policy["page_claim_allowed"] is False
    assert policy["figure_label_allowed"] is False
    assert policy["document_identity_allowed"] is False
    assert policy["document_citations_allowed"] is False


def test_i1_verified_without_binding_fails_closed():
    """Binding 缺失 + 顶层 VERIFIED → 失败关闭（不再回退授权）。"""
    from yuxi.knowledge.orchestration.retrieval_orchestrator import _build_answer_policy

    policy = _build_answer_policy(
        status="VERIFIED",
        observation_available=False,
        vision_status="READY",
        locator_kind="QUOTE_LOCATOR",
        binding=None,
    )
    assert policy["mode"] == "LOCATOR_DEGRADED_NO_BINDING"
    assert policy["page_claim_allowed"] is False
    assert policy["document_citations_allowed"] is False
    assert policy["required_disclosure"]


def test_i1_nonverified_binding_status_overrides_stale_top_level_status():
    from yuxi.knowledge.orchestration.retrieval_orchestrator import _build_answer_policy

    policy = _build_answer_policy(
        status="NOT_FOUND",  # stale audit projection
        observation_available=False,
        vision_status="READY",
        locator_kind="FIGURE_IMAGE",
        binding={
            "binding_id": "vlb-i1-multiple",
            "status": "MULTIPLE_MATCHES",
            "page_binding": "UNRESOLVED",
        },
    )

    assert policy["mode"] == "LOCATOR_AMBIGUOUS"
    assert policy["page_claim_allowed"] is False


# ---- I3：定位状态一致性（Answer–State Mismatch = 0） ----


def test_i3_verified_mode_strips_unlocated_contradiction():
    """生产事故复现：芯片第 8 页 + 正文'该图注本身未被定位到具体页码'→ 矛盾句删除。"""
    policy = {
        "mode": "LOCATOR_VERIFIED",
        "document_identity_allowed": True,
        "figure_label_allowed": True,
        "page_claim_allowed": True,
        "document_citations_allowed": True,
        "visual_explanation_allowed": True,
        "required_disclosure": None,
    }
    text = (
        "已可靠定位到原文：〔引文定位｜正文·第8页｜liu-2024-pbj.pdf〕\n\n"
        "该 Figure 4 图注本身在当前证据策略下未被定位到具体页码。\n\n"
        "图注描述了 T1 代株系的表型分析。"
    )
    guarded, validation = apply_citation_channel(
        text,
        [],
        locator={
            "status": "VERIFIED",
            "page": 99,
            "zone": "MAIN_TEXT",
            "filename": "wrong-top-level.pdf",
            "binding": {
                "binding_id": "vlb-i3-page8",
                "status": "VERIFIED",
                "page_binding": "VERIFIED",
                "page_number": 8,
                "physical_evidence_id": "ev-i3-page8",
                "partition": "MAIN_TEXT",
                "filename": "liu-2024-pbj.pdf",
            },
        },
        authority_policy=policy,
    )
    # 后端权威定位行保留；矛盾句删除；正常解释保留
    assert "已可靠定位到原文" in guarded
    assert "第8页" in guarded and "第99页" not in guarded
    assert "未被定位到" not in guarded and "无法定位" not in guarded
    assert "表型分析" in guarded
    assert validation["answer_policy"]["locator_contradiction_claims_removed"] == 1


def test_i3_unlocated_mode_strips_success_claims():
    policy = _unlocated_policy()
    text = "已成功定位到论文第 8 页。图中可见绿色荧光。"
    guarded, validation = apply_citation_channel(text, [], authority_policy=policy)
    assert "成功定位" not in guarded
    assert "绿色荧光" in guarded


def test_i3_verified_mode_strips_english_unknown_page_claim():
    policy = {
        "page_claim_allowed": True,
        "document_citations_allowed": True,
        "document_identity_allowed": True,
    }
    locator = {
        "status": "VERIFIED",
        "binding": {
            "binding_id": "vlb-i3-english",
            "status": "VERIFIED",
            "page_binding": "VERIFIED",
            "page_number": 4,
            "physical_evidence_id": "ev-i3-english",
            "partition": "MAIN_TEXT",
            "filename": "paper.pdf",
        },
    }

    guarded, validation = apply_citation_channel(
        "The page is unknown. The image contains three panels.",
        [],
        locator=locator,
        authority_policy=policy,
    )

    assert "page is unknown" not in guarded
    assert "three panels" in guarded
    assert validation["answer_policy"]["locator_contradiction_claims_removed"] == 1


# ---- I4：机制句逐 Claim 证据授权 ----


def _main_text_citation(quote: str) -> dict:
    return {
        "ref": "E1",
        "evidence_id": "ev-E1",
        "kb_id": "kb-a",
        "file_id": "file-a",
        "filename": "paper.pdf",
        "zone": "MAIN_TEXT",
        "page_numbers": [6],
        "primary_page": 6,
        "quote_head": quote[:80],
        "anchor_ids": ["ea-E1"],
        "locatable": True,
        "toc_line": False,
        "secondary_of": None,
        "_anchor_id": "ea-E1",
        "_physical_evidence_id": "ev-E1",
        "_retrieval_channel": "DOCUMENT",
        "_span_id": "es-E1",
        "_span_evidence_id": "evs-E1",
        "_parse_revision_id": "pr-active",
        "_index_revision_id": "ir-active",
        "_source_sha256": "a" * 64,
        "_quote": quote,
        "_quote_norm": _norm(quote),
    }


def test_i1_output_guard_vetoes_a_forged_allow_policy():
    """输出守卫独立核验 Binding，不信任上游误传的全 True 权限位。"""
    forged_policy = {
        "mode": "LOCATOR_VERIFIED",
        "document_identity_allowed": True,
        "figure_label_allowed": True,
        "page_claim_allowed": True,
        "document_citations_allowed": True,
        "visual_explanation_allowed": True,
    }
    locator = {
        "status": "VERIFIED",
        "page": 8,
        "binding": {
            "binding_id": "vlb-forged-allow",
            "status": "VERIFIED",
            "page_binding": "UNRESOLVED",
            "page_number": None,
        },
    }
    text = "图中可见绿色信号 [E1]。该图出自 Liu 2024 年的一篇论文。"

    guarded, validation = apply_citation_channel(
        text,
        [_main_text_citation("The image contains a green signal.")],
        locator=locator,
        authority_policy=forged_policy,
    )

    assert "绿色信号" in guarded
    assert "证据E1" not in guarded and "第8页" not in guarded
    assert "Liu" not in guarded and "论文" not in guarded
    assert validation["answer_policy"]["citations_revoked"] is True


def test_i4_unsupported_mechanism_sentence_removed_even_with_pool():
    """评审原例：'Results support OsMYB73 as a negative regulator of Wx.' 无据 → 删除。"""
    from yuxi.knowledge.rendering.explanation_claims import enforce_explanation_grounding

    policy = {"mechanism_attribution_allowed": False}
    # 引用池存在但正文证据不含 OsMYB73/Wx（无法验证该 Claim）
    citations = [_main_text_citation("Grain length measurements across T1 generation lines.")]
    text = "Results support OsMYB73 as a negative regulator of Wx. 图中可见三个 panel。"
    enforced, removed = enforce_explanation_grounding(text, policy=policy, citations=citations)
    assert removed == 1
    assert "regulator" not in enforced and "Wx" not in enforced
    assert "panel" in enforced


def test_i4_supported_mechanism_sentence_kept():
    """有 VERIFIED 证据绑定的机制句保留（逐 Claim 授权，不是全局死刑）。"""
    from yuxi.knowledge.rendering.explanation_claims import enforce_explanation_grounding

    policy = {"mechanism_attribution_allowed": False}
    body_quote = "These results indicate that OsMYB73 acts as a negative regulator of Wx in rice endosperm."
    citations = [_main_text_citation(body_quote)]
    text = "Results support OsMYB73 as a negative regulator of Wx."
    enforced, removed = enforce_explanation_grounding(text, policy=policy, citations=citations)
    assert removed == 0
    assert "OsMYB73" in enforced


def test_i4_chinese_mechanism_variants_removed():
    """评审原例：结果支持/与模型一致/负向作用三种中文变体全部删除。"""
    from yuxi.knowledge.rendering.explanation_claims import enforce_explanation_grounding

    policy = {"mechanism_attribution_allowed": False}
    text = (
        "结果支持 OsMYB73 是 Wx 的上游负调控因子。\n"
        "这些数据与 OsMYB73 抑制 Wx 的模型一致。\n"
        "OsMYB73 对 Wx 具有负向作用。\n"
        "图中可见染色信号。"
    )
    enforced, removed = enforce_explanation_grounding(text, policy=policy, citations=[])
    assert removed == 3
    assert "Wx" not in enforced and "负调控" not in enforced
    assert "染色信号" in enforced


def test_i4_caption_fact_denied_strips_caption_claims():
    from yuxi.knowledge.rendering.explanation_claims import enforce_explanation_grounding

    policy = {"caption_fact_allowed": False, "mechanism_attribution_allowed": True}
    text = "题注中记载了 GUS 染色实验。图中可见三个 panel。"
    enforced, removed = enforce_explanation_grounding(text, policy=policy, citations=[])
    assert removed == 1
    assert "题注" not in enforced
    assert "panel" in enforced


def test_i4_global_allow_does_not_authorize_an_unbound_mechanism_claim():
    """全局能力位不是 Claim 通行证；每个机制句仍须绑定自己的正文证据。"""
    from yuxi.knowledge.rendering.explanation_claims import enforce_explanation_grounding

    citations = [_main_text_citation("Grain length measurements across T1 generation lines.")]
    text = "OsMYB73 regulates Wx expression in rice endosperm."

    enforced, removed = enforce_explanation_grounding(
        text,
        policy={"mechanism_attribution_allowed": True},
        citations=citations,
    )

    assert removed == 1
    assert "regulates" not in enforced


def test_i4_visual_prefix_cannot_bypass_mechanism_grounding():
    from yuxi.knowledge.rendering.explanation_claims import enforce_explanation_grounding

    text = "图中可见的结果支持 OsMYB73 调控 Wx。图中可见三个 panel。"
    enforced, removed = enforce_explanation_grounding(
        text,
        policy={"mechanism_attribution_allowed": True},
        citations=[],
    )

    assert removed == 1
    assert "调控 Wx" not in enforced
    assert "三个 panel" in enforced


def test_i4_caption_evidence_cannot_authorize_a_mechanism_claim():
    from yuxi.knowledge.rendering.explanation_claims import enforce_explanation_grounding

    caption = _main_text_citation("Figure 1 shows that OsMYB73 regulates Wx expression.")
    caption.update(
        {
            "zone": "SUPPORTING_INFO",
            "_anchor_id": "ea-caption",
            "anchor_ids": ["ea-caption"],
        }
    )
    text = "Figure 1 shows that OsMYB73 regulates Wx expression."

    enforced, removed = enforce_explanation_grounding(
        text,
        policy={"mechanism_attribution_allowed": True, "caption_fact_allowed": True},
        citations=[caption],
    )

    assert removed == 1
    assert "regulates" not in enforced


def test_i4_caption_fact_with_matching_caption_evidence_is_kept():
    from yuxi.knowledge.rendering.explanation_claims import enforce_explanation_grounding

    caption = _main_text_citation("Figure 1 shows GUS staining across rice tissues.")
    caption.update({"zone": "SUPPORTING_INFO", "_anchor_id": "ea-caption", "anchor_ids": ["ea-caption"]})
    text = "Figure 1 shows GUS staining across rice tissues."

    enforced, removed = enforce_explanation_grounding(
        text,
        policy={"caption_fact_allowed": True},
        citations=[caption],
    )

    assert removed == 0
    assert enforced == text
