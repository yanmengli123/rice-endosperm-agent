"""文献 LLM 任务入队前的供应商可达性预检。

仅向实际配置端点发送无鉴权 HEAD：不传论文、密钥，不调用聊天生成。
成功仅证明网络可达，不证明模型/凭据可用。真实生成仍经过冻结策略和
append-only 计量；不自动切换供应商。缓存按模型+端点隔离，不串用 BYOK。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from urllib.parse import urlsplit, urlunsplit

import httpx

_TTL_OK_SECONDS = 300.0
_TTL_FAIL_SECONDS = 45.0
_PROBE_TIMEOUT_SECONDS = 12.0
_CACHE_MAX_ENTRIES = 256

_cache: dict[str, tuple[float, bool, str]] = {}
_lock = asyncio.Lock()


class ModelPrecheckError(ValueError):
    """模型供应商不可达；消息面向用户，含切换模型引导。"""


async def _probe(endpoint: str) -> tuple[bool, str]:
    try:
        async with (
            asyncio.timeout(_PROBE_TIMEOUT_SECONDS),
            httpx.AsyncClient(timeout=_PROBE_TIMEOUT_SECONDS, follow_redirects=False) as client,
        ):
            response = await client.head(endpoint)
        if response.status_code >= 500:
            return False, "供应商网关暂不可用"
        # 401/404/405 are transport success, NOT authenticated model success.
        return True, ""
    except (TimeoutError, httpx.HTTPError) as exc:
        return False, type(exc).__name__


async def precheck_model_available(model_spec: str, *, base_url: str | None = None) -> None:
    """Bounded transport check; absent URL means no transport verdict."""
    spec = model_spec.strip()
    if not spec or not base_url:
        return
    parts = urlsplit(base_url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ModelPrecheckError("模型端点配置无效，请在 Yuxi 设置中修正")
    # Query/fragment can contain credentials. Never forward them in a probe.
    endpoint = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    key = hashlib.sha256((spec + "\n" + endpoint).encode()).hexdigest()
    now = time.monotonic()
    cached = _cache.get(key)
    if cached and cached[0] > now:
        if not cached[1]:
            raise ModelPrecheckError(_failure_message(spec, cached[2]))
        return
    async with _lock:
        cached = _cache.get(key)
        now = time.monotonic()
        if cached and cached[0] > now:
            if not cached[1]:
                raise ModelPrecheckError(_failure_message(spec, cached[2]))
            return
        ok, detail = await _probe(endpoint)
        ttl = _TTL_OK_SECONDS if ok else _TTL_FAIL_SECONDS
        if len(_cache) >= _CACHE_MAX_ENTRIES and key not in _cache:
            _cache.clear()  # 有界缓存：配置模型数远小于上限，整清足够
        _cache[key] = (time.monotonic() + ttl, ok, detail)
    if not ok:
        raise ModelPrecheckError(_failure_message(spec, detail))


def _failure_message(model_spec: str, detail: str) -> str:
    return (
        f"模型供应商不可达（{model_spec}）：{detail}。"
        "已取消入队且不计入重试预算；请在顶部模型选择或偏好设置中切换到可用模型后重试"
    )


def reset_precheck_cache() -> None:
    """测试钩子：清空进程内缓存。"""
    _cache.clear()


__all__ = [
    "ModelPrecheckError",
    "precheck_model_available",
    "reset_precheck_cache",
    "_failure_message",
]
