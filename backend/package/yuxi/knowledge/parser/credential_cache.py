"""OCR 云服务凭证的 Redis 运行时缓存。"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from yuxi.storage.redis import sync_redis_client
from yuxi.utils.logging_config import logger

REDIS_CACHE_KEY = "yuxi:ocr_provider_credentials:v2"
# v1 缓存曾包含明文 Token，重建时顺手清除
_LEGACY_REDIS_CACHE_KEY = "yuxi:ocr_provider_credentials"
_CACHE_TTL_SECONDS = 5


@dataclass(frozen=True)
class OCRRuntimeCredential:
    service_id: str
    api_token: str
    api_base: str
    settings: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "service_id": self.service_id,
            "api_token": self.api_token,
            "api_base": self.api_base,
            "settings": self.settings,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OCRRuntimeCredential:
        return cls(
            service_id=data["service_id"],
            api_token=data.get("api_token", ""),
            api_base=data["api_base"],
            settings=data.get("settings") or {},
        )


class OCRCredentialCache:
    """在 API、任务进程和 Worker 之间同步 OCR 凭证。"""

    def __init__(self) -> None:
        self._local_cache: dict[str, OCRRuntimeCredential] | None = None
        self._local_cache_at = 0.0

    def get(self, service_id: str) -> OCRRuntimeCredential | None:
        return self._load_cache().get(service_id)

    def rebuild(self, providers: list[Any]) -> None:
        cache = {
            provider.service_id: OCRRuntimeCredential(
                service_id=provider.service_id,
                api_token=self._open_db_token(provider.api_token),
                api_base=provider.api_base,
                settings=dict(provider.settings_json or {}),
            )
            for provider in providers
            if provider.is_enabled
        }
        try:
            with sync_redis_client() as redis_client:
                redis_client.delete(_LEGACY_REDIS_CACHE_KEY)
        except Exception:
            pass

        self._save_cache(cache)
        self._invalidate_local()
        logger.info(f"OCR credential cache rebuilt: {len(cache)} services → Redis")

    @staticmethod
    def _open_db_token(token: str | None) -> str:
        from yuxi.utils.secret_crypto import decrypt_secret

        return decrypt_secret(token or None, "ocr-provider-db:mineru-official") or ""

    def _load_cache(self) -> dict[str, OCRRuntimeCredential]:
        now = time.monotonic()
        if self._local_cache is not None and now - self._local_cache_at < _CACHE_TTL_SECONDS:
            return self._local_cache

        try:
            with sync_redis_client() as redis_client:
                raw = redis_client.get(REDIS_CACHE_KEY)
            if not raw:
                cache: dict[str, OCRRuntimeCredential] = {}
            else:
                from yuxi.utils.secret_crypto import decrypt_secret

                payload = json.loads(raw)
                cache = {}
                for service_id, data in payload.items():
                    import json as _json

                    raw_settings = decrypt_secret(data.get("settings") or None, f"ocr-cache-settings:{service_id}")
                    cache[service_id] = OCRRuntimeCredential(
                        service_id=service_id,
                        api_token=decrypt_secret(data.get("api_token") or None, f"ocr-cache:{service_id}") or "",
                        api_base=data["api_base"],
                        settings=_json.loads(raw_settings) if raw_settings else {},
                    )
        except Exception as exc:
            logger.warning(f"Failed to load OCR credential cache from Redis: {exc}")
            return {}

        self._local_cache = cache
        self._local_cache_at = now
        return cache

    def _save_cache(self, cache: dict[str, OCRRuntimeCredential]) -> None:
        from yuxi.utils.secret_crypto import encrypt_secret

        payload = {}
        for service_id, credential in cache.items():
            data = credential.to_dict()
            # Redis 只落密文：Token 与 settings 均可能含敏感值
            data["api_token"] = encrypt_secret(credential.api_token, f"ocr-cache:{service_id}") or ""
            data["settings"] = encrypt_secret(
                json.dumps(credential.settings or {}, ensure_ascii=False), f"ocr-cache-settings:{service_id}"
            ) or ""
            payload[service_id] = data
        with sync_redis_client() as redis_client:
            redis_client.set(REDIS_CACHE_KEY, json.dumps(payload, ensure_ascii=False))

    def _invalidate_local(self) -> None:
        self._local_cache = None
        self._local_cache_at = 0.0


ocr_credential_cache = OCRCredentialCache()
