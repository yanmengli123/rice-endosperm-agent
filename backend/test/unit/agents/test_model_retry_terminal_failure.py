"""A failed model request must reach the run failure path, never become an answer."""

from types import SimpleNamespace

import pytest
from langchain.agents.middleware import ModelRetryMiddleware

from yuxi.agents.buildin.chatbot import graph as chatbot
from yuxi.agents.buildin.subagent import graph as subagent


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [chatbot, subagent])
async def test_retry_exhaustion_raises_in_both_agent_builders(module, monkeypatch):
    monkeypatch.setattr(module, "load_chat_model", lambda **kwargs: object())
    monkeypatch.setattr(module, "create_summary_middleware", lambda **kwargs: object())
    monkeypatch.setattr(module, "create_agent_filesystem_middleware", lambda *args, **kwargs: object())
    if module is chatbot:

        async def no_subagents(context):
            return None

        monkeypatch.setattr(module, "create_subagent_task_middleware", no_subagents)
    middlewares = await module._build_middlewares(SimpleNamespace(model="configured:model", model_retry_times=2))
    retry = next(m for m in middlewares if isinstance(m, ModelRetryMiddleware))
    retry.initial_delay = 0
    attempts = 0

    async def failing_request(request):
        nonlocal attempts
        attempts += 1
        raise ConnectionError("provider unavailable")

    with pytest.raises(ConnectionError, match="provider unavailable"):
        await retry.awrap_model_call(None, failing_request)
    assert attempts == 3
