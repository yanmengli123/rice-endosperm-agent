from __future__ import annotations

import json
from contextlib import contextmanager

import pytest

import yuxi.knowledge.parser.credential_cache as cache_module
from yuxi.knowledge.parser.credential_cache import OCRCredentialCache, REDIS_CACHE_KEY

pytestmark = pytest.mark.unit


class _FakeRedis:
    def __init__(self):
        self.data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def set(self, key: str, value: str) -> bool:
        self.data[key] = value
        return True


def _patch_redis(monkeypatch: pytest.MonkeyPatch, redis: _FakeRedis) -> None:
    @contextmanager
    def fake_sync_redis_client(*args, **kwargs):
        del args, kwargs
        yield redis

    monkeypatch.setattr(cache_module, "sync_redis_client", fake_sync_redis_client)


def test_ocr_credential_cache_round_trip_keeps_runtime_secret(monkeypatch):
    redis = _FakeRedis()
    _patch_redis(monkeypatch, redis)
    cache = OCRCredentialCache()

    class Provider:
        service_id = "mineru_official"
        api_token = "secret-token"
        api_base = "https://mineru.net/api/v4"
        settings_json = {"model_version": "vlm"}
        is_enabled = True

    cache.rebuild([Provider()])
    runtime = cache.get("mineru_official")

    assert runtime is not None
    assert runtime.api_token == "secret-token"
    assert runtime.settings["model_version"] == "vlm"
    assert json.loads(redis.data[REDIS_CACHE_KEY])["mineru_official"]["api_token"] == "secret-token"


def test_ocr_credential_cache_does_not_fallback_to_environment(monkeypatch):
    redis = _FakeRedis()
    _patch_redis(monkeypatch, redis)
    monkeypatch.setenv("MINERU_API_TOKEN", "environment-token")

    assert OCRCredentialCache().get("mineru_official") is None
