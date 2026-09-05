from __future__ import annotations

import hashlib
import statistics
import unicodedata
from pathlib import Path

import fitz

from yuxi.knowledge.pdf_evidence.contracts import EvidenceAnchor, NativePdfSnapshot

PYMUPDF_PROVIDER_VERSION = str(fitz.VersionBind)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalized_text(value: str) -> str:
    return " ".join(value.split())


def _printable_ratio(value: str) -> float:
    if not value:
        return 0.0
    printable = sum(1 for char in value if char.isspace() or not unicodedata.category(char).startswith("C"))
    return printable / len(value)


def inspect_native_pdf(file_path: str | Path) -> NativePdfSnapshot:
    """Collect deterministic page/word geometry and stable evidence anchors."""
    path = Path(file_path)
    source_bytes = path.read_bytes()
    pdf_sha256 = hashlib.sha256(source_bytes).hexdigest()
    pages: list[dict] = []
    anchors: list[EvidenceAnchor] = []
    page_char_counts: list[int] = []

    with fitz.open(path) as document:
        for page_index, page in enumerate(document):
            words = page.get_text("words", sort=True)
            word_rows = [
                {
                    "bbox": [round(float(item[i]), 2) for i in range(4)],
                    "text": str(item[4]),
                    "block": int(item[5]),
                    "line": int(item[6]),
                    "word": int(item[7]),
                }
                for item in words
            ]
            page_text = _normalized_text(" ".join(row["text"] for row in word_rows))
            page_char_counts.append(len(page_text))
            blocks: list[dict] = []
            for block_index, raw_block in enumerate(page.get_text("blocks", sort=True)):
                quote = _normalized_text(str(raw_block[4]))
                if not quote:
                    continue
                bbox = tuple(round(float(raw_block[i]), 2) for i in range(4))
                matching_word_indexes = [
                    index
                    for index, word in enumerate(word_rows)
                    if word["bbox"][0] < bbox[2]
                    and word["bbox"][2] > bbox[0]
                    and word["bbox"][1] < bbox[3]
                    and word["bbox"][3] > bbox[1]
                ]
                word_start = matching_word_indexes[0] if matching_word_indexes else 0
                word_end = matching_word_indexes[-1] + 1 if matching_word_indexes else 0
                quote_offset = page_text.find(quote)
                if quote_offset >= 0:
                    prefix = page_text[max(0, quote_offset - 80) : quote_offset]
                    quote_end = quote_offset + len(quote)
                    suffix = page_text[quote_end : quote_end + 80]
                else:
                    # PyMuPDF block and word normalization can differ around ligatures.
                    # Geometry still makes the anchor stable; do not derive bogus context
                    # from Python's -1 slice semantics.
                    prefix = ""
                    suffix = ""
                quote_hash = _digest(quote)
                identity = "|".join(
                    [
                        pdf_sha256,
                        str(page_index + 1),
                        ",".join(f"{value:.2f}" for value in bbox),
                        str(word_start),
                        str(word_end),
                        quote_hash,
                        _digest(prefix),
                        _digest(suffix),
                    ]
                )
                anchor = EvidenceAnchor(
                    anchor_id=f"ea_{_digest(identity)[:40]}",
                    page=page_index + 1,
                    bbox=bbox,
                    word_start=word_start,
                    word_end=word_end,
                    quote=quote,
                    quote_hash=quote_hash,
                    prefix_hash=_digest(prefix),
                    suffix_hash=_digest(suffix),
                )
                anchors.append(anchor)
                blocks.append({"block_index": block_index, "anchor_id": anchor.anchor_id, "text": quote})

            pages.append(
                {
                    "page": page_index + 1,
                    "width": round(float(page.rect.width), 2),
                    "height": round(float(page.rect.height), 2),
                    "text": page_text,
                    "words": word_rows,
                    "blocks": blocks,
                }
            )

    native_text = "\n".join(page["text"] for page in pages)
    page_count = len(pages)
    empty_pages = sum(1 for count in page_char_counts if count < 20)
    empty_page_ratio = empty_pages / page_count if page_count else 1.0
    median_chars = statistics.median(page_char_counts) if page_char_counts else 0
    printable_ratio = _printable_ratio(native_text)
    native_text_usable = (
        len(native_text) >= max(200, page_count * 40) and empty_page_ratio <= 0.5 and printable_ratio >= 0.9
    )
    quality = {
        "native_text_usable": native_text_usable,
        "native_text_chars": len(native_text),
        "median_chars_per_page": median_chars,
        "empty_page_ratio": round(empty_page_ratio, 4),
        "printable_ratio": round(printable_ratio, 4),
        "scanned_likelihood": round(1.0 - min(1.0, median_chars / 500), 4),
    }
    return NativePdfSnapshot(
        pdf_sha256=pdf_sha256,
        page_count=page_count,
        pages=pages,
        anchors=anchors,
        quality=quality,
    )
