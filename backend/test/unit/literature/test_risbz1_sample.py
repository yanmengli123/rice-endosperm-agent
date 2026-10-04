"""Approved real PDF, offline archive integrity only; not OCR/provider acceptance."""

import hashlib
import io
import json
import os
from pathlib import Path

import pytest

from yuxi.literature.history_archive import _build_archive
from yuxi.literature.import_archive import _extract_archive, _validate_business_manifest

pytestmark = pytest.mark.unit
SHA256 = "2a8f03d092ca7f0b29f0bd798cb3e1621f940ffcdb5a2eb1a29a4b19829ce13f"


def test_risbz1_real_pdf_offline_archive_integrity(tmp_path):
    path = Path(os.getenv("RISBZ1_PDF_PATH", "/tmp/risbz1-acceptance.pdf"))
    if not path.is_file():
        pytest.skip("Approved local PDF fixture not mounted; never download a replacement")
    from pypdf import PdfReader

    content = path.read_bytes()
    assert hashlib.sha256(content).hexdigest() == SHA256
    reader = PdfReader(io.BytesIO(content))
    assert len(reader.pages) == 16
    text = reader.pages[0].extract_text()
    assert "RISBZ1" in text and "10.1016/j.xplc.2025.101458" in text
    files = {
        "knowledge_base/10.1016_j.xplc.2025.101458/source.pdf": content,
        "knowledge_base/_yuxi/current-business.json": b'{"fixture":"offline only"}',
    }
    manifest = {
        "schema": "yuxi-literature-business-backup-v1",
        "members": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
        "source_snapshot_sha256": hashlib.sha256(files["knowledge_base/_yuxi/current-business.json"]).hexdigest(),
    }
    biblio, chat = "data/biblio/biblio.db", "data/chat/chat.db"
    result = _build_archive(
        {biblio: {"papers": [{"id": 1}]}, chat: {"sessions": []}},
        files,
        json.dumps(manifest).encode(),
        {biblio: {"papers": ["id"]}, chat: {"sessions": ["id"]}},
        seal_database_digests=True,
    )
    archive = tmp_path / "risbz1.zip"
    archive.write_bytes(result)
    root = _extract_archive(archive, tmp_path / "unpacked")
    _validate_business_manifest(root)
    recovered = root / "knowledge_base/10.1016_j.xplc.2025.101458/source.pdf"
    assert recovered.read_bytes() == content
    assert hashlib.sha256(recovered.read_bytes()).hexdigest() == SHA256
