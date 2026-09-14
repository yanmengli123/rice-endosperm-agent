from types import SimpleNamespace

from yuxi.services.scientific_pdf_ingest_service import (
    _restore_active_file_state,
    _snapshot_active_file_state,
)


def _file_row(**overrides):
    values = {
        "active_parse_revision_id": "spr_active",
        "active_index_revision_id": "sir_active",
        "markdown_file": "minio://active/evidence.md",
        "status": "parsing",
        "processing_params": {"strategy": "scientific_pdf"},
        "evidence_status": "PENDING",
        "evidence_capabilities": {"state": "PENDING"},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_snapshot_uses_serving_revision_instead_of_transient_ingest_state():
    file_row = _file_row()
    active_revision = SimpleNamespace(
        status="INDEXED_FULL",
        capabilities={"pdf_highlight": True, "figure_image_locator": True},
        qa_report={"figure_index": {"locator_ready_assets": 5}},
    )

    snapshot = _snapshot_active_file_state(file_row, active_revision)

    assert snapshot["status"] == "parsed"
    assert snapshot["evidence_status"] == "INDEXED_FULL"
    assert snapshot["evidence_capabilities"] == {
        "state": "INDEXED_FULL",
        "pdf_highlight": True,
        "figure_image_locator": True,
        "qa": {"figure_index": {"locator_ready_assets": 5}},
    }


def test_restore_reinstates_previous_serving_projection_after_shadow_failure():
    file_row = _file_row()
    snapshot = {
        "active_parse_revision_id": "spr_active",
        "active_index_revision_id": "sir_active",
        "markdown_file": "minio://active/evidence.md",
        "status": "parsed",
        "processing_params": {"strategy": "scientific_pdf"},
        "evidence_status": "INDEXED_FULL",
        "evidence_capabilities": {"state": "INDEXED_FULL"},
    }
    file_row.active_parse_revision_id = "spr_candidate"
    file_row.active_index_revision_id = "sir_candidate"
    file_row.markdown_file = "minio://candidate/evidence.md"
    file_row.status = "error_indexing"
    file_row.evidence_status = "FAILED"

    _restore_active_file_state(file_row, snapshot)

    for key, value in snapshot.items():
        assert getattr(file_row, key) == value
