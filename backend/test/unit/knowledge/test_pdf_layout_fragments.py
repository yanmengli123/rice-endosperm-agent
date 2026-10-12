"""Producer-merged text/captions must retain every original physical region."""

import json

from yuxi.knowledge.pdf_evidence.contracts import ParserArtifact
from yuxi.knowledge.pdf_evidence.mineru_layout import extract_mineru_blocks, build_mineru_anchors


def _artifact(kind, payload):
    return ParserArtifact(kind, kind + ".json", json.dumps(payload).encode(), "application/json")


def test_cross_page_text_requires_unique_original_text_and_rectangle():
    first = {
        "bbox": [10, 80, 90, 95],
        "type": "text",
        "lines": [{"bbox": [10, 80, 90, 95], "spans": [{"type": "text", "content": "Rice starch"}]}],
    }
    second = {
        "bbox": [10, 10, 90, 25],
        "type": "text",
        "lines": [{"bbox": [10, 10, 90, 25], "spans": [{"type": "text", "content": "synthesis increases."}]}],
    }
    merged = {
        **first,
        "lines": [
            *first["lines"],
            {
                "bbox": second["bbox"],
                "spans": [{"type": "text", "content": "synthesis increases.", "cross_page": True}],
            },
        ],
    }
    pages = [
        {"page_size": [100, 100], "preproc_blocks": [first], "para_blocks": [merged]},
        {"page_size": [100, 100], "preproc_blocks": [second], "para_blocks": []},
    ]
    geometry = [{"page_index": i, "width": 100, "height": 100} for i in range(3)]
    flat = [{"type": "text", "text": "Rice starch synthesis increases.", "page_idx": 0, "bbox": [100, 800, 900, 950]}]
    artifacts = [_artifact("mineru_content_list", flat), _artifact("mineru_layout", {"pdf_info": pages})]
    blocks = extract_mineru_blocks(artifacts, geometry)
    anchor = build_mineru_anchors("a" * 64, artifacts, blocks=blocks)[0]
    assert [f.page_index for f in anchor.fragments] == [0, 1]
    assert anchor.fragments[1].bbox == (10, 10, 90, 25)
    assert len(build_mineru_anchors("a" * 64, artifacts, blocks=blocks)) == 1
    pages.append({"page_size": [100, 100], "preproc_blocks": [second], "para_blocks": []})
    artifacts[1] = _artifact("mineru_layout", {"pdf_info": pages})
    ambiguous = build_mineru_anchors("a" * 64, artifacts, page_geometry=geometry)[0]
    assert len(ambiguous.fragments) == 1  # Never guess a page from identical text.


def test_caption_outside_image_is_a_separate_source_region():
    caption = {
        "type": "image_caption",
        "bbox": [10, 60, 90, 70],
        "lines": [{"spans": [{"type": "text", "content": "Figure 1 Rice starch."}]}],
    }
    image = {"type": "image", "bbox": [10, 10, 90, 50], "blocks": [caption]}
    geometry = [{"page_index": 0, "width": 100, "height": 100}]
    artifacts = [
        _artifact(
            "mineru_content_list",
            [
                {
                    "type": "image",
                    "page_idx": 0,
                    "bbox": [100, 100, 900, 500],
                    "image_caption": ["Figure 1 Rice starch."],
                    "img_path": "image.png",
                }
            ],
        ),
        _artifact(
            "mineru_layout", {"pdf_info": [{"page_size": [100, 100], "para_blocks": [image], "preproc_blocks": []}]}
        ),
    ]
    blocks = extract_mineru_blocks(artifacts, geometry)
    anchors = build_mineru_anchors("a" * 64, artifacts, blocks=blocks)
    assert len(anchors) == 1
    assert anchors[0].fragments[1].bbox == (10, 60, 90, 70)
    assert blocks[1]["block_type"] == "caption" and blocks[1]["source_region_only"]
