from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.routers import workspace_router
from server.utils.auth_middleware import get_required_user, get_db
from yuxi.services import pdf_evidence_service


def test_locator_checks_current_knowledge_permission_before_resolving(monkeypatch):
    app = FastAPI()
    app.include_router(workspace_router.workspace, prefix="/api")
    app.dependency_overrides[get_required_user] = lambda: SimpleNamespace(uid="u", role="user", department_id=1)
    access = AsyncMock(return_value=False)
    resolve = AsyncMock(return_value={"schema_version": "pdf-physical-locator.v1"})
    monkeypatch.setattr(workspace_router, "knowledge_base", SimpleNamespace(check_accessible=access))
    monkeypatch.setattr(pdf_evidence_service, "resolve_pdf_evidence", resolve)
    with TestClient(app) as client:
        params = {"kb_id": "private-kb", "file_id": "file", "span_id": "span", "parse_revision_id": "historical"}
        assert client.get("/api/workspace/knowledge/locator", params=params).status_code == 403
        resolve.assert_not_called()
        access.return_value = True
        assert client.get("/api/workspace/knowledge/locator", params=params).status_code == 200
        assert resolve.call_args.kwargs["parse_revision_id"] == "historical"
        resolve.side_effect = ValueError("PDF_SOURCE_REVISION_MISMATCH")
        assert client.get("/api/workspace/knowledge/locator", params=params).status_code == 404


def test_locator_requires_authentication():
    app = FastAPI()
    app.include_router(workspace_router.workspace, prefix="/api")
    # Missing credentials reject before any database access. Avoid opening a
    # process-global database pool in TestClient's separate event loop.
    app.dependency_overrides[get_db] = lambda: None
    with TestClient(app) as client:
        assert (
            client.get(
                "/api/workspace/knowledge/locator", params={"kb_id": "kb", "file_id": "file", "span_id": "span"}
            ).status_code
            == 401
        )
