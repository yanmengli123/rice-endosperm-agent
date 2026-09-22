"""Opt-in official API smoke tests inside the isolated gene-authority image."""

from __future__ import annotations

import os
import sys
import types
import unittest
from pathlib import Path

import pytest

# 双运行方式兼容：
# 1) 隔离镜像内（cwd=/app，模块随 COPY 落盘）→ 直接导入；
# 2) 仓库/API 测试容器内 → 按 ROOT 相对路径编译执行（与 test_genomics_mcp_builtins 同款）；
#    构建上下文未挂载时整模块 skip，避免收集期 ImportError 中断整套测试。
def _find_repo_root() -> Path | None:
    """向上找含 docker/mcp 的仓库根：容器内只挂 test/ 时返回 None（走 skip）。"""
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "docker" / "mcp").is_dir():
            return candidate
    return None


ROOT = _find_repo_root()
GENE_AUTHORITY = ROOT / "docker" / "mcp" / "gene-authority" / "gene_authority_mcp.py" if ROOT else None


def _load_sources():
    try:
        import gene_authority_mcp as module  # 镜像内运行路径

        return module
    except ImportError:
        pass
    if GENE_AUTHORITY is None or not GENE_AUTHORITY.is_file():
        pytest.skip(
            "gene-authority sources unavailable（在隔离镜像内运行，或挂载 docker 构建上下文）",
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


sources = _load_sources()


@unittest.skipUnless(os.getenv("YUXI_LIVE_OFFICIAL_SOURCES") == "1", "live official API probe is opt-in")
class LiveOfficialSourceTests(unittest.TestCase):
    def test_pride_project_and_files(self):
        candidates = sources.pride_search_projects_rest("rice", page_size=2)
        self.assertEqual(candidates["status"], "FOUND")
        accession = candidates["data"]["results"][0]["accession"]
        project = sources.pride_project_rest(accession)
        files = sources.pride_project_files_rest(accession, page_size=2)
        self.assertEqual(project["data"]["accession"], accession)
        self.assertEqual(files["provider"], "PRIDE")
        print(f"PRIDE verified accession={accession} file_count={len(files['data'].get('results', []))}")

    def test_entrez_search_summary_fetch(self):
        search = sources.ncbi_eutils_search_rest("bioproject", "Oryza sativa endosperm", retmax=1)
        self.assertEqual(search["status"], "FOUND")
        identifier = search["data"]["esearchresult"]["idlist"][0]
        summary = sources.ncbi_eutils_summary_rest("bioproject", [identifier])
        fetched = sources.ncbi_eutils_fetch_rest("bioproject", [identifier])
        self.assertEqual(summary["status"], "FOUND")
        self.assertTrue(fetched["data"]["xml"].lstrip().startswith("<?xml"))
        print(f"Entrez verified bioproject_uid={identifier} xml_sha256={fetched['data']['raw_sha256'][:12]}")

    def test_europe_pmc_oa_xml_passage(self):
        found = sources.europe_pmc_search_rest("OPEN_ACCESS:Y AND rice endosperm", page_size=5)
        records = found["data"].get("resultList", {}).get("result", [])
        pmcids = [record.get("pmcid") for record in records if record.get("pmcid")]
        self.assertTrue(pmcids, "Europe PMC returned no OA PMCID candidates")
        for pmcid in pmcids:
            passages = sources.europe_pmc_oa_passages_rest(pmcid, "rice", max_hits=2)
            if passages["status"] == "FOUND":
                locator = passages["data"]["passages"][0]["locator"]
                self.assertEqual(locator["kind"], "XML")
                self.assertNotIn("page", locator)
                print(f"Europe PMC verified pmcid={pmcid} matches={passages['data']['matches']}")
                return
        self.fail("OA XML retrieved but no exact rice passage found in five candidate articles")


if __name__ == "__main__":
    unittest.main()
