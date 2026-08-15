from yuxi.knowledge.graphs.managed_import_parser import parse_managed_graph_import

NODE_HEADER = "node_id,name,node_type,rap_id,msu_id,out_degree,in_degree,publication_count"
RELATION_HEADER = (
    "start_id,end_id,relation_type,direction,directness,best_evidence_level,"
    "support_count,literature_count,pmids,dois,evidence_quotes"
)


def _parse(nodes: list[str], relationships: list[str], *, cypher: str | None = None, resolutions=None):
    return parse_managed_graph_import(
        kb_id="kb_test",
        nodes_bytes=("\n".join([NODE_HEADER, *nodes]) + "\n").encode(),
        relationships_bytes=("\n".join([RELATION_HEADER, *relationships]) + "\n").encode(),
        cypher_bytes=cypher.encode() if cypher else None,
        resolutions=resolutions,
    )


def test_gene_rows_merge_with_status_and_relation_evidence_is_canonicalized():
    result = _parse(
        [
            "gene-1,OsPPDK,RICE_GENE_CANDIDATE,Os01g01010,,1,0,1",
            "gene-1,OsPPDK,RICE_GENE,Os01g01010,,1,0,1",
            "phenotype-1,Opaque endosperm,PHENOTYPE,,,0,1,1",
        ],
        [
            "gene-1,phenotype-1,REGULATES_PHENOTYPE,POSITIVE,DIRECT,E1,1,1,123,10.1/test,quoted evidence",
            "gene-1,phenotype-1,REGULATES_PHENOTYPE,POSITIVE,DIRECT,E1,1,1,123,10.1/test,quoted evidence",
        ],
    )

    assert result["valid"] is True
    assert result["counts"]["canonical_entities"] == 2
    assert result["counts"]["canonical_triples"] == 1
    assert result["counts"]["evidence_assertions"] == 1
    gene = next(item for item in result["plan"]["entities"] if item["label"] == "Gene")
    assert gene["attributes"]["gene_status"] == "confirmed"
    assert len(result["plan"]["triple_sources"]) == 2
    assert len(result["plan"]["evidence_sources"]) == 2


def test_case_variant_without_registry_id_requires_explicit_row_selection():
    initial = _parse(
        [
            "gene-1,OsPPDK,RICE_GENE,,,,,",
            "gene-1,osppdk,RICE_GENE_CANDIDATE,,,,,",
        ],
        [],
    )

    assert initial["status"] == "AWAITING_CONFLICT_RESOLUTION"
    assert initial["conflicts"][0]["code"] == "CASE_VARIANT_CONFLICT"
    conflict_id = initial["conflicts"][0]["conflict_id"]
    resolved = _parse(
        [
            "gene-1,OsPPDK,RICE_GENE,,,,,",
            "gene-1,osppdk,RICE_GENE_CANDIDATE,,,,,",
        ],
        [],
        resolutions={conflict_id: {"selected_row_number": 2}},
    )

    assert resolved["valid"] is True
    assert resolved["plan"]["entities"][0]["name"] == "OsPPDK"


def test_gene_and_allele_mutant_are_not_automatically_merged():
    result = _parse(
        [
            "entity-1,FLO2,RICE_GENE_CANDIDATE,,,,,",
            "entity-1,flo2,ALLELE_MUTANT,,,,,",
        ],
        [],
    )

    assert result["valid"] is False
    assert result["conflicts"][0]["code"] == "SEMANTIC_TYPE_CONFLICT"


def test_cypher_is_reported_but_never_added_to_execution_plan():
    result = _parse(
        ["gene-1,OsPPDK,RICE_GENE,Os01g01010,,,1,1"],
        [],
        cypher="CREATE (n:Entity); MATCH (n) DETACH DELETE n;",
    )

    assert result["cypher"]["provided"] is True
    assert result["cypher"]["execution_allowed"] is False
    assert "CREATE" in result["cypher"]["write_keywords"]
    assert "cypher" not in result["plan"]


def test_dangling_relationship_blocks_import():
    result = _parse(
        ["gene-1,OsPPDK,RICE_GENE,Os01g01010,,,1,1"],
        ["gene-1,missing,REGULATES,UNKNOWN,UNKNOWN,E3,1,1,,,,"],
    )

    assert result["valid"] is False
    assert result["errors"][0]["code"] == "DANGLING_RELATION"
