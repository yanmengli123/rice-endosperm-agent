import pytest
from fastapi import HTTPException
from starlette.requests import Request

from server.utils.auth_middleware import enforce_external_key_scope


@pytest.mark.parametrize("path,scopes,allowed", [
    ("/api/agent/runs", [], True),
    ("/api/agent/runs/x/result", ["agent:runs"], True),
    ("/api/agent/runs", ["knowledge:read"], False),
    ("/api/user/apikey", ["agent:runs", "knowledge:read"], False),
    ("/api/user/model-credentials", ["agent:runs"], False),
    ("/api/system/skills", ["agent:runs"], False),
])
def test_external_key_permissions_are_limited_even_for_privileged_owners(path, scopes, allowed):
    request = Request({"type": "http", "method": "POST", "path": path, "headers": [], "scheme": "http", "server": ("localhost", 80), "query_string": b""})
    request.state.api_key_purpose = "external_agent"
    request.state.api_key_scopes = scopes
    if allowed:
        enforce_external_key_scope(request)
    else:
        with pytest.raises(HTTPException) as error:
            enforce_external_key_scope(request)
        assert error.value.status_code == 403
