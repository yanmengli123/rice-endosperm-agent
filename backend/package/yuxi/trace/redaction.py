"""Trace 脱敏策略：敏感内容在进入账本之前 DROP/MASK。

原则（对齐 OWASP 日志安全建议与仓库 secret_crypto/mcp_call_audit 先例）：

- 凭据类（API Key、Authorization、token、secret、密码、连接串）DO NOT COLLECT：
  命中键名或值形态即丢弃，不是"存起来但 visibility=ADMIN"；
- reasoning_content / 系统 prompt 永远不进入 Trace 通道；
- 工具入参等大对象只保留 sha256 摘要（digest），与 mcp_call_audit 的
  arguments_digest 同一做法；
- title/summary 里的密钥形态字符串做掩码。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

# 键名命中即 DROP（大小写不敏感，匹配子串）
_SENSITIVE_KEY_PATTERN = re.compile(
    r"(api[_-]?key|authorization|auth[_-]?token|access[_-]?token|refresh[_-]?token|"
    r"secret|password|passwd|credential|cookie|session[_-]?key|private[_-]?key|"
    r"reasoning|reasoning_content|system[_-]?prompt|connection[_-]?string)",
    re.IGNORECASE,
)

# 值形态命中即视为密钥泄漏（用于 title/summary/attribute 值掩码）
_SECRET_VALUE_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\byxkey_[A-Za-z0-9_-]{6,}"),
    re.compile(r"\byxacct_[A-Za-z0-9_-]{6,}"),
    re.compile(r"\bAKIA[0-9A-Z]{12,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"[a-zA-Z0-9+_/]{24,}={0,2}"),  # 长随机串（base64 形态）仅在超长时掩码
)
_LONG_RANDOM_MIN_CHARS = 40


def digest_text(value: object, *, max_length: int = 4096) -> str:
    """对工具入参等大对象取 sha256 短摘要（与 mcp_call_audit 的 digest 同思路）。"""
    if isinstance(value, (dict, list)):
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            text = str(value)
    else:
        text = "" if value is None else str(value)
    return f"sha256:{hashlib.sha256(text[:max_length].encode('utf-8', errors='replace')).hexdigest()[:16]}"


def is_sensitive_key(key: str) -> bool:
    return bool(_SENSITIVE_KEY_PATTERN.search(key or ""))


def _mask_secret_values(text: str) -> str:
    for pattern in _SECRET_VALUE_PATTERNS[:-1]:
        text = pattern.sub("***", text)
    # 最后一条「长 base64 随机串」规则单独处理，短串不误伤普通词
    text = _SECRET_VALUE_PATTERNS[-1].sub(
        lambda m: m.group(0) if len(m.group(0)) < _LONG_RANDOM_MIN_CHARS else "***",
        text,
    )
    return text


def _redact_value(value: Any, *, depth: int = 0) -> Any:
    """递归生成一个新值；任何层级的敏感键都 DROP，永不调用任意对象的 ``str``。

    Trace 协议最终只接受标量和短标量列表，但脱敏必须先于协议归一化执行，
    否则嵌套字典被字符串化时会把内部凭据重新拼回日志。
    """
    if depth > 8:
        return None
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return _mask_secret_values(value)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, nested in value.items():
            if not isinstance(raw_key, str) or is_sensitive_key(raw_key):
                continue
            redacted = _redact_value(nested, depth=depth + 1)
            if redacted is not None:
                result[raw_key] = redacted
        return result
    if isinstance(value, (list, tuple)):
        return [_redact_value(item, depth=depth + 1) for item in value[:100]]
    # 不受信任对象可能在 __str__ 中泄漏字段；只保留稳定的类型标签。
    return f"<{type(value).__name__}>"


def redact_attributes(attributes: dict | None) -> dict:
    """递归 DROP 敏感键并掩码敏感值，返回与输入无共享引用的新字典。"""
    if not isinstance(attributes, dict):
        return {}
    redacted = _redact_value(attributes)
    return redacted if isinstance(redacted, dict) else {}


def redact_json_text(value: Any, *, max_length: int = 4096) -> str:
    """供必须落结构摘要的边界使用；先递归脱敏，再稳定序列化。"""
    return json.dumps(_redact_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))[:max_length]


def redact_text(text: str | None) -> str | None:
    if not text:
        return text
    return _mask_secret_values(text)
