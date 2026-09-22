"""确定性档案装配器（ricekb_gene_profile）的离线行为契约。"""

from __future__ import annotations

import json

from yuxi.agents.mcp.ricekb_profile import GatewayError, _GatewayClient, assemble_gene_profile


class FakeOpener:
    def __init__(self, responses: dict[str, dict]):
        self._responses = responses
        self.calls: list[str] = []

    def __call__(self, request, timeout):
        path = request.full_url.split("/v1/", 1)[-1].split("?")[0]
        key = "/" + path
        self.calls.append(request.full_url)
        payload = self._responses.get(key)
        if payload is None:
            return 404, b"{}"
        return 200, json.dumps(payload).encode("utf-8")


def _client(responses: dict[str, dict]) -> _GatewayClient:
    return _GatewayClient("http://gateway.test", "token", 5.0, opener=FakeOpener(responses))


def _wx_fixture() -> dict[str, dict]:
    resolve = {
        "status": "FOUND",
        "entity": {
            "entity_key": "RAP:Os06g0133000",
            "canonical_rap_id": "Os06g0133000",
            "matched_identifiers": ["Wx"],
            "matched_namespaces": ["SYMBOL"],
            "source_databases": ["MSU", "ORYZABASE", "RAP_DB"],
            "source_database_count": 3,
            "source_record_count": 12,
            "source_coverage_score": 100,
        },
        "data": [
            {
                "entity_key": "RAP:Os06g0133000",
                "canonical_rap_id": "Os06g0133000",
                "source_databases": ["MSU", "ORYZABASE", "RAP_DB"],
            }
        ],
    }
    entity = {
        "status": "FOUND",
        "data": {
            "identifiers": [
                {
                    "identifier": "LOC_Os06g0133000.1",
                    "identifier_type": "msu_transcript",
                    "namespace": "MSU",
                    "provenance_id": "source_rapdb.rap_msu_crosswalk:1",
                },
                {
                    "identifier": "LOC_Os06g0133000.1",
                    "identifier_type": "msu_transcript",
                    "namespace": "MSU",
                    "provenance_id": "source_rapdb.rap_msu_crosswalk:2",
                },
            ],
            "source_records": {
                "msu_loci": [
                    {
                        "chr": "6",
                        "locus": "LOC_Os06g0133000",
                        "start": 1765622,
                        "stop": 1770656,
                        "ori": "+",
                        "is_representative": True,
                        "source_row_number": 60181,
                    }
                ],
                "rapdb_loci": [
                    {
                        "seqid": "6",
                        "locus_id": "Os06g0133000",
                        "start_position": 1765622,
                        "end_position": 1770653,
                        "strand": "+",
                        "feature_type": "gene",
                        "source_line_number": 22049,
                    }
                ],
                "rap_msu_crosswalk": [{"msu_transcript_id": "LOC_Os06g0133000.1"}],
                "rapdb_annotations": [{"transcript_id": "Os06t0133000-01"}],
            },
        },
    }
    compare = {
        "status": "FOUND",
        "data": {
            "sources": {
                "RAP_DB": [
                    {
                        "description": "Granule-bound starch synthase",
                        "cgsnl_gene_symbol": "WX1",
                        "cgsnl_gene_name": "GLUTINOUS ENDOSPERM",
                        "rap_db_gene_symbol_synonym_s": "Wx, waxy",
                        "oryzabase_gene_symbol_synonym_s": "Wx, GBSS-I, OsWx",
                        "go": "Molecular Function: alpha-1,4-glucan glucosyltransferase activity (GO:0004373)",
                        "interpro": "Glycosyl transferase, family 1 (IPR001296)",
                    }
                ]
            },
            "agreements": [{"field": "chromosome", "value": "6", "sources": ["MSU", "ORYZABASE", "RAP_DB"]}],
            "conflicts": [],
            "source_specific": {"MSU_annotations": ["starch synthase, putative, expressed"]},
            "missing_sources": [],
        },
    }
    support = {
        "status": "FOUND",
        "data": {
            "sources": [
                {"source_database": "RAP_DB", "source_table": "rapdb_loci", "record_count": 1},
                {"source_database": "MSU", "source_table": "msu_loci", "record_count": 1},
            ],
            "source_database_count": 3,
        },
    }
    evidence = {
        "status": "FOUND",
        "data": [{"provenance_id": f"source_rapdb.rapdb_loci:{i}"} for i in range(3)],
    }
    references = {
        "status": "FOUND",
        "data": [
            {
                "pubmedid": "12345678",
                "year": "2001",
                "journal": "Plant Mol Biol",
                "title": "The waxy gene in rice",
                "source_row_number": 705,
            }
        ],
    }
    return {
        "/entities/resolve": resolve,
        "/entities/RAP:Os06g0133000": entity,
        "/entities/RAP:Os06g0133000/compare": compare,
        "/entities/RAP:Os06g0133000/support": support,
        "/entities/RAP:Os06g0133000/evidence": evidence,
        "/entities/RAP:Os06g0133000/references": references,
    }


def test_wx_profile_qc_derivations_are_deterministic():
    profile = assemble_gene_profile(_client(_wx_fixture()), "Wx")
    qc = {(item["rule"], item.get("source")): item["value"] for item in profile["qc"]}

    assert profile["status"] == "FOUND"
    assert qc[("interval_length_v1", "MSU")] == 1770656 - 1765622 + 1
    assert qc[("interval_length_v1", "RAP_DB")] == 1770653 - 1765622 + 1
    assert qc[("coordinate_start_delta_v1", None)] == 0
    assert qc[("coordinate_end_delta_v1", None)] == 3
    assert qc[("distinct_evidence_rows_v1", None)] == 3


def test_profile_locations_are_one_based_inclusive_with_source_refs():
    profile = assemble_gene_profile(_client(_wx_fixture()), "Wx")
    locations = {item["source"]: item for item in profile["data"]["locations"]}

    assert locations["MSU"]["coordinate_system"] == "one_based_inclusive"
    assert locations["MSU"]["source_ref"].startswith("source_msu.msu_loci#row=")
    assert locations["RAP_DB"]["source_ref"].startswith("source_rapdb.rapdb_loci#line=")
    assert locations["RAP_DB"]["start"] == 1765622


def test_profile_is_compact_and_fact_ledger_friendly():
    from yuxi.agents.mcp.fact_ledger import extract_facts

    profile = assemble_gene_profile(_client(_wx_fixture()), "Wx")
    envelope = json.dumps(profile, ensure_ascii=False)
    assert len(envelope) < 12000

    class Result:
        structured_content = profile
        text = ""

    facts, truncated = extract_facts(Result())
    assert not truncated
    assert len(facts) > 60  # identity/locations/qc/xrefs 全部可引用
    paths = {fact.path for fact in facts}
    assert "/qc/2/value" in paths
    assert "/data/locations/0/start" in paths


def test_not_found_and_ambiguous_are_disclosed_verbatim():
    not_found = {"status": "NOT_FOUND", "data": []}
    profile = assemble_gene_profile(_client({"/entities/resolve": not_found}), "OsFake0000000")
    assert profile["status"] == "NOT_FOUND"
    assert "不得改写" in profile["answer_policy"]

    ambiguous = {
        "status": "AMBIGUOUS",
        "data": [{"entity_key": f"RAP:Os0{i}g0000000", "canonical_rap_id": f"Os0{i}g0000000"} for i in range(3)],
    }
    profile = assemble_gene_profile(_client({"/entities/resolve": ambiguous}), "Wx")
    assert profile["status"] == "AMBIGUOUS"
    assert len(profile["candidates"]) == 3


def test_conflicts_promote_status_and_invalid_identifier_rejected():
    fixture = _wx_fixture()
    fixture["/entities/RAP:Os06g0133000/compare"]["data"]["conflicts"] = [
        {"field": "chromosome", "values": {"MSU": "6", "RAP_DB": "06"}}
    ]
    profile = assemble_gene_profile(_client(fixture), "Wx")
    assert profile["status"] == "CONFLICT"
    assert profile["data"]["compare"]["conflicts"]

    import pytest

    with pytest.raises(GatewayError):
        assemble_gene_profile(_client({}), "bad identifier; drop table")
