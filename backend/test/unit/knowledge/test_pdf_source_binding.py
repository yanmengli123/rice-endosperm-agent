from types import SimpleNamespace

from yuxi.knowledge.evidence.sentence_splitter import split_evidence_units


def test_sentences_inherit_source_offsets_without_paragraph_duplicate():
    text = "Gene A increases. Gene B decreases."
    units = split_evidence_units(anchors=[SimpleNamespace(anchor_id="a", quote=text)], markdown_body=text)
    assert len(units) == 2
    assert all(u.anchor_id == "a" for u in units)
    assert [(u.source_start, u.source_end) for u in units] == [(0, 17), (18, 35)]
    assert all(text[u.source_start : u.source_end] == u.quote for u in units)


def test_identical_text_keeps_each_physical_occurrence():
    text = "Gene A increases."
    anchors = [SimpleNamespace(anchor_id=f"a{i}", quote=text) for i in range(2)]
    units = split_evidence_units(anchors=anchors, markdown_body=text)
    assert [u.anchor_id for u in units] == ["a0", "a1"]


def test_repeated_sentence_within_block_keeps_distinct_offsets():
    text = "Gene A increases. Gene A increases."
    units = split_evidence_units(anchors=[SimpleNamespace(anchor_id="a", quote=text)], markdown_body=text)
    assert [u.source_start for u in units] == [0, 18]


def test_headers_do_not_inflate_sentence_coverage():
    anchor = SimpleNamespace(anchor_id="header", quote="Journal 2024", anchor_type="header")
    assert split_evidence_units(anchors=[anchor], markdown_body="") == []


def test_unicode_offsets_and_footer_caption_are_retained():
    text = "  稻米🌾 starch increases."
    units = split_evidence_units(anchors=[SimpleNamespace(anchor_id="a", quote=text)], markdown_body="")
    assert units[0].source_start == 2
    assert text[units[0].source_start : units[0].source_end] == units[0].quote
    caption = SimpleNamespace(
        anchor_id="c", quote="Figure 1 Rice starch increases significantly.", anchor_type="footer"
    )
    assert split_evidence_units(anchors=[caption], markdown_body="")[0].evidence_type == "caption"


def test_panel_labels_are_caption_occurrences_and_do_not_hide_following_figure_caption():
    quote = "(a) (b) Figure 1 Rice starch synthesis increases."
    units = split_evidence_units(
        anchors=[SimpleNamespace(anchor_id="a", quote=quote, anchor_type="image")], markdown_body=""
    )
    assert [u.evidence_type for u in units] == ["caption", "caption"]
    assert units[1].container_label == "Figure 1"
    assert all(quote[u.source_start : u.source_end] == u.quote for u in units)
