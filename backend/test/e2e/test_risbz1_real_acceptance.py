"""Opt-in, one-paper real-provider acceptance. No seeding, forged auth or forced retries.

Requires a normally authenticated test account, TEST_PDF_KB_ID (an existing
pdf_evidence source), and YUXI_RISBZ1_REAL_ACCEPTANCE=1. Keep all secrets in
test/.env.test. Mount/copy the approved PDF to RISBZ1_PDF_PATH (default /tmp).
This leaves the uploaded source/results for inspection; it never deletes them.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
import re
import time
from urllib.parse import quote

import pytest

PDF_NAME = "The bZIP transcription factor RISBZ1 balances grain filling and ER stress response in rice grains.pdf"
PDF_SHA256 = "2a8f03d092ca7f0b29f0bd798cb3e1621f940ffcdb5a2eb1a29a4b19829ce13f"
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.getenv("YUXI_RISBZ1_REAL_ACCEPTANCE") != "1",
        reason="Real-provider acceptance is opt-in and may transmit PDF content/incur costs",
    ),
]


async def test_risbz1_real_source_to_reader_and_l1(e2e_client, e2e_headers):
    kb_id = os.getenv("TEST_PDF_KB_ID", "")
    if not kb_id:
        pytest.skip("Configure TEST_PDF_KB_ID for an authorized existing pdf_evidence source")
    pdf = Path(os.getenv("RISBZ1_PDF_PATH", "/tmp/risbz1-acceptance.pdf"))
    assert pdf.is_file(), "Approved RISBZ1 PDF has not been mounted/copied into the container"
    content = pdf.read_bytes()
    assert hashlib.sha256(content).hexdigest() == PDF_SHA256, "Input is not the approved RISBZ1 PDF"
    client, headers = e2e_client, e2e_headers
    prefix = f"/api/knowledge/databases/{quote(kb_id, safe='')}"
    info = await client.get(prefix, headers=headers)
    assert info.status_code == 200, f"Source preflight failed: HTTP {info.status_code}"
    assert info.json().get("contract_key") == "pdf_evidence", "Selected source is not the PDF evidence contract"
    # Normal intake only. Do not create accounts/keys, activate releases, force
    # provider retries or alter configured models/budgets in an acceptance test.
    response = await client.post(
        "/api/knowledge/files/upload",
        params={"kb_id": kb_id},
        headers=headers,
        files={"file": (PDF_NAME, content, "application/pdf")},
    )
    assert response.status_code == 200, f"PDF upload failed: HTTP {response.status_code}; no automatic retry"
    upload = response.json()
    assert not upload.get("has_same_name"), "Existing same-name upload: stop without starting another paid pipeline"
    uploaded_path = upload.get("file_path")
    assert uploaded_path, "Upload response missing file identity; stop without retry"
    accepted = await client.post(
        prefix + "/documents",
        headers=headers,
        json={
            "items": [uploaded_path],
            "params": {
                "content_type": "file",
                "format_template": "pdf_literature",
                "pdf_evidence_pipeline": True,
                "scientific_processing_mode": "parse_compile",
                "source_paths": {uploaded_path: PDF_NAME},
            },
        },
    )
    assert accepted.status_code == 200, f"Intake failed: HTTP {accepted.status_code}; no automatic resubmission"
    items = accepted.json().get("result", {}).get("items", [])
    assert len(items) == 1 and items[0].get("status") != "failed", "Pipeline not durably accepted"
    file_id, revision_id = items[0]["file_id"], items[0]["parse_revision_id"]
    deadline = time.monotonic() + 900
    last = None
    while time.monotonic() < deadline:
        result = await client.get(prefix + f"/documents/{quote(file_id, safe='')}/evidence-status", headers=headers)
        assert result.status_code == 200, f"Status poll failed: HTTP {result.status_code}"
        last = next((r for r in result.json().get("revisions", []) if r["revision_id"] == revision_id), None)
        if last and last["status"] in {"INDEXED_FULL", "INDEXED_CONTENT_ONLY", "INDEXED_TEXT_ONLY"}:
            break
        assert not last or last["status"] not in {"FAILED", "REJECTED", "PARSED_ONLY"}, (
            f"Source pipeline failed or not activated: {last['status'] if last else 'missing'}"
        )
        await asyncio.sleep(3)
    else:
        pytest.fail(
            f"Source pipeline deadline exceeded; revision={revision_id}; retained for inspection, not resubmitted"
        )
    stages = {s["stage"]: s["status"] for s in last.get("stages", [])}
    assert stages.get("QUALITY") in {"SUCCEEDED", "REUSED"}, "Real output did not pass quality gate"
    assert stages.get("INDEX") in {"SUCCEEDED", "REUSED"}, "Embedding/index stage did not complete"
    print(
        f"Source accepted: revision={revision_id}; status={last['status']}; "
        f"reused={bool(last.get('reused_from_revision_id'))}; stages={stages}"
    )
    listing = await client.get("/api/literature/papers", params={"query": "RISBZ1", "page_size": 100}, headers=headers)
    assert listing.status_code == 200
    paper = next((p for p in listing.json().get("items", []) if p.get("source_revision_id") == revision_id), None)
    assert paper, "Activated source was not registered in the authorized literature domain"
    rid = quote(paper["rid"], safe="")
    reader_url = f"/api/literature/papers/{rid}"
    while time.monotonic() < deadline:
        reader = await client.get(reader_url + "/reader", headers=headers)
        assert reader.status_code == 200
        bundle = reader.json()
        if bundle.get("ready"):
            break
        await asyncio.sleep(3)
    else:
        pytest.fail("Activated source has no ready reading view; no forced render/provider retry")
    markdown = bundle.get("markdown", "")
    assert "RISBZ1" in markdown and len(markdown.strip()) > 1000, "Reader output lacks substantive RISBZ1 content"
    for uri in re.findall(r"!\[[^\]]*\]\((/api/(?:literature/assets|knowledge/[^)]+)[^)]+)\)", markdown)[:3]:
        image = await client.get(uri, headers=headers)
        assert image.status_code == 200 and image.content, "Protected figure is not accessible"
    source = await client.get(reader_url + "/source-pdf", headers=headers)
    assert source.status_code == 200 and hashlib.sha256(source.content).hexdigest() == PDF_SHA256
    # parse_compile itself schedules compilation. Poll it; never enqueue a
    # second LLM job merely because a slow provider has not finished yet.
    while time.monotonic() < deadline:
        note = await client.get(reader_url + "/notes/L1", headers=headers)
        assert note.status_code in {200, 404}
        if note.status_code == 200:
            break
        await asyncio.sleep(3)
    else:
        pytest.fail("No current L1 note within acceptance deadline; retained jobs not retried")
    text = await client.get("/api/literature/assets", params={"uri": note.json()["note_uri"]}, headers=headers)
    assert text.status_code == 200 and len(text.text.strip()) > 100, "L1 product missing/empty"
    print(f"Reader/PDF/L1 verified: rid={paper['rid']}; markdown_chars={len(markdown)}")
