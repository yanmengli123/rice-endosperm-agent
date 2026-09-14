"""Caption Locator v3 单测：label 硬约束、区分度评分、T0-T3 分级匹配、题注通道。"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.knowledge.evidence.caption_locator import (
    TIER_T1_CANONICAL_EXACT,
    TIER_T2_TOKEN_HARD_CONSTRAINTS,
    TIER_T3_WHITESPACE_COMPRESSED,
    canonical_figure_label,
    carrier_label_conflicts,
    extract_figure_label,
    label_conflicts,
    match_tier,
    resolve_figure_caption_locator,
    score_caption_text,
    select_quote_candidates,
)
from yuxi.knowledge.evidence.quote_locator import (
    LOCATOR_KIND_FIGURE,
    LOCATOR_KIND_QUOTE,
    decompose_question_intents,
    resolve_quote_locator_from_citations,
)
from yuxi.storage.postgres.models_knowledge import (
    EvidenceAnchorRecord,
    EvidenceSpanRecord,
    KnowledgeFile,
    KnowledgeParseRevision,
)

pytestmark = [pytest.mark.unit]

_STATS_TEMPLATE = (
    "Bar, 1.0 cm. Lowercase letters indicate significant difference (P < 0.05, ANOVA with Tukey correction)."
)
_GENE_SENTENCE = "CRISPR/Cas9 knockout of OsMYB73 and OsNF-YB1 in rice callus"


# ---- label 硬约束（Golden Test：Figure 5 + 共享统计模板 → 不允许命中 Figure 4）----


def test_figure_label_hard_constraint_rejects_conflicting_label():
    assert label_conflicts("Figure 5", "Figure 4")
    assert label_conflicts("Fig. 5", "figure 4")
    assert label_conflicts("Figure 5", "Figure 50")
    assert label_conflicts("Figure 5", "figure 5A")  # 面板后缀不同也是编号冲突
    assert not label_conflicts("Figure 5", "Fig. 5")
    assert not label_conflicts("Figure 5A", "Figure 5A")
    assert not label_conflicts("Figure 5", None)
    assert not label_conflicts(None, "Figure 4")


def test_canonical_figure_label_variants():
    assert canonical_figure_label("Figure 5") == "figure 5"
    assert canonical_figure_label("Fig. 5") == "figure 5"
    assert canonical_figure_label("figure s8") == "figure s8"
    assert canonical_figure_label("Figure 5A") == "figure 5a"
    assert canonical_figure_label("Table 2") == "table 2"
    assert canonical_figure_label("图5") == "figure 5"
    assert canonical_figure_label("表S1") == "table s1"
    assert canonical_figure_label("no label here") is None


def test_carrier_label_conflict_filters_caption_anchor():
    assert carrier_label_conflicts("Figure 5", f"Figure 4 {_GENE_SENTENCE}")
    assert not carrier_label_conflicts("Figure 5", f"Figure 5 {_GENE_SENTENCE}")
    assert not carrier_label_conflicts(None, f"Figure 4 {_GENE_SENTENCE}")
    # 载体无编号（正文句）不构成冲突
    assert not carrier_label_conflicts("Figure 5", _GENE_SENTENCE)


def _pool_citation(ref: str, page: int, quote: str) -> dict:
    import re
    import unicodedata

    def _norm(text: str) -> str:
        value = unicodedata.normalize("NFKC", text)
        return re.sub(r"\s+", " ", re.sub(r"[^0-9a-z一-鿿]+", " ", value.casefold())).strip()

    return {
        "ref": ref,
        "evidence_id": f"ev-{ref}",
        "kb_id": "kb-a",
        "file_id": "file-a",
        "filename": "paper.pdf",
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


def test_shared_stats_template_cannot_hit_conflicting_figure():
    """Golden Test：统计模板句在 Figure 4/5 题注中逐字重复，label 硬约束保证命中 Figure 5。"""
    citations = [
        _pool_citation("E1", 9, f"Figure 4 {_GENE_SENTENCE}. {_STATS_TEMPLATE}"),
        _pool_citation("E2", 10, f"Figure 5 {_GENE_SENTENCE}. {_STATS_TEMPLATE}"),
    ]
    resolution = resolve_quote_locator_from_citations(
        quote_text=_STATS_TEMPLATE,
        citations=citations,
        figure_label="Figure 5",
    )
    assert resolution["status"] == "VERIFIED"
    assert resolution["page"] == 10
    assert resolution["evidence_id"] == "ev-E2"

    # 反向：问 Figure 4 → 第 9 页（同一模板，不同编号）
    resolution4 = resolve_quote_locator_from_citations(
        quote_text=_STATS_TEMPLATE,
        citations=citations,
        figure_label="Figure 4",
    )
    assert resolution4["status"] == "VERIFIED"
    assert resolution4["page"] == 9


# ---- 区分度评分与多候选选择（max(len) 退役）----


def test_score_caption_text_penalizes_boilerplate():
    gene_score = score_caption_text(f"Figure 5 {_GENE_SENTENCE} OsISA2 OsLTPL36")
    stats_score = score_caption_text(_STATS_TEMPLATE)
    assert gene_score > 0
    assert stats_score < 0
    assert gene_score > stats_score


def test_select_quote_candidates_prefers_distinctive_over_longest():
    """基因符号段必须胜过更长的统计模板段（原 max(len) 的缺陷场景）。"""
    question = f"{_STATS_TEMPLATE} 数据如图所示。{_GENE_SENTENCE} 在论文第几页？"
    candidates = select_quote_candidates(question)
    assert len(candidates) >= 2, "中文分隔应产生多个拉丁段候选"
    assert "OsMYB73" in candidates[0]
    # 模板段仍在候选集中（宽召回），但排序落后
    assert any("ANOVA" in candidate for candidate in candidates[1:])


# ---- T0-T3 分级匹配 ----


def test_match_tier_t0_raw_and_t1_canonical():
    assert match_tier(_GENE_SENTENCE, _GENE_SENTENCE) == "T0_RAW_EXACT"
    # NFKC 连字 ﬁ→fi、破折号变体、大小写、空白 → T1
    carrier = "CRISPR/Cas9 knockout of OsMYB73 and OsNF-YB1 in rice callus, conﬁrmed by sequencing"
    assert match_tier(_GENE_SENTENCE, carrier) == TIER_T1_CANONICAL_EXACT


def test_match_tier_t2_token_coverage_with_hard_constraints():
    quote = "OsMYB73 OsNF-YB1 double mutant callus 115-164 aa"
    carrier = "The double mutant callus of OsNF-YB1 and OsMYB73 carried a 115-164 aa domain"
    assert match_tier(quote, carrier) == TIER_T2_TOKEN_HARD_CONSTRAINTS


def test_match_tier_t3_whitespace_artifact():
    """MinerU 存量伪影：词内被插入空格（Ye ast o f y one 类），压缩包含可命中。"""
    quote = "OsMYB73 protein contains two SANT domains spanning 115-164 aa"
    carrier = "The OsMYB73 pro tein contains two SANT dom ains spanning 115-164 aa in the N-terminus"
    assert match_tier(quote, carrier) == TIER_T3_WHITESPACE_COMPRESSED


def test_match_tier_rejects_unmatched():
    assert match_tier(_GENE_SENTENCE, "Completely unrelated sentence about grain quality.") is None
    assert match_tier("", "carrier") is None
    # 无硬约束的模糊词面重叠不得进入 T2/T3（模糊匹配永远不能单独发布页码）
    weak = "the results showed significant difference in grain weight"
    carrier_weak = "grain weight difference was significant in the results showed analysis"
    assert match_tier(weak, carrier_weak) is None


# ---- 意图分解：figure_label 与 FIGURE kind ----


def test_decompose_extracts_figure_label_only_question():
    decomposed = decompose_question_intents("Figure 5 的题注在论文第几页？")
    assert decomposed["kind"] == LOCATOR_KIND_FIGURE
    assert decomposed["figure_label"] == "Figure 5"
    assert decomposed["quote_text"] is None


def test_decompose_extracts_quote_and_label():
    decomposed = decompose_question_intents(f"Figure 5 {_GENE_SENTENCE} 这句话在哪一页，是什么意思？")
    assert decomposed["kind"] == LOCATOR_KIND_QUOTE
    assert decomposed["figure_label"] == "Figure 5"
    assert decomposed["quote_text"]
    assert decomposed["compound"]


def test_extract_figure_label_from_mixed_text():
    assert extract_figure_label("见 Figure S8 如下") == "Figure S8"
    assert extract_figure_label("图5显示了") == "图5"
    assert extract_figure_label("没有编号的句子") is None


# ---- 题注通道（SQLite 内存库）----


@pytest_asyncio.fixture
async def caption_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(KnowledgeFile.__table__.create)
        await connection.run_sync(KnowledgeParseRevision.__table__.create)
        await connection.run_sync(EvidenceAnchorRecord.__table__.create)
        await connection.run_sync(EvidenceSpanRecord.__table__.create)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def _ensure_source(session, *, row_id: int, revision_id: str, file_id: str):
    existing = (await session.execute(select(KnowledgeFile).where(KnowledgeFile.file_id == file_id))).scalars().first()
    if existing is not None:
        return
    # SQLite 方言：BIGINT 自增主键需显式 id（见 CLAUDE.md 测试注意事项）
    session.add(
        KnowledgeParseRevision(
            id=row_id,
            revision_id=revision_id,
            tenant_id=1,
            kb_id="kb-a",
            file_id=file_id,
            source_sha256=(str(row_id) * 64)[:64],
            parser_fingerprint=("f" + str(row_id)) * 32,
            pipeline_version="scientific_pdf_v2.8",
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


async def _add_caption(
    session,
    *,
    row_id: int,
    revision_id: str,
    file_id: str,
    container_label: str,
    quote: str,
    page: int,
    anchor_type: str = "image",
):
    await _ensure_source(session, row_id=row_id, revision_id=revision_id, file_id=file_id)
    bbox = [40.0, float(100 * page), 280.0, float(100 * page + 80)]
    session.add(
        EvidenceAnchorRecord(
            id=row_id,
            anchor_id=f"ea_{row_id}",
            parse_revision_id=revision_id,
            page=page,
            bbox=bbox,
            word_start=0,
            word_end=8,
            quote_hash=_digest(quote),
            prefix_hash=_digest("prefix"),
            suffix_hash=_digest("suffix"),
            quote=quote,
            fragments=[{"page_index": page - 1, "bbox": bbox, "coordinate_space": "pdf_points"}],
            anchor_type=anchor_type,
            locator_quality="HIGH",
            confidence=1.0,
            locatable=True,
            source="pymupdf",
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
            span_id=f"es_{row_id}",
            anchor_id=f"ea_{row_id}",
            sentence_index=0,
            quote=quote,
            quote_hash=_digest(quote),
            page_number=page,
            evidence_type="caption",
            document_partition="MAIN_TEXT",
            partition_confidence=1.0,
            evidence_id=f"evs_{row_id}",
            container_label=container_label,
        )
    )


@pytest.mark.asyncio
async def test_caption_channel_resolves_figure5_to_page10_not_figure4_page9(caption_session):
    """Figure 5 事故回归：label 硬约束下正确第 10 页，Figure 4 的同模板题注不串页。"""
    await _add_caption(
        caption_session,
        row_id=1,
        revision_id="spr_a",
        file_id="file_a",
        container_label="Figure 4",
        quote=f"Figure 4 {_GENE_SENTENCE}. {_STATS_TEMPLATE}",
        page=9,
    )
    await _add_caption(
        caption_session,
        row_id=2,
        revision_id="spr_a",
        file_id="file_a",
        container_label="Figure 5",
        quote=f"Figure 5 {_GENE_SENTENCE}. {_STATS_TEMPLATE}",
        page=10,
    )
    await caption_session.commit()

    resolution = await resolve_figure_caption_locator(
        caption_session,
        figure_label="Figure 5",
        kb_ids=["kb-a"],
    )
    assert resolution["status"] == "VERIFIED"
    assert resolution["page"] == 10
    assert resolution["locator_kind"] == "FIGURE_CAPTION"
    assert resolution["evidence_type"] == "caption"

    # 反向：Figure 4 → 第 9 页
    resolution4 = await resolve_figure_caption_locator(
        caption_session,
        figure_label="Figure 4",
        kb_ids=["kb-a"],
    )
    assert resolution4["status"] == "VERIFIED"
    assert resolution4["page"] == 9


@pytest.mark.asyncio
async def test_caption_channel_fails_closed_on_multiple_physical_matches(caption_session):
    """两个 PDF 都有 Figure 5 且文本相似 → MULTIPLE_MATCHES，不发布候选页码。"""
    await _add_caption(
        caption_session,
        row_id=1,
        revision_id="spr_a",
        file_id="file_a",
        container_label="Figure 5",
        quote=f"Figure 5 {_GENE_SENTENCE}.",
        page=10,
    )
    await _add_caption(
        caption_session,
        row_id=2,
        revision_id="spr_b",
        file_id="file_b",
        container_label="Figure 5",
        quote=f"Figure 5 {_GENE_SENTENCE}.",
        page=4,
    )
    await caption_session.commit()

    resolution = await resolve_figure_caption_locator(
        caption_session,
        figure_label="Figure 5",
        kb_ids=["kb-a"],
    )
    assert resolution["status"] == "MULTIPLE_MATCHES"
    assert "page" not in resolution


@pytest.mark.asyncio
async def test_caption_channel_returns_none_when_label_absent(caption_session):
    await _add_caption(
        caption_session,
        row_id=1,
        revision_id="spr_a",
        file_id="file_a",
        container_label="Figure 4",
        quote=f"Figure 4 {_GENE_SENTENCE}.",
        page=9,
    )
    await caption_session.commit()

    assert await resolve_figure_caption_locator(caption_session, figure_label="Figure 5", kb_ids=["kb-a"]) is None


@pytest.mark.asyncio
async def test_caption_channel_quote_fragment_must_match_carrier(caption_session):
    """带引文片段时必须 T0-T3 命中题注载体；片段不属于该题注 → None（回退常规路径）。"""
    await _add_caption(
        caption_session,
        row_id=1,
        revision_id="spr_a",
        file_id="file_a",
        container_label="Figure 5",
        quote=f"Figure 5 {_GENE_SENTENCE}.",
        page=10,
    )
    await caption_session.commit()

    matched = await resolve_figure_caption_locator(
        caption_session,
        figure_label="Figure 5",
        quote_text=_GENE_SENTENCE,
        kb_ids=["kb-a"],
    )
    assert matched["status"] == "VERIFIED"
    assert matched["match_tier"] == TIER_T1_CANONICAL_EXACT

    unrelated = await resolve_figure_caption_locator(
        caption_session,
        figure_label="Figure 5",
        quote_text="GUS staining of rice embryo sections",
        kb_ids=["kb-a"],
    )
    assert unrelated is None


@pytest.mark.asyncio
async def test_caption_channel_ignores_toc_captions(caption_session):
    """图表目录行（Figure S8 ... 17）有真实页码但不参与定位。"""
    await _add_caption(
        caption_session,
        row_id=1,
        revision_id="spr_a",
        file_id="file_a",
        container_label="Figure S8",
        quote="Figure S8 Rice grain starch physicochemical characteristics .... 17",
        page=2,
    )
    await caption_session.commit()

    assert await resolve_figure_caption_locator(caption_session, figure_label="Figure S8", kb_ids=["kb-a"]) is None
    # 确认数据确实写入了（排除「空库导致 None」的假阳性）
    count = len((await caption_session.execute(select(EvidenceSpanRecord.span_id))).all())
    assert count == 1


def test_simple_namespace_span_shape_matches_registry_duck_typing():
    """caption span 的 duck-typing 形状与 build_figure_registry 消费一致。"""
    span = SimpleNamespace(
        span_id="es_1",
        evidence_id="evs_1",
        container_label="Figure 5",
        evidence_type="caption",
        quote=f"Figure 5 {_GENE_SENTENCE}",
        page_number=10,
    )
    from yuxi.knowledge.evidence.figures import build_figure_registry

    registry = build_figure_registry([span])
    assert "Figure 5" in registry


# ---- Figure 3 事故回归（2026-09：讨论段复述短语被发布为题注页 → 错页第 5 页）----

_FIG3_CAPTION = (
    "Figure 3 Grain starch physicochemical characteristics comparison of ZH11 and cr-myb73 in T1 generation. "
    "(a) Total starch content; (b) amylose content; (c) total protein content; (d) total soluble sugar content; "
    "(e) total lipid content; (f) gel consistency; (g) starch solubility in 1 . 7% KOH solution, scale bars are "
    "1. 0 cm; (h) chain length distributions of amylopectin in ZH11 and cr-myb73; (i) pasting properties rapid "
    "visco analyser (RVA) of endosperm starch of ZH11 and cr-myb73 . FV, ﬁnal viscosity; HV, hold through "
    "viscosity; PV, peak viscosity. The viscosity value at each temperature is the average of three replicates. "
    "Asterisks indicate statistical signiﬁcance, as determined by a Student's t-test (*P < 0. 05, **P < 0 . 01) ."
)
_FIG3_DISCUSSION_P5 = (
    "Figure 3 showed that grain starch physicochemical characteristics of ZH11 and cr-myb73 differed "
    "significantly: total starch and amylose content, gel consistency, starch solubility in KOH solution, and "
    "chain length distributions of amylopectin in ZH11 and cr-myb73 were all altered, and the viscosity value at "
    "each temperature is the average of three replicates across biological samples."
)
_FIG3_QUESTION = f"{_FIG3_CAPTION} 在哪篇文献哪一页，是什么意思？"


def test_figure_label_pool_requires_caption_carrier_full_containment():
    """池内规则：编号问题只认题注载体 + 全包含——讨论段复述短语不发布页码。"""
    caption_cit = _pool_citation("E1", 7, _FIG3_CAPTION)
    discussion_cit = _pool_citation("E2", 5, _FIG3_DISCUSSION_P5)

    # 只有讨论段（复述了大量题注短语，含 ≥40 字符逐字段）：失败关闭，不发布第 5 页
    only_discussion = resolve_quote_locator_from_citations(
        quote_text=_FIG3_CAPTION, citations=[discussion_cit], figure_label="Figure 3"
    )
    assert only_discussion["status"] == "NOT_FOUND"
    assert "page" not in only_discussion

    # 题注载体在场：正确第 7 页
    with_caption = resolve_quote_locator_from_citations(
        quote_text=_FIG3_CAPTION, citations=[caption_cit, discussion_cit], figure_label="Figure 3"
    )
    assert with_caption["status"] == "VERIFIED"
    assert with_caption["page"] == 7
    assert with_caption["evidence_id"] == "ev-E1"


async def test_figure_label_quote_falls_back_to_caption_channel_golden_page(caption_session):
    """金标路径：新 revision 形态（chart 锚点 + caption span 第 7 页）→ 正确第 7 页。"""
    from yuxi.knowledge.evidence.quote_locator import resolve_quote_locator

    await _add_caption(
        caption_session,
        row_id=1,
        revision_id="spr_a",
        file_id="file_a",
        container_label="Figure 3",
        quote=_FIG3_CAPTION,
        page=7,
        anchor_type="chart",
    )
    await caption_session.commit()

    resolution = await resolve_quote_locator(caption_session, question=_FIG3_QUESTION, kb_ids=["kb-a"])
    assert resolution["status"] == "VERIFIED"
    assert resolution["page"] == 7
    assert resolution["locator_kind"] == "FIGURE_CAPTION"


@pytest.mark.asyncio
async def test_figure3_incident_old_revision_fails_closed(caption_session):
    """事故复现（旧 revision 形态）：caption span 无锚点 + 第 5 页讨论段复述短语 → 失败关闭。

    旧解析中 chart 块被丢弃，caption span anchor_id=NULL；第 5/6 页正文讨论段
    复述题注短语。修复前：讨论段通过部分包含被 VERIFIED → 错页第 5 页发布。
    修复后：编号问题只认题注载体 → NOT_FOUND，不展示任何页码。
    """
    from yuxi.storage.postgres.models_knowledge import EvidenceAnchorRecord, EvidenceSpanRecord

    from yuxi.knowledge.evidence.quote_locator import resolve_quote_locator

    await _ensure_source(caption_session, row_id=1, revision_id="spr_a", file_id="file_a")
    import hashlib as _hashlib

    quote_hash = _hashlib.sha256(_FIG3_DISCUSSION_P5.encode()).hexdigest()
    caption_session.add(
        EvidenceAnchorRecord(
            id=1,
            anchor_id="ea_discuss_p5",
            parse_revision_id="spr_a",
            page=5,
            bbox=[40.0, 500.0, 280.0, 580.0],
            word_start=0,
            word_end=20,
            quote_hash=quote_hash,
            prefix_hash="p",
            suffix_hash="s",
            quote=_FIG3_DISCUSSION_P5,
            fragments=[{"page_index": 4, "bbox": [40.0, 500.0, 280.0, 580.0], "coordinate_space": "pdf_points"}],
            anchor_type="paragraph",
            locator_quality="HIGH",
            confidence=1.0,
            locatable=True,
            source="mineru",
            document_partition="MAIN_TEXT",
        )
    )
    caption_session.add(
        EvidenceSpanRecord(
            id=2,
            tenant_id=1,
            parse_revision_id="spr_a",
            kb_id="kb-a",
            file_id="file_a",
            span_id="es_discuss_p5",
            anchor_id="ea_discuss_p5",
            sentence_index=0,
            quote=_FIG3_DISCUSSION_P5,
            quote_hash=quote_hash,
            page_number=5,
            evidence_type="sentence",
            document_partition="MAIN_TEXT",
            partition_confidence=1.0,
            evidence_id="evs_discuss_p5",
        )
    )
    caption_session.add(
        EvidenceSpanRecord(
            id=3,
            tenant_id=1,
            parse_revision_id="spr_a",
            kb_id="kb-a",
            file_id="file_a",
            span_id="es_fig3_orphan",
            anchor_id=None,  # 旧解析：caption span 无物理锚点
            sentence_index=1,
            quote=_FIG3_CAPTION,
            quote_hash=_hashlib.sha256(_FIG3_CAPTION.encode()).hexdigest(),
            page_number=None,
            evidence_type="caption",
            document_partition="MAIN_TEXT",
            partition_confidence=1.0,
            evidence_id="evs_fig3_orphan",
            container_label="Figure 3",
        )
    )
    await caption_session.commit()

    resolution = await resolve_quote_locator(caption_session, question=_FIG3_QUESTION, kb_ids=["kb-a"])
    assert resolution["status"] == "NOT_FOUND"
    assert "page" not in resolution


@pytest.mark.asyncio
async def test_caption_channel_rejects_span_anchor_page_mismatch(caption_session):
    """跨源页码守卫：caption span 页与锚点页不一致（MinerU 误归属）→ 不发布。"""
    from sqlalchemy import update

    await _add_caption(
        caption_session,
        row_id=1,
        revision_id="spr_a",
        file_id="file_a",
        container_label="Figure 3",
        quote=_FIG3_CAPTION,
        page=7,
    )
    await caption_session.execute(
        update(EvidenceSpanRecord).where(EvidenceSpanRecord.span_id == "es_1").values(page_number=6)
    )
    await caption_session.commit()

    resolution = await resolve_figure_caption_locator(
        caption_session,
        figure_label="Figure 3",
        kb_ids=["kb-a"],
    )
    assert resolution is None
