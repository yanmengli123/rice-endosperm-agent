"""Versioned, reversible text selection on original PDF glyphs or OCR words."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

import fitz

TEXT_GEOMETRY_VERSION = "pdf-text-geometry.v2"
COORDINATE_FRAME = "pdf_crop_unrotated.v1"
NORMALIZER_VERSION = "unicode-source-map.v3"
_TYPOGRAPHIC = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "‐": "-", "‑": "-", "−": "-"})
_MATH_SYMBOLS = {
    "alpha": "α",
    "beta": "β",
    "gamma": "γ",
    "delta": "δ",
    "mu": "μ",
    "sigma": "σ",
    "dagger": "†",
    "times": "×",
    "pm": "±",
    "Delta": "Δ",
    "geq": "≥",
    "leq": "≤",
    "ge": "≥",
    "le": "≤",
    "circ": "°",
    "prime": "′",
    "cdot": "·",
    "sim": "~",
}


@dataclass(frozen=True)
class NormalizedText:
    text: str
    raw_ranges: tuple[tuple[int, int], ...]


def normalize_with_map(value: str, *, dehyphenate: bool = True) -> NormalizedText:
    """Offsets are Unicode code points; expansions retain the source interval.

    Only line-end alphabetic hyphenation and recognized math markup are
    removed. Numeric minus signs and identifier hyphens are retained.
    """
    tokens: list[tuple[str, int, int]] = []
    math_ranges = [(m.start(), m.end()) for m in re.finditer(r"\$[^$]+\$", value)]
    removed = set()
    if dehyphenate:
        for match in re.finditer(r"(?<=[A-Za-z]{2})-\r?\n[ \t]*(?=[a-z]{2})", value):
            removed.update(range(match.start(), match.end()))
    index = 0
    while index < len(value):
        if index in removed:
            index += 1
            continue
        in_math = any(start <= index < end for start, end in math_ranges)
        command = re.match(r"\\([A-Za-z]+)", value[index:]) if in_math else None
        if command and command.group(1) in _MATH_SYMBOLS:
            end = index + len(command.group(0))
            tokens.append((_MATH_SYMBOLS[command.group(1)], index, end))
            index = end
            continue
        if command and command.group(1) in {"mathrm", "text", "mathbf", "mathit", "left", "right"}:
            index += len(command.group(0))
            continue
        if in_math and value[index] in "${}^_":
            index += 1
            continue
        end = index + 1
        while end < len(value) and unicodedata.combining(value[end]):
            end += 1
        if value[index] == "\\" and end < len(value) and value[end] in "*[]()#_%":
            index += 1
            continue
        normalized = unicodedata.normalize("NFKC", value[index:end]).translate(_TYPOGRAPHIC)
        for char in normalized:
            if char == "\u00ad":
                continue
            if char.isspace() or in_math and char == "~":
                if tokens and tokens[-1][0] == " ":
                    tokens[-1] = (" ", tokens[-1][1], end)
                elif tokens:
                    tokens.append((" ", index, end))
            else:
                tokens.append((char, index, end))
        index = end
    if tokens and tokens[-1][0] == " ":
        tokens.pop()
    return NormalizedText("".join(t[0] for t in tokens), tuple((t[1], t[2]) for t in tokens))


def page_geometry(page: fitz.Page, page_index: int) -> dict[str, Any]:
    bounds = page.rect * page.derotation_matrix
    rotation = page.rotation
    # MuPDF omits CropBox translation in transformation_matrix on rotated
    # pages. Freeze the unrotated transform while retaining the render frame.
    page.set_rotation(0)
    try:
        canonical_to_pdf = list(~page.transformation_matrix)
    finally:
        page.set_rotation(rotation)
    return {
        "page_index": page_index,
        "page": page_index + 1,
        "width": float(bounds.width),
        "height": float(bounds.height),
        "render_width": float(page.rect.width),
        "render_height": float(page.rect.height),
        "cropbox": list(page.cropbox),
        "mediabox": list(page.mediabox),
        "rotation": page.rotation,
        "page_label": page.get_label() or str(page_index + 1),
        "coordinate_frame": COORDINATE_FRAME,
        "canonical_to_pdf": canonical_to_pdf,
        "render_to_canonical": list(page.derotation_matrix),
    }


def collect_page_characters(page: fitz.Page) -> dict[str, Any]:
    characters: list[dict[str, Any]] = []
    text = ""
    for block_index, block in enumerate(page.get_text("rawdict", sort=True)["blocks"]):
        if block.get("type") != 0:
            continue
        for line_index, line in enumerate(block.get("lines", [])):
            line_id = f"{block_index}:{line_index}"
            for span in line.get("spans", []):
                for char in span.get("chars", []):
                    value = str(char["c"])
                    quad = fitz.recover_char_quad(line["dir"], span, char)
                    points = [coordinate for point in (quad.ul, quad.ur, quad.ll, quad.lr) for coordinate in point]
                    characters.append(
                        {
                            "start": len(text),
                            "end": len(text) + len(value),
                            "text": value,
                            "bbox": list(char["bbox"]),
                            "quad": points,
                            "line_id": line_id,
                        }
                    )
                    text += value
            text += "\n"
    return {"text": text, "characters": characters, "version": TEXT_GEOMETRY_VERSION}


def locate_text(
    quote: str,
    page: dict[str, Any],
    bbox: list[float] | tuple[float, ...],
    *,
    source_text: str | None = None,
    source_start: int | None = None,
    source_end: int | None = None,
) -> dict[str, Any]:
    """Select only within the known source region; repeated occurrences reject."""
    selected = []
    for char in page.get("characters", []):
        box = char["bbox"]
        center_x, center_y = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        if bbox[0] - 1 <= center_x <= bbox[2] + 1 and bbox[1] - 1 <= center_y <= bbox[3] + 1:
            selected.append(char)
    raw, ranges, previous_line, previous_ocr = "", [], None, False
    for char in selected:
        if previous_line is not None and previous_line != char["line_id"]:
            raw += "\n"
        elif previous_ocr and char.get("provider") == "ocr":
            raw += " "
        start = len(raw)
        raw += char["text"]
        ranges.append((start, len(raw), char))
        previous_line = char["line_id"]
        previous_ocr = char.get("provider") == "ocr"
    normalized, target = normalize_with_map(raw), normalize_with_map(quote).text
    match_mode = "NORMALIZED_EXACT"
    if target and target not in normalized.text:
        preserved = normalize_with_map(raw, dehyphenate=False)
        if target in preserved.text or target.replace(" ", "") in preserved.text.replace(" ", ""):
            normalized = preserved
            match_mode = "LEXICAL_HYPHEN_EXACT"
    # A second, explicit layout profile tolerates line-wrap / superscript spacing
    # only. Every non-whitespace code point must still match, within this source
    # region. Short values and identifiers never use this profile.
    cjk_count = sum("\u3400" <= char <= "\u9fff" for char in target)
    layout_whitespace_allowed = (len(target.replace(" ", "")) >= 24 and len(target.split()) >= 3) or (
        len(target) >= 8 and cjk_count >= 4
    )
    if target and target not in normalized.text and layout_whitespace_allowed:
        compact = NormalizedText(
            "".join(c for c in normalized.text if c != " "),
            tuple(r for c, r in zip(normalized.text, normalized.raw_ranges, strict=True) if c != " "),
        )
        compact_target = target.replace(" ", "")
        if compact_target in compact.text:
            normalized, target = compact, compact_target
            match_mode += "+LAYOUT_WHITESPACE_EXACT"
    matches = list(re.finditer(re.escape(target), normalized.text)) if target else []
    if target and target[0].isalnum() and target[-1].isalnum():
        matches = [
            m
            for m in matches
            if (m.start() == 0 or not normalized.text[m.start() - 1].isalnum())
            and (m.end() == len(normalized.text) or not normalized.text[m.end()].isalnum())
        ]
    # A repeated sentence inside a uniquely matched source block is selected by
    # its inherited source interval, never by the first document-wide occurrence.
    if source_text is not None and source_start is not None and source_end is not None:
        source = normalize_with_map(source_text)
        if "LAYOUT_WHITESPACE_EXACT" in match_mode:
            source = NormalizedText(
                source.text.replace(" ", ""),
                tuple(r for c, r in zip(source.text, source.raw_ranges, strict=True) if c != " "),
            )
        parents = list(re.finditer(re.escape(source.text), normalized.text)) if source.text else []
        intervals = [i for i, (low, high) in enumerate(source.raw_ranges) if low < source_end and high > source_start]
        if len(parents) == 1 and intervals:
            low, high = intervals[0], intervals[-1] + 1
            if source.text[low:high].strip() == target:
                begin = parents[0].start() + low
                while normalized.text[begin : begin + 1] == " ":
                    begin += 1
                matches = [m for m in matches if m.start() == begin]
    if len(matches) != 1:
        return {"precision": "REGION", "status": "AMBIGUOUS" if matches else "UNMATCHED", "quads": []}
    match = matches[0]
    start = normalized.raw_ranges[match.start()][0]
    end = normalized.raw_ranges[match.end() - 1][1]
    glyphs = [char for low, high, char in ranges if low < end and high > start]
    quads = list({tuple(char["quad"]): char["quad"] for char in glyphs if char.get("quad")}.values())
    if not quads:
        return {"precision": "REGION", "status": "UNMATCHED", "quads": []}
    return {
        "precision": "WORD" if any(c.get("provider") == "ocr" for c in glyphs) else "CHAR",
        "status": "VERIFIED",
        "quads": quads,
        "raw_text": raw[start:end],
        "glyphs": glyphs,
        "normalizer_version": NORMALIZER_VERSION,
        "match_mode": match_mode,
        "offset_basis": "unicode_code_point",
    }


def locate_text_fragments(quote: str, fragments: list[dict], pages: list[dict], **source_interval) -> dict[str, Any]:
    combined = []
    for index, fragment in enumerate(fragments):
        page = pages[int(fragment["page_index"])]
        box = fragment["bbox"]
        for char in page.get("characters", []):
            b = char["bbox"]
            if box[0] - 1 <= (b[0] + b[2]) / 2 <= box[2] + 1 and box[1] - 1 <= (b[1] + b[3]) / 2 <= box[3] + 1:
                combined.append({**char, "fragment_index": index, "line_id": f"{index}:{char['line_id']}"})
    located = locate_text(quote, {"characters": combined}, (0, 0, 1e9, 1e9), **source_interval)
    glyphs = located.pop("glyphs", [])
    result = []
    for index, fragment in enumerate(fragments):
        quads = list({tuple(c["quad"]): c["quad"] for c in glyphs if c["fragment_index"] == index}.values())
        if quads or not glyphs:
            result.append({**fragment, "quads": quads, "precision": located["precision"]})
    return {**located, "fragments": result}
