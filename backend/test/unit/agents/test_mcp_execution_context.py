"""MCP 执行上下文清理语义回归测试。

背景缺陷：流式对话链路中 ``set_mcp_execution_context`` 与 finally 里的
``reset_mcp_execution_context`` 可能落在不同 asyncio Task/Context，原生
``ContextVar.reset`` 抛出 ``ValueError: <Token ...> was created in a different
Context``，异常沿生成器逸出后污染已完成的回答流（桌面端表现为
"Yuxi 返回了无法识别的数据"），并使部分路径错过最终持久化。
invalid_agent 在主流式 try/finally 之前返回，必须由该分支显式清理。

本测试只覆盖纯 ContextVar 语义，不依赖数据库。
"""

from __future__ import annotations

import contextvars

from yuxi.agents.mcp.execution import (
    McpExecutionContext,
    get_mcp_execution_context,
    reset_mcp_execution_context,
    set_mcp_execution_context,
)


def _context(tenant_id: int = 1, uid: str = "u-1") -> McpExecutionContext:
    return McpExecutionContext(tenant_id=tenant_id, uid=uid)


def test_reset_in_same_context_restores_previous_value():
    assert get_mcp_execution_context() is None
    token = set_mcp_execution_context(_context())
    try:
        assert get_mcp_execution_context() is not None
    finally:
        reset_mcp_execution_context(token)
    assert get_mcp_execution_context() is None


def test_reset_from_different_context_does_not_raise():
    """跨 Context reset 必须降级为 no-op，而不是抛 ValueError。"""
    holder: dict[str, contextvars.Token] = {}

    def _set_inside_other_context():
        holder["token"] = set_mcp_execution_context(_context(tenant_id=7, uid="u-other"))

    other = contextvars.Context()
    other.run(_set_inside_other_context)
    # 此处当前 Context 里没有该值（copy-on-write），reset 一个"别的 Context 创建的
    # token"在原生实现下必然抛 ValueError；容错实现应吞掉且不覆盖当前值。
    reset_mcp_execution_context(holder["token"])
    assert get_mcp_execution_context() is None


def test_double_reset_does_not_raise():
    token = set_mcp_execution_context(_context())
    reset_mcp_execution_context(token)
    # 第二次 reset（already-used token）同样不能抛异常
    reset_mcp_execution_context(token)
    assert get_mcp_execution_context() is None


def test_reset_none_token_is_noop():
    reset_mcp_execution_context(None)
    assert get_mcp_execution_context() is None


def test_context_keeps_thread_scope_for_workspace_mcp_runtime():
    token = set_mcp_execution_context(
        McpExecutionContext(tenant_id=3, uid="u-3", thread_id="thread-3", run_id="run-3")
    )
    try:
        current = get_mcp_execution_context()
        assert current is not None
        assert current.thread_id == "thread-3"
    finally:
        reset_mcp_execution_context(token)


def test_reset_does_not_resurrect_stale_value_in_current_context():
    """当前 Context 已被外层设置为另一租户时，跨 Context reset 不得覆盖当前值。"""
    outer_token = set_mcp_execution_context(_context(tenant_id=1, uid="u-outer"))
    holder: dict[str, contextvars.Token] = {}

    def _set_inside_other_context():
        holder["token"] = set_mcp_execution_context(_context(tenant_id=9, uid="u-inner"))

    contextvars.Context().run(_set_inside_other_context)
    reset_mcp_execution_context(holder["token"])
    try:
        current = get_mcp_execution_context()
        assert current is not None and current.uid == "u-outer"
    finally:
        reset_mcp_execution_context(outer_token)
    assert get_mcp_execution_context() is None


async def test_generator_aclose_from_another_task_does_not_leak_token():
    """忠实复现桌面端"客户端断开"路径：async generator 由另一个 task 执行 aclose，
    finally 里的 reset 在跨 Context 时绝不能抛 RuntimeError 或让 Token repr 泄漏。"""

    captured: list[BaseException | str] = []

    async def _stream():
        token = set_mcp_execution_context(_context(tenant_id=3, uid="u-stream"))
        try:
            yield "part-1"
            yield "part-2"
        finally:
            try:
                reset_mcp_execution_context(token)
            except BaseException as error:
                # 若 reset 抛错（无论什么类型），记录并让断言失败。
                captured.append(error)

    async def _drive():
        ag = _stream()
        # 消费者用独立 task 消费生成器，模拟 SSE 队列桥接 / 断线 aclose。
        next_task = asyncio.create_task(ag.__anext__())
        first = await asyncio.wait_for(next_task, timeout=2)
        assert first == "part-1"
        close_task = asyncio.create_task(ag.aclose())
        await asyncio.wait_for(close_task, timeout=2)

    import asyncio

    await _drive()
    assert not captured, f"reset leaked error: {captured}"
    # 生成器对应的 task 已销毁，其 Context 不再持有执行上下文。
    assert get_mcp_execution_context() is None
