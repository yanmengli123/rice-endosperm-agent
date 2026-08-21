from yuxi.knowledge.planning.entity_resolver import _normalized_exact_candidates


def test_rice_endosperm_development_maps_to_canonical_english_entity():
    assert _normalized_exact_candidates("水稻胚乳发育") == [
        "水稻胚乳发育",
        "endosperm development",
    ]


def test_unknown_mention_only_uses_its_normalized_form():
    assert _normalized_exact_candidates("  OsNF-YB1  ") == ["osnf-yb1"]
