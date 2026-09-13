"""Figure Image Locator 单测：观察契约、pHash、V0-V4 裁决阶梯、附件路由。"""

from __future__ import annotations

import io

import pytest
from PIL import Image, ImageDraw

from yuxi.knowledge.planning.turn_execution_plan import TaskIntent, plan_turn
from yuxi.knowledge.vision.figure_image_locator import (
    TIER_V0_EXACT_ASSET_SHA,
    TIER_V1_STRONG_PHASH_LABEL,
    TIER_V2_VISUAL_CONSTRAINTS,
    TIER_V3_MULTI_SIGNAL_HARD_CONSTRAINTS,
    adjudicate_figure_candidates,
    build_figure_index,
)
from yuxi.knowledge.vision.phash import compute_asset_digest, compute_phash, phash_hamming_distance
from yuxi.knowledge.vision.visual_observation import VisualObservationEnvelope, parse_visual_observation

pytestmark = [pytest.mark.unit]

_CAPTION_FIG1 = (
    "Figure 1. Expression patterns of OsMYB73 in rice seeds. (a) Relative expression levels of "
    "OsMYB73 during seed development measured by qRT-PCR. (b) Histochemical GUS staining of "
    "transgenic rice seeds. (c) Subcellular localization of OsMYB73-GFP fusion protein in rice "
    "protoplasts. Bar, 1.0 cm."
)
_CAPTION_FIG2 = (
    "Figure 2. Phylogenetic analysis of OsNF-YB1 and related NF-YB proteins from Arabidopsis "
    "and rice. The phylogenetic tree was constructed using the neighbor-joining method with "
    "1000 bootstrap replicates."
)


def _observation(**overrides) -> VisualObservationEnvelope:
    payload = {
        "schema_version": "visual-observation.v1",
        "figure_label": "Figure 1",
        "panel_labels": ["a", "b", "c"],
        "visible_entities": ["OsMYB73-GFP"],
        "visible_text": ["Relative expression levels", "Seed 5 DAF"],
        "caption_fragments": ["Rice OsMYB73 gene expression", "histochemical GUS staining"],
        "visual_structure": {"bar_chart": True, "microscopy": True, "tissue_images": True},
        "confidence": 0.9,
    }
    payload.update(overrides)
    payload = {key: value for key, value in payload.items() if value is not ...}
    return VisualObservationEnvelope.model_validate(payload)


def _candidate(**overrides) -> dict:
    base = {
        "anchor_id": "ea_fig1",
        "span_id": "es_fig1",
        "span_evidence_id": "evs_fig1",
        "evidence_id": "ev_fig1",
        "parse_revision_id": "pr_1",
        "index_revision_id": "ir_1",
        "kb_id": "kb-a",
        "file_id": "file-a",
        "source_sha256": "a" * 64,
        "filename": "osmyb73-paper.pdf",
        "page": 4,
        "zone": "MAIN_TEXT",
        "container_label": "Figure 1",
        "caption": _CAPTION_FIG1,
        "caption_norm": _norm(_CAPTION_FIG1),
        "asset_digest": None,
        "asset_phash": None,
    }
    base.update(overrides)
    return base


def _norm(text: str) -> str:
    from yuxi.knowledge.rendering.claim_evidence_resolver import normalize_for_match

    return normalize_for_match(text)


# ---- 观察契约：Observation，不是 Authority ----


def test_observation_schema_has_no_page_fields_and_rejects_them():
    """Golden Test：模型输出 page_number → 整份观察被拒绝（schema 物理上无此字段）。"""
    assert parse_visual_observation('{"schema_version":"visual-observation.v1","figure_label":"Figure 1","page_number":4}') is None
    assert parse_visual_observation('{"schema_version":"visual-observation.v1","file_id":"file-a"}') is None
    valid = parse_visual_observation('{"schema_version":"visual-observation.v1","figure_label":"Figure 1"}')
    assert valid is not None and valid.figure_label == "Figure 1"
    # schema 字段集中不存在任何定位权威字段
    assert not {"page_number", "file_id", "anchor_id", "bbox"} & set(VisualObservationEnvelope.model_fields)


def test_parse_visual_observation_extracts_fenced_json():
    raw = "前置说明\n```json\n" + _observation().model_dump_json() + "\n```\n尾部"
    assert parse_visual_observation(raw) is not None
    assert parse_visual_observation("") is None
    assert parse_visual_observation("no json at all") is None
    assert parse_visual_observation('{"schema_version":"visual-observation.v1","confidence": 2.0}') is None


# ---- pHash ----


def _bar_chart_png(*, shift: int = 0, different: bool = False) -> bytes:
    image = Image.new("RGB", (320, 240), "white")
    draw = ImageDraw.Draw(image)
    if different:
        draw.ellipse((40, 40, 280, 200), fill=(30, 144, 255))
    else:
        for index, height in enumerate((120, 90, 60, 140, 100)):
            x0 = 40 + index * 55 + shift
            draw.rectangle((x0, 220 - height, x0 + 34, 220), fill=(70, 130, 60))
        draw.line((30, 220, 300, 220), fill="black", width=2)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_phash_identical_and_similar_images():
    original = _bar_chart_png()
    assert compute_phash(original) is not None
    assert phash_hamming_distance(compute_phash(original), compute_phash(original)) == 0
    # 缩放重编码（同图不同尺寸）应保持强匹配（≤8）
    resized = _bar_chart_png(shift=0)
    image = Image.open(io.BytesIO(resized)).resize((160, 120))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    distance = phash_hamming_distance(compute_phash(original), compute_phash(buffer.getvalue()))
    assert distance is not None and distance <= 8


def test_phash_different_images_are_not_strong_match():
    original = _bar_chart_png()
    other = _bar_chart_png(different=True)
    distance = phash_hamming_distance(compute_phash(original), compute_phash(other))
    assert distance is not None and distance > 8


def test_asset_digest_is_content_addressed():
    assert compute_asset_digest(b"abc") == compute_asset_digest(b"abc")
    assert compute_asset_digest(b"abc") != compute_asset_digest(b"abd")


# ---- V0-V4 裁决阶梯 ----


def test_v2_label_plus_constraints_verifies_uploaded_figure_page():
    """Golden Test：上传原图（label + 可见文本 + 实体）→ 正确第 4 页。"""
    resolution = adjudicate_figure_candidates(_observation(), [_candidate(), _candidate_page9_fig4()])
    assert resolution["status"] == "VERIFIED"
    assert resolution["page"] == 4
    assert resolution["locator_kind"] == "FIGURE_IMAGE"
    assert resolution["match_tier"] == TIER_V2_VISUAL_CONSTRAINTS
    assert resolution["evidence_id"] == "ev_fig1"
    signals = resolution["visual_signals"]
    assert signals["label_match"] and (signals["visible_text_hits"] + signals["entity_hits"]) >= 2


def _candidate_page9_fig4() -> dict:
    return _candidate(
        anchor_id="ea_fig4",
        span_id="es_fig4",
        span_evidence_id="evs_fig4",
        evidence_id="ev_fig4",
        page=9,
        container_label="Figure 4",
        caption="Figure 4. Root system architecture of rice seedlings. Bar, 1.0 cm.",
        caption_norm=_norm("Figure 4. Root system architecture of rice seedlings. Bar, 1.0 cm."),
    )


def test_label_conflict_rejects_wrong_figure():
    """观察编号 Figure 1 时 Figure 4 候选（即使统计模板匹配）直接剔除。"""
    observation = _observation(
        visible_text=["Bar, 1.0 cm"],
        caption_fragments=["Bar, 1.0 cm"],
    )
    resolution = adjudicate_figure_candidates(observation, [_candidate_page9_fig4()])
    assert resolution["status"] == "NOT_FOUND"


def test_single_signal_never_publishes_page():
    """双信号最低：只有 label（无文本/实体命中）→ NOT_FOUND。"""
    observation = _observation(visible_text=[], visible_entities=[], caption_fragments=[])
    resolution = adjudicate_figure_candidates(observation, [_candidate()])
    assert resolution["status"] == "NOT_FOUND"
    assert resolution["reason"] == "no_figure_candidate_satisfies_two_signal_minimum"


def test_panel_crop_fails_closed():
    """Golden Test：只上传一个 panel（弱信号）→ 无法唯一定位则 fail closed。"""
    observation = _observation(
        figure_label=None,
        visible_entities=[],
        visible_text=["Relative expression levels"],
        caption_fragments=[],
    )
    # 仅一个文本信号、无实体 → 不足双信号
    resolution = adjudicate_figure_candidates(observation, [_candidate(), _candidate_page9_fig4()])
    assert resolution["status"] == "NOT_FOUND"


def test_two_similar_figures_yield_multiple_matches():
    """Golden Test：两个 PDF 相似 Figure 1 → MULTIPLE_MATCHES，不发布候选页码。"""
    second_file = _candidate(
        anchor_id="ea_fig1_b",
        evidence_id="ev_fig1_b",
        file_id="file-b",
        parse_revision_id="pr_2",
        filename="other-paper.pdf",
        page=7,
    )
    resolution = adjudicate_figure_candidates(_observation(), [_candidate(), second_file])
    assert resolution["status"] == "MULTIPLE_MATCHES"
    assert resolution["reason"] == "figure_match_ambiguous"
    assert "page" not in resolution


def test_v3_no_label_but_text_and_entity_signals():
    observation = _observation(
        figure_label=None,
        visible_text=["Relative expression levels"],
        visible_entities=["OsMYB73-GFP"],
    )
    resolution = adjudicate_figure_candidates(observation, [_candidate()])
    assert resolution["status"] == "VERIFIED"
    assert resolution["match_tier"] == TIER_V3_MULTI_SIGNAL_HARD_CONSTRAINTS


def test_v0_exact_asset_sha_wins():
    image_bytes = _bar_chart_png()
    digest = compute_asset_digest(image_bytes)
    candidate = _candidate(asset_digest=digest, asset_phash=compute_phash(image_bytes))
    resolution = adjudicate_figure_candidates(
        _observation(), [candidate, _candidate_page9_fig4()], image_asset_digest=digest, image_phash=compute_phash(image_bytes)
    )
    assert resolution["status"] == "VERIFIED"
    assert resolution["match_tier"] == TIER_V0_EXACT_ASSET_SHA


def test_v1_strong_phash_plus_label():
    image_bytes = _bar_chart_png()
    candidate = _candidate(asset_phash=compute_phash(image_bytes))
    # 无文本/实体命中，但 pHash 强匹配 + label 一致 → V1
    observation = _observation(visible_text=[], visible_entities=[], caption_fragments=[])
    resolution = adjudicate_figure_candidates(
        observation, [candidate], image_phash=compute_phash(image_bytes)
    )
    assert resolution["status"] == "VERIFIED"
    assert resolution["match_tier"] == TIER_V1_STRONG_PHASH_LABEL


def test_phash_alone_never_publishes():
    """pHash 不能单独开页码：强匹配但 label 不一致（候选 Figure 4）→ REJECT。"""
    image_bytes = _bar_chart_png()
    candidate = _candidate_page9_fig4()
    candidate["asset_phash"] = compute_phash(image_bytes)
    candidate["caption"] += " Relative expression levels OsMYB73-GFP"
    candidate["caption_norm"] = _norm(candidate["caption"])
    observation = _observation(figure_label="Figure 1", visible_text=[], visible_entities=[], caption_fragments=[])
    resolution = adjudicate_figure_candidates(observation, [candidate], image_phash=compute_phash(image_bytes))
    assert resolution["status"] == "NOT_FOUND"


# ---- 附件感知路由 ----


def test_plan_turn_routes_image_attachment_to_figure_image():
    plan = plan_turn(
        "这个图片在哪篇论文哪一页，是什么意思？",
        has_knowledge_scope=True,
        has_image=True,
    )
    assert plan.task.primary_intent == TaskIntent.FIGURE_LOCATOR
    assert plan.task.target_type == "FIGURE_IMAGE"
    assert "ATTACHMENT_FIGURE_IMAGE_ROUTING" in plan.reason_codes
    assert plan.evidence.exact_locator_required
    assert plan.answer.citation_policy == "VERIFIED_ONLY"


def test_plan_turn_image_without_locator_intent_stays_normal():
    plan = plan_turn("帮我总结一下这张图", has_knowledge_scope=True, has_image=True)
    assert plan.task.primary_intent != TaskIntent.FIGURE_LOCATOR
    assert plan.task.target_type != "FIGURE_IMAGE"


def test_plan_turn_without_image_keeps_existing_figure_routing():
    plan = plan_turn("Figure 5 的题注在论文第几页？", has_knowledge_scope=True)
    assert plan.task.primary_intent == TaskIntent.FIGURE_LOCATOR
    assert plan.task.target_type != "FIGURE_IMAGE"


# ---- figure index 构建（SQLite 内存库）----


@pytest.fixture
async def caption_session():
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
        await connection.run_sync(KnowledgeFile.__table__.create)
        await connection.run_sync(KnowledgeParseRevision.__table__.create)
        await connection.run_sync(EvidenceAnchorRecord.__table__.create)
        await connection.run_sync(EvidenceSpanRecord.__table__.create)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(
            KnowledgeParseRevision(
                id=1,
                revision_id="spr_a",
                tenant_id=1,
                kb_id="kb-a",
                file_id="file_a",
                source_sha256="a" * 64,
                parser_fingerprint="f" * 64,
                pipeline_version="scientific_pdf_v2.8",
                status="INDEXED_FULL",
            )
        )
        session.add(
            KnowledgeFile(
                id=1,
                file_id="file_a",
                kb_id="kb-a",
                filename="osmyb73-paper.pdf",
                active_parse_revision_id="spr_a",
                active_index_revision_id="sir_spr_a",
            )
        )
        await session.commit()
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_build_figure_index_joins_image_anchor_with_caption_span(caption_session):
    from yuxi.knowledge.evidence.document_partition import PARTITION_MAIN_TEXT
    from yuxi.storage.postgres.models_knowledge import EvidenceAnchorRecord, EvidenceSpanRecord

    bbox = [40.0, 400.0, 280.0, 480.0]
    caption_session.add(
        EvidenceAnchorRecord(
            id=10,
            anchor_id="ea_img1",
            parse_revision_id="spr_a",
            page=4,
            bbox=bbox,
            word_start=0,
            word_end=12,
            quote_hash="h1",
            prefix_hash="p1",
            suffix_hash="s1",
            quote=_CAPTION_FIG1,
            fragments=[{"page_index": 3, "bbox": bbox, "coordinate_space": "pdf_points"}],
            anchor_type="image",
            locator_quality="HIGH",
            confidence=1.0,
            locatable=True,
            source="mineru",
            document_partition=PARTITION_MAIN_TEXT,
        )
    )
    caption_session.add(
        EvidenceSpanRecord(
            id=11,
            tenant_id=1,
            parse_revision_id="spr_a",
            kb_id="kb-a",
            file_id="file_a",
            span_id="es_fig1",
            anchor_id="ea_img1",
            sentence_index=0,
            quote=_CAPTION_FIG1,
            quote_hash="h1",
            page_number=4,
            evidence_type="caption",
            document_partition=PARTITION_MAIN_TEXT,
            partition_confidence=1.0,
            evidence_id="evs_fig1",
            container_label="Figure 1",
        )
    )
    await caption_session.commit()

    entities = await build_figure_index(caption_session, kb_ids=["kb-a"])
    assert len(entities) == 1
    entity = entities[0]
    assert entity["container_label"] == "Figure 1"
    assert entity["page"] == 4
    assert entity["span_evidence_id"] == "evs_fig1"
    assert entity["asset_digest"] is None  # 指纹入库后 V0/V1 自动启用
    # 端到端：观察 + index → 第 4 页
    resolution = adjudicate_figure_candidates(_observation(), entities)
    assert resolution["status"] == "VERIFIED"
    assert resolution["page"] == 4
