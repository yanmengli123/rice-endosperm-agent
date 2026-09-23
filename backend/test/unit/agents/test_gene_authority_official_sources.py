"""Contract tests for the isolated official-source MCP image (stdlib only)."""

from __future__ import annotations

import io
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

# 与 test_genomics_mcp_builtins 同款加载方式：vendored 脚本按 ROOT 相对路径编译执行，
# 构建上下文未挂载进 API 测试容器时整模块 skip（而不是收集期 ImportError 中断整套）。
def _find_repo_root() -> Path | None:
    """向上找含 docker/mcp 的仓库根：容器内只挂 test/ 时返回 None（走 skip）。"""
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "docker" / "mcp").is_dir():
            return candidate
    return None


ROOT = _find_repo_root()
GENE_AUTHORITY = ROOT / "docker" / "mcp" / "gene-authority" / "gene_authority_mcp.py" if ROOT else None


def _load_gene_authority():
    if GENE_AUTHORITY is None or not GENE_AUTHORITY.is_file():
        raise unittest.SkipTest("docker build context is not mounted into the API test container")
    name = "gene_authority_mcp"
    module = types.ModuleType(name)
    sys.modules[name] = module
    try:
        exec(compile(GENE_AUTHORITY.read_text(encoding="utf-8"), str(GENE_AUTHORITY), "exec"), module.__dict__)  # noqa: S102
    finally:
        sys.modules.pop(name, None)
    return module


sources = _load_gene_authority()


class _Response:
    def __init__(self, body: bytes, status: int = 200):
        self.stream = io.BytesIO(body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.stream.close()

    def read(self, size: int):
        return self.stream.read(size)


class OfficialSourceTests(unittest.TestCase):
    def test_http_200_provider_error_is_not_a_found_record(self):
        self.assertFalse(
            sources._has_records({"messages": [{"error": {"reason": "invalid taxonomy token"}}]})
        )

    def test_entrez_search_discovery_does_not_leak_api_key(self):
        body = b'{"esearchresult":{"count":"1","idlist":["123"]}}'
        with patch.dict("os.environ", {"NCBI_API_KEY": "secret-key", "YUXI_NCBI_EMAIL": "admin@example.org"}):
            with patch.object(sources.urllib.request, "urlopen", return_value=_Response(body)) as open_url:
                result = sources.ncbi_eutils_search_rest("bioproject", "Oryza sativa endosperm")
        self.assertEqual(result["status"], "FOUND")
        self.assertEqual(result["data"]["esearchresult"]["idlist"], ["123"])
        self.assertNotIn("secret-key", result["request"]["url"])
        self.assertNotIn("admin@example.org", result["request"]["url"])
        self.assertIn("api_key=secret-key", open_url.call_args.args[0].full_url)

    def test_entrez_empty_search_and_invalid_database(self):
        with patch.dict("os.environ", {"YUXI_NCBI_EMAIL": "admin@example.org"}):
            body = b'{"esearchresult":{"count":"0","idlist":[]}}'
            with patch.object(sources.urllib.request, "urlopen", return_value=_Response(body)):
                result = sources.ncbi_eutils_search_rest("sra", "unfindable")
        self.assertEqual(result["status"], "NOT_FOUND")
        with self.assertRaises(sources.AuthorityError):
            sources.ncbi_eutils_search_rest("arbitrary", "rice")

    def test_entrez_gene_symbol_resolution_is_allowlisted(self):
        body = b'{"esearchresult":{"count":"1","idlist":["4340018"]}}'
        with patch.object(sources.urllib.request, "urlopen", return_value=_Response(body)) as open_url:
            result = sources.ncbi_eutils_search_rest(
                "gene", "Wx[Gene Name] AND Oryza sativa[Organism]", retmax=10
            )
        self.assertEqual(result["data"]["esearchresult"]["idlist"], ["4340018"])
        self.assertIn("db=gene", open_url.call_args.args[0].full_url)

    def test_gene_esummary_adds_explicit_one_based_coordinates(self):
        body = json.dumps(
            {
                "result": {
                    "uids": ["4340018"],
                    "4340018": {
                        "uid": "4340018",
                        "genomicinfo": [{"chraccver": "NC_029261.1", "chrstart": 1927857, "chrstop": 1922742}],
                    },
                }
            }
        ).encode()
        with patch.object(sources.urllib.request, "urlopen", return_value=_Response(body)):
            result = sources.ncbi_eutils_summary_rest("gene", ["4340018"])

        location = result["data"]["result"]["4340018"]["genomicinfo"][0]
        self.assertEqual(location["chrstart"], 1927857)
        self.assertEqual(location["chrstop"], 1922742)
        self.assertEqual(location["coordinate_system"], "zero_based_inclusive")
        self.assertEqual(location["one_based_inclusive"], {"start": 1922743, "end": 1927858})
        self.assertIn("publish one_based_inclusive only", result["answer_policy"])

    def test_uniprot_calls_request_a_bounded_identity_projection(self):
        body = b'{"results":[{"primaryAccession":"P0C585"}]}'
        with patch.object(sources.urllib.request, "urlopen", return_value=_Response(body)) as open_url:
            result = sources.uniprot_search_rest("gene:Wx AND organism_id:4530", size=1)
        self.assertEqual(result["data"]["results"][0]["primaryAccession"], "P0C585")
        requested = open_url.call_args.args[0].full_url
        self.assertIn("reviewed%3Atrue", requested)
        self.assertIn("fields=accession%2Cid%2Cgene_names", requested)
        self.assertIn("xref_geneid", requested)

    def test_dataset_symbol_lookup_resolves_entrez_then_verifies_numeric_id(self):
        empty = {"data": {"esearchresult": {"idlist": []}}}
        found = {"data": {"esearchresult": {"idlist": ["4340018"]}}}
        dataset = sources._envelope(
            "NCBI_DATASETS",
            "https://api.ncbi.nlm.nih.gov/datasets/v2/gene/id/4340018/dataset_report",
            {"reports": [{"gene": {"gene_id": "4340018"}}]},
        )
        with patch.object(sources, "ncbi_eutils_search_rest", side_effect=[empty, found]) as search:
            with patch.object(sources, "_request_json", return_value=dataset) as request:
                result = sources.ncbi_gene_report_rest(
                    ["WAXY", "Wx"], identifier_type="symbol", taxon="4530"
                )
        self.assertEqual(result["status"], "FOUND")
        self.assertEqual(result["data"]["symbol_resolution"]["gene_ids"], ["4340018"])
        self.assertEqual(search.call_count, 2)
        self.assertIn("/gene/id/4340018/dataset_report", request.call_args.args[1])

    def test_pride_search_is_candidate_and_exact_project_is_bounded(self):
        with patch.object(sources.urllib.request, "urlopen", return_value=_Response(b'[{"accession":"PXD000001"}]')):
            result = sources.pride_search_projects_rest("rice", page_size=1)
        self.assertEqual(result["data"]["results"][0]["accession"], "PXD000001")
        self.assertIn("Candidate", result["answer_policy"])
        with self.assertRaises(sources.AuthorityError):
            sources.pride_project_rest("https://example.com/")

    def test_oa_passages_have_xml_locator_and_exact_quote(self):
        xml = (
            b"<article><body><sec><title>Results</title><p>"
            b"OsbZIP58 binds the Wx promoter in rice endosperm."
            b"</p></sec></body></article>"
        )
        with patch.object(sources.urllib.request, "urlopen", return_value=_Response(xml)):
            result = sources.europe_pmc_oa_passages_rest("PMC12345", "Wx")
        self.assertEqual(result["status"], "FOUND")
        passage = result["data"]["passages"][0]
        self.assertIn("Wx promoter", passage["quote"])
        self.assertEqual(passage["locator"]["kind"], "XML")
        self.assertEqual(passage["locator"]["section"], "Results")
        self.assertNotIn("page", passage["locator"])
        self.assertEqual(result["data"]["raw_sha256"], sources.hashlib.sha256(xml).hexdigest())

    def test_oa_rejects_dtd(self):
        xml = b'<!DOCTYPE article [<!ENTITY unsafe "data">]><article><body><p>&unsafe;</p></body></article>'
        with patch.object(sources.urllib.request, "urlopen", return_value=_Response(xml)):
            with self.assertRaises(sources.AuthorityError):
                sources.europe_pmc_oa_passages_rest("PMC12345", "data")

    def test_efetch_xml_is_bounded_and_private_parameters_are_redacted(self):
        xml = b'<PubmedArticleSet><PubmedArticle /></PubmedArticleSet>'
        with patch.dict("os.environ", {"NCBI_API_KEY": "secret-key", "YUXI_NCBI_EMAIL": "admin@example.org"}):
            with patch.object(sources.urllib.request, "urlopen", return_value=_Response(xml)) as open_url:
                result = sources.ncbi_eutils_fetch_rest("pubmed", ["123"])
        self.assertEqual(result["data"]["xml"], xml.decode())
        self.assertNotIn("secret-key", json.dumps(result))
        self.assertNotIn("admin@example.org", json.dumps(result))
        self.assertIn("api_key=secret-key", open_url.call_args.args[0].full_url)


class CircuitBreakerAndCliFallbackContract(unittest.TestCase):
    """P3 韧性：熔断（连续失败快速失败 + 冷却半开）与 REST→CLI 回退。"""

    def setUp(self):
        sources._BREAKER_STATE.clear()

    def test_breaker_opens_after_consecutive_failures_and_half_opens_after_cooldown(self):
        self.assertTrue(sources._breaker_allows("NCBI_DATASETS"))
        for _ in range(sources._BREAKER_THRESHOLD):
            sources._breaker_record("NCBI_DATASETS", ok=False)
        self.assertFalse(sources._breaker_allows("NCBI_DATASETS"))
        # 冷却结束（模拟时间流逝）→ 半开放行
        sources._BREAKER_STATE["NCBI_DATASETS"]["open_until"] = 0.0
        self.assertTrue(sources._breaker_allows("NCBI_DATASETS"))
        # 成功即复位
        sources._breaker_record("NCBI_DATASETS", ok=False)
        sources._breaker_record("NCBI_DATASETS", ok=True)
        self.assertEqual(sources._BREAKER_STATE["NCBI_DATASETS"]["consecutive_failures"], 0)
        self.assertTrue(sources._breaker_allows("NCBI_DATASETS"))

    def test_open_breaker_fails_fast_without_network(self):
        for _ in range(sources._BREAKER_THRESHOLD):
            sources._breaker_record("NCBI_DATASETS", ok=False)
        with patch.object(sources.urllib.request, "urlopen") as open_url:
            with self.assertRaises(sources.AuthorityError) as ctx:
                sources._request_json("NCBI_DATASETS", sources.NCBI_BASE + "/gene")
        open_url.assert_not_called()
        self.assertIn("circuit breaker open", str(ctx.exception))

    def test_retries_record_breaker_failure_then_open(self):
        # 三次重试全失败 → 记一次 breaker 失败；重复 5 轮 → 熔断打开
        for _ in range(sources._BREAKER_THRESHOLD):
            with patch.object(
                sources.urllib.request, "urlopen", side_effect=sources.urllib.error.URLError("refused")
            ):
                with self.assertRaises(sources.AuthorityError):
                    sources._request_json("NCBI_DATASETS", sources.NCBI_BASE + "/gene")
        self.assertFalse(sources._breaker_allows("NCBI_DATASETS"))

    def test_rest_failure_falls_back_to_cli_summary(self):
        cli_envelope = {
            "schema_version": sources.SCHEMA_VERSION,
            "status": "FOUND",
            "provider": "NCBI_DATASETS_CLI",
            "retrieved_at": sources._now(),
            "data": {"reports": []},
        }
        with patch.object(
            sources, "_request_json", side_effect=sources.AuthorityError("NCBI_DATASETS request failed")
        ), patch.object(sources, "ncbi_gene_summary_cli", return_value=cli_envelope) as cli:
            result = sources.ncbi_gene_report_rest(["4340018"], identifier_type="gene-id")
        cli.assert_called_once()
        self.assertEqual(result["status"], "FOUND")
        self.assertTrue(result["data"]["rest_fallback"]["used_cli"])
        self.assertIn("NCBI_DATASETS request failed", result["data"]["rest_fallback"]["rest_error"])

    def test_rest_and_cli_both_failing_raises_unavailable(self):
        with patch.object(
            sources, "_request_json", side_effect=sources.AuthorityError("NCBI_DATASETS request failed")
        ), patch.object(
            sources, "ncbi_gene_summary_cli", side_effect=sources.AuthorityError("CLI rejected")
        ):
            with self.assertRaises(sources.AuthorityError):
                sources.ncbi_gene_report_rest(["4340018"], identifier_type="gene-id")


if __name__ == "__main__":
    unittest.main()
