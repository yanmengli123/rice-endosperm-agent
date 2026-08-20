from yuxi.knowledge.graphs.managed_import_parser import parse_managed_graph_import
from yuxi.knowledge.graphs.managed_import_service import _aggregate_evidence

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
            "gene-1,phenotype-1,REGULATES_PHENOTYPE,POSITIVE,DIRECT,E1,1,1,12345678,10.1007/test,quoted evidence",
            "gene-1,phenotype-1,REGULATES_PHENOTYPE,POSITIVE,DIRECT,E1,1,1,12345678,10.1007/test,quoted evidence",
        ],
    )

    assert result["valid"] is True
    assert result["counts"]["canonical_entities"] == 2
    assert result["counts"]["canonical_triples"] == 1
    assert result["counts"]["evidence_assertions"] == 1
    assert result["plan"]["triples"][0]["literature_count"] == 1
    gene = next(item for item in result["plan"]["entities"] if item["label"] == "Gene")
    assert gene["attributes"]["gene_status"] == "confirmed"
    assert len(result["plan"]["triple_sources"]) == 2
    assert len(result["plan"]["evidence_sources"]) == 2


def test_case_variant_without_registry_id_is_nonblocking_and_preserves_aliases():
    result = _parse(
        [
            "gene-1,OsPPDK,RICE_GENE,,,,,",
            "gene-1,osppdk,RICE_GENE_CANDIDATE,,,,,",
        ],
        [],
    )

    assert result["status"] == "READY"
    assert result["blockers"] == []
    assert result["warnings"][0]["code"] == "CASE_UNRESOLVED"
    entity = result["plan"]["entities"][0]
    assert entity["name"] == "OsPPDK"
    assert entity["attributes"]["aliases"] == ["OsPPDK", "osppdk"]
    assert entity["attributes"]["normalization_status"] == "CASE_UNRESOLVED"


def test_gene_and_allele_mutant_are_split_linked_and_relations_are_routed_by_semantics():
    result = _parse(
        [
            "entity-1,FLO2,RICE_GENE_CANDIDATE,,,,,",
            "entity-1,flo2,ALLELE_MUTANT,,,,,",
            "phenotype-1,floury endosperm,PHENOTYPE,,,,,",
        ],
        [
            "entity-1,phenotype-1,MUTANT_EFFECT,POSITIVE,DIRECT,E1,1,1,12345671,,mutant quote",
            "entity-1,phenotype-1,PROMOTES_PHENOTYPE,POSITIVE,DIRECT,E2,1,1,12345672,,gene quote",
        ],
    )

    assert result["valid"] is True
    assert result["conflicts"] == []
    assert result["counts"]["semantic_splits"] == 1
    assert result["semantic_splits"][0]["code"] == "GENE_ALLELE_IDENTITY_SPLIT"
    effective = result["effective_resolutions"][result["semantic_splits"][0]["split_id"]]
    assert effective["action"] == "split"
    assert effective["relation_routes"]["2"]["start"] == "allele"
    assert effective["relation_routes"]["3"]["start"] == "gene"
    entities = {item["label"]: item for item in result["plan"]["entities"]}
    triples = result["plan"]["triples"]
    assert {"Gene", "AlleleMutant", "Phenotype"} == set(entities)
    assert (
        next(item for item in triples if item["relation_type"] == "MUTANT_EFFECT")["source_entity_id"]
        == entities["AlleleMutant"]["entity_id"]
    )
    assert (
        next(item for item in triples if item["relation_type"] == "PROMOTES_PHENOTYPE")["source_entity_id"]
        == entities["Gene"]["entity_id"]
    )
    allele_of = next(item for item in triples if item["relation_type"] == "ALLELE_OF")
    assert allele_of["source_entity_id"] == entities["AlleleMutant"]["entity_id"]
    assert allele_of["target_entity_id"] == entities["Gene"]["entity_id"]


def test_semantic_route_can_be_overridden_per_relation_endpoint():
    initial = _parse(
        [
            "entity-1,FLO2,RICE_GENE_CANDIDATE,,,,,",
            "entity-1,flo2,ALLELE_MUTANT,,,,,",
            "process-1,starch synthesis,PROCESS,,,,,",
        ],
        ["entity-1,process-1,REQUIRED_FOR,POSITIVE,DIRECT,E1,1,1,12345671,,quote"],
    )
    split_id = initial["semantic_splits"][0]["split_id"]
    overridden = _parse(
        [
            "entity-1,FLO2,RICE_GENE_CANDIDATE,,,,,",
            "entity-1,flo2,ALLELE_MUTANT,,,,,",
            "process-1,starch synthesis,PROCESS,,,,,",
        ],
        ["entity-1,process-1,REQUIRED_FOR,POSITIVE,DIRECT,E1,1,1,12345671,,quote"],
        resolutions={split_id: {"relation_routes": {"2": {"start": "allele"}}}},
    )
    allele = next(item for item in overridden["plan"]["entities"] if item["label"] == "AlleleMutant")
    required_for = next(item for item in overridden["plan"]["triples"] if item["relation_type"] == "REQUIRED_FOR")
    assert required_for["source_entity_id"] == allele["entity_id"]


def test_multiple_quotes_for_one_literature_are_merged_without_losing_claim_eligibility():
    result = _parse(
        [
            "gene-1,OsPPDK,RICE_GENE,Os01g01010,,,,",
            "process-1,starch synthesis,PROCESS,,,,,",
        ],
        [
            "gene-1,process-1,REQUIRED_FOR,POSITIVE,DIRECT,E1,1,1,12345671,10.1007/a,"
            "quote one||quote two||quote three"
        ],
    )

    assert result["valid"] is True
    assert result["blockers"] == []
    assert result["counts"]["evidence_assertions"] == 1
    evidence = result["plan"]["evidence"][0]
    assert evidence["evidence_quote"] == "quote one\n\nquote two\n\nquote three"
    assert evidence["evidence_alignment_status"] == "ALIGNED"
    assert evidence["claim_eligible"] is True


def test_ambiguous_multi_literature_evidence_is_preserved_as_nonclaimable_row_bundle():
    result = _parse(
        [
            "gene-1,OsPPDK,RICE_GENE,Os01g01010,,,,",
            "process-1,starch synthesis,PROCESS,,,,,",
        ],
        [
            "gene-1,process-1,REQUIRED_FOR,POSITIVE,DIRECT,E1,2,2,12345671,"
            "10.1007/a|10.1007/b,quote one||quote two"
        ],
    )

    assert result["valid"] is True
    assert result["blockers"] == []
    warning = next(item for item in result["warnings"] if item["code"] == "EVIDENCE_ALIGNMENT_ROW_LEVEL")
    assert warning["row_number"] == 2
    assert result["counts"]["evidence_assertions"] == 1
    evidence = result["plan"]["evidence"][0]
    assert evidence["literature_id"] is None
    assert evidence["pmid"] is None
    assert evidence["doi"] is None
    assert evidence["evidence_alignment_status"] == "ROW_LEVEL"
    assert evidence["claim_eligible"] is False
    assert evidence["metadata_json"]["pmids"] == ["12345671"]
    assert evidence["metadata_json"]["dois"] == ["10.1007/a", "10.1007/b"]
    assert evidence["metadata_json"]["quotes"] == ["quote one", "quote two"]


def test_aligned_multiple_literature_records_are_split_one_to_one():
    result = _parse(
        [
            "gene-1,OsPPDK,RICE_GENE,Os01g01010,,,,",
            "phenotype-1,grain yield,PHENOTYPE,,,,,",
        ],
        [
            "gene-1,phenotype-1,REGULATES_PHENOTYPE,POSITIVE,DIRECT,E2,2,2,"
            "12345671|12345672,10.1007/a|10.1007/b,quote one||quote two"
        ],
    )

    assert result["valid"] is True
    assert result["counts"]["evidence_assertions"] == 2
    assert {item["pmid"] for item in result["plan"]["evidence"]} == {"12345671", "12345672"}
    assert all(item["claim_eligible"] for item in result["plan"]["evidence"])
    assert all(item["outcome_class"] == "DIRECT_YIELD" for item in result["plan"]["evidence"])


def test_scientific_notation_identifier_blocks_import_without_rounding():
    result = _parse(
        [
            "gene-1,OsPPDK,RICE_GENE,Os01g01010,,,,",
            "phenotype-1,grain yield,PHENOTYPE,,,,,",
        ],
        ["gene-1,phenotype-1,REGULATES_PHENOTYPE,POSITIVE,DIRECT,E2,1,1,3.95521e+07,,quote"],
    )

    assert result["valid"] is False
    assert any(item["code"] == "INVALID_SCIENTIFIC_NOTATION" for item in result["blockers"])


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


def test_projection_aggregate_excludes_ambiguous_evidence_from_exact_literature_count():
    aggregate = _aggregate_evidence(
        [
            {
                "evidence_id": "aligned",
                "triple_id": "triple-1",
                "literature_id": "pmid:1",
                "direction": "POSITIVE",
                "evidence_level": "E2",
                "evidence_alignment_status": "ALIGNED",
                "metadata_json": {"pmids": ["1"], "dois": []},
            },
            {
                "evidence_id": "ambiguous",
                "triple_id": "triple-1",
                "literature_id": "pmid:2",
                "direction": "POSITIVE",
                "evidence_level": "E1",
                "evidence_alignment_status": "AMBIGUOUS",
                "metadata_json": {"pmids": ["2", "3"], "dois": ["10.1/x"]},
            },
        ]
    )["triple-1"]

    assert aggregate["support_count"] == 2
    assert aggregate["literature_count"] == 1
    assert aggregate["best_evidence_level"] == "E1"
    assert aggregate["ambiguous_evidence_count"] == 1
