from __future__ import annotations

import pytest
from fastapi import HTTPException

from server.routers.knowledge_scope_router import _validate_retrieval_policy


def test_validate_retrieval_policy_accepts_known_keys():
    _validate_retrieval_policy(
        {
            "exact_first": True,
            "enumeration_exhaustive": False,
            "narrative_evidence_limit": 10,
            "display_limit": 20,
            "bounded_top_k": None,
        }
    )


def test_validate_retrieval_policy_rejects_unknown_key():
    with pytest.raises(HTTPException) as exc_info:
        _validate_retrieval_policy({"top_k": 5})
    assert exc_info.value.status_code == 422
    assert "未知字段" in str(exc_info.value.detail)


def test_validate_retrieval_policy_rejects_non_integer_limits():
    with pytest.raises(HTTPException) as exc_info:
        _validate_retrieval_policy({"narrative_evidence_limit": "auto"})
    assert exc_info.value.status_code == 422

    with pytest.raises(HTTPException):
        _validate_retrieval_policy({"bounded_top_k": 0})


def test_validate_retrieval_policy_rejects_non_boolean_flags():
    with pytest.raises(HTTPException):
        _validate_retrieval_policy({"exact_first": 1})
