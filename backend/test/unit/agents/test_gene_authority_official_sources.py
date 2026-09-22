"""Contract tests for the isolated official-source MCP image (stdlib only)."""

from __future__ import annotations

import io
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

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
        pytest.skip(
            "docker build context is not mounted into the API test container",
            allow_module_level=True,
        )
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
            with patch.object(sources.urllib.request, "urlopen", return_value=_Response(b'{"esearchresult":{"count":"0","idlist":[]}}')):
                result = sources.ncbi_eutils_search_rest("sra", "unfindable")
        self.assertEqual(result["status"], "NOT_FOUND")
        with self.assertRaises(sources.AuthorityError):
            sources.ncbi_eutils_search_rest("arbitrary", "rice")

    def test_pride_search_is_candidate_and_exact_project_is_bounded(self):
        with patch.object(sources.urllib.request, "urlopen", return_value=_Response(b'[{"accession":"PXD000001"}]')):
            result = sources.pride_search_projects_rest("rice", page_size=1)
        self.assertEqual(result["data"]["results"][0]["accession"], "PXD000001")
        self.assertIn("Candidate", result["answer_policy"])
        with self.assertRaises(sources.AuthorityError):
            sources.pride_project_rest("https://example.com/")

    def test_oa_passages_have_xml_locator_and_exact_quote(self):
        xml = b'<article><body><sec><title>Results</title><p>OsbZIP58 binds the Wx promoter in rice endosperm.</p></sec></body></article>'
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


if __name__ == "__main__":
    unittest.main()
