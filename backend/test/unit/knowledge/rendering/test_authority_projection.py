from __future__ import annotations

from yuxi.knowledge.rendering.profile_projection import project_data_plane
from yuxi.knowledge.rendering.source_output_guard import guard_answer_for_evidence_level


_FACTS = [
    {"id": "f_gene_id", "path": "/data/reports/0/gene/gene_id", "numeric_value": 4340018},
    {"id": "f_symbol", "path": "/data/reports/0/gene/symbol", "string_value": "Wx"},
    {
        "id": "f_description",
        "path": "/data/reports/0/gene/description",
        "string_value": "granule-bound starch synthase 1",
    },
    {"id": "f_taxname", "path": "/data/reports/0/gene/taxname", "string_value": "Oryza sativa"},
]


def _use() -> dict:
    return {
        "source_use_id": "mcp:501",
        "provider_id": "gene-authority",
        "operation": "ncbi_datasets_gene_report_rest",
        "status": "SUCCESS",
        "execution_status": "SUCCESS",
        "provider_status": "FOUND",
        "adopted": True,
        "provenance": {"mcp_call_audit_id": 501, "fact_manifest": {"facts": _FACTS}},
    }


def test_ncbi_official_link_is_derived_from_verified_gene_id():
    source_uses = [_use()]
    projection = project_data_plane(source_uses)
    assert projection is not None
    assert projection.kind == "official_link"
    assert "`4340018`" in projection.blocks
    assert "https://www.ncbi.nlm.nih.gov/gene/4340018" in projection.blocks
    assert "https://api.ncbi.nlm.nih.gov/datasets/v2/gene/id/4340018/dataset_report" in projection.blocks
    assert "29482" not in projection.blocks
    assert "[MCP-F:501:f_gene_id]" in projection.blocks

    _, audit = guard_answer_for_evidence_level(
        projection.blocks,
        evidence_level="E1_DATA_PROVENANCE",
        source_uses=source_uses,
        source_policy="MCP_ONLY",
        requires_mcp=True,
    )
    assert audit["status"] == "PASSED", audit

