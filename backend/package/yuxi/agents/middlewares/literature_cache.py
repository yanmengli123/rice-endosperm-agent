"""Cache a side-effect-free model candidate, not an authorized final answer.

Placement is inside TokenUsage/KnowledgeContext: cache hits remain graph-native
AI messages and pass the current knowledge, source, citation and content gates.
Tool-call responses, reasoning blocks and structured outputs are never cached.
"""

import uuid

from langchain.agents.middleware.types import AgentMiddleware, ModelResponse
from langchain_core.messages import AIMessage

from yuxi.literature import answer_cache
from yuxi.literature.access import _access
from yuxi.utils.reasoning_visibility import sanitize_visible_text


class LiteratureCandidateCacheMiddleware(AgentMiddleware):
    async def awrap_model_call(self, request, handler):
        # 缓存是优化不是依赖。**指纹渲染类错误**（pydantic schema 渲染异常及其基类
        # RuntimeError/TypeError 的窄集）→ fail-open 跳过缓存：历史上 ToolRuntime 字段
        # 曾让文献会话全部模型调用崩成 "Model call failed after 3 attempts"。
        # **授权/版本/来源语义异常**（PermissionError 及身份计算内建的 ValueError）
        # 原样上抛：失败关闭是安全语义（撤权用户不得借缓存跳过门禁），不允许被
        # 优化层吞掉——由外层既有错误路径转为用户可见失败。
        try:
            identity = await answer_cache.model_cache_identity(request)
        except (PermissionError, ValueError):
            raise
        except (TypeError, RuntimeError) as exc:
            import logging

            # pydantic 的 schema 渲染失败派生自 RuntimeError；类型注解渲染问题派生自
            # TypeError。这类与授权无关，跳过缓存不改变本轮的权限裁决。
            logging.getLogger(__name__).warning(
                "文献候选缓存身份渲染失败，本轮跳过缓存: %s: %s", type(exc).__name__, str(exc)[:120]
            )
            return await handler(request)
        if identity is None or request.response_format is not None:
            return await handler(request)
        token = _access.set(identity["scope"])
        try:
            options = {
                "rid": identity["rid"],
                "scope_fingerprint": identity["fingerprint"],
                "model_spec": identity["model"],
                "policy_version": identity["policy"],
            }
            candidate = await answer_cache.cache_get(identity["prompt"], **options)
        finally:
            _access.reset(token)
        if candidate:
            if identity["key"] != (await answer_cache.model_cache_identity(request) or {}).get("key"):
                raise ValueError("缓存读取期间来源或策略已变更")
            from yuxi.trace import emit_trace

            emit_trace(
                category="MODEL",
                operation="cache",
                event_type="model.cache.hit",
                title="复用模型候选（当前门禁仍执行）",
                attributes={"cache_hit": True},
                visibility="USER",
            )
            return ModelResponse(
                result=[
                    AIMessage(
                        id=f"msg_{uuid.uuid4().hex}",
                        content=candidate,
                        additional_kwargs={"literature_candidate_cache": {"hit": True}},
                        usage_metadata={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                    )
                ]
            )
        response = await handler(request)
        if (
            response.structured_response is not None
            or len(response.result) != 1
            or not isinstance(response.result[0], AIMessage)
        ):
            return response
        message = response.result[0]
        if (
            message.tool_calls
            or message.invalid_tool_calls
            or not isinstance(message.content, str)
            or not message.content.strip()
            or len(message.content.encode()) > 256 * 1024
        ):
            return response
        candidate = sanitize_visible_text(message.content)
        if candidate != message.content or message.additional_kwargs.get("reasoning_content"):
            return response
        if identity["key"] != (await answer_cache.model_cache_identity(request) or {}).get("key"):
            raise ValueError("模型调用期间来源或策略已变更")
        token = _access.set(identity["scope"])
        try:
            await answer_cache.cache_set(identity["prompt"], candidate, **options)
        finally:
            _access.reset(token)
        return response
