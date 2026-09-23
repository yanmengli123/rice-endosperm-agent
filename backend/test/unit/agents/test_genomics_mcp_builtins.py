from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from yuxi.agents.mcp import service as mcp_service
from yuxi.agents.mcp.capability_registry import profile_for_protocol_name, profile_for_server_tool
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


def test_genomics_runtime_requires_matching_launcher_probe(monkeypatch):
    slug = "gene-authority"
    revision = mcp_service._GENOMICS_MCP_RUNTIMES[slug][2]
    monkeypatch.setattr(mcp_service.shutil, "which", lambda _command: "/usr/bin/docker")

    def run(command, **_kwargs):
        if command[0] == "docker":
            return SimpleNamespace(returncode=0, stdout=f"{revision}|{slug}|1\n")
        assert command == ["/usr/local/bin/yuxi-genomics-mcp", "--probe", slug]
        return SimpleNamespace(returncode=0, stdout=f"ready:{slug}:{revision}\n")

    monkeypatch.setattr(mcp_service.subprocess, "run", run)
    mcp_service._genomics_mcp_runtime_ready.cache_clear()
    assert mcp_service._genomics_mcp_runtime_ready(slug) is True

    def stale_launcher(command, **kwargs):
        result = run(command, **kwargs)
        if command[0] != "docker":
            result.stdout = "ready:gene-authority:stale-revision\n"
        return result

    monkeypatch.setattr(mcp_service.subprocess, "run", stale_launcher)
    mcp_service._genomics_mcp_runtime_ready.cache_clear()
    assert mcp_service._genomics_mcp_runtime_ready(slug) is False


def test_gene_authority_catalog_runtime_and_launcher_versions_are_synchronized():
    """目录 source_ref、运行时镜像与受管启动器必须同版，禁止半边升级。"""
    config = mcp_service._DEFAULT_MCP_SERVERS["gene-authority"]
    _env_name, image, revision = mcp_service._GENOMICS_MCP_RUNTIMES["gene-authority"]
    assert config["source_ref"] == "builtin:gene-authority@1.5.1+ncbi-datasets-18.37.0"
    assert image == "yuxi-gene-authority:1.5.1"
    assert revision == "gene-authority-1.5.1+ncbi-datasets-18.37.0"


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


def test_gene_authority_delta_semantics_distinct_from_interval_length():
    module = _load_gene_authority()
    delta = module.compute_delta(1770653, 1770556, label="RAP-DB minus MSU start")

    assert delta["data"]["delta"] == 97
    assert delta["data"]["abs_delta"] == 97
    assert delta["data"]["formula"] == "minuend - subtrahend"
    assert delta["data"]["label"] == "RAP-DB minus MSU start"
    # 差值语义不得与区间长度语义混用：同两数的闭区间长度是 98。
    assert module.verify_genomic_interval(1770556, 1770653)["data"]["length"] == 98


def test_gene_authority_delta_rejects_nonfinite_inputs():
    module = _load_gene_authority()
    with pytest.raises(module.AuthorityError):
        module.compute_delta(float("nan"), 1)
    with pytest.raises(module.AuthorityError):
        module.compute_delta(True, 1)


@pytest.mark.parametrize(
    "name",
    [
        "analyze_locus_synth",
        "find_homologs_synth",
        "biological_context_synth",
        "consensus_homologs",
        "gene_report",
        "go_enrichment",
        "blast_sequence",
    ],
)
def test_synthesis_and_analysis_tools_are_excluded_from_the_trust_registry(name):
    """合成/聚合与语义不符工具无 profile：不进 source-constrained 轮次，不满足来源义务。"""
    assert profile_for_protocol_name(name) is None


def test_plant_synthesis_tools_are_disabled_for_new_installations():
    values = mcp_service._builtin_row_values(
        "plant-genomics", mcp_service._DEFAULT_MCP_SERVERS["plant-genomics"]
    )
    assert set(values["disabled_tools"]) == {
        "analyze_locus_synth",
        "find_homologs_synth",
        "biological_context_synth",
        "consensus_homologs",
        "gene_report",
        "go_enrichment",
        "blast_sequence",
    }


@pytest.mark.parametrize("name", ["solr_suggest", "mongo_list_collections"])
def test_gramene_metadata_tools_cannot_satisfy_gene_record_obligation(name):
    assert profile_for_protocol_name(name) is None


def test_exact_retrieval_tools_remain_registered():
    for name in ("gramene_homologs", "batch_string_interactions", "locus_variants", "vep_annotate"):
        profile = profile_for_protocol_name(name)
        assert profile is not None
        assert Capability.GENE_RECORD_LOOKUP in profile.capabilities


def test_dataset_candidates_cannot_satisfy_answer_authority():
    for name in ("ncbi_eutils_search_rest", "pride_search_projects_rest"):
        profile = profile_for_protocol_name(name)
        assert profile is not None
        assert Capability.DATASET_LOOKUP in profile.capabilities
        assert profile.source_class == "DISCOVERY"
        assert profile.answer_eligible is False
    for name in ("ncbi_eutils_summary_rest", "pride_project_rest", "pride_project_files_rest"):
        profile = profile_for_protocol_name(name)
        assert profile is not None
        assert profile.answer_eligible is True


def test_large_datasets_download_is_not_callable_in_interactive_chat():
    config = mcp_service._DEFAULT_MCP_SERVERS["gene-authority"]
    assert "ncbi_datasets_gene_package_cli" in config["disabled_tools"]
    assert "ncbi_datasets_gene_package_cli" in mcp_service._POLICY_DISABLED_TOOLS["gene-authority"]
    assert {"fetch", "operate"} <= mcp_service._POLICY_DISABLED_TOOLS["data-aggregator"]


def test_aggregator_generic_names_are_trusted_only_within_reviewed_server():
    candidate = profile_for_server_tool("data-aggregator", "search")
    assert candidate is not None
    assert candidate.answer_eligible is False
    assert profile_for_server_tool("another-server", "search") is None
    assert profile_for_server_tool("data-aggregator", "fetch") is None
