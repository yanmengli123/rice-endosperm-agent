"""运行流错误归一：连接类失败与通用错误的分类契约。

背景：模型供应商连接中断（ModelRetryMiddleware 耗尽后整 run 失败）曾被统一
超时文案（"服务端长时间未收到检索或模型输出…"）掩盖，把网络故障误导成检索/
看门狗问题。契约分两层：chat_service 源头按异常因果链分类（强信号），run_worker
chunk 映射层对未分类生产者做文本兜底（弱信号）。
"""

from __future__ import annotations

from yuxi.services.run_stream_errors import (
    MODEL_CONNECTION_ERROR,
    is_connection_error_text,
    is_model_connection_error,
    normalize_stream_error_chunk,
)


class APIConnectionError(Exception):
    """openai 形态：类型名 + 消息双命中。"""


class ModelRetryExhausted(Exception):
    """langchain ModelRetryMiddleware 耗尽后的包装异常（消息内嵌底层错误）。"""


def test_connection_exception_type_name_matched():
    assert is_model_connection_error(APIConnectionError("Connection error."))


def test_wrapped_exception_message_matched():
    wrapped = ModelRetryExhausted("Model call failed after 3 attempts with APIConnectionError: Connection error.")
    assert is_model_connection_error(wrapped)


def test_cause_chain_matched():
    try:
        try:
            raise APIConnectionError("Connection error.")
        except APIConnectionError as inner:
            raise RuntimeError("stream failed") from inner
    except RuntimeError as chained:
        assert is_model_connection_error(chained)


def test_generic_exception_not_matched():
    assert not is_model_connection_error(ValueError("bad payload shape"))
    assert not is_model_connection_error(RuntimeError("tool crashed"))
    assert not is_connection_error_text("")


def test_normalize_overwrites_only_generic_types():
    chunk = {
        "status": "error",
        "error_type": "unexpected_error",
        "error_message": (
            "Error streaming messages: Model call failed after 3 attempts with APIConnectionError: Connection error."
        ),
    }
    normalized = normalize_stream_error_chunk(chunk)
    assert normalized["error_type"] == MODEL_CONNECTION_ERROR
    assert normalized["retryable"] is True
    assert normalized["error_message"] != chunk["error_message"]
    # 返回新 dict，绝不改写调用方原对象
    assert chunk["error_type"] == "unexpected_error"


def test_normalize_preserves_specific_types():
    chunk = {
        "status": "error",
        "error_type": "run_idle_timeout",
        "error_message": "服务端长时间未收到检索或模型输出，已安全结束本次任务，请重试。",
    }
    assert normalize_stream_error_chunk(chunk) is chunk


def test_normalize_ignores_non_connection_messages():
    chunk = {"status": "error", "error_type": "", "error_message": "工具执行失败: KeyError"}
    assert normalize_stream_error_chunk(chunk) is chunk


def test_normalize_ignores_non_error_chunks():
    chunk = {"status": "loading", "content": "text"}
    assert normalize_stream_error_chunk(chunk) is chunk
