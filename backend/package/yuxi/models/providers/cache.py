"""模型缓存服务 - 基于 Redis 的跨进程模型信息缓存。

本模块将数据库中的 model_providers 表数据序列化到 Redis，
供 API 和 Worker 等多进程同步读取，避免在同步函数中查询异步数据库。

模型 spec 格式: provider_id:model_id（冒号分隔）。model_id 允许包含斜杠。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from yuxi.storage.redis import sync_redis_client
from yuxi.utils.logging_config import logger

REDIS_CACHE_KEY = "yuxi:model_cache:v2"
# v1 缓存曾包含明文凭据，重建时顺手清除，避免残留
_LEGACY_REDIS_CACHE_KEY = "yuxi:model_cache"
_CACHE_TTL_SECONDS = 5


@dataclass(frozen=True)
class ModelInfo:
    """不可变的模型信息，供运行时使用。"""

    provider_id: str
    model_id: str
    model_type: str  # chat / embedding / rerank
    display_name: str

    # 运行时配置
    api_key: str
    base_url: str
    provider_type: str  # openai / anthropic / gemini / openrouter

    # 可选配置
    headers: dict[str, str] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    # Embedding 专属
    dimension: int | None = None
    batch_size: int = 40

    @property
    def spec(self) -> str:
        return f"{self.provider_id}:{self.model_id}"

    def to_dict(self) -> dict:
        return {
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "model_type": self.model_type,
            "display_name": self.display_name,
            "api_key": self.api_key,
            "base_url": self.base_url,
            "provider_type": self.provider_type,
            "headers": self.headers,
            "extra": self.extra,
            "dimension": self.dimension,
            "batch_size": self.batch_size,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ModelInfo:
        from yuxi.utils.secret_crypto import decrypt_secret

        spec = f"{data['provider_id']}:{data['model_id']}"
        return cls(
            provider_id=data["provider_id"],
            model_id=data["model_id"],
            model_type=data["model_type"],
            display_name=data["display_name"],
            api_key=decrypt_secret(data.get("api_key") or None, f"model-cache:{spec}") or "",
            base_url=data["base_url"],
            provider_type=data["provider_type"],
            headers={
                key: decrypt_secret(value, f"model-cache-hdr:{spec}:{key}") or ""
                for key, value in (data.get("headers") or {}).items()
                if value
            },
            extra=data.get("extra", {}),
            dimension=data.get("dimension"),
            batch_size=data.get("batch_size", 40),
        )


class ModelCache:
    """基于 Redis 的模型缓存，所有写入均走 Redis，保证跨进程一致。"""

    def __init__(self) -> None:
        self._local_cache: dict[str, ModelInfo] | None = None
        self._local_cache_at: float = 0.0

    def _load_cache(self) -> dict[str, ModelInfo]:
        now = time.monotonic()
        if self._local_cache is not None and (now - self._local_cache_at) < _CACHE_TTL_SECONDS:
            return self._local_cache

        try:
            with sync_redis_client() as redis_client:
                raw = redis_client.get(REDIS_CACHE_KEY)
            if not raw:
                self._local_cache = {}
                self._local_cache_at = now
                return {}

            items = json.loads(raw)
            cache = {spec: ModelInfo.from_dict(data) for spec, data in items.items()}
        except Exception as e:
            from yuxi.utils.secret_crypto import SecretCryptoError

            if isinstance(e, SecretCryptoError):
                raise
            logger.warning(f"Failed to load model cache from Redis: {e}")
            return {}

        self._local_cache = cache
        self._local_cache_at = now
        return cache

    def _invalidate_local(self) -> None:
        self._local_cache = None
        self._local_cache_at = 0.0

    def get_model_info(self, spec: str) -> ModelInfo | None:
        cache = self._load_cache()
        return cache.get(spec)

    def get_all_specs(self, model_type: str | None = None) -> list[ModelInfo]:
        cache = self._load_cache()
        if model_type is None:
            return list(cache.values())
        return [info for info in cache.values() if info.model_type == model_type]

    def get_specs_grouped_by_provider(self, model_type: str = "chat") -> dict[str, list[ModelInfo]]:
        cache = self._load_cache()
        grouped: dict[str, list[ModelInfo]] = {}
        for info in cache.values():
            if info.model_type != model_type:
                continue
            grouped.setdefault(info.provider_id, []).append(info)
        return grouped

    def rebuild(self, providers: list[Any]) -> None:
        from yuxi.models.providers.service import resolve_api_key, resolve_provider_headers

        try:
            with sync_redis_client() as redis_client:
                redis_client.delete(_LEGACY_REDIS_CACHE_KEY)
        except Exception:
            pass

        new_cache: dict[str, ModelInfo] = {}

        for provider in providers:
            if not provider.is_enabled:
                continue

            api_key = resolve_api_key(provider)

            for model in provider.enabled_models or []:
                model_type = model.get("type", "chat")
                base_url = model.get("base_url_override") or self._get_base_url_for_type(provider, model_type)

                info = ModelInfo(
                    provider_id=provider.provider_id,
                    model_id=model["id"],
                    model_type=model_type,
                    display_name=model.get("display_name", model["id"]),
                    api_key=api_key or "",
                    base_url=base_url,
                    provider_type=provider.provider_type,
                    headers=resolve_provider_headers(provider),
                    extra=dict(provider.extra_json or {}),
                    dimension=model.get("dimension"),
                    batch_size=model.get("batch_size", 40),
                )
                new_cache[info.spec] = info

        self._save_cache(new_cache)
        self._invalidate_local()
        logger.info(f"Model cache rebuilt: {len(new_cache)} models → Redis")

    def _save_cache(self, cache: dict[str, ModelInfo]) -> None:
        from yuxi.utils.secret_crypto import encrypt_secret

        # Redis 只落密文（AAD 绑定 spec），明文仅存在于进程内存的 ModelInfo 中。
        # 写入失败必须传递给调用方，禁止管理接口在运行时缓存未更新时返回成功。
        data = {}
        for spec, info in cache.items():
            payload = info.to_dict()
            payload["api_key"] = encrypt_secret(info.api_key, f"model-cache:{spec}") or ""
            payload["headers"] = {
                key: encrypt_secret(value, f"model-cache-hdr:{spec}:{key}") or ""
                for key, value in info.headers.items()
                if value
            }
            data[spec] = payload
        with sync_redis_client() as redis_client:
            redis_client.set(REDIS_CACHE_KEY, json.dumps(data, ensure_ascii=False))

    @staticmethod
    def _get_base_url_for_type(provider: Any, model_type: str) -> str:
        if model_type == "embedding" and provider.embedding_base_url:
            return provider.embedding_base_url
        if model_type == "rerank" and provider.rerank_base_url:
            return provider.rerank_base_url
        return provider.base_url


model_cache = ModelCache()


def resolve_model_spec(spec: str) -> ModelInfo:
    """根据 spec 返回 ModelInfo。"""
    if not spec:
        raise ValueError("model spec 不能为空")

    info = model_cache.get_model_info(spec)
    if info:
        return info

    all_specs = model_cache.get_all_specs()
    available = [item.spec for item in all_specs[:10]]
    raise ValueError(f"未找到模型: '{spec}'。可用模型 ({len(all_specs)}): {available}")
