import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.routers.source_annotation_router import annotations
from server.utils.auth_middleware import get_db, get_required_user
from yuxi.services import source_annotation_service as service


def test_raw_markdown_lines_preserve_blanks_and_exclude_executable_content():
    answer = (
        '\n\nA claim.\n\n```widget\n{"type":"chart"}\n```\n\n![caption](image.png)\n'
        "\n<div>preview</div>\n\n$$x=2$$\n\n|A|B|\n|---|---|\n|1|2|\n"
    )
    lines = {line["line"] for line in service.answer_lines(answer)}
    assert {3, 9, 15, 16, 17} <= lines
    assert not lines & {1, 5, 6, 7, 11, 13}


@pytest.mark.parametrize(
    "changes",
    [
        {"source_id": "invented"},
        {"quote": "invented"},
        {"answer_start_line": 0},
        {"answer_end_line": 9},
        {"answer_start_line": 4},
        {"page": 1},
        {"answer_start_line": True},
    ],
)
def test_rejects_fabricated_sources_quotes_lines_and_geometry(changes):
    match = {"answer_start_line": 1, "answer_end_line": 1, "source_id": "source", "quote": "verified text"}
    match.update(changes)
    with pytest.raises(ValueError):
        service.validate_matches(
            json.dumps({"citations": [match]}),
            "A claim.\n\n```py\ncode\n```",
            [{"id": "source", "content": "verified text"}],
        )


def test_matches_deduplicate_and_current_visibility_never_reconstructs_location():
    import hashlib

    match = {"answer_start_line": 1, "answer_end_line": 1, "source_id": "s", "quote": "verified text"}
    sources = [{"id": "s", "content": "verified text", "evidence": {"source": {"parse_revision_id": "original"}}}]
    assert service.validate_matches(json.dumps({"citations": [match, match]}), "A claim.", sources) == [match]
    saved = {"answer_hash": hashlib.sha256(b"A claim.").hexdigest(), "citations": [match]}
    result = service._visible(saved, answer="A claim.", sources=[])
    assert result["citations"] == [] and result["hidden_count"] == 1
    assert service._visible(saved, answer="Changed answer", sources=sources) is None
    raw = "\n\nA claim."
    saved = {
        "answer_hash": hashlib.sha256(raw.encode()).hexdigest(),
        "citations": [{**match, "answer_start_line": 3, "answer_end_line": 3}],
    }
    visible = service._visible(saved, answer=raw, sources=sources)
    assert visible["citations"][0]["answer_excerpt"] == "A claim."
    assert visible["sources"][0]["evidence"]["source"]["parse_revision_id"] == "original"


@pytest.mark.parametrize("purpose", ["external_agent", "desktop_legacy"])
def test_annotations_reject_all_api_key_purposes(monkeypatch, purpose):
    app = FastAPI()
    app.include_router(annotations)

    @app.middleware("http")
    async def identity(request, call_next):
        request.state.api_key_purpose = purpose
        return await call_next(request)

    app.dependency_overrides[get_required_user] = lambda: SimpleNamespace(uid="alice")
    app.dependency_overrides[get_db] = lambda: None
    client = TestClient(app)
    assert client.get("/agent/runs/run/source-annotations").status_code == 403
    assert client.post("/agent/runs/run/source-annotations").status_code == 403
