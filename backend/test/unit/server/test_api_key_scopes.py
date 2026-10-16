"""Chat-only API keys must not inherit document or billing capabilities."""

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from server.utils.auth_middleware import require_scope

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("scopes", [None, [], ["chat"], ["literature:read"]])
async def test_api_key_without_billing_scope_is_denied(scopes):
    request = Request({"type": "http"})
    request.state.api_key_purpose = "automation"
    request.state.api_key_scopes = scopes
    with pytest.raises(HTTPException) as error:
        await require_scope("literature:billing")(request)
    assert error.value.status_code == 403


@pytest.mark.parametrize("purpose,scopes", [(None, None), ("desktop_legacy", []),
                                            ("automation", ["literature:billing"]), ("automation", ["*"])])
async def test_interactive_legacy_and_explicit_capabilities_preserve_access(purpose, scopes):
    request = Request({"type": "http"})
    request.state.api_key_purpose = purpose
    request.state.api_key_scopes = scopes
    await require_scope("literature:billing")(request)
