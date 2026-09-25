from __future__ import annotations

import json

import pytest

from yuxi.agents.mcp.host import McpToolResult
from yuxi.services.gene_authority_service import execute_official_link_query


@pytest.mark.asyncio
async def test_ncbi_official_link_executor_builds_arguments_without_model(monkeypatch):
    observed: list[tuple[str, dict]] = []

    async def fake_call(tool_name: str, arguments: dict) -> McpToolResult:
        observed.append((tool_name, arguments))
        return McpToolResult(
            text=json.dumps(
                {
                    "status": "FOUND",
                    "data": {
                        "reports": [
                            {
                                "gene": {
                                    "gene_id": 4340018,
                                    "symbol": "Wx",
                                    "description": "granule-bound starch synthase 1",
                                    "tax_id": 4530,
                                    "taxname": "Oryza sativa",
                                }
                            }
                        ]
                    },
                }
            )
        )

    monkeypatch.setattr("yuxi.services.gene_authority_service._call_gene_authority", fake_call)
    result = await execute_official_link_query("通过MCP服务，Wx的在NCBI上给我官网地址")

    assert result.succeeded is True
    assert result.records[0]["gene"]["gene_id"] == 4340018
    assert observed == [
        (
            "ncbi_datasets_gene_report_rest",
            {
                "identifiers": ["Wx"],
                "identifier_type": "symbol",
                "taxon": "Oryza sativa",
                "page_size": 20,
            },
        )
    ]


@pytest.mark.asyncio
async def test_ncbi_executor_rejects_cross_species_record(monkeypatch):
    async def fake_call(tool_name: str, arguments: dict) -> McpToolResult:
        return McpToolResult(
            text=json.dumps(
                {
                    "status": "FOUND",
                    "data": {
                        "reports": [
                            {
                                "gene": {
                                    "gene_id": 29482,
                                    "symbol": "Slc1a2",
                                    "tax_id": 10116,
                                    "taxname": "Rattus norvegicus",
                                }
                            }
                        ]
                    },
                }
            )
        )

    monkeypatch.setattr("yuxi.services.gene_authority_service._call_gene_authority", fake_call)
    result = await execute_official_link_query("Wx的在NCBI上给我官网地址")

    assert result.status == "CONFLICT"
    assert result.succeeded is False
    assert result.records == []
