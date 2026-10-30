from contextlib import asynccontextmanager
from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from yuxi.agents.middlewares.tool_approval import ToolApprovalMiddleware
from yuxi.agents.state import BaseState
from yuxi.services import waitpoint_service
from yuxi.storage.postgres.manager import pg_manager


@dataclass
class ApprovalContext:
    run_id: str


@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_approval_survives_checkpoint_reopen_and_precedes_side_effect(tmp_path, monkeypatch, decision):
    middleware = ToolApprovalMiddleware()
    executed = []
    persist = AsyncMock()

    @asynccontextmanager
    async def db():
        yield None

    monkeypatch.setattr(pg_manager, "get_async_session_context", db)
    monkeypatch.setattr(waitpoint_service, "persist_waitpoint", persist)
    call = {"id": "call-1", "type": "tool_call", "name": "write_report", "args": {"path": "report.txt"}}

    class ApprovalModel(BaseChatModel):
        @property
        def _llm_type(self):
            return "local-approval-probe"

        def bind_tools(self, *_args, **_kwargs):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **_kwargs):
            message = (
                AIMessage(content="done")
                if isinstance(messages[-1], ToolMessage)
                else AIMessage(
                    content="",
                    tool_calls=[call],
                )
            )
            return ChatResult(generations=[ChatGeneration(message=message)])

    @tool
    async def write_report(path: str) -> str:
        """Write the requested report."""
        executed.append({"path": path})
        return "written"

    def compile(saver):
        return create_agent(
            model=ApprovalModel(),
            tools=[write_report],
            middleware=[middleware],
            state_schema=BaseState,
            context_schema=ApprovalContext,
            checkpointer=saver,
        )

    config = {"configurable": {"thread_id": "checkpointed-approval"}}
    path = str(tmp_path / "approval.sqlite")
    async with AsyncSqliteSaver.from_conn_string(path) as saver:
        state = await compile(saver).ainvoke(
            {"messages": [HumanMessage(content="Write a report")]}, config, context=ApprovalContext("run-1")
        )
        assert "__interrupt__" in state and executed == []
    persist.assert_awaited_once()
    assert persist.await_args.kwargs["approval_calls"] == [
        {"call_id": "call-1", "name": "write_report", "arguments": {"path": "report.txt"}}
    ]
    async with AsyncSqliteSaver.from_conn_string(path) as saver:
        graph = compile(saver)
        state = await graph.ainvoke(Command(resume={"call-1": decision}), config, context=ApprovalContext("run-2"))
        assert len(executed) == (1 if decision == "approve" else 0)
        response = next(message for message in state["messages"] if isinstance(message, ToolMessage))
        assert response.status == ("success" if decision == "approve" else "error")
        assert persist.await_count == 1
    changed = {**call, "args": {"path": "different.txt"}}
    handler = AsyncMock()
    result = await middleware.awrap_tool_call(
        ToolCallRequest(tool_call=changed, tool=None, state=state, runtime=None), handler
    )
    assert result.status == "error" and handler.await_count == 0
