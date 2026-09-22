"""Opt-in smoke test of the pinned data-aggregator MCP artifact."""

from __future__ import annotations

import json
import os
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@unittest.skipUnless(os.getenv("YUXI_LIVE_DATA_AGGREGATOR") == "1", "live aggregator probe is opt-in")
class DataAggregatorLiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_only_discovery_tools_and_live_sources(self):
        parameters = StdioServerParameters(
            command="python", args=["/opt/yuxi/data_aggregator_entrypoint.py"], env=dict(os.environ)
        )
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                listing = await session.list_tools()
                names = {tool.name for tool in listing.tools}
                self.assertTrue({"search", "resolve", "list_sources"} <= names)
                sources = await session.call_tool("list_sources", {})
                self.assertFalse(sources.isError)
                text = " ".join(block.text for block in sources.content if block.type == "text")
                self.assertIn("bioproject", text.lower())
                search = await session.call_tool(
                    "search", {"query": "Oryza sativa endosperm", "sources": ["omics"], "size": 2}
                )
                self.assertFalse(search.isError, str(search.content)[:1200])
                results = " ".join(block.text for block in search.content if block.type == "text")
                payload = json.loads(results)
                self.assertGreater(payload["count"], 0)
                self.assertTrue(payload["results"][0]["id"])
                print(f"Aggregator verified tools={sorted(names)} first_id={payload['results'][0]['id']}")


if __name__ == "__main__":
    unittest.main()
