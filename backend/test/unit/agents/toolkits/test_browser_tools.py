"""浏览器工具组单测：门控装配与结构化错误语义。"""

from __future__ import annotations

import contextvars
from types import SimpleNamespace

import pytest

from yuxi.agents.toolkits.browser.gateway_client import (
    BrowserExecutionContext,
    dispatch_browser_op,
    end_browser_task_best_effort,
    get_browser_execution_context,
    reset_browser_execution_context,
    set_browser_execution_context,
)
from yuxi.agents.toolkits.browser.tools import get_browser_runtime_tools
from yuxi.services.browser_gateway_service import (
    BrowserGatewayError,
    browser_not_paired,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

_BROWSER_TOOL_NAMES = {
    "browser_get_status",
    "browser_navigate",
    "browser_read_page",
    "browser_click",
    "browser_type",
    "browser_screenshot",
}


def _make_context(browser_enabled: bool):
    return SimpleNamespace(
        tools=[],
        mcps=[],
        skills=[],
        knowledges=[],
        subagents=[],
        uid="admin",
        browser_enabled=browser_enabled,
        _effective_knowledge_scope={"allow_web": True},
    )


async def _resolve(context):
    from yuxi.agents.toolkits.service import resolve_configured_runtime_tools

    return await resolve_configured_runtime_tools(context)


async def test_browser_tools_absent_by_default():
    tools = await _resolve(_make_context(browser_enabled=False))
    assert _BROWSER_TOOL_NAMES.isdisjoint({tool.name for tool in tools})


async def test_browser_tools_assembled_when_enabled():
    tools = await _resolve(_make_context(browser_enabled=True))
    names = {tool.name for tool in tools}
    assert _BROWSER_TOOL_NAMES.issubset(names)


async def test_borrowed_tab_id_is_forwarded(monkeypatch):
    calls = []

    async def _dispatch(op, payload, **kwargs):
        calls.append((op, payload))
        return {"title": "Borrowed"}

    monkeypatch.setattr("yuxi.agents.toolkits.browser.tools.dispatch_browser_op", _dispatch)
    tools = {tool.name: tool for tool in get_browser_runtime_tools()}
    await tools["browser_click"].coroutine(selector="#ok", tab_id=42)
    assert calls == [("click", {"selector": "#ok", "tab_id": 42})]


async def test_read_page_does_not_require_hidden_tool_call_argument(monkeypatch):
    async def _dispatch(op, payload, **kwargs):
        assert (op, payload) == ("read_page", {})
        return {"text": "Example Domain"}

    monkeypatch.setattr("yuxi.agents.toolkits.browser.tools.dispatch_browser_op", _dispatch)
    tools = {tool.name: tool for tool in get_browser_runtime_tools()}
    raw = await tools["browser_read_page"].coroutine()
    assert "Example Domain" in raw


async def test_tool_returns_structured_error_when_not_paired(monkeypatch):
    async def _raise(op, payload, **kwargs):
        raise browser_not_paired()

    monkeypatch.setattr("yuxi.agents.toolkits.browser.tools.dispatch_browser_op", _raise)
    tools = {tool.name: tool for tool in get_browser_runtime_tools()}
    raw = await tools["browser_navigate"].coroutine(url="https://example.com")
    payload = __import__("json").loads(raw)
    assert payload["status"] == "error"
    assert payload["error_code"] == "BROWSER_NOT_PAIRED"


async def test_dispatch_requires_execution_context():
    with pytest.raises(BrowserGatewayError) as missing:
        await dispatch_browser_op("get_status", {})
    assert missing.value.code == "BROWSER_CONTEXT_MISSING"


async def test_read_page_text_truncated_for_context():
    from yuxi.agents.toolkits.browser.tools import _compact_page_result

    big = "字" * 30000
    compacted = _compact_page_result({"text": big, "url": "https://example.com"})
    assert len(compacted["text"]) < 30000
    assert compacted["text_truncated"] is True
    assert compacted["text_total_length"] == 30000


async def test_execution_context_roundtrip():
    token = set_browser_execution_context(
        BrowserExecutionContext(tenant_id=1, uid="admin", thread_id="t1", run_id="r1")
    )
    context = get_browser_execution_context()
    assert context is not None and context.run_id == "r1"
    reset_browser_execution_context(token)
    assert get_browser_execution_context() is None


async def test_execution_context_reset_is_cross_context_safe():
    token = set_browser_execution_context(
        BrowserExecutionContext(tenant_id=1, uid="admin", thread_id="t1", run_id="r1")
    )
    other_context = contextvars.Context()
    other_context.run(reset_browser_execution_context, token)
    assert get_browser_execution_context() is not None
    reset_browser_execution_context(token)
    assert get_browser_execution_context() is None


async def test_browser_task_cleanup_only_after_use(monkeypatch):
    calls = []

    async def fake_dispatch(op, payload, **kwargs):
        calls.append((op, payload, kwargs))
        return {"ok": True}

    monkeypatch.setattr("yuxi.agents.toolkits.browser.gateway_client.dispatch_browser_op", fake_dispatch)
    context = BrowserExecutionContext(tenant_id=1, uid="admin", thread_id="t1", run_id="r1")
    token = set_browser_execution_context(context)
    try:
        await end_browser_task_best_effort()
        assert calls == []
        context.used = True
        await end_browser_task_best_effort()
        assert calls == [("end_task", {}, {"timeout_s": 15})]
    finally:
        reset_browser_execution_context(token)


async def test_gateway_error_carries_code():
    error = BrowserGatewayError("BROWSER_TIMEOUT", "超时", http_status=504)
    assert error.code == "BROWSER_TIMEOUT"
    assert error.http_status == 504
