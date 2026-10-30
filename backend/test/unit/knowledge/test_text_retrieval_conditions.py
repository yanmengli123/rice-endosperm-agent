import json

import pytest

from yuxi.knowledge.text_retrieval import (
    TextRetrievalError,
    literal_highlights,
    require_phrase_version,
    text_conditions,
)


def test_text_filters_escape_values_and_keep_all_conditions():
    phrase = 'grain "filling"'
    expression, highlights = text_conditions(
        {"required_terms": 'Wx x")', "excluded_terms": "review", "exact_phrase": phrase}
    )
    assert 'TEXT_MATCH(content, "Wx")' in expression
    assert 'TEXT_MATCH(content, "x\\")")' in expression
    assert 'not TEXT_MATCH(content, "review")' in expression
    assert f"PHRASE_MATCH(content, {json.dumps(phrase)}, 0)" in expression
    assert highlights == ["Wx", 'x")', 'grain "filling"']


@pytest.mark.parametrize(
    "options",
    [
        {"required_terms": None},
        {"excluded_terms": []},
        {"exact_phrase": "x" * 2049},
        {"required_terms": " ".join(["x"] * 33)},
    ],
)
def test_invalid_filter_options_are_explicit_errors(options):
    with pytest.raises(TextRetrievalError):
        text_conditions(options)


def test_phrase_version_is_explicit_and_never_silently_ignored():
    for version in ("2.5.6", "v2.5.16", "unknown"):
        with pytest.raises(TextRetrievalError):
            require_phrase_version(version)
    for version in ("v2.5.17", "2.6.0", "3.0.2"):
        require_phrase_version(version)


def test_highlights_preserve_original_text_and_never_create_html():
    content = "<script>alert(1)</script> OsbZIP58 and grain filling"
    fragments = literal_highlights(content, ["OsbZIP58", "grain filling"])
    assert "".join(part["text"] for part in fragments[0]) == content
    assert [part["text"] for part in fragments[0] if part["matched"]] == ["OsbZIP58", "grain filling"]
    assert literal_highlights("nothing", ["Wx"]) == []
