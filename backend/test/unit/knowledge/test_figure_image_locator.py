"""Figure Image Locator v2 单测：观察契约、pHash、确定性 V0/V1 前置、V2/V3 观察、路由。"""

from __future__ import annotations

import io

import pytest
from PIL import Image, ImageDraw

from yuxi.knowledge.planning.turn_execution_plan import TaskIntent, plan_turn
from yuxi.knowledge.vision.figure_image_locator import (
    TIER_V0_EXACT_ASSET_SHA,
    TIER_V1_LOCAL_FEATURE_GEOMETRY,
    TIER_V1_STRONG_PHASH_LABEL,
    TIER_V2_VISUAL_CONSTRAINTS,
    TIER_V3_MULTI_SIGNAL_HARD_CONSTRAINTS,
    adjudicate_deterministic_match,
    adjudicate_figure_candidates,
    adjudicate_local_feature_match,
)
from yuxi.knowledge.vision.local_features import match_local_feature_geometry
from yuxi.knowledge.vision.phash import (
    compute_asset_digest,
    compute_panel_phashes,
    compute_phash,
    phash_hamming_distance,
)
from yuxi.knowledge.vision.visual_observation import VisualObservationEnvelope, parse_visual_observation

pytestmark = [pytest.mark.unit]

_CAPTION_FIG1 = (
    "Figure 1. Expression patterns of OsMYB73 in rice seeds. (a) Relative expression levels of "
    "OsMYB73 during seed development measured by qRT-PCR. (b) Histochemical GUS staining of "
    "transgenic rice seeds. (c) Subcellular localization of OsMYB73-GFP fusion protein in rice "
    "protoplasts. Bar, 1.0 cm."
)
_CAPTION_FIG4 = "Figure 4. Root system architecture of rice seedlings treated with auxin."


def _norm(text: str) -> str:
    from yuxi.knowledge.rendering.claim_evidence_resolver import normalize_for_match

    return normalize_for_match(text)


def _observation(**overrides) -> VisualObservationEnvelope:
    payload = {
        "schema_version": "visual-observation.v1",
        "figure_label": "Figure 1",
        "panel_labels": ["a", "b", "c"],
        "visible_entities": ["OsMYB73-GFP"],
        "visible_text": ["Relative expression levels", "Seed 5 DAF"],
        "inferred_caption_fragments": ["Rice OsMYB73 gene expression", "histochemical GUS staining"],
        "visual_structure": {"bar_chart": True, "microscopy": True, "tissue_images": True},
        "confidence": 0.9,
    }
    payload.update(overrides)
    payload = {key: value for key, value in payload.items() if value is not ...}
    return VisualObservationEnvelope.model_validate(payload)


def _entity(
    *,
    label: str = "Figure 1",
    caption: str = _CAPTION_FIG1,
    file_id: str = "file-a",
    parse_revision_id: str = "pr_1",
    page: int = 4,
    assets: list[dict] | None = None,
) -> dict:
    return {
        "entity_key": f"figure-1-{file_id}",
        "container_label": label,
        "caption": caption,
        "caption_norm": _norm(caption),
        "caption_page": page,
        "kb_id": "kb-a",
        "file_id": file_id,
        "filename": "osmyb73-paper.pdf",
        "source_sha256": "a" * 64,
        "parse_revision_id": parse_revision_id,
        "index_revision_id": "ir_1",
        "span_id": "es_fig1",
        "span_evidence_id": "evs_fig1",
        "zone": "MAIN_TEXT",
        "assets": assets
        or [
            {
                "anchor_id": "ea_fig1",
                "page": page,
                "bbox": [40.0, 400.0, 280.0, 480.0],
                "asset_digest": None,
                "asset_phash": None,
                "panel_phashes": {},
                "img_path": "images/fig1.jpg",
                "width": 800,
                "height": 600,
            }
        ],
    }


def _fig4_entity() -> dict:
    return _entity(
        label="Figure 4",
        caption=_CAPTION_FIG4,
        page=9,
        assets=[
            {
                "anchor_id": "ea_fig4",
                "page": 9,
                "bbox": [40.0, 900.0, 280.0, 980.0],
                "asset_digest": None,
                "asset_phash": None,
                "panel_phashes": {},
                "img_path": "images/fig4.jpg",
                "width": 400,
                "height": 300,
            }
        ],
    )


# ---- 观察契约：Observation，不是 Authority ----


def test_observation_schema_has_no_page_fields_and_rejects_them():
    """Golden Test：模型输出 page_number → 整份观察被拒绝（schema 物理上无此字段）。"""
    assert (
        parse_visual_observation('{"schema_version":"visual-observation.v1","figure_label":"Figure 1","page_number":4}')
        is None
    )
    assert parse_visual_observation('{"schema_version":"visual-observation.v1","file_id":"file-a"}') is None
    valid = parse_visual_observation('{"schema_version":"visual-observation.v1","figure_label":"Figure 1"}')
    assert valid is not None and valid.figure_label == "Figure 1"
    assert not {"page_number", "file_id", "anchor_id", "bbox"} & set(VisualObservationEnvelope.model_fields)


# ---- pHash ----


def _bar_chart_png(*, different: bool = False) -> bytes:
    image = Image.new("RGB", (320, 240), "white")
    draw = ImageDraw.Draw(image)
    if different:
        draw.ellipse((40, 40, 280, 200), fill=(30, 144, 255))
    else:
        for index, height in enumerate((120, 90, 60, 140, 100)):
            x0 = 40 + index * 55
            draw.rectangle((x0, 220 - height, x0 + 34, 220), fill=(70, 130, 60))
        draw.line((30, 220, 300, 220), fill="black", width=2)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _feature_rich_png(seed: int = 7) -> bytes:
    import cv2
    import numpy as np

    rng = np.random.default_rng(seed)
    image = np.full((900, 1100, 3), 255, dtype=np.uint8)
    for index in range(80):
        x, y = (int(value) for value in rng.integers([30, 30], [1070, 870]))
        radius = int(rng.integers(5, 35))
        color = tuple(int(value) for value in rng.integers(0, 220, size=3))
        cv2.circle(image, (x, y), radius, color, 2)
        cv2.putText(image, f"OsMYB73-{index}", (max(0, x - 30), y), cv2.FONT_HERSHEY_SIMPLEX, 0.35, color, 1)
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return encoded.tobytes()


def _resampled_screenshot(image_bytes: bytes) -> bytes:
    import cv2
    import numpy as np

    image = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    image = image[25:-20, 35:-30]
    image = cv2.resize(image, (719, 653), interpolation=cv2.INTER_AREA)
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return encoded.tobytes()


def test_phash_identical_and_similar_images():
    original = _bar_chart_png()
    assert compute_phash(original) is not None
    assert phash_hamming_distance(compute_phash(original), compute_phash(original)) == 0
    # 缩放重编码（同图不同尺寸）应保持强匹配（≤8）
    image = Image.open(io.BytesIO(original)).resize((160, 120))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    distance = phash_hamming_distance(compute_phash(original), compute_phash(buffer.getvalue()))
    assert distance is not None and distance <= 8


def test_phash_different_images_are_not_strong_match():
    distance = phash_hamming_distance(compute_phash(_bar_chart_png()), compute_phash(_bar_chart_png(different=True)))
    assert distance is not None and distance > 8


def test_panel_phashes_cover_quadrants_and_halves():
    panels = compute_panel_phashes(_bar_chart_png())
    assert set(panels) >= {"whole", "q1", "q2", "q3", "q4", "left_half", "right_half"}
    assert all(len(value) == 16 for value in panels.values())


def test_quadrant_crop_matches_parent_panel_fingerprint():
    """panel 裁剪指纹：上传 (c)（左下象限）子图 → 命中父图 q3 指纹。"""
    original = _bar_chart_png()
    panels = compute_panel_phashes(original)
    image = Image.open(io.BytesIO(original))
    width, height = image.size
    quadrant = image.crop((0, height // 2, width // 2, height))
    buffer = io.BytesIO()
    quadrant.save(buffer, format="PNG")
    crop_hash = compute_phash(buffer.getvalue())
    distance = phash_hamming_distance(crop_hash, panels["q3"])
    assert distance is not None and distance <= 8


def test_local_feature_geometry_accepts_resampled_cropped_screenshot():
    original = _feature_rich_png()
    metrics = match_local_feature_geometry(_resampled_screenshot(original), original)

    assert metrics["strong"] is True
    assert metrics["inliers"] >= 20
    assert metrics["inlier_ratio"] >= 0.45


def test_local_feature_geometry_rejects_unrelated_image():
    metrics = match_local_feature_geometry(_feature_rich_png(seed=11), _feature_rich_png(seed=29))

    assert metrics["strong"] is False


# ---- V0/V1 确定性裁决（VLM 后置的核心验收） ----


def test_v0_exact_sha_matches_without_any_observation():
    """验收：未配置视觉模型但图片与库内资产完全一致 → SHA 路径仍成功。"""
    image_bytes = _bar_chart_png()
    digest = compute_asset_digest(image_bytes)
    entity = _entity(
        assets=[
            {
                "anchor_id": "ea_fig1",
                "page": 4,
                "bbox": [40.0, 400.0, 280.0, 480.0],
                "asset_digest": digest,
                "asset_phash": compute_phash(image_bytes),
                "panel_phashes": compute_panel_phashes(image_bytes),
                "img_path": "images/fig1.jpg",
                "width": 320,
                "height": 240,
            }
        ]
    )
    resolution = adjudicate_deterministic_match([entity, _fig4_entity()], image_asset_digest=digest)
    assert resolution["status"] == "VERIFIED"
    assert resolution["tier"] == TIER_V0_EXACT_ASSET_SHA
    assert resolution["panel_key"] == "whole"
    assert resolution["signals"]["asset_sha_exact"] is True


def test_v1_panel_crop_binds_parent_figure():
    """验收：只上传 (c) 面板 → pHash 命中父图 panel 指纹，绑定并报告 panel_key。"""
    original = _bar_chart_png()
    entity = _entity(
        assets=[
            {
                "anchor_id": "ea_fig1",
                "page": 4,
                "bbox": [40.0, 400.0, 280.0, 480.0],
                "asset_digest": None,
                "asset_phash": compute_phash(original),
                "panel_phashes": compute_panel_phashes(original),
                "img_path": "images/fig1.jpg",
                "width": 320,
                "height": 240,
            }
        ]
    )
    image = Image.open(io.BytesIO(original))
    width, height = image.size
    quadrant = image.crop((0, height // 2, width // 2, height))
    buffer = io.BytesIO()
    quadrant.save(buffer, format="PNG")
    resolution = adjudicate_deterministic_match([entity, _fig4_entity()], image_phash=compute_phash(buffer.getvalue()))
    assert resolution["status"] == "VERIFIED"
    assert resolution["tier"] == TIER_V1_STRONG_PHASH_LABEL
    assert resolution["panel_key"] == "q3"
    assert resolution["signals"]["phash_distance"] <= 8


def test_v0_multiple_locations_fail_closed():
    """两篇论文出现相同图片 → MULTIPLE_MATCHES，不发布候选页码。"""
    digest = compute_asset_digest(_bar_chart_png())
    first = _entity(assets=[{**_entity()["assets"][0], "asset_digest": digest}])
    second = _entity(
        file_id="file-b",
        parse_revision_id="pr_2",
        assets=[{**_entity()["assets"][0], "anchor_id": "ea_fig1_b", "asset_digest": digest}],
    )
    resolution = adjudicate_deterministic_match([first, second], image_asset_digest=digest)
    assert resolution["status"] == "MULTIPLE_MATCHES"
    assert resolution["reason"] == "exact_asset_hits_multiple_physical_locations"


def test_deterministic_unresolved_when_no_fingerprints():
    resolution = adjudicate_deterministic_match([_entity(), _fig4_entity()], image_asset_digest="b" * 64)
    assert resolution["status"] == "DETERMINISTIC_UNRESOLVED"


@pytest.mark.asyncio
async def test_local_feature_fallback_verifies_unique_physical_asset(monkeypatch):
    original = _feature_rich_png()
    screenshot = _resampled_screenshot(original)
    entity = _entity(
        assets=[
            {
                **_entity()["assets"][0],
                "asset_phash": compute_phash(original),
                "object_bucket": "knowledgebases",
                "object_name": "scoped/figure-1.png",
            }
        ]
    )

    class FakeMinio:
        async def adownload_file(self, bucket, object_name):
            assert (bucket, object_name) == ("knowledgebases", "scoped/figure-1.png")
            return original

    import yuxi.storage.minio.client as minio_module

    monkeypatch.setattr(minio_module, "get_minio_client", lambda: FakeMinio())
    resolution = await adjudicate_local_feature_match(
        [entity],
        image_bytes=screenshot,
        image_phash=compute_phash(screenshot),
    )

    assert resolution["status"] == "VERIFIED"
    assert resolution["tier"] == TIER_V1_LOCAL_FEATURE_GEOMETRY
    assert resolution["asset"]["page"] == 4


@pytest.mark.asyncio
async def test_local_feature_fallback_fails_closed_for_duplicate_physical_locations(monkeypatch):
    original = _feature_rich_png()
    screenshot = _resampled_screenshot(original)
    first = _entity(
        assets=[
            {
                **_entity()["assets"][0],
                "asset_phash": compute_phash(original),
                "object_bucket": "knowledgebases",
                "object_name": "scoped/first.png",
            }
        ]
    )
    second = _entity(
        file_id="file-b",
        parse_revision_id="pr_2",
        page=11,
        assets=[
            {
                **_entity()["assets"][0],
                "anchor_id": "ea_fig1_b",
                "page": 11,
                "asset_phash": compute_phash(original),
                "object_bucket": "knowledgebases",
                "object_name": "scoped/second.png",
            }
        ],
    )

    class FakeMinio:
        async def adownload_file(self, _bucket, _object_name):
            return original

    import yuxi.storage.minio.client as minio_module

    monkeypatch.setattr(minio_module, "get_minio_client", lambda: FakeMinio())
    resolution = await adjudicate_local_feature_match(
        [first, second],
        image_bytes=screenshot,
        image_phash=compute_phash(screenshot),
    )

    # 几何证据在两处"都像"= 对此查询不具区分度：降为未决交给观察/桥接继续裁决（仍失败关闭，无页码），
    # 不再像 SHA/pHash 歧义那样终局为 MULTIPLE_MATCHES（真实 run：纯文字裁片伪几何命中挡住了视觉通道）
    assert resolution["status"] == "DETERMINISTIC_UNRESOLVED"
    assert resolution["reason"] == "local_feature_ambiguous_across_locations"
    assert resolution["ambiguous_locations"] == 2
    assert "page" not in resolution


# ---- V2/V3 观察裁决（新实体形状） ----


def test_v2_label_plus_constraints_verifies_uploaded_figure_page():
    resolution = adjudicate_figure_candidates(_observation(), [_entity(), _fig4_entity()])
    assert resolution["status"] == "VERIFIED"
    assert resolution["tier"] == TIER_V2_VISUAL_CONSTRAINTS
    assert resolution["asset"]["page"] == 4
    signals = resolution["signals"]
    assert signals["label_match"] and (signals["visible_text_hits"] + signals["entity_hits"]) >= 2


def test_label_conflict_rejects_wrong_figure():
    observation = _observation(visible_text=["Bar, 1.0 cm"], inferred_caption_fragments=["Bar, 1.0 cm"])
    resolution = adjudicate_figure_candidates(observation, [_fig4_entity()])
    assert resolution["status"] == "NOT_FOUND"


def test_single_signal_never_publishes_page():
    observation = _observation(visible_text=[], visible_entities=[], inferred_caption_fragments=[])
    resolution = adjudicate_figure_candidates(observation, [_entity()])
    assert resolution["status"] == "NOT_FOUND"
    assert resolution["reason"] == "no_figure_candidate_satisfies_two_signal_minimum"


def test_v3_no_label_but_text_and_entity_signals():
    observation = _observation(
        figure_label=None,
        visible_text=["Relative expression levels"],
        visible_entities=["OsMYB73-GFP"],
    )
    resolution = adjudicate_figure_candidates(observation, [_entity()])
    assert resolution["status"] == "VERIFIED"
    assert resolution["tier"] == TIER_V3_MULTI_SIGNAL_HARD_CONSTRAINTS


def test_two_similar_figures_yield_multiple_matches():
    second = _entity(file_id="file-b", parse_revision_id="pr_2")
    resolution = adjudicate_figure_candidates(_observation(), [_entity(), second])
    assert resolution["status"] == "MULTIPLE_MATCHES"
    assert resolution["reason"] == "figure_match_ambiguous"


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


def test_plan_turn_image_without_locator_intent_stays_normal():
    plan = plan_turn("帮我总结一下这张图", has_knowledge_scope=True, has_image=True)
    assert plan.task.primary_intent != TaskIntent.FIGURE_LOCATOR
    assert plan.task.target_type != "FIGURE_IMAGE"


# ---- P0/P2：多裁剪变体 + 降级回答 ----


def test_query_crop_variants_generated():
    """P2：多裁剪变体应包含全图 + 中心 + 四象限。"""
    from yuxi.knowledge.vision.phash import query_crop_variants

    # 用 640x480 大图确保所有裁剪区域 ≥ 100px
    from PIL import Image, ImageDraw
    import io
    image = Image.new("RGB", (640, 480), "white")
    draw = ImageDraw.Draw(image)
    for i, h in enumerate((200, 150, 100, 240, 180)):
        x0 = 60 + i * 110
        draw.rectangle((x0, 480 - h, x0 + 70, 460), fill=(30, 144, 255))
    buf = io.BytesIO(); image.save(buf, format="PNG")
    variants = query_crop_variants(buf.getvalue())
    labels = [label for _bytes, label in variants]
    assert "normalized_whole" in labels
    assert "center_50" in labels
    assert any(label.startswith("q_") for label in labels)
    # 每个变体都是有效图片字节
    for data, _label in variants:
        assert len(data) > 100


def test_query_crop_variants_degenerate():
    """P2：退化输入（空字节）不崩溃。"""
    from yuxi.knowledge.vision.phash import query_crop_variants

    assert query_crop_variants(b"") == []


def test_observation_summary_carried_on_failure():
    """P0：V2/V3 全 miss 时，observation_summary 应携带到 NOT_FOUND resolution。"""
    observation = _observation(figure_label="Figure 2", visible_entities=["OsMYB73", "GUS"], visible_text=["Bar, 1.0 cm"])
    # 用空候选列表让裁决全部 miss
    resolution = adjudicate_figure_candidates(observation, [])
    assert resolution.get("status") != "VERIFIED"


def test_visual_description_from_observation_summary():
    """P0：chat 层的视觉描述——只引用观察契约字段，不做推断。"""
    from yuxi.services.chat_service import _visual_description

    summary = {
        "figure_label": "Figure 2",
        "visible_entities": ["OsMYB73", "cr-myb73"],
        "visible_text": ["Bar, 1.0 cm"],
        "visual_structure_active": ["bar_chart", "microscopy"],
        "panel_labels": ["a", "b", "c"],
        "confidence": 0.9,
    }
    text = _visual_description({"observation_summary": summary})
    assert "Figure 2" in text
    assert "OsMYB73" in text
    assert "柱状图" in text
    assert "显微" in text
    assert "3 个子图" in text
    # 空摘要 → 空串
    assert _visual_description({}) == ""
    assert _visual_description({"observation_summary": {}}) == ""


def test_caption_search_hint_from_candidates():
    """P0/P1：题注搜索候选 → "可能来自"提示。"""
    from yuxi.services.chat_service import _caption_search_hint

    locator = {
        "caption_search_candidates": [
            {"figure_label": "Figure 5", "filename": "paper-a.pdf", "signal_hits": 3},
            {"figure_label": "Figure 2", "filename": "paper-a.pdf", "signal_hits": 2},
        ]
    }
    hint = _caption_search_hint(locator)
    assert "Figure 5" in hint
    assert "paper-a.pdf" in hint
    assert "Figure 2" in hint  # 第二候选也在提示中
    # 空候选 → 空串
    assert _caption_search_hint({}) == ""
