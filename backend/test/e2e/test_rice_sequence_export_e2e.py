"""Live contract for deterministic RiceKB sequence export and FASTA download."""

from __future__ import annotations

import asyncio
import uuid
from urllib.parse import quote

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e]


async def test_wx_transcripts_are_returned_with_downloadable_fasta(
    e2e_client,
    e2e_headers,
    e2e_agent_context,
):
    agent_slug = e2e_agent_context["agent_slug"]
    thread_response = await e2e_client.post(
        "/api/chat/thread",
        json={
            "agent_id": agent_slug,
            "title": f"rice-sequence-e2e-{uuid.uuid4().hex[:8]}",
            "metadata": {"test": "rice-sequence-export"},
        },
        headers=e2e_headers,
    )
    assert thread_response.status_code == 200, thread_response.text
    thread_id = str(thread_response.json().get("thread_id") or thread_response.json().get("id"))

    request_id = f"rice-sequence-e2e-{uuid.uuid4()}"
    run_response = await e2e_client.post(
        "/api/agent/runs",
        json={
            "query": "Wx的转录本序列给我",
            "agent_slug": agent_slug,
            "thread_id": thread_id,
            "meta": {"request_id": request_id},
        },
        headers=e2e_headers,
    )
    assert run_response.status_code == 200, run_response.text
    run_id = str(run_response.json()["run_id"])

    result = None
    for _ in range(120):
        response = await e2e_client.get(f"/api/agent/runs/{run_id}/result", headers=e2e_headers)
        assert response.status_code == 200, response.text
        result = response.json()
        if result.get("status") in {"completed", "failed", "cancelled", "interrupted"}:
            break
        await asyncio.sleep(1)

    assert result is not None and result.get("status") == "completed", result
    output = str(result.get("output") or "")
    assert "未获取到可发布的数据值" not in output
    assert "MCP-F" not in output
    assert "Os06t0133000-01" in output
    assert "Os06t0133000-02" in output

    fasta_artifacts = [
        artifact for artifact in result.get("artifacts") or [] if str(artifact.get("name") or "").endswith(".fa")
    ]
    assert {artifact["name"] for artifact in fasta_artifacts} == {
        "Os06t0133000-01_transcript.fa",
        "Os06t0133000-02_transcript.fa",
    }
    for artifact in fasta_artifacts:
        encoded_path = "/".join(quote(part, safe="") for part in artifact["virtual_path"].split("/") if part)
        download = await e2e_client.get(
            f"/api/chat/thread/{thread_id}/artifacts/{encoded_path}?download=true",
            headers=e2e_headers,
        )
        assert download.status_code == 200, download.text
        assert download.content.startswith(b">Os06t0133000-")
