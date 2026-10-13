"""dsh LLM 网关消息/用量转换器的单元测试（纯函数面，不触 DB/Redis）。"""

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
import pytest

from server.routers.llm_gateway_router import (
    _ChatCompletionRequest,
    _OpenAIMessage,
    _OpenAIToolCall,
    _finish_reason,
    _tool_calls_to_openai,
    _usage_from_langchain,
    openai_messages_to_langchain,
)


def test_openai_messages_to_langchain_covers_role_set():
    messages = openai_messages_to_langchain(
        [
            _OpenAIMessage(role="system", content="你是科研问答助手"),
            _OpenAIMessage(role="user", content="水稻胚乳？"),
            _OpenAIMessage(
                role="assistant",
                content="",
                tool_calls=[
                    _OpenAIToolCall(
                        id="call_1",
                        type="function",
                        function={"name": "mcp__yuxi__yuxi_ask_user", "arguments": '{"question": "哪种作物？"}'},
                    )
                ],
            ),
            _OpenAIMessage(role="tool", content="CLARIFICATION_ESCALATED", tool_call_id="call_1"),
        ]
    )
    assert isinstance(messages[0], SystemMessage)
    assert isinstance(messages[1], HumanMessage)
    assistant = messages[2]
    assert isinstance(assistant, AIMessage)
    assert assistant.tool_calls[0]["name"] == "mcp__yuxi__yuxi_ask_user"
    assert assistant.tool_calls[0]["args"] == {"question": "哪种作物？"}
    tool = messages[3]
    assert isinstance(tool, ToolMessage)
    assert tool.tool_call_id == "call_1"


def test_openai_messages_multimodal_content_flattens_to_text():
    message = _OpenAIMessage(role="user", content=[{"type": "text", "text": "看图"}, {"type": "text", "text": "回答"}])
    assert openai_messages_to_langchain([message])[0].content == "看图回答"


def test_usage_from_usage_metadata_and_token_usage():
    message = AIMessage(content="x", usage_metadata={"input_tokens": 7, "output_tokens": 3, "total_tokens": 10})
    assert _usage_from_langchain(message)["total_tokens"] == 10

    message = AIMessage(content="x", response_metadata={"token_usage": {"prompt_tokens": 5, "completion_tokens": 2}})
    assert _usage_from_langchain(message)["total_tokens"] == 7

    assert _usage_from_langchain(AIMessage(content="x")) is None


def test_tool_calls_to_openai_serializes_function_arguments_json():
    message = AIMessage(
        content="",
        tool_calls=[{"id": "call_9", "name": "yuxi_ask_user", "args": {"question": "？", "options": ["a"]}}],
    )
    calls = _tool_calls_to_openai(message)
    assert calls[0]["function"]["name"] == "yuxi_ask_user"
    assert '"question"' in calls[0]["function"]["arguments"]
    assert _tool_calls_to_openai(AIMessage(content="plain")) is None


def test_finish_reason_prefers_tool_calls_then_metadata():
    tool_message = AIMessage(content="", tool_calls=[{"id": "c", "name": "t", "args": {}}])
    assert _finish_reason(tool_message) == "tool_calls"
    plain = AIMessage(content="done", response_metadata={"finish_reason": "stop"})
    assert _finish_reason(plain) == "stop"


def test_request_model_defaults():
    request = _ChatCompletionRequest(messages=[_OpenAIMessage(role="user", content="hi")])
    assert request.stream is False
    assert request.tools is None


def test_images_and_thinking_tool_history_are_preserved():
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,abcd"}}
    result = openai_messages_to_langchain(
        [
            _OpenAIMessage(role="user", content=[{"type": "text", "text": "看图"}, image]),
            _OpenAIMessage(
                role="assistant",
                content="",
                reasoning_content="需要澄清",
                tool_calls=[
                    _OpenAIToolCall(
                        id="c1", function={"name": "mcp__yuxi__yuxi_ask_user", "arguments": '{"question":"范围？"}'}
                    )
                ],
            ),
        ]
    )
    assert result[0].content[1] == image
    assert result[1].additional_kwargs["reasoning_content"] == "需要澄清"
    assert result[1].tool_calls[0]["args"] == {"question": "范围？"}
    with pytest.raises(ValueError):
        _OpenAIMessage(role="unknown", content="cannot become a system message")


async def test_stream_preserves_tool_indices_and_settles_cancelled_usage(monkeypatch):
    import asyncio
    import importlib
    import json
    from langchain_core.messages import AIMessageChunk

    gateway = importlib.import_module("server.routers.llm_gateway_router")
    recorded = []

    async def record(*args):
        recorded.append(args)

    monkeypatch.setattr(gateway, "record_call", record)

    class Model:
        async def astream(self, _messages):
            yield AIMessageChunk(
                content="",
                additional_kwargs={"reasoning_content": "分析"},
                tool_call_chunks=[
                    {"index": 2, "id": "c", "name": "tool", "args": '{"x":'},
                ],
            )
            yield AIMessageChunk(content="", tool_call_chunks=[{"index": 2, "id": None, "name": None, "args": "1}"}])
            raise asyncio.CancelledError

    stream = gateway._stream_completion(Model(), [], "call", "completion", 0, "model")
    chunks = []
    with pytest.raises(asyncio.CancelledError):
        async for frame in stream:
            chunks.append(json.loads(frame.removeprefix("data: ").strip()))
    assert chunks[0]["choices"][0]["delta"]["reasoning_content"] == "分析"
    assert [chunk["choices"][0]["delta"]["tool_calls"][0]["index"] for chunk in chunks] == [2, 2]
    assert recorded[-1][2:] == ("cancelled", "client_disconnected")


async def test_model_execution_token_cannot_reconcile_financial_usage(monkeypatch):
    import importlib
    import httpx
    from fastapi import FastAPI
    from server.utils.auth_middleware import get_db
    from yuxi.agents.buildin.dsh.gateway_tokens import mint_gateway_token

    gateway = importlib.import_module("server.routers.llm_gateway_router")
    monkeypatch.setenv("YUXI_SECRET_MASTER_KEY", "test-only-master-key-0123456789abcdef")
    token = mint_gateway_token("run", execution_id="execution", ttl_seconds=60)
    app = FastAPI()
    app.include_router(gateway.router)
    app.dependency_overrides[get_db] = lambda: None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/internal/llm/v1/usage/call/reconcile",
            headers={"Authorization": f"Bearer {token}"},
            json={"input_tokens": 1, "output_tokens": 2, "evidence_sha256": "a" * 64},
        )
        assert response.status_code == 401


async def test_conversation_only_admin_key_cannot_write_financial_usage():
    import importlib
    from types import SimpleNamespace
    from starlette.requests import Request

    gateway = importlib.import_module("server.routers.llm_gateway_router")
    http_request = Request({"type": "http", "headers": [(b"authorization", b"Bearer yxkey_fixture")]})
    http_request.state.api_key_scopes = []
    response = await gateway.reconcile_usage(
        "call",
        gateway._UsageReconciliation(input_tokens=1, output_tokens=2, evidence_sha256="a" * 64),
        http_request,
        actor=SimpleNamespace(role="superadmin", uid="admin"),
    )
    assert response.status_code == 403
