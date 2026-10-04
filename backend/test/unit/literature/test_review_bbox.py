"""复核页图 bbox 归一契约测试（P0-①）。

对标上游 PaperAgent review_service：
- BBOX_SCALE = 0.5（PaddleOCR-VL 块 bbox 是 PDF 页的 2× 画布）
- 取值顺序 paddle.bbox → mineru.bbox → [0,0,0,0]

Yuxi 的取舍：比上游更保守——非法/零面积 bbox 整体丢弃为 None（而非退化
[0,0,0,0]），因为零面积框画到页图上会误导人工裁决；丢弃不阻断仲裁。
"""
import pytest

from yuxi.literature.review_contract import (
    BBOX_SCALE,
    ReviewItem,
    normalize_bbox,
    normalize_review_items,
)


def _payload(bbox, *, paddle_bbox=None, mineru_bbox=None, idx=0, block="b1"):
    return {
        "report_idx": idx,
        "page": 1,
        "paddleocr": {"text": "b", "block_id": block, **({"bbox": paddle_bbox} if paddle_bbox else {})},
        "mineru": {"text": "a", "block_id": block, **({"bbox": mineru_bbox} if mineru_bbox else {})},
        "bbox": bbox,
    }


def test_bbox_scaled_by_half():
    """2× 画布坐标按 0.5 折算回 PDF 页坐标。"""
    assert normalize_bbox([200, 100, 400, 300]) == (100.0, 50.0, 200.0, 150.0)
    assert BBOX_SCALE == 0.5


@pytest.mark.parametrize(
    "raw",
    [
        [0, 0, 0, 0],  # 零面积（上游的默认值）→ 丢弃
        [100, 100, 100, 100],  # 宽度为零 → 丢弃
        [300, 100, 200, 300],  # x1 < x0 → 丢弃
        [-10, 0, 100, 100],  # 负坐标 → 丢弃
        [0, 0, 1e9, 1e9],  # 越界 → 丢弃
        [0, 0, float("nan"), 10],  # NaN → 丢弃
        [0, 0, float("inf"), 10],  # Inf → 丢弃
        [0, 0, 10],  # 长度不对 → 丢弃
        "0,0,10,10",  # 非序列 → 丢弃
        [True, 0, 10, 10],  # bool 冒充数字 → 丢弃
        None,
    ],
)
def test_invalid_bbox_is_dropped(raw):
    """非法 bbox 一律丢弃为 None，绝不退化成 [0,0,0,0]。"""
    assert normalize_bbox(raw) is None


def test_review_item_prefers_paddle_bbox_then_mineru():
    """取值顺序：paddleocr 优先，回落 mineru，再回落顶层。"""
    item = ReviewItem.from_payload(
        _payload(None, paddle_bbox=[200, 100, 400, 300], mineru_bbox=[0, 0, 100, 100]),
        render_id="r1",
    )
    assert item.bbox == (100.0, 50.0, 200.0, 150.0)

    item = ReviewItem.from_payload(_payload(None, mineru_bbox=[200, 100, 400, 300]), render_id="r1")
    assert item.bbox == (100.0, 50.0, 200.0, 150.0)

    item = ReviewItem.from_payload(_payload([200, 100, 400, 300]), render_id="r1")
    assert item.bbox == (100.0, 50.0, 200.0, 150.0)


def test_review_item_bbox_empty_when_absent():
    """供应商未给 bbox → 空元组，序列化后为空列表，前端据此显示"无页图定位"。"""
    item = ReviewItem.from_payload(_payload(None), render_id="r1")
    assert item.bbox == ()
    assert item.to_payload()["bbox"] == []


def test_bbox_does_not_break_normalization_flow():
    """bbox 缺失/非法都不得影响既有归一与身份去重语义。

    三项用不同 report_idx 与 block_id 构造——normalize_review_items 对
    重复身份是 fail-closed 的（"禁止按序号猜测裁决"），不能靠同序号造三项。
    """
    items = normalize_review_items(
        [
            _payload(None, paddle_bbox=[200, 100, 400, 300], idx=0, block="blk-0"),
            _payload(None, idx=1, block="blk-1"),
            _payload([0, 0, 0, 0], idx=2, block="blk-2"),
        ],
        render_id="r1",
        render_digest="d1",
    )
    assert len(items) == 3
    assert [bool(i.bbox) for i in items] == [True, False, False]
    assert len({i.item_id for i in items}) == 3
    assert [i.report_idx for i in items] == [0, 1, 2]