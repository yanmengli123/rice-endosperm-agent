from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage

from yuxi.agents.models import _ToolCallChunkFixChatOpenAI


def _model(*, legacy_compatibility: bool = False) -> _ToolCallChunkFixChatOpenAI:
    return _ToolCallChunkFixChatOpenAI(
        model="test-model",
        api_key="test-key",
        base_url="https://example.com/v1",
        disable_thinking_for_legacy_tool_history=legacy_compatibility,
    )


def _tool_call_message(*, reasoning_content: str | None) -> AIMessage:
    additional_kwargs = {}
    if reasoning_content is not None:
        additional_kwargs["reasoning_content"] = reasoning_content
    return AIMessage(
        content="",
        additional_kwargs=additional_kwargs,
        tool_calls=[
            {
                "name": "lookup",
                "args": {"query": "grain size"},
                "id": "call_1",
                "type": "tool_call",
            }
        ],
    )


def test_reasoning_content_is_preserved_in_followup_request() -> None:
    model = _model()
    assistant = _tool_call_message(reasoning_content="I should query the knowledge base.")

    payload = model._get_request_payload(
        [
            HumanMessage("Which genes regulate grain size?"),
            assistant,
            ToolMessage("GS3", tool_call_id="call_1"),
        ]
    )

    assistant_payload = payload["messages"][1]
    assert assistant_payload["reasoning_content"] == "I should query the knowledge base."
    assert assistant_payload["content"] == ""


def test_stream_chunk_preserves_reasoning_content_for_checkpoint_accumulation() -> None:
    model = _model()

    generation = model._convert_chunk_to_generation_chunk(
        {
            "model": "test-model",
            "choices": [
                {
                    "delta": {"role": "assistant", "content": "", "reasoning_content": "First step"},
                    "finish_reason": None,
                }
            ],
        },
        AIMessageChunk,
        {},
    )

    assert generation is not None
    assert generation.message.additional_kwargs["reasoning_content"] == "First step"


def test_non_stream_response_preserves_reasoning_content() -> None:
    model = _model()

    result = model._create_chat_result(
        {
            "model": "test-model",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "GS3 regulates grain size.",
                        "reasoning_content": "The evidence supports GS3.",
                    },
                    "finish_reason": "stop",
                }
            ],
        }
    )

    assert result.generations[0].message.additional_kwargs["reasoning_content"] == "The evidence supports GS3."


def test_legacy_deepseek_tool_history_disables_thinking_without_guessing_reasoning() -> None:
    model = _model(legacy_compatibility=True)

    payload = model._get_request_payload(
        [
            HumanMessage("Continue the previous request."),
            _tool_call_message(reasoning_content=None),
            ToolMessage("GS3", tool_call_id="call_1"),
        ]
    )

    assert "reasoning_content" not in payload["messages"][1]
    assert payload["extra_body"]["thinking"] == {"type": "disabled"}


def test_normal_history_does_not_change_thinking_configuration() -> None:
    model = _model(legacy_compatibility=True)

    payload = model._get_request_payload([HumanMessage("Hello")])

    assert "extra_body" not in payload
