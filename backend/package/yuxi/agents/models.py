import contextvars

from langchain.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from yuxi import config as sys_config
from yuxi.models.providers.cache import model_cache
from yuxi.utils import get_docker_safe_url
from yuxi.utils.logging_config import logger


def resolve_chat_model_spec(model_spec: str | None, *, fallback: str | None = None) -> str:
    """解析空模型配置，不吞掉已经配置但无效的模型值。

    这里仅处理模型为空时的优先级：请求或配置值、调用方 fallback、系统默认模型；
    具体模型是否存在、是否为聊天模型仍由 model_cache 校验。
    """
    for candidate in (model_spec, fallback, sys_config.default_model):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    raise ValueError("model spec 不能为空")


# P3 BYOK：Worker 执行期按 run 冻结的用户凭据覆盖平台 Key（任务级隔离）
_user_credential_ctx: "contextvars.ContextVar[tuple[str, str] | None]" = contextvars.ContextVar(
    "user_model_credential", default=None
)


def set_user_credential_override(provider_id: str, api_key: str):
    """在当前异步任务内激活用户凭据；返回可传给 ContextVar.reset 的 token。"""
    return _user_credential_ctx.set((provider_id, api_key))


def reset_user_credential_override(token) -> None:
    _user_credential_ctx.reset(token)


def apply_credential_override(info):
    """纯函数：若上下文中的用户凭据命中该供应商，则替换 ModelInfo 的密钥。"""
    from dataclasses import replace

    override = _user_credential_ctx.get()
    if override and override[0] == info.provider_id and override[1]:
        return replace(info, api_key=override[1])
    return info


def load_chat_model(fully_specified_name: str | None, **kwargs) -> BaseChatModel:
    fully_specified_name = resolve_chat_model_spec(fully_specified_name)

    info = model_cache.get_model_info(fully_specified_name)
    if not info:
        available_specs = model_cache.get_all_specs("chat")
        available_ids = [item.spec for item in available_specs[:10]]
        raise ValueError(
            f"Unknown model spec: '{fully_specified_name}'. "
            f"Available chat models ({len(available_specs)}): {available_ids}"
        )

    if info.model_type != "chat":
        raise ValueError(f"Model {fully_specified_name} is not a chat model (type={info.model_type})")

    info = apply_credential_override(info)

    api_key = info.api_key
    base_url = get_docker_safe_url(info.base_url)

    logger.debug(f"Loading model {fully_specified_name} with provider_type={info.provider_type}")

    if info.provider_type == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=info.model_id,
            api_key=SecretStr(api_key),
            base_url=base_url,
            **kwargs,
        )
    if info.provider_type == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=info.model_id,
            google_api_key=SecretStr(api_key),
            **kwargs,
        )

    return _ToolCallChunkFixChatOpenAI(
        model=info.model_id,
        api_key=SecretStr(api_key),
        base_url=base_url,
        stream_usage=True,
        disable_thinking_for_legacy_tool_history=(
            info.provider_id.lower() == "deepseek" or "api.deepseek.com" in base_url.lower()
        ),
        **kwargs,
    )


class _ToolCallChunkFixChatOpenAI(ChatOpenAI):
    """兼容 OpenAI 风格供应商的流式工具调用和思考内容。"""

    disable_thinking_for_legacy_tool_history: bool = False

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        messages = self._convert_input(input_).to_messages()
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        payload_messages = payload.get("messages")
        if not isinstance(payload_messages, list) or len(payload_messages) != len(messages):
            return payload

        has_legacy_tool_history = False
        for message, payload_message in zip(messages, payload_messages, strict=True):
            if not isinstance(payload_message, dict) or payload_message.get("role") != "assistant":
                continue
            reasoning_content = getattr(message, "additional_kwargs", {}).get("reasoning_content")
            if not isinstance(reasoning_content, str):
                has_legacy_tool_history = has_legacy_tool_history or bool(payload_message.get("tool_calls"))
                continue
            payload_message["reasoning_content"] = reasoning_content
            if payload_message.get("tool_calls") and payload_message.get("content") is None:
                payload_message["content"] = ""

        if self.disable_thinking_for_legacy_tool_history and has_legacy_tool_history:
            extra_body = dict(payload.get("extra_body") or {})
            extra_body["thinking"] = {"type": "disabled"}
            payload["extra_body"] = extra_body
        return payload

    def _convert_chunk_to_generation_chunk(self, chunk, default_chunk_class, base_generation_info):
        generation_chunk = super()._convert_chunk_to_generation_chunk(
            chunk,
            default_chunk_class,
            base_generation_info,
        )
        if generation_chunk is None:
            return None

        choices = chunk.get("choices", []) or chunk.get("chunk", {}).get("choices", [])
        if choices and isinstance(choices[0].get("delta"), dict):
            reasoning_content = choices[0]["delta"].get("reasoning_content")
            if isinstance(reasoning_content, str):
                generation_chunk.message.additional_kwargs["reasoning_content"] = reasoning_content
        return generation_chunk

    def _create_chat_result(self, response, generation_info=None):
        result = super()._create_chat_result(response, generation_info)
        response_dict = response if isinstance(response, dict) else response.model_dump()
        choices = response_dict.get("choices") or []
        for generation, choice in zip(result.generations, choices, strict=False):
            message = choice.get("message") if isinstance(choice, dict) else None
            reasoning_content = message.get("reasoning_content") if isinstance(message, dict) else None
            if isinstance(reasoning_content, str):
                generation.message.additional_kwargs["reasoning_content"] = reasoning_content
        return result

    async def _astream(self, *args, **kwargs):
        async for chunk in super()._astream(*args, **kwargs):
            _normalize_tool_call_chunks(chunk.message)
            yield chunk

    def _stream(self, *args, **kwargs):
        for chunk in super()._stream(*args, **kwargs):
            _normalize_tool_call_chunks(chunk.message)
            yield chunk


def _normalize_tool_call_chunks(message) -> None:
    """把工具调用续片里空字符串的 name/id 归一化为 None。

    LangGraph v3 流式累积对 tool_call 字段是“后值覆盖”：部分 OpenAI 兼容提供商
    （siliconflow、阿里云百炼等）在续片里把 name/id 下发为空字符串 ""，会覆盖首片
    的真实值（siliconflow 丢 name、百炼丢 id），导致工具结果无法按 tool_call_id
    关联、工具状态停留在“进行中”。OpenAI 官方在续片里发 None 不会触发覆盖，这里
    把空串归一化为 None 对齐该行为。待上游修复 v3 协议后可移除。
    """
    for chunk in message.tool_call_chunks:
        if chunk.get("name") == "":
            chunk["name"] = None
        if chunk.get("id") == "":
            chunk["id"] = None
