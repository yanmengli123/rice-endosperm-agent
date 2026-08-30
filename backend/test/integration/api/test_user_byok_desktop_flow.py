"""Desktop API Key + user-scoped BYOK contract against the live Yuxi API."""

from __future__ import annotations

import json

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.auth]


@pytest.mark.asyncio
async def test_new_user_api_key_can_import_byok_and_access_default_agent(test_client, standard_user):
    user = standard_user["user"]
    api_key = user.get("api_key_secret")
    assert isinstance(api_key, str) and api_key.startswith("yxkey_")
    key_headers = {"Authorization": f"Bearer {api_key}"}

    quota_response = await test_client.get("/api/user/quota", headers=key_headers)
    assert quota_response.status_code == 200, quota_response.text
    quota = quota_response.json()
    assert quota["model_access_policy"] == "byok_optional"
    assert quota["byok_platform_token_exempt"] is True

    agent_response = await test_client.get("/api/agent/default", headers=key_headers)
    assert agent_response.status_code == 200, agent_response.text
    assert agent_response.json()["agent"]["slug"] == "default-chatbot"

    import_response = await test_client.post(
        "/api/user/model-credentials/import",
        headers=key_headers,
        json={
            "configuration": json.dumps(
                {
                    "env": {
                        "ANTHROPIC_API_KEY": "integration-placeholder",
                        "ANTHROPIC_BASE_URL": "https://models.example.org",
                        "ANTHROPIC_MODEL": "enterprise-test-model",
                    }
                }
            ),
            "activate_as_default": True,
        },
    )
    assert import_response.status_code == 200, import_response.text
    imported = import_response.json()
    assert imported["model_spec"] == "user-anthropic-compatible:enterprise-test-model"
    assert "integration-placeholder" not in import_response.text

    preference_response = await test_client.get("/api/user/model-preference", headers=key_headers)
    assert preference_response.status_code == 200, preference_response.text
    assert preference_response.json()["chat_model_spec"] == imported["model_spec"]

    credentials_response = await test_client.get("/api/user/model-credentials", headers=key_headers)
    assert credentials_response.status_code == 200, credentials_response.text
    assert credentials_response.json()["credentials"][0]["masked_hint"]
    assert "integration-placeholder" not in credentials_response.text
