"""Trace 脱敏策略单测：敏感内容在进入账本之前 DROP/MASK。"""

from __future__ import annotations

import pytest
from yuxi.trace.redaction import (
    digest_text,
    is_sensitive_key,
    redact_attributes,
    redact_text,
)

pytestmark = [pytest.mark.unit]


def test_sensitive_keys_are_dropped_entirely():
    redacted = redact_attributes(
        {
            "api_key": "sk-1234567890abcdef",
            "Authorization": "Bearer abc.def.ghi",
            "user_password": "hunter2",
            "reasoning_content": "internal chain of thought",
            "system_prompt": "You are...",
            "mcp_secret": "xxx",
            "refresh_token": "yyy",
            "normal_count": 42,
            "note": "普通字段",
        }
    )
    assert "api_key" not in redacted
    assert "Authorization" not in redacted
    assert "user_password" not in redacted
    assert "reasoning_content" not in redacted
    assert "system_prompt" not in redacted
    assert "mcp_secret" not in redacted
    assert "refresh_token" not in redacted
    assert redacted["normal_count"] == 42
    assert redacted["note"] == "普通字段"


def test_secret_shaped_values_are_masked_in_visible_text():
    masked = redact_text("调用失败，key=sk-abcdefghijklmnop1234 请检查")
    assert "sk-abcdefghijklmnop1234" not in masked
    assert "***" in masked

    masked_bearer = redact_text("header Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.token")
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in masked_bearer

    masked_key = redact_text("用户出示了 yxkey_abc123def456")
    assert "yxkey_abc123def456" not in masked_key


def test_normal_short_text_not_masked():
    assert redact_text("检索完成，命中 18 条") == "检索完成，命中 18 条"
    assert redact_text(None) is None
    assert redact_text("") == ""


def test_attribute_values_get_masked_not_dropped():
    redacted = redact_attributes(
        {
            "endpoint": "https://api.example.com/v1?token=sk-abcdefghijklmnop1234",
            "tool": "query_knowledge_scope",
        }
    )
    assert "sk-abcdefghijklmnop1234" not in str(redacted["endpoint"])
    assert redacted["tool"] == "query_knowledge_scope"


def test_digest_text_stable_and_short():
    args = {"gene": "OsSPL14", "limit": 10}
    first = digest_text(args)
    second = digest_text({"limit": 10, "gene": "OsSPL14"})  # key 顺序无关
    assert first == second
    assert first.startswith("sha256:") and len(first) == len("sha256:") + 16
    assert "OsSPL14" not in first  # 原文绝不出现


def test_is_sensitive_key_matches_common_shapes():
    for key in ("api_key", "X-Auth-Token", "MCP_SECRET", "db_connection_string", "password"):
        assert is_sensitive_key(key), key
    for key in ("tool", "hit_count", "model_spec"):
        assert not is_sensitive_key(key), key


def test_nested_secrets_are_removed_before_protocol_stringification():
    token = "sk-nestedabcdefghijklmnop"
    redacted = redact_attributes(
        {
            "safe": {
                "authorization": f"Bearer {token}",
                "nested": [{"api_key": token, "label": f"prefix {token}"}],
            }
        }
    )
    serialized = repr(redacted)
    assert token not in serialized
    assert "authorization" not in serialized
    assert "api_key" not in serialized
    assert "***" in serialized
