from yuxi.knowledge.retrieval.neo4j_path_retriever import _seed_keywords


def test_gene_identifier_is_the_primary_path_seed():
    assert _seed_keywords("OsARF12通过什么机制影响 grain size？") == ["OsARF12"]


def test_biological_phrase_is_preserved_without_gene_identifier():
    assert _seed_keywords("高温如何影响淀粉合成？") == ["淀粉合成"]
