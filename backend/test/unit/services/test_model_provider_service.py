import os

import pytest

os.environ.setdefault("OPENAI_API_KEY", "test-key")

from yuxi.models.providers.builtin import BUILTIN_PROVIDERS
from yuxi.models.providers import service as provider_service
from yuxi.models.providers.service import (
    _normalize_payload,
    _normalize_remote_model,
    check_credential_status,
    ensure_builtin_model_providers_in_db,
    fetch_remote_models,
    resolve_api_key,
)
from yuxi.storage.postgres.models_business import ModelProvider


def test_normalize_payload_accepts_enabled_chat_model():
    payload = _normalize_payload(
        {
            "provider_id": "openrouter-local",
            "display_name": "OpenRouter Local",
            "base_url": "https://openrouter.ai/api/v1",
            "enabled_models": [{"id": "anthropic/claude-sonnet-4.5", "type": "chat"}],
        }
    )

    assert payload["provider_id"] == "openrouter-local"
    assert payload["provider_type"] == "openai"
    assert "models_endpoint" not in payload
    assert "embedding_models_endpoint" not in payload
    assert payload["enabled_models"][0]["display_name"] == "anthropic/claude-sonnet-4.5"


def test_normalize_payload_accepts_anthropic_provider_type():
    payload = _normalize_payload(
        {
            "provider_id": "xiaomi-token-plan",
            "display_name": "Xiaomi Token Plan",
            "provider_type": "anthropic",
            "base_url": "https://token-plan-cn.xiaomimimo.com/anthropic",
            "capabilities": ["chat"],
            "enabled_models": [{"id": "mimo-v2.5-pro", "type": "chat", "source": "manual"}],
        }
    )

    assert payload["provider_type"] == "anthropic"
    assert payload["enabled_models"][0]["id"] == "mimo-v2.5-pro"


def test_normalize_payload_rejects_unknown_enabled_model_type():
    with pytest.raises(ValueError, match="type 必须是"):
        _normalize_payload(
            {
                "provider_id": "openrouter-local",
                "display_name": "OpenRouter Local",
                "base_url": "https://openrouter.ai/api/v1",
                "enabled_models": [{"id": "unknown-model", "type": "unknown"}],
            }
        )


def test_normalize_payload_allows_embedding_without_dimension():
    """embedding 模型的 dimension 是可选字段，不提供也不会报错。"""
    payload = _normalize_payload(
        {
            "provider_id": "embedding-local",
            "display_name": "Embedding Local",
            "base_url": "https://example.com/v1",
            "capabilities": ["embedding"],
            "embedding_base_url": "https://example.com/v1/embeddings",
            "enabled_models": [{"id": "text-embedding", "type": "embedding"}],
        }
    )
    assert payload["provider_id"] == "embedding-local"
    assert payload["enabled_models"][0].get("dimension") is None


def test_normalize_remote_model_preserves_detailed_model_config():
    model = _normalize_remote_model(
        {
            "id": "xiaomi/mimo-v2-omni",
            "name": "Xiaomi: MiMo-V2-Omni",
            "context_length": 262144,
            "architecture": {
                "input_modalities": ["text", "audio", "image", "video"],
                "output_modalities": ["text"],
            },
            "top_provider": {"max_completion_tokens": 65536},
            "supported_parameters": ["temperature", "tools"],
        }
    )

    assert model["id"] == "xiaomi/mimo-v2-omni"
    assert model["display_name"] == "Xiaomi: MiMo-V2-Omni"
    assert model["type"] == "chat"
    assert model["input_modalities"] == ["text", "audio", "image", "video"]
    assert model["max_completion_tokens"] == 65536
    assert model["raw_metadata"]["supported_parameters"] == ["temperature", "tools"]


def test_normalize_remote_model_uses_endpoint_model_type():
    model = _normalize_remote_model({"id": "BAAI/bge-m3", "object": "model"}, "embedding")

    assert model["id"] == "BAAI/bge-m3"
    assert model["type"] == "embedding"


@pytest.mark.asyncio
async def test_fetch_remote_models_loads_embedding_only_when_capability_enabled(monkeypatch):
    calls = []

    async def fake_fetch(client, provider, headers, endpoint, model_type):
        calls.append((endpoint, model_type))
        return [{"id": f"{model_type}-model", "type": model_type}]

    monkeypatch.setattr("yuxi.models.providers.service._fetch_models_from_endpoint", fake_fetch)

    class Provider:
        base_url = "https://example.com/v1"
        api_key = None
        api_key_env = None
        headers_json = {}
        capabilities = ["chat", "embedding", "rerank"]
        models_endpoint = "/models"
        embedding_models_endpoint = "/embeddings/models"
        rerank_models_endpoint = None

    models = await fetch_remote_models(Provider())

    assert calls == [("/models", "chat"), ("/embeddings/models", "embedding")]
    assert [model["type"] for model in models] == ["chat", "embedding"]


def test_normalize_payload_rejects_ollama_provider_type():
    with pytest.raises(ValueError, match="provider_type 必须是"):
        _normalize_payload(
            {
                "provider_id": "ollama-local",
                "display_name": "Ollama Local",
                "provider_type": "ollama",
                "base_url": "http://localhost:11434",
            }
        )


def test_builtin_provider_templates_default_to_openai_provider_type():
    assert len(BUILTIN_PROVIDERS) >= 16
    provider_types = {
        _normalize_payload(
            {
                "provider_id": provider["provider_id"],
                "display_name": provider["display_name"],
                "base_url": provider["base_url"],
                "provider_type": provider.get("provider_type"),
            }
        )["provider_type"]
        for provider in BUILTIN_PROVIDERS
    }
    assert provider_types == {"openai"}
    assert all("ollama" not in provider["provider_id"] for provider in BUILTIN_PROVIDERS)


def test_builtin_siliconflow_provider_includes_default_runnable_models():
    provider = next(item for item in BUILTIN_PROVIDERS if item["provider_id"] == "siliconflow-cn")
    models = {model["id"]: model for model in provider["enabled_models"]}

    assert provider["capabilities"] == ["chat", "embedding", "rerank"]
    assert provider["embedding_base_url"] == "https://api.siliconflow.cn/v1/embeddings"
    assert provider["rerank_base_url"] == "https://api.siliconflow.cn/v1/rerank"
    assert models["Pro/BAAI/bge-m3"]["type"] == "embedding"
    assert models["Pro/BAAI/bge-m3"]["dimension"] == 1024
    assert "base_url_override" not in models["Pro/BAAI/bge-m3"]
    assert models["Pro/BAAI/bge-reranker-v2-m3"]["type"] == "rerank"
    assert "base_url_override" not in models["Pro/BAAI/bge-reranker-v2-m3"]


def test_builtin_dashscope_provider_includes_default_embedding_and_rerank_models():
    provider = next(item for item in BUILTIN_PROVIDERS if item["provider_id"] == "alibaba")
    models = {model["id"]: model for model in provider["enabled_models"]}

    assert provider["capabilities"] == ["chat", "embedding", "rerank"]
    assert provider["embedding_base_url"] == "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings"
    assert provider["rerank_base_url"] == "https://dashscope.aliyuncs.com/compatible-api/v1/reranks"
    assert "embedding_models_endpoint" not in provider
    assert "rerank_models_endpoint" not in provider
    assert models["text-embedding-v4"]["type"] == "embedding"
    assert models["text-embedding-v4"]["dimension"] == 1024
    assert models["qwen3-rerank"]["type"] == "rerank"


def testcheck_credential_status_disabled_provider_always_ok():
    """未启用的 provider 无论凭证如何配置，状态始终为 ok。"""

    class Provider:
        is_enabled = False
        api_key = None
        api_key_env = None

    assert check_credential_status(Provider()) == "ok"


def testcheck_credential_status_direct_api_key_ok():
    """直接配置了 api_key 的启用 provider 状态为 ok。"""

    class Provider:
        is_enabled = True
        api_key = "sk-test"
        api_key_env = None

    assert check_credential_status(Provider()) == "ok"


def test_runtime_credential_does_not_fallback_to_environment(monkeypatch):
    """运行时只读取供应商页面落库的密钥，不再从环境变量回退。"""
    monkeypatch.setenv("TEST_API_KEY", "exists")

    class Provider:
        is_enabled = True
        api_key = None
        api_key_env = "TEST_API_KEY"

    provider = Provider()

    assert resolve_api_key(provider) is None
    assert check_credential_status(provider) == "warning"


def testcheck_credential_status_both_empty_warning():
    """api_key 和 api_key_env 都未配置时状态为 warning。"""

    class Provider:
        is_enabled = True
        api_key = None
        api_key_env = None

    assert check_credential_status(Provider()) == "warning"


@pytest.mark.asyncio
async def test_builtin_credential_migration_preserves_page_key_and_clears_env_reference(monkeypatch):
    """升级时页面已保存的密钥优先，旧环境变量引用只清理、不覆盖。"""
    monkeypatch.setenv("TEST_PROVIDER_KEY", "env-key")
    monkeypatch.setattr(
        provider_service,
        "BUILTIN_PROVIDERS",
        [{"provider_id": "test-provider", "bootstrap_api_key_env": "TEST_PROVIDER_KEY"}],
    )
    provider = type(
        "Provider",
        (),
        {
            "provider_id": "test-provider",
            "api_key": "page-key",
            "api_key_env": "TEST_PROVIDER_KEY",
            "enabled_models": [],
            "capabilities": [],
            "updated_by": None,
        },
    )()

    class Db:
        async def flush(self):
            return None

    monkeypatch.setattr(provider_service, "list_model_providers", lambda _db: _async_value([provider]))

    await ensure_builtin_model_providers_in_db(Db())

    assert provider.api_key == "page-key"
    assert provider.api_key_env is None


@pytest.mark.asyncio
async def test_builtin_credential_migration_imports_env_once(monkeypatch):
    """旧记录没有页面密钥时，将旧环境变量值一次性导入数据库并清除引用。"""
    monkeypatch.setenv("TEST_PROVIDER_KEY", "env-key")
    monkeypatch.setattr(
        provider_service,
        "BUILTIN_PROVIDERS",
        [{"provider_id": "test-provider", "bootstrap_api_key_env": "TEST_PROVIDER_KEY"}],
    )
    provider = type(
        "Provider",
        (),
        {
            "provider_id": "test-provider",
            "api_key": None,
            "api_key_env": "TEST_PROVIDER_KEY",
            "enabled_models": [],
            "capabilities": [],
            "updated_by": None,
        },
    )()

    class Db:
        async def flush(self):
            return None

    monkeypatch.setattr(provider_service, "list_model_providers", lambda _db: _async_value([provider]))

    await ensure_builtin_model_providers_in_db(Db())

    from yuxi.utils.secret_crypto import decrypt_secret, is_encrypted

    assert is_encrypted(provider.api_key)
    assert decrypt_secret(provider.api_key, "model-provider-db:test-provider") == "env-key"
    assert provider.api_key_env is None


@pytest.mark.asyncio
async def test_new_builtin_provider_imports_bootstrap_key_without_storing_env_reference(monkeypatch):
    """首次安装可导入 .env，但新记录只保存实际密钥。"""
    monkeypatch.setenv("TEST_PROVIDER_KEY", "bootstrap-key")
    monkeypatch.setattr(
        provider_service,
        "BUILTIN_PROVIDERS",
        [
            {
                "provider_id": "test-provider",
                "display_name": "Test Provider",
                "base_url": "https://example.com/v1",
                "bootstrap_api_key_env": "TEST_PROVIDER_KEY",
            }
        ],
    )
    captured = {}

    async def fake_create(_db, payload):
        captured.update(payload)

    monkeypatch.setattr(provider_service, "list_model_providers", lambda _db: _async_value([]))
    monkeypatch.setattr(provider_service, "create_model_provider", fake_create)

    await ensure_builtin_model_providers_in_db(object())

    assert captured["api_key"] == "bootstrap-key"
    assert "api_key_env" not in captured


def test_provider_serialization_never_exposes_secret():
    provider = ModelProvider(
        provider_id="test-provider",
        display_name="Test Provider",
        base_url="https://example.com/v1",
        api_key="secret-value",
        api_key_env="LEGACY_KEY",
        headers_json={"Authorization": "enc.v1:ciphertext"},
    )

    payload = provider.to_dict()

    assert "api_key" not in payload
    assert "api_key_env" not in payload
    assert payload["api_key_configured"] is True
    assert payload["headers_json"] == {"Authorization": "__YUXI_SECRET_UNCHANGED__"}
    assert "ciphertext" not in str(payload)


async def _async_value(value):
    return value


# ==================== 手动添加模型 / source 字段 ====================


def test_normalize_payload_default_model_source_is_remote():
    """未显式指定 source 时，规范化后默认填入 remote，向后兼容旧数据。"""
    payload = _normalize_payload(
        {
            "provider_id": "openrouter-local",
            "display_name": "OpenRouter Local",
            "base_url": "https://openrouter.ai/api/v1",
            "enabled_models": [{"id": "anthropic/claude-sonnet-4.5", "type": "chat"}],
        }
    )

    assert payload["enabled_models"][0]["source"] == "remote"


def test_normalize_payload_accepts_manual_source():
    """source=manual 表示管理员手动添加的模型，规范化保留该标签。"""
    payload = _normalize_payload(
        {
            "provider_id": "custom-local",
            "display_name": "Custom Local",
            "base_url": "https://example.com/v1",
            "capabilities": ["chat"],
            "enabled_models": [{"id": "my-chat-model", "type": "chat", "source": "manual"}],
        }
    )

    assert payload["enabled_models"][0]["source"] == "manual"


def test_normalize_payload_rejects_invalid_source():
    """source 仅允许 manual 或 remote，其他取值视为非法。"""
    with pytest.raises(ValueError, match="source 必须是"):
        _normalize_payload(
            {
                "provider_id": "custom-local",
                "display_name": "Custom Local",
                "base_url": "https://example.com/v1",
                "enabled_models": [{"id": "x", "type": "chat", "source": "custom"}],
            }
        )


def test_normalize_payload_rejects_model_type_not_in_capabilities():
    """provider 仅声明 chat 能力时，不允许写入 embedding 类型的模型。"""
    with pytest.raises(ValueError, match="不在 provider 能力"):
        _normalize_payload(
            {
                "provider_id": "chat-only",
                "display_name": "Chat Only",
                "base_url": "https://example.com/v1",
                "capabilities": ["chat"],
                "enabled_models": [{"id": "rogue-embedding", "type": "embedding", "dimension": 1024}],
            }
        )


def test_normalize_payload_allows_model_type_within_capabilities():
    """provider 同时声明 chat + embedding 时，两类模型均可正常写入。"""
    payload = _normalize_payload(
        {
            "provider_id": "multi-cap",
            "display_name": "Multi Cap",
            "base_url": "https://example.com/v1",
            "capabilities": ["chat", "embedding"],
            "embedding_base_url": "https://example.com/v1/embeddings",
            "embedding_models_endpoint": "/embeddings/models",
            "enabled_models": [
                {"id": "chat-1", "type": "chat", "source": "manual"},
                {
                    "id": "embed-1",
                    "type": "embedding",
                    "source": "manual",
                    "dimension": 1024,
                },
            ],
        }
    )

    types = [model["type"] for model in payload["enabled_models"]]
    sources = [model["source"] for model in payload["enabled_models"]]
    assert types == ["chat", "embedding"]
    assert sources == ["manual", "manual"]
