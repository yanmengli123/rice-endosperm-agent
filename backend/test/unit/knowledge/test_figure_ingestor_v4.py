"""figure_ingestor v4：多 panel 聚合 / bbox 锚点配对 / 合成整图 / partial 门 / 角色。"""

from __future__ import annotations

import hashlib
import io

import pytest
import pytest_asyncio
from PIL import Image, ImageDraw
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.knowledge.vision.figure_ingestor import (
    FIGURE_INGESTOR_VERSION,
    ROLE_PANEL,
    ROLE_PRIMARY,
    _bbox_gap,
    _bbox_iou,
    _panel_letter_count,
    group_visual_blocks,
    persist_figure_index,
)
from yuxi.storage.postgres.models_knowledge import (
    EvidenceAnchorRecord,
    EvidenceSpanRecord,
    FigureAssetRecord,
    FigureEntityRecord,
    KnowledgeFile,
    KnowledgeParseRevision,
)

pytestmark = [pytest.mark.unit]

_CAPTION = (
    "Figure 2 Grain phenotypes of ZH11 and cr-myb73. (a) Grain length. (b) Grain width. "
    "(c) Chalkiness rate. (d) Starch granules."
)


def _block(img: str, page: int, bbox, caption: str = ""):
    from yuxi.knowledge.vision.figure_ingestor import _bbox, _entity_key_for_caption

    return {
        "asset": {"img_path": img, "page": page, "bbox": list(bbox), "caption": caption},
        "caption": caption,
        "label_key": _entity_key_for_caption(caption),
        "page": page,
        "page_index": page - 1,
        "bbox": _bbox(bbox),
        "img_path": img,
        "block_id": f"blk:{img}",
    }


# ---- 纯函数 ----


def test_bbox_helpers():
    assert _bbox_gap((0, 0, 10, 10), (5, 5, 20, 20)) == 0.0
    assert _bbox_gap((0, 0, 10, 10), (20, 0, 30, 10)) == 10.0  # 并排，水平间隙 10
    assert round(_bbox_gap((0, 0, 10, 10), (20, 20, 30, 30)), 3) == round((200) ** 0.5, 3)
    assert _bbox_iou((0, 0, 10, 10), (0, 0, 10, 10)) == 1.0
    assert _bbox_iou((0, 0, 10, 10), (5, 0, 15, 10)) == pytest.approx(1 / 3)
    assert _panel_letter_count(_CAPTION) == 4
    assert _panel_letter_count("Figure 1 no panels") == 0


def test_group_merges_adjacent_unlabeled_blocks_into_labeled_group_only_same_page():
    blocks = [
        _block("k.jpg", 5, (72, 500, 300, 700), _CAPTION),  # 带题注的 panel (k)
        _block("a.jpg", 5, (72, 72, 300, 280)),  # 上方相邻（链式：a-b 相邻，b 与 k 相邻）
        _block("b.jpg", 5, (72, 290, 300, 490)),
        _block("far.jpg", 5, (72, 760, 300, 830)),  # 与 k 间隙 60pt > 36 → 不并入
        _block("p6.jpg", 6, (72, 72, 300, 280)),  # 另一页 → 不并入
    ]
    groups = group_visual_blocks(blocks)
    labeled = next(members for key, members in groups if key == "figure 2")
    assert [m["img_path"] for m in labeled] == ["a.jpg", "b.jpg", "k.jpg"]  # 阅读序（上→下）
    assert all(m.get("merged_into_label") for m in labeled if m["img_path"] != "k.jpg")
    unlabeled = [members for key, members in groups if key is None]
    assert sorted(m["img_path"] for cluster in unlabeled for m in cluster) == ["far.jpg", "p6.jpg"]
    assert len(unlabeled) == 2  # far 与 p6 不同页，各自成簇


def test_group_never_merges_two_labeled_groups_and_assigns_nearest():
    blocks = [
        _block("f3.jpg", 7, (72, 72, 300, 300), "Figure 3 A"),
        _block("f4.jpg", 7, (72, 500, 300, 700), "Figure 4 B"),
        _block("x.jpg", 7, (72, 310, 300, 340)),  # 距 Figure 3 10pt、距 Figure 4 160pt → 归 Figure 3
    ]
    groups = dict(group_visual_blocks(blocks))
    assert [m["img_path"] for m in groups["figure 3"]] == ["f3.jpg", "x.jpg"]
    assert [m["img_path"] for m in groups["figure 4"]] == ["f4.jpg"]


# ---- 集成：sqlite + fake MinIO + 合成整图 ----


def _png(color: str, size=(200, 200)) -> bytes:
    image = Image.new("RGB", size, "white")
    ImageDraw.Draw(image).rectangle((20, 20, size[0] - 20, size[1] - 20), fill=color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _tiny_pdf() -> bytes:
    import fitz

    with fitz.open() as document:
        # 块在第 5 页（page_index=4）：前 4 页留白，渲染器按页索引取页
        for _ in range(4):
            document.new_page(width=595, height=842)
        page = document.new_page(width=595, height=842)
        page.draw_rect(fitz.Rect(72, 72, 300, 280), color=(0, 0, 1), fill=(0.2, 0.4, 0.9))
        page.draw_rect(fitz.Rect(310, 72, 540, 280), color=(1, 0, 0), fill=(0.9, 0.3, 0.2))
        page.draw_rect(fitz.Rect(72, 290, 300, 490), color=(0, 1, 0), fill=(0.2, 0.8, 0.3))
        return document.tobytes()


@pytest_asyncio.fixture
async def v4_session(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        for table in (
            KnowledgeFile.__table__,
            KnowledgeParseRevision.__table__,
            EvidenceAnchorRecord.__table__,
            EvidenceSpanRecord.__table__,
            FigureEntityRecord.__table__,
            FigureAssetRecord.__table__,
        ):
            await connection.run_sync(table.create)

    prefix = f"tenants/1/documents/{'a' * 64}/mineru/spr_v4/images"
    objects = {
        f"{prefix}/d1-a.jpg": _png("blue"),
        f"{prefix}/d2-b.jpg": _png("red"),
        f"{prefix}/d3-k.jpg": _png("green"),
    }
    uploads: dict[str, bytes] = {}

    class fake_minio:
        async def alist_object_names_by_prefix(self, bucket, pfx):
            return list(objects)

        async def adownload_file(self, bucket, object_name):
            return objects[object_name]

        async def aupload_file(self, bucket, object_name, data, content_type=None):
            uploads[object_name] = data
            return object_name

    import yuxi.storage.minio.client as minio_module

    monkeypatch.setattr(minio_module, "get_minio_client", lambda: fake_minio())

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(
            KnowledgeParseRevision(
                id=1,
                revision_id="spr_v4",
                tenant_id=1,
                kb_id="kb-a",
                file_id="file_v4",
                source_sha256="a" * 64,
                parser_fingerprint="f" * 64,
                pipeline_version="scientific_pdf_v3.0",
                status="INDEXED_FULL",
            )
        )
        session.add(
            KnowledgeFile(id=1, file_id="file_v4", kb_id="kb-a", filename="v4.pdf", active_parse_revision_id="spr_v4")
        )
        await session.flush()
        yield session, uploads
    await engine.dispose()


def _anchor(row_id: int, page: int, bbox, quote: str) -> EvidenceAnchorRecord:
    return EvidenceAnchorRecord(
        id=row_id,
        anchor_id=f"ea_{row_id}",
        parse_revision_id="spr_v4",
        page=page,
        bbox=list(bbox),
        word_start=0,
        word_end=5,
        quote_hash=hashlib.sha256(quote.encode()).hexdigest(),
        prefix_hash="p",
        suffix_hash="s",
        quote=quote,
        fragments=[{"page_index": page - 1, "bbox": list(bbox), "coordinate_space": "pdf_points"}],
        anchor_type="image",
        locator_quality="HIGH",
        confidence=1.0,
        locatable=True,
        source="mineru",
        document_partition="MAIN_TEXT",
    )


@pytest.mark.asyncio
async def test_v4_groups_panels_pairs_anchors_by_bbox_and_renders_synthetic_primary(v4_session):
    session, uploads = v4_session
    revision = (await session.execute(select(KnowledgeParseRevision))).scalars().one()
    # 锚点 bbox 与块 bbox 略有偏移（IoU 仍 > 0.5），题注文本与块 caption 不一致 → 只有 bbox 配对能配上
    anchors = [
        _anchor(1, 5, (74, 70, 302, 282), "panel a image"),
        _anchor(2, 5, (312, 74, 542, 282), "panel b image"),
        _anchor(3, 5, (70, 292, 298, 488), "panel k image"),
    ]
    span = EvidenceSpanRecord(
        id=1,
        tenant_id=1,
        parse_revision_id="spr_v4",
        kb_id="kb-a",
        file_id="file_v4",
        span_id="es_fig2",
        anchor_id="ea_3",
        sentence_index=0,
        quote=_CAPTION,
        quote_hash=hashlib.sha256(_CAPTION.encode()).hexdigest(),
        page_number=5,
        evidence_type="caption",
        document_partition="MAIN_TEXT",
        partition_confidence=1.0,
        evidence_id="evs_fig2",
        container_label="Figure 2",
    )
    assets = [
        {
            "kind": "figure",
            "img_path": "images/k.jpg",
            "page": 5,
            "page_index": 4,
            "bbox": [72, 290, 300, 490],
            "caption": _CAPTION,
        },
        {
            "kind": "figure",
            "img_path": "images/a.jpg",
            "page": 5,
            "page_index": 4,
            "bbox": [72, 72, 300, 280],
            "caption": "",
        },
        {
            "kind": "figure",
            "img_path": "images/b.jpg",
            "page": 5,
            "page_index": 4,
            "bbox": [310, 72, 540, 280],
            "caption": "",
        },
    ]

    async def load_pdf():
        return _tiny_pdf()

    summary = await persist_figure_index(
        session, revision=revision, article_assets=assets, spans=[span], anchors=anchors, pdf_bytes_loader=load_pdf
    )
    await session.flush()

    assert summary["version"] == FIGURE_INGESTOR_VERSION
    assert summary["entities"] == 1  # v3 会是 3 个实体（两块无 label 各自成实体）
    assert summary["merged_unlabeled_blocks"] == 2
    assert summary["assets"] == 4 and summary["fingerprinted"] == 4
    assert summary["anchored_assets"] == 4  # 3 块按 bbox 配到锚点 + 合成整图继承最大块锚点
    assert summary["synthetic_primary"] == 1 and summary["synthetic_failures"] == 0
    assert summary["partial_figure_suspected"] == 1  # 题注 4 个 panel 字母，只有 3 块

    entity = (await session.execute(select(FigureEntityRecord))).scalars().one()
    assert entity.entity_key == "figure 2" and entity.asset_count == 4 and entity.caption_anchor_id == "ea_3"
    rows = (await session.execute(select(FigureAssetRecord).order_by(FigureAssetRecord.group_index))).scalars().all()
    synthetic = [row for row in rows if row.asset_key == "synthetic:figure 2"]
    panels = [row for row in rows if row.asset_key != "synthetic:figure 2"]
    assert len(synthetic) == 1 and synthetic[0].role == ROLE_PRIMARY and synthetic[0].group_index == -1
    assert synthetic[0].object_name in uploads and synthetic[0].object_name.endswith("-synthetic_figure_2.png")
    assert synthetic[0].asset_sha256 == hashlib.sha256(uploads[synthetic[0].object_name]).hexdigest()
    assert synthetic[0].bbox == [72.0, 72.0, 540.0, 490.0]  # 图组并集
    assert [row.img_path for row in panels] == ["images/a.jpg", "images/b.jpg", "images/k.jpg"]  # 阅读序
    assert all(row.role == ROLE_PANEL for row in panels)  # 有合成整图时成员全部 panel
    assert [row.anchor_id for row in panels] == ["ea_1", "ea_2", "ea_3"]  # bbox 配对
    assert synthetic[0].anchor_id in {"ea_1", "ea_2", "ea_3"}


@pytest.mark.asyncio
async def test_v4_without_pdf_promotes_largest_panel_and_keeps_far_block_separate(v4_session):
    session, uploads = v4_session
    revision = (await session.execute(select(KnowledgeParseRevision))).scalars().one()
    assets = [
        {"kind": "figure", "img_path": "images/k.jpg", "page": 5, "bbox": [72, 290, 300, 490], "caption": "Figure 2 X"},
        {"kind": "figure", "img_path": "images/a.jpg", "page": 5, "bbox": [72, 72, 300, 280], "caption": ""},
        {
            "kind": "figure",
            "img_path": "images/b.jpg",
            "page": 5,
            "bbox": [72, 700, 540, 800],
            "caption": "",
        },  # 间隙 210 → 独立
    ]
    summary = await persist_figure_index(session, revision=revision, article_assets=assets, spans=[], anchors=[])
    await session.flush()
    assert summary["entities"] == 2 and summary["synthetic_primary"] == 0 and not uploads
    entities = {row.entity_key: row for row in (await session.execute(select(FigureEntityRecord))).scalars().all()}
    assert set(entities) == {"figure 2", "asset:images/b.jpg"}
    rows = (await session.execute(select(FigureAssetRecord))).scalars().all()
    by_path = {row.img_path: row for row in rows}
    # 无合成整图：面积最大的块升为 primary（a 228×208 > k 228×200），其余 panel；唯一 primary
    assert by_path["images/a.jpg"].role == ROLE_PRIMARY and by_path["images/k.jpg"].role == ROLE_PANEL
    assert sum(1 for row in rows if row.entity_id == entities["figure 2"].id and row.role == ROLE_PRIMARY) == 1
    assert by_path["images/b.jpg"].role == ROLE_PRIMARY  # 单块无 label 实体自身即 primary


# ---- v4.1：题注 span 反向绑定（跨页页脚断链修复）----


def _ns_span(label, page, anchor_id):
    from types import SimpleNamespace

    return SimpleNamespace(
        container_label=label,
        quote=f"{label} Yeast two-hybrid and one-hybrid assays of rice transcription factor.",
        page_number=page,
        anchor_id=anchor_id,
        span_id=f"es_{label.replace(' ', '_')}",
        evidence_id=f"evs_{label.replace(' ', '_')}",
        document_partition="MAIN_TEXT",
    )


def _ns_anchor(anchor_id, bbox):
    from types import SimpleNamespace

    return SimpleNamespace(anchor_id=anchor_id, bbox=list(bbox), fragments=None)


def test_bind_orphan_captions_cross_page_footer_with_monotonic_guard():
    from yuxi.knowledge.vision.figure_ingestor import _bind_orphan_captions

    groups = [
        ("figure 3", [_block("f3.jpg", 7, (72, 72, 300, 300), "Figure 3 A")]),
        ("figure 5", [_block("f5.jpg", 10, (72, 72, 300, 300), "Figure 5 B")]),
        (None, [_block("top9.jpg", 9, (72, 60, 300, 200))]),  # 下一页最靠上 → 应绑 Figure 4
        (None, [_block("far9.jpg", 9, (72, 600, 300, 760))]),  # 非最靠上 → 保持无 label
    ]
    span_by_label = {
        "figure 3": _ns_span("Figure 3", 7, "ea_f3"),
        "figure 4": _ns_span("Figure 4", 8, "ea_f4"),
        "figure 5": _ns_span("Figure 5", 10, "ea_f5"),
    }
    summary: dict = {}
    bound = _bind_orphan_captions(
        groups, span_by_label=span_by_label, anchors=[_ns_anchor("ea_f4", (60, 700, 520, 760))], summary=summary
    )
    keys = {id(members): key for key, members in bound}
    assert keys[id(groups[2][1])] == "figure 4"  # 顶部簇绑上题注
    assert keys[id(groups[3][1])] is None  # 非顶部簇失败关闭
    assert summary["caption_span_linked_clusters"] == 1


def test_bind_orphan_captions_same_page_nearest_and_tie_fails_closed():
    from yuxi.knowledge.vision.figure_ingestor import _bind_orphan_captions

    groups = [
        (None, [_block("a.jpg", 5, (72, 60, 300, 200))]),
        (None, [_block("b.jpg", 5, (72, 500, 300, 640))]),
    ]
    span_by_label = {
        "figure 9": _ns_span("Figure 9", 5, "ea_f9"),
    }
    summary: dict = {}
    bound = _bind_orphan_captions(
        groups, span_by_label=span_by_label, anchors=[_ns_anchor("ea_f9", (60, 220, 520, 300))], summary=summary
    )
    keys = {id(members): key for key, members in bound}
    assert keys[id(groups[0][1])] == "figure 9"  # 同页最近（题注在顶部簇下方 20pt）
    assert keys[id(groups[1][1])] is None

    # 同分打平：两个未认领题注到簇的 (页距, bbox 距离) 完全一致 → 失败关闭。
    # 注意 dict 迭代顺序：先入者更优（score < 才更新），同分 ties+=1 → 保持无 label
    tie_span_by_label = {
        "figure 9": _ns_span("Figure 9", 5, "ea_f9a"),
        "figure 10": _ns_span("Figure 10", 5, "ea_f9b"),
    }
    bound2 = _bind_orphan_captions(
        [(None, [_block("c.jpg", 5, (72, 60, 300, 200))])],
        span_by_label=tie_span_by_label,
        anchors=[_ns_anchor("ea_f9a", (60, 220, 520, 300)), _ns_anchor("ea_f9b", (60, 220, 520, 300))],
        summary={},
    )
    assert bound2[0][0] is None  # 同分歧义 → 失败关闭


@pytest.mark.asyncio
async def test_v41_reverse_binding_creates_labeled_entity_with_caption_page(v4_session):
    session, _uploads = v4_session
    revision = (await session.execute(select(KnowledgeParseRevision))).scalars().one()
    span4 = EvidenceSpanRecord(
        id=9,
        tenant_id=1,
        parse_revision_id="spr_v4",
        kb_id="kb-a",
        file_id="file_v4",
        span_id="es_fig4",
        anchor_id="ea_fig4_footer",
        sentence_index=0,
        quote="Figure 4 Yeast two-hybrid and one-hybrid assays of OsMYB73.",
        quote_hash=hashlib.sha256(b"fig4").hexdigest(),
        page_number=8,
        evidence_type="caption",
        document_partition="MAIN_TEXT",
        partition_confidence=1.0,
        evidence_id="evs_fig4",
        container_label="Figure 4",
    )
    footer_anchor = _anchor(9, 8, (60, 700, 520, 760), "Figure 4 Yeast two-hybrid")
    labeled_assets = [
        {"kind": "figure", "img_path": "images/f3.jpg", "page": 7, "bbox": [72, 72, 300, 300], "caption": "Figure 3 A"},
        {
            "kind": "figure",
            "img_path": "images/f5.jpg",
            "page": 10,
            "bbox": [72, 72, 300, 300],
            "caption": "Figure 5 B",
        },
        # Figure 4 的图块在第 9 页顶部，自身无 caption 文本（题注压在上一页页脚）
        {"kind": "figure", "img_path": "images/f4a.jpg", "page": 9, "bbox": [72, 60, 540, 300], "caption": ""},
        {"kind": "figure", "img_path": "images/f4b.jpg", "page": 9, "bbox": [72, 320, 540, 560], "caption": ""},
    ]
    summary = await persist_figure_index(
        session, revision=revision, article_assets=labeled_assets, spans=[span4], anchors=[footer_anchor]
    )
    await session.flush()
    assert summary["caption_span_linked_clusters"] == 1
    entities = {row.entity_key: row for row in (await session.execute(select(FigureEntityRecord))).scalars().all()}
    fig4 = entities["figure 4"]
    assert fig4.caption_anchor_id == "ea_fig4_footer"
    assert fig4.caption_page == 8  # 题注页（跨页：图在第 9 页）
    assert fig4.association_method == "span_linkage"
    assert fig4.caption.startswith("Figure 4 Yeast")
    rows = (
        (await session.execute(select(FigureAssetRecord).where(FigureAssetRecord.entity_id == fig4.id))).scalars().all()
    )
    assert sorted(row.page for row in rows) == [9, 9]  # 资产页 = 图页
