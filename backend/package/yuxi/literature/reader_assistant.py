"""Version fence for optional reader-originated user input, never evidence."""

from __future__ import annotations

from yuxi.services.error_registry import http_error

_FIELDS = {"schema_version", "rid", "source_revision_id", "reading_version", "surface", "action"}
_SURFACES = {"original", "translation", "parallel", "note", "pdf", "image", "paper"}
_ACTIONS = {
    "explain",
    "translate",
    "summarize",
    "ask",
    "figure",
    "screenshot",
    "overview",
    "methods",
    "findings",
    "limitations",
}


def validate_reader_context(value: object, snapshot: dict) -> dict | None:
    """Compare only against the server-authorized binding, before persisting a run.

    Page hints, quotes, coordinates and evidence ids are deliberately absent.
    Ordinary requests without the optional field remain byte-for-byte unchanged.
    """
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise http_error("literature_reader_context_invalid")
    binding = snapshot.get("literature_binding")
    if (
        not isinstance(binding, dict)
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or not isinstance(value["rid"], str)
        or not value["rid"]
        or len(value["rid"]) > 200
        or type(value["reading_version"]) is not int
        or value["reading_version"] < 0
        or not isinstance(value["surface"], str)
        or value["surface"] not in _SURFACES
        or not isinstance(value["action"], str)
        or value["action"] not in _ACTIONS
        or (value["source_revision_id"] is not None and not isinstance(value["source_revision_id"], str))
        or (isinstance(value["source_revision_id"], str) and len(value["source_revision_id"]) > 200)
    ):
        raise http_error("literature_reader_context_invalid")
    if (
        value["rid"] != binding.get("rid")
        or value["source_revision_id"] != binding.get("parse_revision_id")
        or value["reading_version"] != binding.get("reading_version")
    ):
        raise http_error("literature_reader_source_changed")
    return dict(value)
