"""P0 加固批测试：AC12-15、伪造定位芯片、能力状态机、观察缓存、资格收口、桥接消歧。"""

from __future__ import annotations

import re
import unicodedata

import pytest

from yuxi.knowledge.evidence.anchor_eligibility import (
    anchor_answer_eligible,
    is_answer_eligible,
    is_running_head,
)
from yuxi.knowledge.rendering.authority_markers import (
    AuthorityMarkerKind,
    count_authority_markers,
    parse_authority_markers,
)
from yuxi.knowledge.rendering.citation_channel import apply_citation_channel
from yuxi.knowledge.vision.visual_observation import VisualObservationEnvelope

pytestmark = [pytest.mark.unit]

_RUNNING_HEAD = "Plant Biotechnology Journal (2025) 23, pp. 1021–1038"


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
        "_quote": quote,
        "_quote_norm": _norm(quote),
    }


# ---- P0-A：统一 authority marker 解析器 ----


def test_authority_marker_parser_single_source():
    text = "结论如下 〔证据E3｜正文·第6页｜paper.pdf〕，定位 〔引文定位｜正文·第7页｜paper.pdf〕。"
    markers = parse_authority_markers(text)
    assert [marker["kind"] for marker in markers] == [
        AuthorityMarkerKind.EVIDENCE_CITATION,
        AuthorityMarkerKind.LOCATOR_CITATION,
    ]
    assert markers[0]["ref"] == "E3" and markers[1]["ref"] is None
    assert count_authority_markers(text) == {"EVIDENCE_CITATION": 1, "LOCATOR_CITATION": 1}


# ---- AC12：双重 guard 字节稳定 ----


def test_ac12_guard_is_idempotent_byte_for_byte():
    citations = [_citation("E1", 7, "OsMYB73 spans residues 115-164 aa with SANT domains in rice endosperm.")]
    locator = {"status": "VERIFIED", "page": 7, "zone": "MAIN_TEXT", "filename": "paper.pdf"}
    text = (
        "已可靠定位到原文：〔引文定位｜正文·第7页｜paper.pdf〕\n\n"
        "该基因编码的蛋白含 SANT 结构域，跨度 115-164 aa。〔证据E1｜正文·第7页｜paper.pdf〕\n\n"
        "第 5 页还有一处裸页码表述。"
    )
    once, validation1 = apply_citation_channel(text, citations, locator=locator)
    twice, validation2 = apply_citation_channel(once, citations, locator=locator)
    assert once == twice  # 双重应用字节级稳定（守卫+落库双重场景）
    assert validation2["changed"] is False


# ---- AC13：伪造定位芯片即使数值正确也必须剥离 ----


def test_ac13_fabricated_locator_chip_with_correct_values_is_stripped():
    """模型自造「〔引文定位｜正文·第4页｜合法文件名〕」即使数值碰巧正确：无后端签发 → 伪造。"""
    citations = []
    text = "模型自造芯片：已可靠定位到原文：〔引文定位｜正文·第4页｜Plant Biotechnology Journal - 2024 - Liu.pdf〕"
    guarded, validation = apply_citation_channel(text, citations)
    assert "引文定位" not in guarded
    assert "已可靠定位到原文" not in guarded
    assert validation["fabrication"]["fabricated_locator_removed"] == 1
    assert "第4页" not in guarded and "第 4 页" not in guarded


def test_fabricated_locator_chip_with_verified_locator_rewritten_to_canonical():
    citations = [_citation("E1", 7, "OsMYB73 spans residues 115-164 aa with SANT domains in rice endosperm.")]
    locator = {"status": "VERIFIED", "page": 7, "zone": "MAIN_TEXT", "filename": "paper.pdf"}
    text = "已可靠定位到原文：〔引文定位｜正文·第9页｜paper.pdf〕"  # 模型自造（页码错误）
    guarded, validation = apply_citation_channel(text, citations, locator=locator)
    # 后端已验证 → 权威芯片替换模型版本（第 7 页），不残留第 9 页
    assert "〔引文定位｜正文·第7页｜paper.pdf〕" in guarded
    assert "第9页" not in guarded
    assert validation["fabrication"]["fabricated_locator_removed"] == 1


# ---- P0-B：能力状态机 + 观察缓存 ----


@pytest.mark.asyncio
async def test_vision_capability_not_configured_by_default(monkeypatch):
    from yuxi.knowledge.vision import provider as provider_module

    monkeypatch.setattr(provider_module.app_config, "vision_model_spec", "", raising=False)
    capability = await provider_module.probe_vision_capability(force=True)
    assert capability["status"] == provider_module.VISION_NOT_CONFIGURED
    assert provider_module.current_vision_status()["status"] == provider_module.VISION_NOT_CONFIGURED


@pytest.mark.asyncio
async def test_vision_observation_cache_hits_by_composite_key(monkeypatch):
    from yuxi.knowledge.vision import provider as provider_module

    provider_module._OBSERVATION_CACHE.clear()
    calls = {"count": 0}
    observation = VisualObservationEnvelope.model_validate(
        {"schema_version": "visual-observation.v1", "figure_label": "Figure 1"}
    )

    async def fake_describe_uncached(self, image_bytes):
        calls["count"] += 1
        return observation

    monkeypatch.setattr(provider_module.ChatModelVisionProvider, "_describe_uncached", fake_describe_uncached)
    provider = provider_module.ChatModelVisionProvider("minimax-cn:MiniMax-M3")
    first = await provider.describe(b"same-image-bytes")
    second = await provider.describe(b"same-image-bytes")
    other = await provider.describe(b"different-image-bytes")
    assert first is not None and second is not None and other is not None
    assert calls["count"] == 2  # 同字节命中缓存；不同字节才真实调用
    assert second == first  # 缓存命中返回等值副本


def test_vision_prompt_uses_inferred_field_name():
    from yuxi.knowledge.vision.provider import OBSERVATION_PROMPT

    assert "inferred_caption_fragments" in OBSERVATION_PROMPT
    assert '"caption_fragments"' not in OBSERVATION_PROMPT


# ---- P0-C：inferred 题注片段退出信号计数 ----


def test_inferred_caption_fragments_alone_never_publish():
    from yuxi.knowledge.vision.figure_image_locator import adjudicate_figure_candidates

    observation = VisualObservationEnvelope.model_validate(
        {
            "schema_version": "visual-observation.v1",
            "figure_label": "Figure 1",
            "visible_text": [],
            "visible_entities": [],
            "inferred_caption_fragments": ["Rice OsMYB73 gene expression", "histochemical GUS staining"],
        }
    )
    caption = "Figure 1. Rice OsMYB73 gene expression analysis and histochemical GUS staining in transgenic rice seeds."
    entity = {
        "entity_key": "figure 1",
        "container_label": "Figure 1",
        "caption": caption,
        "caption_norm": _norm(caption),
        "caption_page": 4,
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
                "anchor_id": "ea_1",
                "page": 4,
                "bbox": [40.0, 400.0, 280.0, 480.0],
                "asset_digest": None,
                "asset_phash": None,
                "panel_phashes": {},
                "img_path": "",
                "width": 0,
                "height": 0,
            }
        ],
    }
    resolution = adjudicate_figure_candidates(observation, [entity])
    assert resolution["status"] == "NOT_FOUND"


# ---- P0-E：资格收口（可定位 ≠ 可作回答证据） ----


def test_running_head_detection_and_ineligibility():
    assert is_running_head(_RUNNING_HEAD)
    assert is_running_head("Plant Biotechnology Journal (2025) 23, pp. 1021-1038")
    # 正文引用形态（超长上下文）不误伤
    assert not is_running_head(
        "As reported previously in Nature Communications (2020) 12, pp. 1-9, the OsMYB73 "
        "transcription factor regulates grain chalkiness in rice endosperm development, and "
        "we confirmed this observation across three independent T1 generation lines."
    )
    assert not is_answer_eligible(anchor_type="text", quote=_RUNNING_HEAD)
    assert is_answer_eligible(anchor_type="text", quote="OsMYB73 is expressed in rice endosperm.")


def test_structural_header_anchor_types_are_ineligible():
    assert not is_answer_eligible(anchor_type="header", quote="任意文本")
    assert not is_answer_eligible(anchor_type="page_number", quote="7")


def test_anchor_answer_eligible_duck_typing():
    class _Anchor:
        anchor_type = "text"
        quote = _RUNNING_HEAD

    assert not anchor_answer_eligible(_Anchor())
    assert anchor_answer_eligible({"anchor_type": "chart", "quote": "Figure 3 grain starch data"})


def test_citation_pool_excludes_running_head_anchor():
    from yuxi.knowledge.rendering.citation_channel import build_citation_rows

    class _Anchor:
        def __init__(self, anchor_id, page, quote):
            self.anchor_id = anchor_id
            self.page = page
            self.bbox = [40.0, 100.0, 280.0, 180.0]
            self.word_start = 0
            self.word_end = 5
            self.quote = quote
            self.anchor_type = "text"
            self.locatable = True
            self.document_partition = "MAIN_TEXT"

    body_quote = "OsMYB73 protein contains two SANT domains spanning residues 115-164."
    rows = [
        {
            "evidence_id": "ev_body",
            "kb_id": "kb-a",
            "file_id": "file_main",
            "parse_revision_id": "pr_active",
            "evidence_quote": f"【页码】7\n【证据锚点】ea_body\n{body_quote}",
            "anchor_ids": ["ea_body"],
        },
        {
            "evidence_id": "ev_header",
            "kb_id": "kb-a",
            "file_id": "file_main",
            "parse_revision_id": "pr_active",
            "evidence_quote": f"【页码】7\n【证据锚点】ea_head\n{_RUNNING_HEAD}",
            "anchor_ids": ["ea_head"],
        },
    ]
    citations = build_citation_rows(
        rows,
        anchor_index={
            ("pr_active", "ea_body"): _Anchor("ea_body", 7, body_quote),
            ("pr_active", "ea_head"): _Anchor("ea_head", 7, _RUNNING_HEAD),
        },
        si_start_by_file={},
        filename_by_file={"file_main": "paper.pdf"},
    )
    quotes = [str(citation.get("quote_head") or citation.get("_quote") or "") for citation in citations]
    assert any("OsMYB73" in quote for quote in quotes)
    assert not any("Plant Biotechnology Journal (2025)" in quote for quote in quotes)
    # 硬过滤：页眉行不产生任何引用（不是降级占位）
    assert all(citation.get("evidence_id") != "ev_header" for citation in citations)


# ---- AC14：Caption Bridge 多文档消歧（纯函数层） ----


@pytest.mark.asyncio
async def test_ac14_caption_bridge_multi_document_disambiguation(tmp_path):
    """两篇文献都有 Figure 1：可见实体只在一篇题注中出现 → 唯一 VERIFIED。"""
    import hashlib

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from yuxi.storage.postgres.models_knowledge import (
        EvidenceAnchorRecord,
        EvidenceSpanRecord,
        KnowledgeFile,
        KnowledgeParseRevision,
    )

    from yuxi.knowledge.evidence.caption_locator import FigureCaptionQuery, resolve_caption_bridge

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
        for row_id, (revision_id, file_id, caption, page) in enumerate(
            [
                ("spr_a", "file_a", "Figure 1. Subcellular localization of OsMYB73-GFP fusion protein.", 4),
                ("spr_b", "file_b", "Figure 1. Root system architecture of rice seedlings.", 11),
            ],
            start=1,
        ):
            session.add(
                KnowledgeParseRevision(
                    id=row_id,
                    revision_id=revision_id,
                    tenant_id=1,
                    kb_id="kb-a",
                    file_id=file_id,
                    source_sha256=(str(row_id) * 64)[:64],
                    parser_fingerprint="f" * 64,
                    pipeline_version="scientific_pdf_v3.0",
                    status="INDEXED_FULL",
                )
            )
            session.add(
                KnowledgeFile(
                    id=row_id,
                    file_id=file_id,
                    kb_id="kb-a",
                    filename=f"paper-{file_id}.pdf",
                    active_parse_revision_id=revision_id,
                    active_index_revision_id=f"sir_{revision_id}",
                )
            )
            session.add(
                EvidenceAnchorRecord(
                    id=row_id,
                    anchor_id=f"ea_{file_id}",
                    parse_revision_id=revision_id,
                    page=page,
                    bbox=[40.0, float(page * 100), 280.0, float(page * 100 + 80)],
                    word_start=0,
                    word_end=6,
                    quote_hash=hashlib.sha256(caption.encode()).hexdigest(),
                    prefix_hash="p",
                    suffix_hash="s",
                    quote=caption,
                    fragments=[
                        {
                            "page_index": page - 1,
                            "bbox": [40.0, float(page * 100), 280.0, float(page * 100 + 80)],
                            "coordinate_space": "pdf_points",
                        }
                    ],
                    anchor_type="image",
                    locator_quality="HIGH",
                    confidence=1.0,
                    locatable=True,
                    source="mineru",
                    document_partition="MAIN_TEXT",
                )
            )
            session.add(
                EvidenceSpanRecord(
                    id=row_id,
                    tenant_id=1,
                    parse_revision_id=revision_id,
                    kb_id="kb-a",
                    file_id=file_id,
                    span_id=f"es_{file_id}",
                    anchor_id=f"ea_{file_id}",
                    sentence_index=0,
                    quote=caption,
                    quote_hash=hashlib.sha256(caption.encode()).hexdigest(),
                    page_number=page,
                    evidence_type="caption",
                    document_partition="MAIN_TEXT",
                    partition_confidence=1.0,
                    evidence_id=f"evs_{file_id}",
                    container_label="Figure 1",
                )
            )
        await session.commit()

        # 实体 OsMYB73-GFP 只在 file_a 题注中 → 唯一 VERIFIED 第 4 页
        resolved = await resolve_caption_bridge(
            session,
            query=FigureCaptionQuery(
                canonical_label="Figure 1",
                verbatim_segments=("Relative expression levels",),
                visible_entities=("OsMYB73-GFP",),
            ),
            kb_ids=["kb-a"],
        )
        assert resolved is not None
        assert resolved["status"] == "VERIFIED"
        assert resolved["page"] == 4
        assert resolved["file_id"] == "file_a"
    await engine.dispose()


@pytest.mark.asyncio
async def test_ac14_caption_bridge_ambiguous_without_disambiguator(tmp_path):
    """两篇同编号且逐字文本无法区分 → MULTIPLE_MATCHES，不发布候选页码。"""
    import hashlib

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from yuxi.storage.postgres.models_knowledge import (
        EvidenceAnchorRecord,
        EvidenceSpanRecord,
        KnowledgeFile,
        KnowledgeParseRevision,
    )

    from yuxi.knowledge.evidence.caption_locator import FigureCaptionQuery, resolve_caption_bridge

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
    caption = "Figure 1. Phenotype comparison of transgenic rice lines under field conditions."
    async with session_factory() as session:
        for row_id, (revision_id, file_id, page) in enumerate(
            [("spr_a", "file_a", 4), ("spr_b", "file_b", 11)], start=1
        ):
            session.add(
                KnowledgeParseRevision(
                    id=row_id,
                    revision_id=revision_id,
                    tenant_id=1,
                    kb_id="kb-a",
                    file_id=file_id,
                    source_sha256=(str(row_id) * 64)[:64],
                    parser_fingerprint="f" * 64,
                    pipeline_version="scientific_pdf_v3.0",
                    status="INDEXED_FULL",
                )
            )
            session.add(
                KnowledgeFile(
                    id=row_id,
                    file_id=file_id,
                    kb_id="kb-a",
                    filename=f"paper-{file_id}.pdf",
                    active_parse_revision_id=revision_id,
                    active_index_revision_id=f"sir_{revision_id}",
                )
            )
            session.add(
                EvidenceAnchorRecord(
                    id=row_id,
                    anchor_id=f"ea_{file_id}",
                    parse_revision_id=revision_id,
                    page=page,
                    bbox=[40.0, float(page * 100), 280.0, float(page * 100 + 80)],
                    word_start=0,
                    word_end=6,
                    quote_hash=hashlib.sha256(caption.encode()).hexdigest(),
                    prefix_hash="p",
                    suffix_hash="s",
                    quote=caption,
                    fragments=[],
                    anchor_type="image",
                    locator_quality="HIGH",
                    confidence=1.0,
                    locatable=True,
                    source="mineru",
                    document_partition="MAIN_TEXT",
                )
            )
            session.add(
                EvidenceSpanRecord(
                    id=row_id,
                    tenant_id=1,
                    parse_revision_id=revision_id,
                    kb_id="kb-a",
                    file_id=file_id,
                    span_id=f"es_{file_id}",
                    anchor_id=f"ea_{file_id}",
                    sentence_index=0,
                    quote=caption,
                    quote_hash=hashlib.sha256(caption.encode()).hexdigest(),
                    page_number=page,
                    evidence_type="caption",
                    document_partition="MAIN_TEXT",
                    partition_confidence=1.0,
                    evidence_id=f"evs_{file_id}",
                    container_label="Figure 1",
                )
            )
        await session.commit()

        resolved = await resolve_caption_bridge(
            session,
            query=FigureCaptionQuery(
                canonical_label="Figure 1",
                verbatim_segments=("Phenotype comparison of transgenic rice lines",),
                visible_entities=(),
            ),
            kb_ids=["kb-a"],
        )
        assert resolved is not None
        assert resolved["status"] == "MULTIPLE_MATCHES"
        assert "page" not in resolved
    await engine.dispose()
