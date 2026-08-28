"""固化 Bug1 回归：执行上下文审计不允许 Token / ContextVar / callable 任何路径写入。

这一组用例独立覆盖 ``execution.stable_digest`` 与 ``record_mcp_call``：它们是
唯一把 arguments / result 落到 ``MCPCallAudit.arguments_digest`` /
``.result_digest`` 的入口。如果这里再退回到 ``json.dumps(..., default=str)``，
会让 ``<Token var=... at 0x...>`` 整段 repr 进入审计行并污染后续响应。
"""

from __future__ import annotations

import asyncio
import contextvars

import pytest

from yuxi.agents.mcp.execution import (
    McpExecutionContext,
    record_mcp_call,
    reset_mcp_execution_context,
    set_mcp_execution_context,
    stable_digest,
)


def _secret_token() -> contextvars.Token:
    var = contextvars.ContextVar("_test_token_holder", default=None)
    return var.set("definitely-not-a-mcp-context")


def test_stable_digest_refuses_token() -> None:
    var = contextvars.ContextVar("_test_token_holder", default=None)
    token = var.set("payload")
    with pytest.raises(TypeError) as exc:
        stable_digest(token)
    assert "Token" in str(exc.value)


def test_stable_digest_refuses_context_var() -> None:
    var = contextvars.ContextVar("_test_var_holder", default=None)
    with pytest.raises(TypeError) as exc:
        stable_digest(var)
    assert "ContextVar" in str(exc.value)


def test_stable_digest_refuses_callable() -> None:
    with pytest.raises(TypeError) as exc:
        stable_digest(lambda: None)
    assert "callable" in str(exc.value)


def test_stable_digest_dict_with_token_value_fails_loud() -> None:
    # 即使 token 藏在 dict 里也必须抛错——> ``default=str`` 会被静默 str()。
    payload = {"client": "desktop", "token": _secret_token()}
    with pytest.raises(TypeError) as exc:
        stable_digest(payload)
    assert "Token" in str(exc.value)


def test_stable_digest_serializes_normal_args() -> None:
    digest = stable_digest({"tool": "fetch", "url": "https://example.com", "limit": 200})
    assert digest.startswith("sha256:")
    # 必须是稳定结构化哈希——> 不能含任何 repr 字节。
    assert "<Token" not in digest
    assert "<ContextVar" not in digest


def test_stable_digest_is_independent_of_mapping_insertion_order() -> None:
    first = {"tool": "fetch", "limit": 200}
    second = {"limit": 200, "tool": "fetch"}
    assert stable_digest(first) == stable_digest(second)


def test_record_mcp_call_skips_audit_when_arguments_hold_token() -> None:
    """关键回归：即使上游错误地把 Token 传进 arguments，``record_mcp_call`` 也不写库、不污染日志。"""
    token = _secret_token()
    captured: dict[str, str] = {}

    async def run() -> None:
        ctx_token = set_mcp_execution_context(McpExecutionContext(tenant_id=1, uid="u1"))
        try:
            # 模拟 arguments 里混入了 Token；record_mcp_call 必须跳过。
            await record_mcp_call(
                server_slug="demo",
                capability_type="tool",
                capability_name="demo_tool",
                arguments={"first": "ok", "secret": token},
                result={"ok": True},
                status="completed",
                duration_ms=5,
            )
        finally:
            reset_mcp_execution_context(ctx_token)

    # 把 logger 的 warning 截到 captured 里
    from yuxi.utils import logger

    original_warning = logger.warning
    logger.warning = lambda *args, **kwargs: captured.setdefault("msg", str(args[0]))
    try:
        asyncio.run(run())
    finally:
        logger.warning = original_warning

    # 未写入审计行；warning 已记录；信息中不应含 Token repr。
    assert "msg" in captured
    assert "<Token" not in captured["msg"]
    assert "Skipped MCP call audit" in captured["msg"]
