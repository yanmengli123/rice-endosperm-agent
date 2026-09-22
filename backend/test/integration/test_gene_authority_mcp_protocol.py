"""Live MCP protocol smoke test for the pinned gene-authority image."""

from __future__ import annotations

import os
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@unittest.skipUnless(os.getenv("YUXI_LIVE_OFFICIAL_SOURCES") == "1", "live official API probe is opt-in")
class GeneAuthorityProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_discovery_and_pride_record(self):
        parameters = StdioServerParameters(command="python", args=["/app/gene_authority_mcp.py"], env=dict(os.environ))
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                listed = await session.list_tools()
                names = {tool.name for tool in listed.tools}
                self.assertTrue({
                    "ncbi_eutils_search_rest", "ncbi_eutils_summary_rest", "ncbi_eutils_fetch_rest",
                    "pride_search_projects_rest", "pride_project_rest", "pride_project_files_rest",
                    "europe_pmc_oa_passages_rest",
                } <= names)
                result = await session.call_tool("pride_project_rest", {"accession": "PXD082271"})
                self.assertFalse(result.isError)
                self.assertIsInstance(result.structuredContent, dict)
                self.assertEqual(result.structuredContent["status"], "FOUND")
                self.assertEqual(result.structuredContent["data"]["accession"], "PXD082271")
                print(f"Gene Authority MCP verified tools={len(names)} structured_status=FOUND")


if __name__ == "__main__":
    unittest.main()
