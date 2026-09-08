"""图谱导出服务单测：往返自证（导出包直接喂回 v3 解析器）、identity 保持、xlsx 结构。

核心断言：同一 kb_id 下，导出的 CSV 重新解析后，plan 中的 entity_id / triple_id /
evidence_id 与源数据完全一致——导入校验器就是导出格式的验收器。
"""

import csv
import io
import json
import zipfile

import pytest

from yuxi.knowledge.graphs.graph_export_service import (
    build_evidence_workbook,
    build_roundtrip_csvs,
    build_roundtrip_package,
    reverse_map_node_type,
)
from yuxi.utils import hashstr
from yuxi.knowledge.graphs.graph_utils import compute_entity_id, compute_triple_id, normalize_entity_name
from yuxi.knowledge.graphs.managed_import_parser import parse_managed_graph_import

KB = "kb_exporttest"

GENE_IDENTITY = "rap:os01g0100100"
PROCESS_IDENTITY = "name:endosperm development"
PHENOTYPE_IDENTITY = "name:chalky endosperm"
ALLELE_NAME = "os01g0100100 crispr"
ALLELE_IDENTITY = f"allele:{GENE_IDENTITY}:{normalize_entity_name(ALLELE_NAME)}"

GENE_ENTITY_ID = compute_entity_id(KB, GENE_IDENTITY, "Gene")
PROCESS_ENTITY_ID = compute_entity_id(KB, PROCESS_IDENTITY, "Process")
PHENOTYPE_ENTITY_ID = compute_entity_id(KB, PHENOTYPE_IDENTITY, "Phenotype")
ALLELE_ENTITY_ID = compute_entity_id(KB, ALLELE_IDENTITY, "AlleleMutant")

T_PROMOTES = compute_triple_id(KB, GENE_IDENTITY, "Gene", "PROMOTES_PROCESS", PROCESS_IDENTITY, "Process")
T_MUTANT = compute_triple_id(KB, ALLELE_IDENTITY, "AlleleMutant", "MUTANT_EFFECT", PHENOTYPE_IDENTITY, "Phenotype")
T_ALLELE_OF = compute_triple_id(KB, ALLELE_IDENTITY, "AlleleMutant", "ALLELE_OF", GENE_IDENTITY, "Gene")


def _expected_evidence_id(triple_id, literature_id, direction, directness, level, alignment, quote):
    identity = "|".join([KB, triple_id, literature_id or "", direction, directness, level, alignment, quote or ""])
    return hashstr(identity, length=32)


ALIGNED_QUOTE = "Os01g0100100 promotes endosperm development."
ROW_LEVEL_QUOTE = "q1\n\nq2"
EXPECTED_EV_ALIGNED = _expected_evidence_id(
    T_PROMOTES, "pmid:12345678", "POSITIVE", "DIRECT", "2", "ALIGNED", ALIGNED_QUOTE
)
EXPECTED_EV_ROW_LEVEL = _expected_evidence_id(T_MUTANT, None, "NEGATIVE", "DIRECT", "2", "ROW_LEVEL", ROW_LEVEL_QUOTE)


def _entity(entity_id, identity, label, name, *, attributes=None):
    return {
        "entity_id": entity_id,
        "kb_id": KB,
        "canonical_identity": identity,
        "normalized_name": normalize_entity_name(name),
        "label": label,
        "name": name,
        "attributes": attributes or {},
        "content": name,
    }


def _triple(triple_id, source_entity_id, target_entity_id, relation_type, **kwargs):
    return {
        "triple_id": triple_id,
        "kb_id": KB,
        "source_entity_id": source_entity_id,
        "target_entity_id": target_entity_id,
        "relation_type": relation_type,
        "content": f"{relation_type}",
        "support_count": kwargs.get("support_count", 1),
        "literature_count": kwargs.get("literature_count", 1),
        "best_evidence_level": kwargs.get("best_evidence_level", "2"),
        "consensus_direction": kwargs.get("consensus_direction", "POSITIVE"),
    }


def _evidence(evidence_id, triple_id, **kwargs):
    return {
        "evidence_id": evidence_id,
        "triple_id": triple_id,
        "literature_id": kwargs.get("literature_id"),
        "pmid": kwargs.get("pmid"),
        "doi": kwargs.get("doi"),
        "identifier_status": kwargs.get("identifier_status", "VALID"),
        "direction": kwargs.get("direction", "POSITIVE"),
        "directness": kwargs.get("directness", "DIRECT"),
        "assertion_status": "ASSERTED",
        "evidence_level": kwargs.get("evidence_level", "2"),
        "evidence_quote": kwargs.get("evidence_quote"),
        "evidence_alignment_status": kwargs.get("evidence_alignment_status", "ALIGNED"),
        "outcome_class": kwargs.get("outcome_class", "OTHER"),
        "metadata_json": kwargs.get("metadata_json", {}),
    }


@pytest.fixture()
def sample_snapshot():
    entities = [
        _entity(
            GENE_ENTITY_ID,
            GENE_IDENTITY,
            "Gene",
            "Os01g0100100",
            attributes={
                "source": "managed_csv_import",
                "external_ids": ["Os01g0100100"],
                "aliases": ["Os01g0100100"],
                "rap_ids": ["Os01g0100100"],
                "msu_ids": ["LOC_Os01g01009.1"],
                "canonical_identity": GENE_IDENTITY,
                "gene_status": "confirmed",
            },
        ),
        _entity(PROCESS_ENTITY_ID, PROCESS_IDENTITY, "Process", "endosperm development"),
        _entity(PHENOTYPE_ENTITY_ID, PHENOTYPE_IDENTITY, "Phenotype", "chalky endosperm"),
        _entity(
            ALLELE_ENTITY_ID,
            ALLELE_IDENTITY,
            "AlleleMutant",
            ALLELE_NAME,
            attributes={
                "external_ids": [ALLELE_NAME],
                "aliases": [ALLELE_NAME],
                "rap_ids": [],
                "msu_ids": [],
                "canonical_identity": ALLELE_IDENTITY,
                "parent_gene_identity": GENE_IDENTITY,
            },
        ),
    ]
    triples = [
        _triple(T_PROMOTES, GENE_ENTITY_ID, PROCESS_ENTITY_ID, "PROMOTES_PROCESS"),
        _triple(T_MUTANT, ALLELE_ENTITY_ID, PHENOTYPE_ENTITY_ID, "MUTANT_EFFECT"),
        # 派生关系：导出应跳过，重导入由 normalizer 重建
        _triple(
            T_ALLELE_OF,
            ALLELE_ENTITY_ID,
            GENE_ENTITY_ID,
            "ALLELE_OF",
            support_count=0,
            literature_count=0,
            best_evidence_level=None,
            consensus_direction="UNKNOWN",
        ),
    ]
    evidence = [
        _evidence(
            "ev_aligned_1", T_PROMOTES, pmid="12345678", literature_id="pmid:12345678", evidence_quote=ALIGNED_QUOTE
        ),
        _evidence(
            "ev_rowlevel_1",
            T_MUTANT,
            evidence_alignment_status="ROW_LEVEL",
            direction="NEGATIVE",
            evidence_quote=ROW_LEVEL_QUOTE,
            metadata_json={"pmids": ["11111111", "22222222"], "dois": ["10.1234/test.001"]},
        ),
    ]
    aliases = {GENE_ENTITY_ID: ["OS01G0100100"]}
    return {"entities": entities, "triples": triples, "evidence": evidence, "aliases": aliases}


def _read_csv_rows(data: bytes) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig"), newline=""))
    return list(reader)


# ---------------------------------------------------------------- 反向映射 ----


def test_reverse_map_node_type_gene_three_states():
    assert reverse_map_node_type("Gene", "confirmed") == "RICE_GENE"
    assert reverse_map_node_type("Gene", "candidate") == "RICE_GENE_CANDIDATE"
    assert reverse_map_node_type("Gene", "unspecified") == "GENE"
    assert reverse_map_node_type("Gene", None) == "GENE"
    assert reverse_map_node_type("AlleleMutant", None) == "ALLELE_MUTANT"
    assert reverse_map_node_type("QTL", None) == "QTL_LOCUS"
    assert reverse_map_node_type("Compound", None) is None


# ------------------------------------------------------------ 往返自证 ----


def test_roundtrip_reparse_preserves_identity(sample_snapshot):
    package = build_roundtrip_package(
        kb_id=KB,
        entities=sample_snapshot["entities"],
        triples=sample_snapshot["triples"],
        evidence=sample_snapshot["evidence"],
        aliases_by_entity=sample_snapshot["aliases"],
        exported_by="tester",
    )

    with zipfile.ZipFile(io.BytesIO(package["content"])) as archive:
        names = set(archive.namelist())
        nodes_csv = archive.read("nodes.csv")
        relationships_csv = archive.read("relationships.csv")
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))

    assert names == {"nodes.csv", "relationships.csv", "manifest.json"}
    assert manifest["schema_version"] == "rice-endosperm-csv-v3"
    assert manifest["reparse_self_check"]["status"] == "READY"
    assert manifest["reparse_self_check"]["identity_preserved"] is True

    reparsed = parse_managed_graph_import(kb_id=KB, nodes_bytes=nodes_csv, relationships_bytes=relationships_csv)
    assert reparsed["valid"] is True
    assert reparsed["blockers"] == []

    plan = reparsed["plan"]
    # identity 保持：同 kb 重导入 entity_id/triple_id 完全一致（含派生 ALLELE_OF 重建）
    assert {entity["entity_id"] for entity in plan["entities"]} == {
        GENE_ENTITY_ID,
        PROCESS_ENTITY_ID,
        PHENOTYPE_ENTITY_ID,
        ALLELE_ENTITY_ID,
    }
    assert {triple["triple_id"] for triple in plan["triples"]} == {T_PROMOTES, T_MUTANT, T_ALLELE_OF}
    # 证据逐条还原：ALIGNED 单条 + ROW_LEVEL 捆绑包重导入后 evidence_id 与解析器算法一致
    assert {item["evidence_id"] for item in plan["evidence"]} == {EXPECTED_EV_ALIGNED, EXPECTED_EV_ROW_LEVEL}


def test_roundtrip_node_rows(sample_snapshot):
    built = build_roundtrip_csvs(
        entities=sample_snapshot["entities"],
        triples=sample_snapshot["triples"],
        evidence=sample_snapshot["evidence"],
        aliases_by_entity=sample_snapshot["aliases"],
    )
    node_rows = _read_csv_rows(built["nodes_csv"])
    rice_gene_rows = [row for row in node_rows if row["node_type"] == "RICE_GENE"]

    assert any(row["rap_id"] == "Os01g0100100" for row in rice_gene_rows)
    # 多 registry id 拆行，msu 单独一行，node_id 相同
    gene_msu_rows = [
        row for row in node_rows if row["msu_id"] == "LOC_Os01g01009.1" and row["node_id"] == "Os01g0100100"
    ]
    assert len(gene_msu_rows) == 1
    # allele 行挂靠父基因 node_id（union-find 并组 -> 语义拆分重建 parent_identity）
    allele_rows = [row for row in node_rows if row["node_type"] == "ALLELE_MUTANT"]
    assert len(allele_rows) == 1
    assert allele_rows[0]["node_id"] == "Os01g0100100"
    assert allele_rows[0]["name"] == ALLELE_NAME
    # 别名附加列（导入器忽略）
    assert any(row["aliases"] == "OS01G0100100" for row in rice_gene_rows)

    relation_rows = _read_csv_rows(built["relationships_csv"])
    # ALLELE_OF 派生关系不导出
    assert all(row["relation_type"] != "ALLELE_OF" for row in relation_rows)
    aligned = next(row for row in relation_rows if row["pmids"] == "12345678")
    assert aligned["relation_type"] == "PROMOTES_PROCESS"
    row_level = next(row for row in relation_rows if row["pmids"] == "11111111|22222222")
    assert row_level["dois"] == "10.1234/test.001" and row_level["evidence_quotes"] == "q1\n\nq2"


def test_roundtrip_skips_unknown_labels_and_dangling_relations(sample_snapshot):
    unknown = _entity("e_compound", "name:starch", "Compound", "starch")
    sample_snapshot["entities"].append(unknown)
    sample_snapshot["triples"].append(_triple("t_dangling_1", GENE_ENTITY_ID, "e_compound", "REQUIRED_FOR"))

    package = build_roundtrip_package(
        kb_id=KB,
        entities=sample_snapshot["entities"],
        triples=sample_snapshot["triples"],
        evidence=sample_snapshot["evidence"],
        aliases_by_entity=sample_snapshot["aliases"],
    )
    manifest = package["manifest"]
    assert manifest["skipped"]["unsupported_label_entities"] == [
        {"entity_id": "e_compound", "label": "Compound", "name": "starch"}
    ]
    assert manifest["skipped"]["triples_with_skipped_endpoint"] == 1


def test_roundtrip_empty_data_produces_header_only_csvs():
    built = build_roundtrip_csvs(entities=[], triples=[], evidence=[])
    assert _read_csv_rows(built["nodes_csv"]) == []
    assert _read_csv_rows(built["relationships_csv"]) == []


# ------------------------------------------------------------- xlsx ----


def test_evidence_workbook_structure(sample_snapshot):
    package = build_evidence_workbook(
        kb_id=KB,
        entities=sample_snapshot["entities"],
        triples=sample_snapshot["triples"],
        evidence=sample_snapshot["evidence"],
        aliases_by_entity=sample_snapshot["aliases"],
    )
    assert package["filename"].endswith(".xlsx")

    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(package["content"]))
    assert workbook.sheetnames == ["实体", "三元组", "证据明细"]
    entity_sheet = workbook["实体"]
    assert entity_sheet.max_row == len(sample_snapshot["entities"]) + 1
    assert entity_sheet["A1"].value == "entity_id"
    triple_sheet = workbook["三元组"]
    assert triple_sheet.max_row == len(sample_snapshot["triples"]) + 1
    evidence_sheet = workbook["证据明细"]
    assert evidence_sheet.max_row == len(sample_snapshot["evidence"]) + 1
    headers = [cell.value for cell in evidence_sheet[1]]
    assert "experimental_subject_type" in headers and "claim_eligible" in headers
    # source 列渲染为 名称(标签)
    assert triple_sheet.cell(row=2, column=2).value == "Os01g0100100(Gene)"
