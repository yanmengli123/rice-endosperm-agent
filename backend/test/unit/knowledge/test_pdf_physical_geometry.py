import fitz
import pytest

from yuxi.knowledge.pdf_evidence.geometry import mineru_bbox_to_pdf_points
from yuxi.knowledge.pdf_evidence.text_geometry import (
    collect_page_characters,
    locate_text,
    locate_text_fragments,
    normalize_with_map,
    page_geometry,
)
from yuxi.services.pdf_evidence_service import validate_physical_fragment


@pytest.mark.parametrize(
    "native,quote",
    [
        ("The world's rice starch-synthesis increases.", "The world’s rice starch-synthesis increases."),
        ("The rice starch-\nsynthesis increases significantly.", "The rice starch-synthesis increases significantly."),
        (
            "The rice starch synthesis increases 23\npercent significantly.",
            "The rice starch synthesis increases 23percent significantly.",
        ),
    ],
)
def test_layout_profiles_keep_all_non_whitespace_characters(native, quote):
    # Synthetic glyph positions exercise mapping, not fuzzy text similarity.
    chars = [
        {"text": c, "bbox": [i, 1, i + 1, 2], "quad": [i, 1, i + 1, 1, i, 2, i + 1, 2], "line_id": str(i)}
        for i, c in enumerate(native)
    ]
    located = locate_text(quote, {"characters": chars}, [0, 0, 1000, 10])
    assert located["status"] == "VERIFIED"
    assert located["precision"] == "CHAR"
    assert (
        locate_text(
            quote.replace("23", "24").replace("increases", "decreases"), {"characters": chars}, [0, 0, 1000, 10]
        )["status"]
        == "UNMATCHED"
    )


def test_normalization_retains_reversible_unicode_intervals_and_numeric_minus():
    raw = "ﬁ e\u0301   amy-\nlose -12 OsMYB-73 $\\alpha$"
    mapped = normalize_with_map(raw)
    assert mapped.text == "fi é amylose -12 OsMYB-73 α"
    assert mapped.raw_ranges[:2] == ((0, 1), (0, 1))
    assert raw[slice(*mapped.raw_ranges[3])] == "e\u0301"
    assert raw[slice(*mapped.raw_ranges[-1])] == "\\alpha"


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_cropped_rotated_page_maps_back_to_original_pdf_coordinates(rotation):
    with fitz.open() as document:
        page = document.new_page(width=600, height=800)
        page.insert_text((100, 200), "Original rice evidence.")
        page.set_cropbox(fitz.Rect(50, 100, 550, 700))
        page.set_rotation(rotation)
        frame = page_geometry(page, 0)
        assert frame["width"] == 500 and frame["height"] == 600
        original_point = fitz.Point(50, 100) * fitz.Matrix(frame["canonical_to_pdf"])
        assert original_point.x == pytest.approx(100)
        assert original_point.y == pytest.approx(600)
        raw = collect_page_characters(page)
        located = locate_text("Original rice evidence.", raw, [0, 0, 500, 600])
        assert located["precision"] == "CHAR" and located["status"] == "VERIFIED"
        rectangle = fitz.Rect(40, 60, 200, 160)
        rendered = rectangle * page.rotation_matrix
        source_box = [
            rendered.x0 / frame["render_width"] * 1000,
            rendered.y0 / frame["render_height"] * 1000,
            rendered.x1 / frame["render_width"] * 1000,
            rendered.y1 / frame["render_height"] * 1000,
        ]
        assert mineru_bbox_to_pdf_points(source_box, frame) == pytest.approx(list(rectangle), abs=0.001)


def test_repeated_sentence_uses_inherited_source_offset():
    quote = "Rice grows. Rice grows."
    with fitz.open() as document:
        page = document.new_page()
        page.insert_text((40, 70), quote)
        chars = collect_page_characters(page)
        assert locate_text("Rice grows.", chars, [0, 0, 500, 200])["status"] == "AMBIGUOUS"
        second = locate_text("Rice grows.", chars, [0, 0, 500, 200], source_text=quote, source_start=12, source_end=23)
        assert second["status"] == "VERIFIED"
        assert second["quads"][0][0] > chars["characters"][0]["bbox"][0] + 30


def test_cross_page_sentence_keeps_both_physical_fragments():
    with fitz.open() as document:
        first = document.new_page()
        first.insert_text((40, 70), "Rice starch")
        second = document.new_page()
        second.insert_text((40, 70), "synthesis increases.")
        pages = [collect_page_characters(p) for p in document]
        fragments = [{"page_index": i, "bbox": [0, 0, 500, 200], "region_id": f"r{i}"} for i in range(2)]
        result = locate_text_fragments("Rice starch synthesis increases.", fragments, pages)
        assert result["precision"] == "CHAR"
        assert [f["region_id"] for f in result["fragments"]] == ["r0", "r1"]
        assert all(f["quads"] for f in result["fragments"])


def test_number_substring_is_not_a_cell_value_match():
    with fitz.open() as document:
        page = document.new_page()
        page.insert_text((40, 70), "Sample 123")
        assert locate_text("23", collect_page_characters(page), [0, 0, 500, 200])["status"] == "UNMATCHED"


def test_invalid_or_noninvertible_geometry_fails_closed():
    fragment = {
        "page_index": 0,
        "bbox": [0, 0, 10, 10],
        "coordinate_frame": "pdf_crop_unrotated.v1",
        "canonical_to_pdf": [0] * 6,
    }
    assert not validate_physical_fragment(fragment)
    fragment["canonical_to_pdf"] = [1, 0, 0, -1, 0, 800]
    assert validate_physical_fragment(fragment)
    fragment["quads"] = [[float("nan")] * 8]
    assert not validate_physical_fragment(fragment)


def test_cjk_ocr_spacing_retains_exact_characters_and_rejects_transcription_errors():
    quote = "水稻胚乳发育受到转录因子调控。"
    with fitz.open() as document:
        page = document.new_page()
        page.insert_text((40, 70), quote, fontname="china-s", fontsize=14)
        chars = collect_page_characters(page)
        for char in chars["characters"]:
            char["provider"] = "ocr"
        located = locate_text(quote, chars, [0, 0, 500, 200])
        assert located["status"] == "VERIFIED" and located["precision"] == "WORD"
        assert len(located["quads"]) == len(quote)
        assert locate_text(quote.replace("水稻", "小麦"), chars, [0, 0, 500, 200])["status"] == "UNMATCHED"
