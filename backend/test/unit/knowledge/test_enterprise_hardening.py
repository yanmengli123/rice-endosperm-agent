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
