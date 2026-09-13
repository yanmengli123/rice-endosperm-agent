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


__all__ = [
    "PHASH_STRONG_DISTANCE",
    "PHASH_VERSION",
    "compute_asset_digest",
    "compute_panel_phashes",
    "compute_phash",
    "compute_phash_from_image",
    "image_dimensions",
    "phash_hamming_distance",
]
