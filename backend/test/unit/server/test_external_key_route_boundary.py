import pytest
from fastapi import HTTPException
from starlette.requests import Request

from server.utils.auth_middleware import enforce_external_key_scope


@pytest.mark.parametrize(
    "path,scopes,allowed",
    [
        ("/api/agent/runs", [], True),
        ("/api/agent/runs/x/result", ["agent:runs"], True),
        ("/api/agent/runs", ["knowledge:read"], False),
        ("/api/user/apikey", ["agent:runs", "knowledge:read"], False),
        ("/api/user/model-credentials", ["agent:runs"], False),
        ("/api/system/skills", ["agent:runs"], False),
    ],
)
def test_external_key_permissions_are_limited_even_for_privileged_owners(path, scopes, allowed):
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [],
            "scheme": "http",
            "server": ("localhost", 80),
            "query_string": b"",
        }
    )
    request.state.api_key_purpose = "external_agent"
    request.state.api_key_scopes = scopes
    if allowed:
        enforce_external_key_scope(request)
    else:
        with pytest.raises(HTTPException) as error:
            enforce_external_key_scope(request)
        assert error.value.status_code == 403


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/agent/research"),
        ("POST", "/api/agent-invocation/eval/runs"),
    ],
)
def test_existing_agent_read_and_evaluation_entries_accept_run_scope(method, path):
    request = Request({"type": "http", "method": method, "path": path, "headers": []})
    request.state.api_key_purpose, request.state.api_key_scopes = "external_agent", ["agent:runs"]
    enforce_external_key_scope(request)


@pytest.mark.parametrize("purpose", [None, "desktop_legacy"])
def test_existing_session_and_desktop_purpose_keep_their_permission_checks(purpose):
    request = Request({"type": "http", "method": "PUT", "path": "/api/system/skills/demo", "headers": []})
    request.state.api_key_purpose = purpose
    enforce_external_key_scope(request)
