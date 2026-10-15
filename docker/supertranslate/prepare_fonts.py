# SPDX-License-Identifier: AGPL-3.0-or-later
"""Prepare pinned CJK faces at build time; glyph outlines are never modified."""

import hashlib
import json

import fitz
from fontTools.ttLib import TTCollection
from pdf_zh_translator.pdf_layout import (
    TTC_FACE_SOURCES,
    _normalize_cjk_vertical_metrics,
    _sanitize_noto_cjk_unicode_cmap,
)


def prepare():
    output = {}
    for destination, candidates in TTC_FACE_SOURCES:
        path, name = next((path, name) for path, name in candidates if " CJK SC" in name)
        collection = TTCollection(path, lazy=True)
        try:
            face = next(face for face in collection.fonts if (face["name"].getDebugName(4) or "").startswith(name))
            # Only cmap and vertical metrics change. Recalculating every CFF glyph
            # bounding box is unnecessary and makes a cold first task take minutes.
            face.recalcBBoxes = False
            face.recalcTimestamp = False
            _sanitize_noto_cjk_unicode_cmap(face)
            _normalize_cjk_vertical_metrics(face, destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            face.save(destination)
            assert fitz.Font(fontfile=str(destination)).has_glyph(ord("稻"))
            destination.chmod(0o444)
            output[destination.name] = hashlib.sha256(destination.read_bytes()).hexdigest()
        finally:
            collection.close()
    print(json.dumps(output, sort_keys=True))


if __name__ == "__main__":
    prepare()
