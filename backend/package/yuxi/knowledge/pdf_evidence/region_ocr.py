"""Local OCR of original page crops with explicit pixel-to-page geometry."""

from __future__ import annotations

from functools import lru_cache
import hashlib
import os
from pathlib import Path
from importlib.metadata import version
from threading import Lock

import fitz
import numpy as np

OCR_VERSION = "pdf-region-ocr.v4"
_INFERENCE_LOCK = Lock()

# SHA256 values from RapidOCR 3.8.1 default_models.yaml (official v3.8.0 assets).
_MODEL_BASE = "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.8.0/onnx"
OCR_MODELS = {
    "ch_PP-OCRv5_det_mobile.onnx": {
        "sha256": "4d97c44a20d30a81aad087d6a396b08f786c4635742afc391f6621f5c6ae78ae",
        "url": f"{_MODEL_BASE}/PP-OCRv5/det/ch_PP-OCRv5_det_mobile.onnx",
    },
    "ch_ppocr_mobile_v2.0_cls_mobile.onnx": {
        "sha256": "e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c",
        "url": f"{_MODEL_BASE}/PP-OCRv4/cls/ch_ppocr_mobile_v2.0_cls_mobile.onnx",
    },
    "ch_PP-OCRv5_rec_mobile.onnx": {
        "sha256": "5825fc7ebf84ae7a412be049820b4d86d77620f204a041697b0494669b1742c5",
        "url": f"{_MODEL_BASE}/PP-OCRv5/rec/ch_PP-OCRv5_rec_mobile.onnx",
    },
}


def model_directory() -> Path:
    import rapidocr

    return Path(os.environ.get("PDF_OCR_MODEL_DIR") or Path(rapidocr.__file__).parent / "models")


@lru_cache(maxsize=16)
def _weight_hash(path: str, size: int, modified: int) -> str:
    with open(path, "rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def ocr_manifest() -> dict:
    root = model_directory()
    weights = {}
    for name in OCR_MODELS:
        path = root / name
        stat = path.stat() if path.is_file() else None
        weights[name] = _weight_hash(str(path), stat.st_size, stat.st_mtime_ns) if stat else "UNAVAILABLE"
    return {
        "provider": "rapidocr",
        "provider_version": version("rapidocr"),
        "runtime_version": version("onnxruntime"),
        "weights": weights,
        "ready": all(weights[name] == metadata["sha256"] for name, metadata in OCR_MODELS.items()),
        "model": "PP-OCRv5-mobile",
        "det_box_thresh": 0.3,
    }


@lru_cache(maxsize=1)
def _engine():
    from rapidocr import RapidOCR
    from yuxi.knowledge.parser.rapid_ocr import RapidOCRParser

    parser = RapidOCRParser()
    if not ocr_manifest()["ready"]:
        raise RuntimeError("PDF_OCR_MODEL_NOT_PROVISIONED_OR_CHECKSUM_MISMATCH")
    params = parser._get_model_params()
    for kind, name in zip(("Det", "Cls", "Rec"), OCR_MODELS, strict=True):
        params[f"{kind}.model_path"] = str(model_directory() / name)
    return RapidOCR(params=params)


def recognize_region(
    page: fitz.Page, bbox, *, dpi: int = 144, min_confidence: float = 0.9, max_pixels: int = 20_000_000
) -> dict:
    page.set_rotation(0)
    if not 72 <= dpi <= 600 or not 0 <= min_confidence <= 1:
        raise ValueError("PDF_OCR_OPTIONS_INVALID")
    scale = dpi / 72
    if (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) * scale**2 > max_pixels:
        return {"status": "BUDGET_EXCEEDED", "words": [], "version": OCR_VERSION}
    original = fitz.Rect(bbox) & page.rect
    clip = (original + (-8, -8, 8, 8)) & page.rect
    if clip.is_empty or clip.is_infinite:
        raise ValueError("PDF_OCR_GEOMETRY_INVALID")
    if min(original.width, original.height) * scale < 64:
        # Detection needs context around small panel labels. Retain the
        # original selector, and accept only word centers inside that selector.
        scale = max(scale, 4)
    # Context padding and adaptive DPI are part of the real allocation budget.
    width = int(np.ceil(clip.x1 * scale)) - int(np.floor(clip.x0 * scale))
    height = int(np.ceil(clip.y1 * scale)) - int(np.floor(clip.y0 * scale))
    if width * height > max_pixels:
        return {"status": "BUDGET_EXCEEDED", "words": [], "version": OCR_VERSION}
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip, colorspace=fitz.csRGB, alpha=False)
    image = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
    with _INFERENCE_LOCK:
        result = _engine()(image, return_word_box=True)
    words = []
    for line_index, group in enumerate(result.word_results or ()):
        if not isinstance(group, (list, tuple)):
            continue
        for item in group:
            if not isinstance(item, (list, tuple)) or len(item) != 3:
                continue
            text, confidence, box = item
            if box is None or float(confidence) < min_confidence:
                continue
            points = [[(float(p[0]) + pix.x) / scale, (float(p[1]) + pix.y) / scale] for p in box]
            if len(points) != 4:
                continue
            center = [sum(p[axis] for p in points) / 4 for axis in (0, 1)]
            if not (bbox[0] <= center[0] <= bbox[2] and bbox[1] <= center[1] <= bbox[3]):
                continue
            quad = [v for p in (points[0], points[1], points[3], points[2]) for v in p]
            words.append(
                {
                    "text": str(text),
                    "confidence": float(confidence),
                    "quad": quad,
                    "bbox": [
                        min(p[0] for p in points),
                        min(p[1] for p in points),
                        max(p[0] for p in points),
                        max(p[1] for p in points),
                    ],
                    "line_id": str(line_index),
                    "provider": "ocr",
                }
            )
    return {
        "status": "READY",
        "words": words,
        "version": OCR_VERSION,
        "provider": "rapidocr",
        "provider_version": version("rapidocr"),
        "model_manifest": ocr_manifest(),
        "dpi": dpi,
        "effective_dpi": scale * 72,
        "min_confidence": min_confidence,
        "pixel_to_page": [1 / scale, 0, 0, 1 / scale, pix.x / scale, pix.y / scale],
    }
