from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.routers import system_router
from server.routers.system_router import MinerUConfigPayload, system
from yuxi.storage.postgres.models_business import OCRProviderConfig

pytestmark = pytest.mark.unit


def test_discovery_endpoint_is_public(monkeypatch):
    monkeypatch.setattr("server.routers.system_router.get_version", lambda: "0.7.1.dev0")

    app = FastAPI()
    app.include_router(system, prefix="/api")
    response = TestClient(app).get("/api/system/discovery")

    assert response.status_code == 200
    payload = response.json()
    assert payload["name"] == "稻芯智析"
    assert payload["version"] == "0.7.1.dev0"
    assert payload["api_prefix"] == "/api"
    assert payload["capabilities"]["cli"]["browser_login"] is True
    assert payload["capabilities"]["cli"]["api_key_auth"] is True
    assert payload["capabilities"]["cli"]["kb_upload"] is True
    assert payload["endpoints"]["cli_auth_sessions"] == "/api/auth/cli/sessions"


@pytest.mark.asyncio
async def test_update_mineru_provider_commits_before_cache_and_sets_default(monkeypatch):
    calls = []
    provider = OCRProviderConfig(
        service_id="mineru_official",
        display_name="MinerU 官方 API",
        api_base="https://mineru.net/api/v4",
        api_token="secret-token",
        settings_json={"model_version": "vlm"},
        is_enabled=True,
    )

    class Db:
        async def commit(self):
            calls.append("commit")

    class RuntimeConfig:
        default_ocr_engine = "rapid_ocr"

        def set_value(self, key, value):
            calls.append(("set", key, value))
            self.default_ocr_engine = value

        def save(self):
            calls.append("save-config")

    async def fake_save(db, data, username):
        del db, data
        calls.append(("save-provider", username))
        return provider, {"status": "healthy", "message": "MinerU 官方 API 连接正常"}

    async def fake_list(db):
        del db
        return [provider]

    monkeypatch.setattr("yuxi.services.ocr_provider_service.save_mineru_official_config", fake_save)
    monkeypatch.setattr("yuxi.services.ocr_provider_service.get_all_ocr_providers", fake_list)
    monkeypatch.setattr(
        "yuxi.knowledge.parser.credential_cache.ocr_credential_cache.rebuild",
        lambda providers: calls.append(("rebuild", len(providers))),
    )
    monkeypatch.setattr(system_router, "config", RuntimeConfig())

    result = await system_router.update_mineru_official_provider(
        MinerUConfigPayload(model_version="vlm", set_as_default=True),
        current_user=type("User", (), {"username": "superadmin"})(),
        db=Db(),
    )

    assert result["success"] is True
    assert result["data"]["token_configured"] is True
    assert "api_token" not in result["data"]
    assert calls == [
        ("save-provider", "superadmin"),
        "commit",
        ("rebuild", 1),
        ("set", "default_ocr_engine", "mineru_official"),
        "save-config",
    ]
