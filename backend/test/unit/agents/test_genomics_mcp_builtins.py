from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from yuxi.agents.mcp import service as mcp_service
from yuxi.agents.mcp.capability_registry import profile_for_protocol_name
from yuxi.knowledge.planning.turn_execution_plan import Capability

ROOT = Path(__file__).resolve().parents[4]
GENE_AUTHORITY = ROOT / "docker" / "mcp" / "gene-authority" / "gene_authority_mcp.py"


def _load_gene_authority():
    if not GENE_AUTHORITY.is_file():
        pytest.skip("docker build context is not mounted into the API test container")
    name = "gene_authority_mcp_test"
    module = types.ModuleType(name)
    sys.modules[name] = module
    try:
        exec(compile(GENE_AUTHORITY.read_text(encoding="utf-8"), str(GENE_AUTHORITY), "exec"), module.__dict__)  # noqa: S102
        return module
    finally:
        sys.modules.pop(name, None)


@pytest.mark.parametrize("slug", ["gene-authority", "plant-genomics", "gramene"])
def test_genomics_builtins_use_the_governed_launcher_and_start_disabled(slug, monkeypatch):
    config = mcp_service._DEFAULT_MCP_SERVERS[slug]
    assert config["command"] == "/usr/local/bin/yuxi-genomics-mcp"
    assert config["args"] == [slug]
    assert config["transport"] == "stdio"
    monkeypatch.setenv("ALLOW_DEVELOPMENT_MCP_RUNTIME", "true")
    monkeypatch.setattr(mcp_service, "_genomics_mcp_runtime_ready", lambda candidate: candidate == slug)
    values = mcp_service._builtin_row_values(slug, config)
    assert values["enabled"] == 0
    assert values["lifecycle_status"] == "READY"
    assert values["runtime_level"] == "managed_oci"
    assert values["runtime_artifact"]["kind"] == "oci_image"
    assert "@sha256:" in values["runtime_artifact"]["image_digest"]


def test_gene_authority_interval_is_deterministic_and_coordinate_explicit():
    module = _load_gene_authority()
    inclusive = module.verify_genomic_interval(1770556, 1770653)
    half_open = module.verify_genomic_interval(1770556, 1770653, "zero_based_half_open")

    assert inclusive["data"]["length"] == 98
    assert inclusive["data"]["formula"] == "end - start + 1"
    assert half_open["data"]["length"] == 97


@pytest.mark.parametrize(
    "name,capability",
    [
        ("ncbi_datasets_gene_report_rest", Capability.GENE_RECORD_LOOKUP),
        ("uniprot_entry_rest", Capability.GENE_RECORD_LOOKUP),
        ("europe_pmc_search_rest", Capability.BIBLIOGRAPHIC_SEARCH),
        ("ensembl_plants_lookup_locus", Capability.GENE_RECORD_LOOKUP),
        ("solr_search", Capability.GENE_RECORD_LOOKUP),
    ],
)
def test_new_authority_tools_are_in_the_server_side_trust_registry(name, capability):
    profile = profile_for_protocol_name(name)
    assert profile is not None
    assert capability in profile.capabilities
    assert profile.fallback_policy == "FAIL_CLOSED"
