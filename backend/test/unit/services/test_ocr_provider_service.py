from __future__ import annotations

import pytest

from yuxi.services import ocr_provider_service
from yuxi.storage.postgres.models_business import OCRProviderConfig

pytestmark = pytest.mark.unit


def test_ocr_provider_response_never_exposes_token():
    provider = OCRProviderConfig(
        service_id="mineru_official",
        display_name="MinerU 官方 API",
        api_base="https://mineru.net/api/v4",
        api_token="secret-token",
        settings_json={"model_version": "vlm"},
        is_enabled=True,
    )

    payload = provider.to_dict()

    assert payload["token_configured"] is True
    assert "api_token" not in payload
    assert "secret-token" not in str(payload)


def test_normalize_mineru_config_defaults_to_recommended_vlm():
    payload = ocr_provider_service.normalize_mineru_config_payload({})

    assert payload == {"model_version": "vlm"}


def test_normalize_mineru_config_rejects_unknown_model_version():
    with pytest.raises(ValueError, match="model_version"):
        ocr_provider_service.normalize_mineru_config_payload({"model_version": "unknown"})


@pytest.mark.asyncio
async def test_save_mineru_config_preserves_existing_token_when_input_is_blank(monkeypatch):
    provider = OCRProviderConfig(
        service_id="mineru_official",
        display_name="MinerU 官方 API",
        api_base="https://mineru.net/api/v4",
        api_token="existing-token",
        settings_json={"model_version": "pipeline"},
        is_enabled=True,
    )

    async def fake_get(db, service_id):
        del db
        assert service_id == "mineru_official"
        return provider

    async def fake_update(db, current, data):
        del db
        for key, value in data.items():
            setattr(current, key, value)
        return current

    async def fake_test(api_token, api_base):
        assert api_token == "existing-token"
        assert api_base == "https://mineru.net/api/v4"
        return {"status": "healthy", "message": "MinerU 官方 API 连接正常"}

    monkeypatch.setattr(ocr_provider_service, "get_ocr_provider", fake_get)
    monkeypatch.setattr(ocr_provider_service, "update_ocr_provider", fake_update)
    monkeypatch.setattr(ocr_provider_service, "test_mineru_official_connection", fake_test)

    saved, health = await ocr_provider_service.save_mineru_official_config(
        object(), {"api_token": "", "model_version": "vlm"}, "superadmin"
    )

    assert saved.api_token == "existing-token"
    assert saved.settings_json == {"model_version": "vlm"}
    assert health["status"] == "healthy"


@pytest.mark.asyncio
async def test_bootstrap_imports_current_token_environment_once(monkeypatch):
    created = []
    monkeypatch.setenv("MINERU_API_TOKEN", "bootstrap-token")
    monkeypatch.setenv("MINERU_API_KEY", "legacy-token")

    async def fake_get(db, service_id):
        del db, service_id
        return None

    async def fake_create(db, data):
        del db
        created.append(data)
        return OCRProviderConfig(**data)

    monkeypatch.setattr(ocr_provider_service, "get_ocr_provider", fake_get)
    monkeypatch.setattr(ocr_provider_service, "create_ocr_provider", fake_create)

    await ocr_provider_service.ensure_builtin_ocr_provider_in_db(object())

    from yuxi.services.ocr_provider_service import _open_db_token

    assert created[0]["api_token"] != "bootstrap-token"
    assert _open_db_token(created[0]["api_token"]) == "bootstrap-token"
    assert created[0]["settings_json"] == {"model_version": "vlm"}
