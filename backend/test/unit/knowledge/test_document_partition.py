from yuxi.knowledge.evidence.document_partition import (
    PARTITION_MAIN_TEXT,
    PARTITION_REFERENCES,
    PARTITION_SUPPORTING_INFO,
    classify_anchor_partitions,
)


def test_partition_transitions_follow_physical_reading_order():
    anchors = [
        {"anchor_id": "main", "page": 2, "word_start": 1, "quote": "Results and discussion"},
        {"anchor_id": "refs", "page": 12, "word_start": 1, "quote": "References"},
        {"anchor_id": "ref-row", "page": 13, "word_start": 2, "quote": "Liu et al. 2024"},
        {"anchor_id": "si", "page": 15, "word_start": 1, "quote": "Supporting Information"},
        {"anchor_id": "si-row", "page": 17, "word_start": 2, "quote": "Figure S1 ..."},
    ]
    classified = classify_anchor_partitions(anchors)
    assert classified[0]["document_partition"] == PARTITION_MAIN_TEXT
    assert classified[1]["document_partition"] == PARTITION_REFERENCES
    assert classified[2]["document_partition"] == PARTITION_REFERENCES
    assert classified[3]["document_partition"] == PARTITION_SUPPORTING_INFO
    assert classified[4]["document_partition"] == PARTITION_SUPPORTING_INFO


def test_partition_classifier_preserves_input_order():
    anchors = [
        {"anchor_id": "later", "page": 3, "word_start": 1, "quote": "Body"},
        {"anchor_id": "earlier", "page": 1, "word_start": 1, "quote": "Title"},
    ]
    classified = classify_anchor_partitions(anchors)
    assert [item["anchor_id"] for item in classified] == ["later", "earlier"]
