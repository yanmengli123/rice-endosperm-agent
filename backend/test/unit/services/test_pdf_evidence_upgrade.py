from types import SimpleNamespace

import pytest

from yuxi.services import scientific_pdf_ingest_service as service

pytestmark = pytest.mark.unit


def _file(**overrides):
    values = {
        "file_id": "file-1",
        "kb_id": "kb-1",
        "filename": "paper.pdf",
        "original_filename": None,
        "content_hash": "a" * 64,
        "processing_params": {"ocr_engine": "mineru_official"},
        "active_parse_revision_id": "rev-1",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _revision(**overrides):
    values = {
        "pipeline_version": service.PIPELINE_VERSION,
        "parser_fingerprint": "expected",
        "sealed_at": object(),
        "status": "INDEXED_FULL",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_upgrade_classifier_distinguishes_current_and_stale(monkeypatch):
    monkeypatch.setattr(service, "build_parser_fingerprint", lambda *_args, **_kwargs: "expected")

    current = service.classify_pdf_evidence_upgrade(_file(), _revision())
    assert current["upgrade_required"] is False
    assert current["reasons"] == []

    stale = service.classify_pdf_evidence_upgrade(
        _file(),
        _revision(
            pipeline_version="scientific_pdf_v3.1",
            parser_fingerprint="old",
            sealed_at=None,
            status="RUNNING",
        ),
    )
    assert stale["upgrade_required"] is True
    assert stale["reasons"] == [
        "PIPELINE_VERSION_OUTDATED",
        "PARSER_FINGERPRINT_OUTDATED",
        "ACTIVE_REVISION_NOT_SEALED",
        "ACTIVE_REVISION_NOT_INDEXED",
    ]


def test_upgrade_classifier_exposes_missing_revision_and_hash():
    result = service.classify_pdf_evidence_upgrade(
        _file(content_hash="", active_parse_revision_id=None),
        None,
    )
    assert result["expected_parser_fingerprint"] is None
    assert result["reasons"] == ["SOURCE_HASH_REQUIRES_BACKFILL", "NO_ACTIVE_PARSE_REVISION"]
