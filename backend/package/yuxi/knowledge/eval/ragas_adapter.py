"""ragas 适配层：把 yuxi 自研模型层桥接为 ragas 0.4.x collections API 所需组件。

- YuxiRagasLLM: 实现 InstructorBaseRagasLLM（generate/agenerate(prompt, response_model)），
  内部走 LangChainChatAdapter（select_model 产物），用 json_repair 解析结构化输出。
- YuxiRagasEmbedding: 实现 BaseRagasEmbedding，内部走 OtherEmbedding（select_embedding_model 产物）。
- langchain v1 兼容垫片：ragas 0.4.x 顶层 import 依赖 langchain_community.chat_models.vertexai /
  langchain_community.llms.VertexAI，两者在 langchain v1 重组后已移除，且仅被 ragas 用于
  isinstance/能力探测，注入占位类即可。

ragas 未安装时本模块可安全 import（RAGAS_AVAILABLE=False），构造适配器会抛 RuntimeError。
"""

import asyncio
import json
import sys
import types
import typing
from typing import Any, TypeVar

import json_repair

from yuxi.utils import logger

T = TypeVar("T")


def _install_langchain_compat_shims() -> None:
    """为 ragas 0.4.x 注入 langchain v1 重组后缺失的模块符号（幂等）。"""
    for module_name, attr_name in (
        ("langchain_community.chat_models.vertexai", "ChatVertexAI"),
        ("langchain_community.llms", "VertexAI"),
    ):
        try:
            module = __import__(module_name, fromlist=["__name__"])
        except ImportError:
            module = types.ModuleType(module_name)
            sys.modules[module_name] = module
        if not hasattr(module, attr_name):
            setattr(module, attr_name, type(attr_name, (), {"__ragas_compat_shim__": True}))


try:
    _install_langchain_compat_shims()
    from ragas.embeddings.base import BaseRagasEmbedding
    from ragas.llms.base import InstructorBaseRagasLLM
    from ragas.prompt.metrics.base_prompt import BasePrompt

    RAGAS_AVAILABLE = True
except ImportError as e:  # pragma: no cover - 仅在未安装 ragas 的环境触发
    RAGAS_AVAILABLE = False
    _ragas_import_error = e

    class InstructorBaseRagasLLM:  # type: ignore[no-redef]
        """占位基类：ragas 未安装时保证模块可 import。"""

    class BaseRagasEmbedding:  # type: ignore[no-redef]
        """占位基类：ragas 未安装时保证模块可 import。"""

    class BasePrompt:  # type: ignore[no-redef]
        """占位基类：ragas 未安装时保证模块可 import。"""


def require_ragas() -> None:
    if not RAGAS_AVAILABLE:
        raise RuntimeError(
            f"ragas 未安装或不可用（{_ragas_import_error}）。"
            "请在启用 RAGAS 评估前安装 ragas 依赖组（yuxi[ragas]）。"
        )


def _strip_code_fences(text: str) -> str:
    content = text.strip()
    if content.startswith("```"):
        first_newline = content.find("\n")
        if first_newline != -1:
            content = content[first_newline + 1 :]
        if content.rstrip().endswith("```"):
            content = content.rstrip()[:-3]
    return content.strip()


def _coerce_parsed(parsed: Any, response_model: type) -> Any:
    """模型常见偏差纠正：
    1. 直接输出 JSON 数组 → 包一层到唯一的 list 型字段；
    2. 输出对象但字段名错误（如 translation 代替 statements）且值为数组 → 改名为缺失的必填 list 字段。
    """
    if isinstance(parsed, list):
        list_fields = [
            name
            for name, field in response_model.model_fields.items()
            if typing.get_origin(field.annotation) is list
        ]
        if len(list_fields) == 1:
            return {list_fields[0]: parsed}
    elif isinstance(parsed, dict):
        required_list_fields = [
            name
            for name, field in response_model.model_fields.items()
            if field.is_required() and typing.get_origin(field.annotation) is list
        ]
        unknown_keys = [key for key in parsed if key not in response_model.model_fields]
        if (
            len(required_list_fields) == 1
            and required_list_fields[0] not in parsed
            and len(unknown_keys) == 1
            and isinstance(parsed[unknown_keys[0]], list)
        ):
            return {**parsed, required_list_fields[0]: parsed.pop(unknown_keys[0])}
    return parsed


def _strict_retry_suffix(response_model: type) -> str:
    schema = json.dumps(response_model.model_json_schema(), ensure_ascii=False)
    return (
        "\n\n你上一次的输出不符合要求。请严格只输出一个 JSON 对象，"
        f"必须完全符合以下 JSON Schema（字段名必须一致）：\n{schema}\n"
        "不要输出任何解释、Markdown 代码块或多余文本。"
    )


class YuxiRagasLLM(InstructorBaseRagasLLM):
    """把 LangChainChatAdapter 适配为 ragas InstructorBaseRagasLLM。

    ragas 的结构化输出协议：agenerate(prompt: str, response_model: Type[BaseModel]) -> BaseModel。
    实现：调用模型 -> 剥离代码围栏 -> json_repair 解析 -> pydantic 校验，失败带提示重试。
    """

    def __init__(self, chat_adapter: Any, *, max_parse_retries: int = 2):
        require_ragas()
        self._chat = chat_adapter
        self._max_parse_retries = max_parse_retries
        self._llm_call_total = 0

    async def agenerate(self, prompt: str, response_model: type):
        last_error: Exception | None = None
        for attempt in range(self._max_parse_retries + 1):
            full_prompt = prompt if attempt == 0 else prompt + _strict_retry_suffix(response_model)
            try:
                self._llm_call_total += 1
                response = await self._chat.call(full_prompt, stream=False)
                content = (response.content if response else "") or ""
                parsed = json_repair.loads(_strip_code_fences(content))
                if isinstance(parsed, str):
                    parsed = json_repair.loads(parsed)
                return response_model.model_validate(_coerce_parsed(parsed, response_model))
            except Exception as e:
                last_error = e
                logger.debug(f"ragas LLM 结构化输出解析失败（第{attempt + 1}次）: {e}")
        raise RuntimeError(f"ragas LLM 输出无法解析为 {response_model.__name__}: {last_error}")

    def generate(self, prompt: str, response_model: type):
        try:
            asyncio.get_running_loop()
            raise RuntimeError("同步 generate() 不能在事件循环内调用，请使用 agenerate()")
        except RuntimeError as e:
            if "agenerate()" in str(e):
                raise
        return asyncio.run(self.agenerate(prompt, response_model))

    def usage_stats(self) -> dict[str, int]:
        return {"llm_calls": getattr(self, "_llm_call_total", 0)}


class YuxiRagasEmbedding(BaseRagasEmbedding):
    """把 OtherEmbedding 适配为 ragas BaseRagasEmbedding。"""

    def __init__(self, embedding_model: Any):
        require_ragas()
        self._embedding_model = embedding_model
        self._embedding_call_total = 0

    async def aembed_text(self, text: str, **kwargs: Any) -> list[float]:
        vectors = await self._embedding_model.aencode([text])
        self._embedding_call_total += 1
        return vectors[0]

    def embed_text(self, text: str, **kwargs: Any) -> list[float]:
        vectors = self._embedding_model.encode([text])
        self._embedding_call_total += 1
        return vectors[0]

    async def aembed_texts(self, texts: list[str], **kwargs: Any) -> list[list[float]]:
        if not texts:
            return []
        vectors = await self._embedding_model.aencode(list(texts))
        self._embedding_call_total += len(texts)
        return vectors

    def embed_texts(self, texts: list[str], **kwargs: Any) -> list[list[float]]:
        if not texts:
            return []
        vectors = self._embedding_model.encode(list(texts))
        self._embedding_call_total += len(texts)
        return vectors

    def usage_stats(self) -> dict[str, int]:
        return {"embedding_calls": self._embedding_call_total}
