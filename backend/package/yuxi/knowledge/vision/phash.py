"""感知哈希（pHash）：图片定位的视觉信号之一，永远不能单独发布页码。

实现为 32×32 灰度 DCT → 左上 8×8 低频块 → 中值阈值 64bit 指纹。PIL/numpy
是容器内既有传递依赖（导入守卫：缺失时返回 None，定位走失败关闭而不是降级
到「模型说像」）。

发布规则（figure_image_locator V0-V4）：pHash 强匹配（距离 ≤ 8）必须叠加
第二信号（图表编号 / 可见文本 / 实体约束）且物理唯一才可 VERIFIED——用户
可能截图、裁剪、加白边、缩放、压缩或只发一个 panel。
"""

from __future__ import annotations

import hashlib
import io

try:  # pragma: no cover - 依赖存在性由容器保证，缺失路径走 None
    import numpy as _np
    from PIL import Image as _PILImage

    _HAS_IMAGE_STACK = True
except ImportError:  # pragma: no cover
    _np = None
    _PILImage = None
    _HAS_IMAGE_STACK = False

PHASH_VERSION = "phash_dct32_v1"
# 强视觉匹配阈值：汉明距离 ≤ 8（64bit 中 ≤12.5% 翻转）
PHASH_STRONG_DISTANCE = 8


def compute_asset_digest(image_bytes: bytes) -> str:
    """内容寻址资产摘要（V0 精确资产匹配的输入；与 MinIO 图片对象名同源）。"""
    return hashlib.sha256(image_bytes).hexdigest()


def compute_phash_from_image(image: _PILImage.Image) -> str:
    """对已解码 PIL 图像计算 pHash（panel 裁剪变体复用同一 DCT 路径）。"""
    if not _HAS_IMAGE_STACK:
        return ""
    grayscale = image.convert("L").resize((32, 32), _PILImage.Resampling.LANCZOS)
    pixels = _np.asarray(grayscale, dtype=_np.float64)
    n = pixels.shape[0]
    indices = _np.arange(n)
    basis = _np.cos((_np.pi / n) * (indices[None, :] + 0.5) * indices[:, None])
    dct = basis @ pixels @ basis.T
    low = dct[:8, :8].copy()
    low[0, 0] = 0.0
    median = float(_np.median(low))
    bits = "".join("1" if value > median else "0" for value in low.flatten())
    return format(int(bits, 2), "016x")


# panel 变体：覆盖常见多 panel 版式的确定性裁剪（无视觉模型参与）
_PANEL_CROPS: dict[str, tuple[float, float, float, float]] = {
    "whole": (0.0, 0.0, 1.0, 1.0),
    "q1": (0.0, 0.0, 0.5, 0.5),
    "q2": (0.5, 0.0, 1.0, 0.5),
    "q3": (0.0, 0.5, 0.5, 1.0),
    "q4": (0.5, 0.5, 1.0, 1.0),
    "left_half": (0.0, 0.0, 0.5, 1.0),
    "right_half": (0.5, 0.0, 1.0, 1.0),
    "top_half": (0.0, 0.0, 1.0, 0.5),
    "bottom_half": (0.0, 0.5, 1.0, 1.0),
}


def compute_panel_phashes(image_bytes: bytes) -> dict[str, str]:
    """整图 + panel 裁剪变体的 pHash 集合（用户只上传 (c) 子图时仍可命中父图）。

    纯确定性裁剪，不猜测 panel 边界；空指纹（解码失败/依赖缺失）时仅返回
    可计算的部分，绝不编造。
    """
    if not _HAS_IMAGE_STACK or not image_bytes:
        return {}
    try:
        with _PILImage.open(io.BytesIO(image_bytes)) as image:
            width, height = image.size
            result: dict[str, str] = {}
            for panel_key, (x0, y0, x1, y1) in _PANEL_CROPS.items():
                box = (
                    int(x0 * width),
                    int(y0 * height),
                    max(int(x1 * width), int(x0 * width) + 1),
                    max(int(y1 * height), int(y0 * height) + 1),
                )
                cropped = image.crop(box)
                value = compute_phash_from_image(cropped)
                if value:
                    result[panel_key] = value
            return result
    except Exception:  # noqa: BLE001 - 解码失败按无指纹处理，不猜
        return {}


def compute_phash(image_bytes: bytes) -> str | None:
    """计算 64bit 感知哈希（十六进制 16 位）；解码失败或依赖缺失 → None。"""
    if not _HAS_IMAGE_STACK or not image_bytes:
        return None
    try:
        with _PILImage.open(io.BytesIO(image_bytes)) as image:
            return compute_phash_from_image(image)
    except Exception:  # noqa: BLE001 - 解码失败按无指纹处理，不猜
        return None


def image_dimensions(image_bytes: bytes) -> tuple[int, int, str]:
    """(宽, 高, MIME)；解码失败 → (0, 0, '')。"""
    if not _HAS_IMAGE_STACK or not image_bytes:
        return (0, 0, "")
    try:
        with _PILImage.open(io.BytesIO(image_bytes)) as image:
            return (int(image.width), int(image.height), str(image.format or "").lower())
    except Exception:  # noqa: BLE001
        return (0, 0, "")


def phash_hamming_distance(first: str | None, second: str | None) -> int | None:
    """两个 16 位十六进制指纹的汉明距离；任一缺失/非法 → None。"""
    if not first or not second:
        return None
    try:
        return bin(int(first, 16) ^ int(second, 16)).count("1")
    except ValueError:
        return None


QUERY_NORMALIZE_VERSION = "query_normalize_v1"
# 近白像素阈值（截图四周的纸面/网页白边）；裁掉面积 ≥1% 才视为"有边可去"
_BORDER_WHITE_THRESHOLD = 242
_MIN_CROP_AREA_RATIO = 0.01
_QUERY_MAX_SIDE = 1600


def normalize_query_image(image_bytes: bytes) -> tuple[bytes, dict]:
    """截图查询侧归一化（确定性、无模型）：去近白边框 + 限制最长边。

    截图相对库内资产最常见的漂移是四周白边与缩放——两者都会把 64bit pHash 的汉明距离
    推过强匹配阈值（8），也会拉低 ORB 的覆盖率。去边/缩放后再算指纹，命中面显著变大，
    而发布门禁（物理唯一、RANSAC 几何一致）一律不放松。任何失败返回原字节且
    ``changed=False``，绝不阻断定位。
    """
    info: dict = {"version": QUERY_NORMALIZE_VERSION, "changed": False}
    if not _HAS_IMAGE_STACK or not image_bytes:
        return image_bytes, info
    try:
        with _PILImage.open(io.BytesIO(image_bytes)) as source:
            image = source.convert("RGB")
        width, height = image.size
        info["original_size"] = [width, height]
        grayscale = _np.asarray(image.convert("L"), dtype=_np.uint8)
        content_mask = grayscale < _BORDER_WHITE_THRESHOLD
        rows = _np.where(content_mask.any(axis=1))[0]
        cols = _np.where(content_mask.any(axis=0))[0]
        normalized = image
        if rows.size and cols.size:
            pad_x = max(2, int(width * 0.01))
            pad_y = max(2, int(height * 0.01))
            box = (
                max(0, int(cols[0]) - pad_x),
                max(0, int(rows[0]) - pad_y),
                min(width, int(cols[-1]) + 1 + pad_x),
                min(height, int(rows[-1]) + 1 + pad_y),
            )
            crop_area = (box[2] - box[0]) * (box[3] - box[1])
            if 0 < crop_area <= width * height * (1.0 - _MIN_CROP_AREA_RATIO):
                normalized = image.crop(box)
                info["crop_box"] = list(box)
                info["changed"] = True
        longest = max(normalized.size)
        if longest > _QUERY_MAX_SIDE:
            scale = _QUERY_MAX_SIDE / float(longest)
            normalized = normalized.resize(
                (max(1, round(normalized.width * scale)), max(1, round(normalized.height * scale))),
                _PILImage.Resampling.LANCZOS,
            )
            info["changed"] = True
        info["normalized_size"] = list(normalized.size)
        if not info["changed"]:
            return image_bytes, info
        buffer = io.BytesIO()
        normalized.save(buffer, format="PNG")
        return buffer.getvalue(), info
    except Exception:  # noqa: BLE001 - 归一化失败按未归一化处理，不阻断
        return image_bytes, {**info, "changed": False, "error": "normalize_failed"}


QUERY_CROP_VERSION = "query_crop_v1"
_CROP_MIN_SIDE = 100


def query_crop_variants(image_bytes: bytes) -> list[tuple[bytes, str]]:
    """P2：查询截图的裁剪变体集（确定性、无模型），扩大 pHash 命中面。

    用户截取的子图在存储资产中可能是任意位置（panel (k) 的左上角 25%），
    固定四分裁剪覆盖最常见的 panel 排版。返回 [(bytes, variant_label)]，
    含归一化全图 + 中心 50% + 四象限；每个变体独立算 pHash 比对。
    任何解码失败只返回全图归一化，绝不阻断。
    """
    if not _HAS_IMAGE_STACK or not image_bytes:
        return []
    try:
        normalized, _info = normalize_query_image(image_bytes)
        variants: list[tuple[bytes, str]] = [(normalized, "normalized_whole")]
        if _info.get("changed"):
            variants.append((image_bytes, "original_whole"))
        with _PILImage.open(io.BytesIO(normalized)) as source:
            image = source.convert("RGB")
        width, height = image.size
        # 中心 50%（panel 常在中间、白边在外围）
        _append_crop(
            variants, image, (int(width * 0.25), int(height * 0.25), int(width * 0.75), int(height * 0.75)), "center_50"
        )
        # 四象限（2×2 panel 版式最常见的子图位置）
        half_w, half_h = width // 2, height // 2
        for name, box in {
            "q_top_left": (0, 0, half_w, half_h),
            "q_top_right": (half_w, 0, width, half_h),
            "q_bottom_left": (0, half_h, half_w, height),
            "q_bottom_right": (half_w, half_h, width, height),
        }.items():
            _append_crop(variants, image, box, name)
        return variants
    except Exception:  # noqa: BLE001 - 变体生成失败按全图
        try:
            normalized, _info = normalize_query_image(image_bytes)
            return [(normalized, "normalized_whole")]
        except Exception:
            return []


def _append_crop(variants: list[tuple[bytes, str]], image, box: tuple, label: str) -> None:
    """裁剪 → PNG → 加入变体列表；区域太小（<100px）跳过。"""
    try:
        cropped = image.crop(box)
        if cropped.width < _CROP_MIN_SIDE or cropped.height < _CROP_MIN_SIDE:
            return
        buffer = io.BytesIO()
        cropped.save(buffer, format="PNG")
        variants.append((buffer.getvalue(), label))
    except Exception:  # noqa: BLE001
        pass


__all__ = [
    "PHASH_STRONG_DISTANCE",
    "PHASH_VERSION",
    "QUERY_CROP_VERSION",
    "QUERY_NORMALIZE_VERSION",
    "compute_asset_digest",
    "compute_panel_phashes",
    "compute_phash",
    "compute_phash_from_image",
    "image_dimensions",
    "normalize_query_image",
    "phash_hamming_distance",
    "query_crop_variants",
]
