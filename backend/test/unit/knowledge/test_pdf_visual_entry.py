"""R-P1/R-P2/R-P3/R-P0 真实入口验收测试。

覆盖反馈清单中「测试全通过但真实 PDF 仍失败」的缺口：真实 MinerU
chart/chart_caption 形态、超长题注、HTML 实体/连字/小数空格归一化、
Figure Ingestor 指纹持久化、绑定 v2 页码语义、状态投影四集合契约。
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.knowledge.evidence.sentence_splitter import _classify_quote
from yuxi.knowledge.pdf_evidence.contracts import ParserArtifact
from yuxi.knowledge.pdf_evidence.mineru_layout import (
    MINERU_LAYOUT_ADAPTER_VERSION,
    build_mineru_anchors,
    extract_mineru_blocks,
)
from yuxi.knowledge.rendering.claim_evidence_resolver import (
    extract_hard_constraints,
    normalize_for_match,
)
from yuxi.storage.postgres.models_knowledge import (
    EvidenceAnchorRecord,
    EvidenceSpanRecord,
    FigureAssetRecord,
    FigureEntityRecord,
    KnowledgeChunk,
    KnowledgeFile,
    KnowledgeParseRevision,
)

pytestmark = [pytest.mark.unit]

# 真实 MinerU content_list 形态（Figure 5 事故样本：type=chart，题注在 chart_caption）
_FIG5_CAPTION = (
    "Figure 5. CRISPR/Cas9-mediated knockout of OsMYB73 and OsNF-YB1 in rice callus. "
    "(a) Expression analysis of OsMYB73, OsNF-YB1, OsISA2 and OsLTPL36 by qRT-PCR. "
    "(b) Phenotypes of regenerated seedlings. Bar, 1.0 cm. Lowercase letters indicate "
    "significant differences (P < 0.05, one-way ANOVA with Tukey correction)."
)


def _chart_content_list() -> list[dict]:
    return [
        {"type": "text", "page_idx": 8, "bbox": [50, 50, 950, 120], "text": "Results and discussion"},
        {
            "type": "chart",
            "page_idx": 9,
            "bbox": [80, 150, 920, 700],
            "img_path": "images/fig5-chart.jpg",
            "chart_caption": _FIG5_CAPTION,
        },
    ]


def _artifacts(rows: list[dict]) -> list[ParserArtifact]:
    import json

    return [
        ParserArtifact(
            kind="mineru_content_list",
            filename="content_list.json",
            content=json.dumps(rows).encode("utf-8"),
            content_type="application/json",
        )
    ]


# ---- R-P1：真实 chart 入口 ----


def test_real_chart_block_produces_physical_anchor_with_caption():
    """验收：MinerU type=chart 且题注只在 chart_caption → 必须产生第 10 页物理锚点。"""
    assert MINERU_LAYOUT_ADAPTER_VERSION == "mineru_layout_v3"
    blocks = extract_mineru_blocks(_artifacts(_chart_content_list()))
    chart_blocks = [block for block in blocks if block["block_type"] == "chart"]
    assert len(chart_blocks) == 1
    block = chart_blocks[0]
    assert block["page"] == 10  # page_idx=9 → 物理第 10 页
    assert "OsMYB73" in block["text"]
    assert block["source_path"] == "images/fig5-chart.jpg"

    anchors = build_mineru_blocks_anchors(blocks)
    chart_anchors = [anchor for anchor in anchors if anchor.anchor_type == "chart"]
    assert len(chart_anchors) == 1
    assert chart_anchors[0].page == 10
    assert "Figure 5" in chart_anchors[0].quote
    # img_path 随 fragments 持久化（Figure Ingestor 据此回收 MinIO 对象）
    assert chart_anchors[0].fragments[0].source_path == "images/fig5-chart.jpg"


def build_mineru_blocks_anchors(blocks):
    return build_mineru_anchors("a" * 64, _artifacts(_chart_content_list()), blocks=blocks)


def test_empty_visual_chart_block_is_not_dropped_from_blocks():
    """视觉块即使文本为空也不能在块抽取阶段丢弃（指纹回收的入口）。"""
    rows = [{"type": "chart", "page_idx": 9, "bbox": [80, 150, 920, 700], "img_path": "images/fig5.jpg"}]
    blocks = extract_mineru_blocks(_artifacts(rows))
    assert len(blocks) == 1 and blocks[0]["block_type"] == "chart"


def test_chart_with_only_caption_field_is_supported():
    rows = [
        {
            "type": "chart",
            "page_idx": 3,
            "bbox": [80, 150, 920, 700],
            "caption": _FIG5_CAPTION,
        }
    ]
    blocks = extract_mineru_blocks(_artifacts(rows))
    assert "Figure 5" in blocks[0]["text"]


# ---- R-P1：长题注不再降级 ----


def test_long_caption_still_classified_as_caption():
    """验收：超过 300 字符的题注仍然分类为 caption（题注判定不看长度）。"""
    assert len(_FIG5_CAPTION) > 300
    evidence_type, container_label, row_key = _classify_quote(_FIG5_CAPTION)
    assert evidence_type == "caption"
    assert container_label == "Figure 5"
    assert row_key is None


# ---- R-P1：归一化（HTML 实体 / 连字 / 小数空格） ----


def test_html_entity_in_user_text_normalizes_to_space():
    """验收：粘贴含 &#x20; 实体的题注片段与原文精确匹配。"""
    pasted = f"CRISPR/Cas9-mediated knockout of OsMYB73 and&#x20;OsNF-YB1 in rice callus."
    original = "CRISPR/Cas9-mediated knockout of OsMYB73 and OsNF-YB1 in rice callus."
    assert normalize_for_match(pasted) == normalize_for_match(original)
    # 实体残骸不再产生假 token（旧实现会多出 "x20"）
    assert "x20" not in normalize_for_match(pasted).split()


def test_ligature_and_decimal_space_normalization():
    assert normalize_for_match("signiﬁcant") == normalize_for_match("significant")
    assert normalize_for_match("Bar, 1 . 0 cm") == normalize_for_match("Bar, 1.0 cm")


def test_decimal_number_hard_constraints_match_across_spacing():
    """P < 0.05 类数值约束在「0. 05」形态载体上仍判定满足。"""
    carrier_norm = normalize_for_match("differences were significant at P < 0. 05 by ANOVA")
    hard = extract_hard_constraints("P < 0.05")
    assert "0.05" in hard["numbers"]
    from yuxi.knowledge.rendering.claim_evidence_resolver import _constraints_satisfied

    assert _constraints_satisfied(hard, carrier_norm)


# ---- R-P2：Figure Ingestor 指纹持久化 ----


@pytest_asyncio.fixture
async def figure_session(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(KnowledgeFile.__table__.create)
        await connection.run_sync(KnowledgeParseRevision.__table__.create)
        await connection.run_sync(KnowledgeChunk.__table__.create)
        await connection.run_sync(EvidenceAnchorRecord.__table__.create)
        await connection.run_sync(EvidenceSpanRecord.__table__.create)
        await connection.run_sync(FigureEntityRecord.__table__.create)
        await connection.run_sync(FigureAssetRecord.__table__.create)

    import io

    from PIL import Image

    image = Image.new("RGB", (320, 240), "white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    image_bytes = buffer.getvalue()

    from yuxi.knowledge.vision import figure_ingestor

    async def fake_resolve():
        return {"fig5-chart.jpg": "evidence/assets/abc123-fig5-chart.jpg"}

    class fake_minio:
        async def adownload_file(self, bucket, object_name):
            assert bucket == "knowledgebases"
            assert object_name == "evidence/assets/abc123-fig5-chart.jpg"
            return image_bytes

    import yuxi.storage.minio.client as minio_module

    monkeypatch.setattr(figure_ingestor, "_resolve_asset_objects", fake_resolve)
    monkeypatch.setattr(minio_module, "get_minio_client", lambda: fake_minio())

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(
            KnowledgeParseRevision(
                id=1,
                revision_id="spr_fig",
                tenant_id=1,
                kb_id="kb-a",
                file_id="file_fig",
                source_sha256="a" * 64,
                parser_fingerprint="f" * 64,
                pipeline_version="scientific_pdf_v3.0",
                status="INDEXED_FULL",
            )
        )
        session.add(
            KnowledgeFile(
                id=1,
                file_id="file_fig",
                kb_id="kb-a",
                filename="liu-2024-pbj.pdf",
                active_parse_revision_id="spr_fig",
                active_index_revision_id="sir_fig",
            )
        )
        yield session, image_bytes
    await engine.dispose()


@pytest.mark.asyncio
async def test_persist_figure_index_computes_fingerprints(figure_session):
    session, image_bytes = figure_session
    from yuxi.knowledge.vision.figure_ingestor import FIGURE_INGESTOR_VERSION, persist_figure_index
    from yuxi.knowledge.vision.phash import compute_asset_digest, compute_phash

    revision = (await session.execute(select(KnowledgeParseRevision))).scalars().one()
    caption_span = EvidenceSpanRecord(
        id=1,
        tenant_id=1,
        parse_revision_id="spr_fig",
        kb_id="kb-a",
        file_id="file_fig",
        span_id="es_fig5",
        anchor_id="ea_fig5",
        sentence_index=0,
        quote=_FIG5_CAPTION,
        quote_hash=hashlib.sha256(_FIG5_CAPTION.encode()).hexdigest(),
        page_number=10,
        evidence_type="caption",
        document_partition="MAIN_TEXT",
        partition_confidence=1.0,
        evidence_id="evs_fig5",
        container_label="Figure 5",
    )
    chart_anchor = EvidenceAnchorRecord(
        id=1,
        anchor_id="ea_fig5",
        parse_revision_id="spr_fig",
        page=10,
        bbox=[80.0, 150.0, 920.0, 700.0],
        word_start=0,
        word_end=10,
        quote_hash=hashlib.sha256(_FIG5_CAPTION.encode()).hexdigest(),
        prefix_hash="p",
        suffix_hash="s",
        quote=_FIG5_CAPTION,
        fragments=[{"page_index": 9, "bbox": [80.0, 150.0, 920.0, 700.0], "coordinate_space": "pdf_points"}],
        anchor_type="chart",
        locator_quality="HIGH",
        confidence=1.0,
        locatable=True,
        source="mineru",
    )
    await session.flush()

    summary = await persist_figure_index(
        session,
        revision=revision,
        article_assets=[
            {
                "kind": "figure",
                "block_type": "chart",
                "img_path": "images/fig5-chart.jpg",
                "page_index": 9,
                "page": 10,
                "bbox": [80.0, 150.0, 920.0, 700.0],
                "caption": _FIG5_CAPTION,
                "block_id": "mineru:9:1",
            }
        ],
        spans=[caption_span],
        anchors=[chart_anchor],
    )
    assert summary["version"] == FIGURE_INGESTOR_VERSION
    assert summary["entities"] == 1
    assert summary["assets"] == 1
    assert summary["fingerprinted"] == 1

    entity = (await session.execute(select(FigureEntityRecord))).scalars().one()
    asset = (await session.execute(select(FigureAssetRecord))).scalars().one()
    assert entity.entity_key == "figure 5"
    assert entity.caption_span_evidence_id == "evs_fig5"
    assert entity.association_method == "span_linkage"
    assert asset.anchor_id == "ea_fig5"
    assert asset.asset_sha256 == compute_asset_digest(image_bytes)
    assert asset.asset_phash == compute_phash(image_bytes)
    assert "whole" in (asset.panel_phashes or {})
    assert asset.object_name == "evidence/assets/abc123-fig5-chart.jpg"


@pytest.mark.asyncio
async def test_persist_figure_index_is_idempotent_on_retry(figure_session):
    session, _image = figure_session
    from yuxi.knowledge.vision.figure_ingestor import persist_figure_index

    revision = (await session.execute(select(KnowledgeParseRevision))).scalars().one()
    assets = [
        {
            "kind": "figure",
            "block_type": "chart",
            "img_path": "images/fig5-chart.jpg",
            "page": 10,
            "bbox": [80.0, 150.0, 920.0, 700.0],
            "caption": _FIG5_CAPTION,
        }
    ]
    await persist_figure_index(session, revision=revision, article_assets=assets, spans=[], anchors=[])
    await persist_figure_index(session, revision=revision, article_assets=assets, spans=[], anchors=[])
    entities = (await session.execute(select(FigureEntityRecord))).scalars().all()
    rows = (await session.execute(select(FigureAssetRecord))).scalars().all()
    assert len(entities) == 1 and len(rows) == 1


# ---- R-P3：绑定 v2 页码语义 ----


def test_binding_v2_split_page_semantics_for_cross_page_figure():
    from yuxi.knowledge.contracts.locator_binding import binding_from_locator_resolution

    resolution = {
        "status": "VERIFIED",
        "page": 10,  # 图片入口 display = asset 页
        "asset_page": 10,
        "caption_page": 11,  # 题注跨页在第 11 页
        "source_page_index": 9,
        "zone": "MAIN_TEXT",
        "anchor_id": "ea_fig5",
        "evidence_id": "ev_fig5",
        "parse_revision_id": "pr_1",
        "kb_id": "kb-a",
        "file_id": "file-a",
        "source_sha256": "a" * 64,
        "quote_head": "Figure 5 CRISPR/Cas9...",
        "match_tier": "V0_EXACT_ASSET_SHA",
        "panel_match": "whole",
    }
    binding = binding_from_locator_resolution(resolution, retrieval_id="kr_1", locator_kind="FIGURE_IMAGE")
    # 跨页图表两个页码都显式携带，不静默合并
    assert binding.page_number == 10
    assert binding.asset_pdf_page_number == 10
    assert binding.caption_pdf_page_number == 11
    assert binding.source_page_index == 9
    assert binding.panel_match == "whole"
    assert binding.match_tier == "V0_EXACT_ASSET_SHA"


def test_binding_v2_single_page_fallback_keeps_compat():
    from yuxi.knowledge.contracts.locator_binding import binding_from_locator_resolution

    binding = binding_from_locator_resolution(
        {"status": "VERIFIED", "page": 15, "evidence_id": "ev_x", "anchor_id": "ea_x"},
        retrieval_id="kr_1",
    )
    assert binding.page_number == 15
    assert binding.asset_pdf_page_number is None
    assert binding.caption_pdf_page_number is None
    assert binding.source_page_index == 14


# ---- R-P0：状态投影四集合契约 ----


@pytest.mark.asyncio
async def test_locator_run_projects_retrieval_candidates_and_counts(tmp_path):
    """验收：Locator NOT_FOUND 但检索有命中 → 状态显示「N 候选、0 已验证」。"""
    from yuxi.knowledge.evidence.assembler import assemble_evidence_for_run
    from yuxi.repositories.knowledge_retrieval_repository import KnowledgeRetrievalRepository
    from yuxi.storage.postgres.models_knowledge import KnowledgeRetrievalRun

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(KnowledgeFile.__table__.create)
        await connection.run_sync(KnowledgeParseRevision.__table__.create)
        await connection.run_sync(KnowledgeChunk.__table__.create)
        await connection.run_sync(EvidenceAnchorRecord.__table__.create)
        await connection.run_sync(EvidenceSpanRecord.__table__.create)
        await connection.run_sync(KnowledgeRetrievalRun.__table__.create)

    records = [
        SimpleNamespace(
            retrieval_id="kr_fig5",
            status="DEGRADED",
            intent="QUOTE_LOCATOR",
            chunk_ids_json=["chunk_1"],
            evidence_ids_json=["evdoc_1"],
            locator_resolution_json={"status": "NOT_FOUND", "reason": "figure_label_caption_not_found_in_scope"},
        )
    ]

    async def list_for_run(_repository, _run_id):
        return records

    KnowledgeRetrievalRepository.list_for_run = list_for_run
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(
            KnowledgeParseRevision(
                id=1,
                revision_id="spr_1",
                tenant_id=1,
                kb_id="kb-a",
                file_id="file_1",
                source_sha256="1" * 64,
                parser_fingerprint="f" * 64,
                pipeline_version="scientific_pdf_v3.0",
                status="INDEXED_FULL",
            )
        )
        session.add(
            KnowledgeFile(
                id=1,
                file_id="file_1",
                kb_id="kb-a",
                filename="paper.pdf",
                active_parse_revision_id="spr_1",
                active_index_revision_id="sir_1",
            )
        )
        quote = "OsMYB73 is expressed in rice endosperm during seed development."
        session.add(
            KnowledgeChunk(
                id=1,
                chunk_id="chunk_1",
                kb_id="kb-a",
                file_id="file_1",
                chunk_index=0,
                content=f"context {quote} tail",
                source_provenance={
                    "schema_version": "scientific_pdf_chunk_v2",
                    "parse_revision_id": "spr_1",
                    "index_revision_id": "sir_1",
                    "evidence_anchor_ids": ["ea_1"],
                },
            )
        )
        session.add(
            EvidenceAnchorRecord(
                id=1,
                anchor_id="ea_1",
                parse_revision_id="spr_1",
                page=3,
                bbox=[40.0, 300.0, 280.0, 380.0],
                word_start=1,
                word_end=4,
                quote_hash=hashlib.sha256(quote.encode()).hexdigest(),
                prefix_hash=hashlib.sha256("context ".encode()).hexdigest(),
                suffix_hash=hashlib.sha256(" tail".encode()).hexdigest(),
                quote=quote,
                fragments=[{"page_index": 2, "bbox": [40.0, 300.0, 280.0, 380.0], "coordinate_space": "pdf_points"}],
                anchor_type="paragraph",
                locator_quality="HIGH",
                confidence=1.0,
                locatable=True,
                source="pymupdf",
            )
        )
        await session.commit()

        result = await assemble_evidence_for_run(session, "run_fig5", allowed_kb_ids={"kb-a"})
    await engine.dispose()

    # 四集合契约：检索候选与已验证绑定分开；0 可引用 ≠ 0 检索
    assert result["projection_status"] == "LOCATOR_FAILED"
    assert result["evidence"] == []
    assert len(result["retrieval_candidates"]) == 1
    assert result["retrieval_candidates"][0]["evidence_role"] == "RETRIEVAL_CANDIDATE"
    assert result["locator_status"] == "NOT_FOUND"
    assert result["locator_status_reason"] == "figure_label_caption_not_found_in_scope"
    summary = result["summary"]
    assert summary["retrieval_candidate_count"] == 1
    assert summary["verified_binding_count"] == 0
    assert summary["answer_evidence_count"] == 0
