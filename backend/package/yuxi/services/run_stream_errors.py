"""运行流（SSE error chunk）错误类型归一。

与 ``error_registry`` 的分工：注册表管 HTTP API 错误（``detail.code``，随
error_bodies.json 契约语料同步桌面端）；本模块管**流式 error chunk 的
``error_type`` 词汇表**——桌面端/Web 前端按 ``error_type`` + ``retryable``
渲染，不经过注册表。

分工两层：
- 源头分类（强信号）：chat_service 捕获异常时用 ``is_model_connection_error``
  沿异常因果链做类型名匹配；
- 防御归一（弱信号）：run_worker 的 chunk 映射层对历史/旁路生产者构造的
  error chunk 做文本匹配兜底，任何生产者漏分类时用户看到的仍是正确类别。

背景：模型供应商连接中断（APIConnectionError，ModelRetryMiddleware 重试耗尽
后整 run 失败）曾被统一超时文案（"服务端长时间未收到检索或模型输出…"）掩盖，
把网络故障误导成检索/看门狗问题。
"""

from __future__ import annotations

import re
from typing import Any

MODEL_CONNECTION_ERROR = "model_connection_error"
MODEL_CONNECTION_ERROR_MESSAGE = "模型服务连接中断，本次回答未完成，请稍后重试。"

# 未细分类的通用 error_type：归一层只对这几种做文本兜底，已分类的不覆盖。
_GENERIC_ERROR_TYPES = frozenset({"", "unexpected_error", "stream_error", "error"})

# 连接类异常的类型名（跨供应商：openai 的 APIConnectionError 同时继承内建
# ConnectionError；httpx 的 ConnectError/ConnectTimeout 不继承，故按名匹配）。
_CONNECTION_EXCEPTION_NAMES = frozenset(
    {
        "APIConnectionError",
        "APITimeoutError",
        "ConnectError",
        "ConnectTimeout",
        "ConnectionError",
        "ConnectionResetError",
        "ConnectionAbortedError",
    }
)

# 消息文本兜底标记（小写子串匹配）。
_CONNECTION_MESSAGE_MARKERS = (
    "apiconnectionerror",
    "apitimeouterror",
    "connection error",
    "connection refused",
    "connection reset",
    "connection aborted",
    "connection timed out",
    "connect timeout",
    "getaddrinfo",
    "name or service not known",
    "network unreachable",
    "remote end closed connection",
    "ssl eof occurred",
    "all connection attempts failed",
)

_CONNECTION_MARKER_RE = re.compile("|".join(re.escape(marker) for marker in _CONNECTION_MESSAGE_MARKERS))


def _iter_exception_chain(exc: BaseException, *, max_depth: int = 8):
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(seen) < max_depth:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def is_model_connection_error(exc: BaseException) -> bool:
    """判断异常（及其因果链）是否模型服务连接类失败。"""
    for item in _iter_exception_chain(exc):
        if type(item).__name__ in _CONNECTION_EXCEPTION_NAMES:
            return True
        message = str(item)
        if message and _CONNECTION_MARKER_RE.search(message.lower()):
            return True
    return False


def is_connection_error_text(text: Any) -> bool:
    message = str(text or "")
    return bool(message) and bool(_CONNECTION_MARKER_RE.search(message.lower()))


def normalize_stream_error_chunk(chunk: dict) -> dict:
    """error chunk 防御归一：仅当 error_type 未细分类且消息命中连接标记时改写。

    返回新 dict，不改写调用方原对象；已细分类的 error_type（如 run_idle_timeout、
    content_guard_blocked）永不覆盖。
    """
    if not isinstance(chunk, dict) or chunk.get("status") != "error":
        return chunk
    error_type = str(chunk.get("error_type") or "")
    if error_type not in _GENERIC_ERROR_TYPES:
        return chunk
    message = str(chunk.get("error_message") or chunk.get("message") or "")
    if not is_connection_error_text(message):
        return chunk
    return {
        **chunk,
        "error_type": MODEL_CONNECTION_ERROR,
        "error_message": MODEL_CONNECTION_ERROR_MESSAGE,
        "message": MODEL_CONNECTION_ERROR_MESSAGE,
        "retryable": True,
    }


__all__ = [
    "MODEL_CONNECTION_ERROR",
    "MODEL_CONNECTION_ERROR_MESSAGE",
    "is_connection_error_text",
    "is_model_connection_error",
    "normalize_stream_error_chunk",
]
