"""Internal DSH gateway. PG owns leases, governed inputs, budgets and provider usage."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from pydantic import BaseModel, ConfigDict, Field, model_validator

from yuxi.agents.buildin.dsh.events import ASK_USER_TOOL_NAME
from yuxi.agents.buildin.dsh.execution import record_call, reserve_call
from yuxi.agents.buildin.dsh.gateway_tokens import (
    GatewayTokenError,
    bearer_token_from_authorization,
    verify_gateway_token,
)
from yuxi.agents.models import load_chat_model, reset_user_credential_override, set_user_credential_override
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import AgentRun
from yuxi.utils import logger
from server.utils.auth_middleware import get_superadmin_user

router = APIRouter(prefix="/internal/llm", tags=["internal-llm-gateway"])


class _UsageReconciliation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_tokens: int = Field(strict=True, ge=0, le=100000000)
    output_tokens: int = Field(strict=True, ge=0, le=100000000)
    cache_read_tokens: int = Field(default=0, strict=True, ge=0, le=100000000)
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


@router.post("/v1/usage/{call_id}/reconcile")
async def reconcile_usage(
    call_id: str, request: _UsageReconciliation, http_request: Request, actor=Depends(get_superadmin_user)
):
    from yuxi.agents.buildin.dsh.execution import reconcile_call_usage, settle_run_usage

    # Run after authentication has populated state. Conversation-only API keys
    # must never acquire financial write access through an empty scope list.
    if http_request.headers.get("authorization", "").startswith("Bearer yxkey_"):
        scopes = getattr(http_request.state, "api_key_scopes", None) or []
        if "system:write" not in scopes and "*" not in scopes:
            return _error(403, "reconciliation_forbidden", "API key requires system:write scope")
    if request.cache_read_tokens > request.input_tokens:
        return _error(422, "invalid_provider_usage", "Cache usage exceeds input usage")
    usage = {
        "prompt_tokens": request.input_tokens,
        "completion_tokens": request.output_tokens,
        "total_tokens": request.input_tokens + request.output_tokens,
        "prompt_cache_hit_tokens": request.cache_read_tokens,
    }
    try:
        run_id = await reconcile_call_usage(call_id, usage, request.evidence_sha256, actor)
        await settle_run_usage(run_id)
    except LookupError:
        return _error(404, "model_call_not_found", "Unknown model call")
    except PermissionError:
        return _error(403, "reconciliation_forbidden", "Provider reconciliation is restricted")
    except ValueError as exc:
        return _error(409, "reconciliation_conflict", str(exc))
    return {"run_id": run_id, "call_id": call_id, "reconciled": True}


def _error(status_code, code, message):
    return JSONResponse(status_code=status_code, content={"error": {"message": message, "type": code, "code": code}})


class _OpenAIToolCall(BaseModel):
    id: str = Field(min_length=1, max_length=256)
    type: Literal["function"] = "function"
    function: dict[str, Any]


class _OpenAIMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[dict] | None = None
    reasoning_content: str | None = None
    tool_calls: list[_OpenAIToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None

    @model_validator(mode="after")
    def validate_content(self):
        if isinstance(self.content, list):
            for block in self.content:
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    continue
                image = block.get("image_url")
                url = image.get("url") if isinstance(image, dict) else None
                if (
                    self.role == "user"
                    and block.get("type") == "image_url"
                    and isinstance(url, str)
                    and url.startswith(
                        (
                            "data:image/png;base64,",
                            "data:image/jpeg;base64,",
                            "data:image/webp;base64,",
                            "data:image/gif;base64,",
                        )
                    )
                ):
                    continue
                raise ValueError("Unsupported message content")
        if self.role == "tool" and not self.tool_call_id:
            raise ValueError("Tool result requires tool_call_id")
        if self.role != "assistant" and (self.tool_calls or self.reasoning_content):
            raise ValueError("Only assistant messages may contain reasoning/tool calls")
        return self


class _ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = ""
    messages: list[_OpenAIMessage] = Field(min_length=1, max_length=128)
    stream: bool = False
    stream_options: dict | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, gt=0, le=131072)
    max_completion_tokens: int | None = Field(default=None, gt=0, le=131072)
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] | None = None
    parallel_tool_calls: bool | None = None
    store: bool | None = None


def openai_messages_to_langchain(messages):
    converted: list[BaseMessage] = []
    for item in messages:
        content = item.content or ""
        if isinstance(content, list) and all(block.get("type") == "text" for block in content):
            content = _content_text(content)
        if item.role == "system":
            converted.append(SystemMessage(content=content))
        elif item.role == "user":
            converted.append(HumanMessage(content=content))
        elif item.role == "assistant":
            calls = []
            for call in item.tool_calls or []:
                raw = call.function.get("arguments", "{}")
                args = json.loads(raw) if isinstance(raw, str) else raw
                if not isinstance(args, dict) or call.function.get("name") != ASK_USER_TOOL_NAME:
                    raise ValueError("Invalid tool call")
                calls.append({"id": call.id, "name": call.function["name"], "args": args})
            converted.append(
                AIMessage(
                    content=content,
                    tool_calls=calls,
                    additional_kwargs={"reasoning_content": item.reasoning_content}
                    if item.reasoning_content is not None
                    else {},
                )
            )
        elif item.role == "tool":
            converted.append(ToolMessage(content=content, tool_call_id=item.tool_call_id))
        else:
            raise ValueError("Unsupported role")
    return converted


def _content_text(content):
    if isinstance(content, list):
        return "".join(
            block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text"
        )
    return str(content or "")


def _usage_from_langchain(message):
    meta = getattr(message, "usage_metadata", None) or {}
    raw = (getattr(message, "response_metadata", None) or {}).get("token_usage") or {}
    if not meta and not raw:
        return None
    prompt = int(meta.get("input_tokens", raw.get("prompt_tokens", 0)) or 0)
    completion = int(meta.get("output_tokens", raw.get("completion_tokens", 0)) or 0)
    total = int(meta.get("total_tokens", raw.get("total_tokens", prompt + completion)) or 0)
    cached = int(
        (meta.get("input_token_details") or {}).get(
            "cache_read",
            raw.get("prompt_cache_hit_tokens", (raw.get("prompt_tokens_details") or {}).get("cached_tokens", 0)),
        )
        or 0
    )
    if min(prompt, completion, total, cached) < 0 or total < prompt + completion or cached > prompt:
        raise ValueError("Invalid provider usage")
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "prompt_cache_hit_tokens": cached,
        "prompt_cache_miss_tokens": prompt - cached,
        "prompt_tokens_details": {"cached_tokens": cached},
    }


def _tool_calls_to_openai(message):
    if not message.tool_calls:
        return None
    return [
        {
            "id": call.get("id") or "",
            "type": "function",
            "function": {
                "name": str(call.get("name") or ""),
                "arguments": json.dumps(call.get("args") or {}, ensure_ascii=False),
            },
        }
        for call in message.tool_calls
    ]


def _finish_reason(message):
    if isinstance(message, AIMessage) and message.tool_calls:
        return "tool_calls"
    return str((getattr(message, "response_metadata", None) or {}).get("finish_reason") or "stop")


async def _load_active_run(claims):
    async with pg_manager.get_async_session_context() as db:
        run = await db.get(AgentRun, claims["run_id"])
        payload = (run.input_payload or {}) if run else {}
        lease = payload.get("dsh_execution") or {}
        if (
            run is None
            or run.status != "running"
            or payload.get("agent_runtime") != "dsh"
            or (
                not lease.get("active")
                or lease.get("id") != claims["execution_id"]
                or lease.get("expires_at", 0) <= time.time()
            )
        ):
            return None
        return run


def governed_messages(run, request):
    snapshot = (run.input_payload or {}).get("dsh_context")
    if not isinstance(snapshot, dict):
        raise ValueError("Missing governed DSH input")
    # Replay only approved PG history, preserving its roles. Runtime-generated
    # messages belong to this execution and can never seed a subsequent run.
    items = [_OpenAIMessage(role="system", content=snapshot["system"])]
    items.extend(_OpenAIMessage(**item) for item in snapshot["history"])
    items.extend(item for item in request.messages if item.role != "system")
    return items


def validate_tools(request):
    names = [tool.get("function", {}).get("name") for tool in request.tools or []]
    if names != [ASK_USER_TOOL_NAME]:
        raise ValueError("Runtime tool inventory differs from the Yuxi allowlist")
    if (
        isinstance(request.tool_choice, dict)
        and request.tool_choice.get("function", {}).get("name") != ASK_USER_TOOL_NAME
    ):
        raise ValueError("Tool choice is outside the allowlist")
    if (
        request.tool_choice is not None
        and not isinstance(request.tool_choice, dict)
        and request.tool_choice not in {"auto", "none", "required"}
    ):
        raise ValueError("Unsupported tool choice")


async def _build_chat_model(run):
    from yuxi.agents.models import resolve_chat_model_spec
    from yuxi.services.user_credential_service import open_user_credential_runtime

    payload = run.input_payload or {}
    credential = payload.get("user_credential") or {}
    override = None
    try:
        if credential.get("credential_id") and credential.get("provider_id"):
            async with pg_manager.get_async_session_context() as db:
                runtime = await open_user_credential_runtime(
                    db,
                    str(run.uid),
                    int(credential["credential_id"]),
                    expected_provider_id=str(credential["provider_id"]),
                )
            if not runtime or not runtime.get("api_key"):
                raise PermissionError("Frozen user model credential is unavailable")
            override = set_user_credential_override(
                str(credential["provider_id"]),
                str(runtime["api_key"]),
                model_spec=runtime.get("model_spec"),
                model_id=runtime.get("model_id"),
                base_url=runtime.get("base_url"),
                protocol=runtime.get("protocol"),
            )
        return load_chat_model(resolve_chat_model_spec(payload.get("model_spec")))
    finally:
        if override is not None:
            reset_user_credential_override(override)


@router.post("/v1/chat/completions")
async def chat_completions(request: _ChatCompletionRequest, authorization: str | None = Header(None)):
    try:
        claims = verify_gateway_token(bearer_token_from_authorization(authorization))
    except GatewayTokenError:
        return _error(401, "invalid_gateway_token", "Invalid execution token")
    run = await _load_active_run(claims)
    if run is None:
        return _error(403, "run_not_active", "DSH execution is inactive")
    try:
        from yuxi.agents.models import resolve_chat_model_spec

        if request.model != resolve_chat_model_spec((run.input_payload or {}).get("model_spec")):
            raise ValueError("Model differs from the frozen run")
        validate_tools(request)
        items = governed_messages(run, request)
        messages = openai_messages_to_langchain(items)
        audit = request.model_dump(exclude_none=True)
        audit["messages"] = [item.model_dump(exclude_none=True) for item in items]
        size = len(json.dumps(audit, ensure_ascii=False).encode())
        if size > 8 * 1024 * 1024:
            raise ValueError("Model input exceeds the gateway frame limit")
        # Byte count is deliberately conservative for text/BPE tokenizers.
        # Images reserve the full model context since vision tokenization is provider-specific.
        runtime = (run.input_payload or {})["agent_runtime_snapshot"]
        has_image = any(
            isinstance(item.content, list) and any(block.get("type") == "image_url" for block in item.content)
            for item in items
        )
        input_bound = runtime["context_window"] - runtime["max_output_tokens"] if has_image else size + 1024
        model = await _build_chat_model(run)
        run, call, output_cap = await reserve_call(
            claims,
            audit,
            input_bound,
            request.max_completion_tokens or request.max_tokens or runtime["max_output_tokens"],
        )
    except PermissionError:
        return _error(403, "execution_unavailable", "Execution or credential is unavailable")
    except OverflowError:
        return _error(429, "run_token_budget_exceeded", "Run token budget exhausted")
    except ValueError as exc:
        return _error(400, "invalid_runtime_request", str(exc))
    except Exception as exc:
        logger.error(f"DSH gateway preparation failed ({type(exc).__name__})")
        return _error(503, "gateway_unavailable", "Gateway is unavailable")

    options = {"max_tokens": output_cap}
    if request.temperature is not None:
        options["temperature"] = request.temperature
    if request.reasoning_effort is not None:
        options["reasoning_effort"] = request.reasoning_effort
    try:
        if request.tools:
            model = model.bind_tools(
                request.tools, **({"tool_choice": request.tool_choice} if request.tool_choice else {})
            )
        model = model.bind(**options)
    except Exception:
        await record_call(
            call.id, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, "failed", "model_binding_failed"
        )
        return _error(502, "model_binding_failed", "Model parameters are unsupported")
    completion_id, created = f"chatcmpl-yuxi-{uuid.uuid4().hex}", int(time.time())
    if request.stream:
        from yuxi.agents.buildin.dsh.stream import publish_token_delta, token_stream_enabled

        tap_run_id = str(run.id) if token_stream_enabled() else None
        return StreamingResponse(
            _stream_completion(
                model, messages, call.id, completion_id, created, request.model, tap_run_id, publish_token_delta
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    usage, status, error = None, "failed", None
    try:
        message = await model.ainvoke(messages)
        usage = _usage_from_langchain(message)
        if usage is None:
            error = "provider_usage_missing"
            return _error(502, error, "Provider did not return usage")
        await record_call(call.id, usage, "completed")
        status = "completed"
        reply = {"role": "assistant", "content": _content_text(message.content) or None}
        reasoning = (getattr(message, "additional_kwargs", None) or {}).get("reasoning_content")
        if reasoning is not None:
            reply["reasoning_content"] = reasoning
        calls = _tool_calls_to_openai(message) if isinstance(message, AIMessage) else None
        if calls:
            reply["tool_calls"] = calls
        return JSONResponse(
            content={
                "id": completion_id,
                "object": "chat.completion",
                "created": created,
                "model": request.model,
                "choices": [{"index": 0, "message": reply, "finish_reason": _finish_reason(message)}],
                "usage": usage,
            }
        )
    except asyncio.CancelledError:
        status, error = "cancelled", "client_disconnected"
        raise
    except Exception as exc:
        error = type(exc).__name__
        logger.error(f"DSH provider request failed ({error})")
        return _error(502, "model_request_failed", "Model call failed")
    finally:
        await asyncio.shield(record_call(call.id, usage, status, error))


async def _stream_completion(
    model, messages, call_id, completion_id, created, requested_model, tap_run_id=None, publish=None
):
    usage, finish, status, error = None, None, "failed", None
    try:
        async for chunk in model.astream(messages):
            delta = {}
            text = _content_text(chunk.content)
            if text:
                delta["content"] = text
                if tap_run_id is not None and publish is not None:
                    await publish(tap_run_id, text)
            reasoning = (getattr(chunk, "additional_kwargs", None) or {}).get("reasoning_content")
            if reasoning is not None:
                delta["reasoning_content"] = reasoning
            pieces = getattr(chunk, "tool_call_chunks", None) or []
            if pieces:
                delta["tool_calls"] = []
                for item in pieces:
                    if not isinstance(item.get("index"), int):
                        raise ValueError("Tool chunk index is missing")
                    value = {"index": item["index"], "type": "function", "function": {}}
                    if item.get("id") is not None:
                        value["id"] = item["id"]
                    for source, target in (("name", "name"), ("args", "arguments")):
                        if item.get(source) is not None:
                            value["function"][target] = item[source]
                    delta["tool_calls"].append(value)
            chunk_usage = _usage_from_langchain(chunk)
            if chunk_usage is not None:
                usage = chunk_usage
            finish = (getattr(chunk, "response_metadata", None) or {}).get("finish_reason") or finish
            payload = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": requested_model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            }
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        if usage is None or finish not in {"stop", "tool_calls", "length"}:
            raise ValueError("Provider stream is missing usage or finish_reason")
        status = "completed"
        # Persist before advertising completion, so a successful DSH turn can read authoritative usage.
        await record_call(call_id, usage, status)
        final = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": requested_model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
            "usage": usage,
        }
        yield f"data: {json.dumps(final, ensure_ascii=False)}\n\n"
    except asyncio.CancelledError:
        status, error = "cancelled", "client_disconnected"
        raise
    except Exception as exc:
        error = type(exc).__name__
        logger.error(f"DSH provider stream failed ({error})")
        yield (
            'data: {"error":{"message":"Model stream failed",'
            '"type":"model_stream_failed","code":"model_stream_failed"}}\n\n'
        )
    finally:
        await asyncio.shield(record_call(call_id, usage, status, error))
    yield "data: [DONE]\n\n"
