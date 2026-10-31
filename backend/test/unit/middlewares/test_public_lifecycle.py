"""Real LangGraph checkpoints: safe steer and cancellation without side effects."""

from contextlib import asynccontextmanager
from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest
from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from yuxi.agents.checkpoint_cleanup import CheckpointCleanupModel, cancel_checkpoint
from yuxi.agents.middlewares.public_steer import PublicSteerMiddleware
from yuxi.agents.middlewares.tool_approval import ToolApprovalMiddleware
from yuxi.agents.state import BaseState
from yuxi.storage.postgres.manager import pg_manager


@dataclass
class Context:
    run_id: str = "run"
    uid: str = "alice"


class ProbeModel(BaseChatModel):
    calls: int = 0

    @property
    def _llm_type(self):
        return "public-lifecycle-probe"

    def bind_tools(self, *args, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        output = (
            AIMessage(content="finished")
            if self.calls > 1
            else AIMessage(
                id="model-tool-batch",
                content="",
                tool_calls=[
                    {
                        "id": "call-1",
                        "name": "write_file",
                        "args": {"text": "report"},
                        "type": "tool_call",
                    }
                ],
            )
        )
        return ChatResult(generations=[ChatGeneration(message=output)])


@pytest.mark.parametrize("backend_id", ["ChatbotAgent", "SubAgentBackend"])
async def test_cancel_reopened_production_graph_does_not_run_pending_tool(tmp_path, monkeypatch, backend_id):
    from yuxi.agents.buildin import agent_manager
    from yuxi.services import waitpoint_service

    @asynccontextmanager
    async def db():
        yield None

    monkeypatch.setattr(pg_manager, "get_async_session_context", db)
    monkeypatch.setattr(waitpoint_service, "persist_waitpoint", AsyncMock())
    effects = []

    @tool
    async def write_file(text: str) -> str:
        """Save a report."""
        effects.append(text)
        return "written"

    config = {"configurable": {"thread_id": backend_id, "uid": "alice"}}
    path = str(tmp_path / "cancel.sqlite")
    async with AsyncSqliteSaver.from_conn_string(path) as saver:
        graph = create_agent(
            model=ProbeModel(),
            tools=[write_file],
            middleware=[ToolApprovalMiddleware()],
            state_schema=BaseState,
            checkpointer=saver,
            context_schema=Context,
        )
        result = await graph.ainvoke({"messages": [HumanMessage(content="write report")]}, config, context=Context())
        assert result["__interrupt__"] and not effects
    backend = agent_manager.get_agent(backend_id)
    assert backend is not None
    context = backend.context_schema()
    context.update_from_dict(
        {
            "uid": "alice",
            "thread_id": backend_id,
            "run_id": "run",
            "public_session_id": backend_id,
            "tool_approval_mode": "always_ask",
        }
    )
    async with AsyncSqliteSaver.from_conn_string(path) as saver:
        monkeypatch.setattr(backend, "_get_checkpointer", AsyncMock(return_value=saver))
        graph = await backend.get_graph(context=context, checkpoint_only=True)
        await cancel_checkpoint(graph, config)
        await cancel_checkpoint(graph, config)
        state = await graph.aget_state(config)
        assert not state.next and not state.interrupts and not effects
        responses = [m for m in state.values["messages"] if isinstance(m, ToolMessage)]
        assert len(responses) == 1 and responses[0].tool_call_id == "call-1" and responses[0].status == "error"
        assert state.values["messages"][0].content == "write report"
        # No maintenance model may be invoked during this operation.
        with pytest.raises(RuntimeError, match="禁止模型执行"):
            CheckpointCleanupModel().invoke("forbidden")


async def test_steer_waits_until_the_whole_tool_batch_finishes(tmp_path, monkeypatch):
    from yuxi.services import public_execution_service

    @asynccontextmanager
    async def db():
        yield None

    monkeypatch.setattr(pg_manager, "get_async_session_context", db)
    pending = False
    effects = []

    async def boundary(*args, **kwargs):
        return pending

    monkeypatch.setattr(public_execution_service, "mark_steer_boundary", boundary)

    @tool
    async def write_file(text: str) -> str:
        """Save a report."""
        nonlocal pending
        pending = True
        effects.append(text)
        return "written"

    model = ProbeModel()
    config = {"configurable": {"thread_id": "steer"}}
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "steer.sqlite")) as saver:
        graph = create_agent(
            model=model,
            tools=[write_file],
            middleware=[PublicSteerMiddleware()],
            state_schema=BaseState,
            checkpointer=saver,
            context_schema=Context,
        )
        result = await graph.ainvoke({"messages": [HumanMessage(content="write")]}, config, context=Context())
        assert model.calls == 1 and effects == ["report"]
        assert isinstance(result["messages"][-1], ToolMessage)
        assert not (await graph.aget_state(config)).next
        pending = False
        result = await graph.ainvoke({"messages": [HumanMessage(content="new direction")]}, config, context=Context())
        assert effects == ["report"] and model.calls == 2
        assert result["messages"][-1].content == "finished"
        assert result["messages"][0].content == "write"
