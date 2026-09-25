"""Host 层错误判定：结构化状态优先，文本前缀兜底（RC5 回归锁）。

错误文本（超时、工具异常被吞成字符串返回）绝不能以 success 身份进入
事实账本——否则会被下游门禁当成"已核验事实"转述，核验体系自我污染。
"""

from __future__ import annotations

from types import SimpleNamespace

from yuxi.agents.mcp.host import _normalize_tool_output


def _normalize(output):
    return _normalize_tool_output(output, provenance={})


def test_langgraph_style_error_executing_text_is_error():
    result = _normalize("Error executing tool ricekb_resolve: timeout after 30s")
    assert result.is_error is True


def test_legacy_error_prefix_still_detected():
    assert _normalize("error: bad arguments").is_error is True
    assert _normalize("  Error: ToolException raised").is_error is True


def test_bracketed_provider_exception_is_error():
    result = _normalize("[UpstreamUnavailableError] Ensembl Plants exhausted 3 retries (last HTTP 500)")
    assert result.is_error is True


def test_structured_tool_message_status_wins_even_with_clean_text():
    result = _normalize(SimpleNamespace(status="error", content="partially rendered text"))
    assert result.is_error is True


def test_mcp_iserror_block_flag_detected():
    result = _normalize(([{"type": "text", "text": "ok", "isError": True}], {"structured_content": {}}))
    assert result.is_error is True


def test_artifact_error_flag_detected():
    result = _normalize((["ok"], {"structured_content": {}, "is_error": True}))
    assert result.is_error is True


def test_normal_tool_text_is_not_error():
    result = _normalize("LOC_Os06g0133000 resolved: Wx, chromosome 6")
    assert result.is_error is False


def test_error_word_mid_text_does_not_falsely_trigger():
    result = _normalize("测序错误率 error rate 为 0.3%，全部样本通过质控。")
    assert result.is_error is False
