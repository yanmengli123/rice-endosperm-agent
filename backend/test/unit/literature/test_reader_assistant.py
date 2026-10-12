from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from yuxi.literature.reader_assistant import validate_reader_context


def context():
    return {
        "schema_version": 1,
        "rid": "paper-a",
        "source_revision_id": "revision-a",
        "reading_version": 3,
        "surface": "pdf",
        "action": "screenshot",
    }


def snapshot():
    return {
        "literature_binding": {
            "rid": "paper-a",
            "parse_revision_id": "revision-a",
            "reading_version": 3,
            "kb_id": "authorized-kb",
            "file_id": "authorized-file",
        },
        "effective_kb_ids": ["authorized-kb"],
    }


def test_ordinary_input_is_noop_and_does_not_mutate_scope():
    scope = {"effective_kb_ids": ["kb-other"]}
    before = deepcopy(scope)
    assert validate_reader_context(None, scope) is None
    assert scope == before


def test_reader_context_is_a_copy_and_never_expands_scope():
    value, scope = context(), snapshot()
    before = deepcopy(scope)
    result = validate_reader_context(value, scope)
    assert result == value and result is not value
    assert scope == before
    assert not {"evidence_id", "quote", "bbox", "page", "kb_id", "file_id"} & result.keys()


@pytest.mark.parametrize("key,value", [("rid", "other"), ("source_revision_id", "new"), ("reading_version", 4)])
def test_version_or_paper_change_fails_closed(key, value):
    capture = context()
    capture[key] = value
    with pytest.raises(HTTPException) as raised:
        validate_reader_context(capture, snapshot())
    assert raised.value.status_code == 409
    assert raised.value.detail["code"] == "literature_reader_source_changed"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(reading_version=True),
        lambda value: value.update(reading_version=-1),
        lambda value: value.update(schema_version=True),
        lambda value: value.update(surface=[]),
        lambda value: value.update(action="execute"),
        lambda value: value.update(evidence_id="forged"),
        lambda value: value.update(kb_id="other-tenant"),
        lambda value: value.update(source_revision_id={}),
        lambda value: value.pop("reading_version"),
    ],
)
def test_malformed_and_authority_fields_rejected(mutate):
    value = context()
    mutate(value)
    with pytest.raises(HTTPException) as raised:
        validate_reader_context(value, snapshot())
    assert raised.value.status_code == 422


@pytest.mark.parametrize("scope", [{}, {"literature_binding": None}, {"literature_binding": "forged"}])
def test_non_paper_session_cannot_accept_reader_metadata(scope):
    with pytest.raises(HTTPException) as raised:
        validate_reader_context(context(), scope)
    assert raised.value.status_code == 422


@pytest.mark.asyncio
async def test_bibliography_version_is_authoritative_without_adding_evidence_scope(monkeypatch):
    from yuxi.literature.session import bind_paper_evidence_scope, validate_frozen_paper_scope

    access = SimpleNamespace(rids={"paper-a"}, sources={}, reading_versions={"paper-a": 3})
    monkeypatch.setattr("yuxi.literature.access.build_access", AsyncMock(return_value=access))
    scope, _ = await bind_paper_evidence_scope(
        user=object(),
        tenant_id=1,
        binding={"kind": "paper", "rid": "paper-a"},
        snapshot={"effective_kb_ids": ["kb-other"], "members": []},
        mentions=None,
    )
    assert scope["literature_binding"]["reading_version"] == 3
    assert scope["effective_kb_ids"] == []
    assert scope["document_scope_ceiling"]["file_ids"] == []
    value = context()
    value["source_revision_id"] = None
    assert validate_reader_context(value, scope) == value
    await validate_frozen_paper_scope(user=object(), tenant_id=1, snapshot=scope)
    access.reading_versions["paper-a"] = 4
    with pytest.raises(PermissionError):
        await validate_frozen_paper_scope(user=object(), tenant_id=1, snapshot=scope)
