"""MCP 数据产物确定性物化的行为契约。

设计原则：
- 成功的数据查询结果由程序落盘（结构化 JSON 优先、长文本次之），是否出现
  产物不依赖模型调用 present_artifacts；
- 小结果留在回答正文（不产生产物噪音）；错误结果绝不物化；
- 护栏：单文件上限与单 run 文件数上限，超限只记日志不阻断；
- 累积器排空进 state.artifacts 由 ArtifactStateMiddleware.after_model 消费。
"""

from __future__ import annotations

import pytest

from yuxi.agents.mcp.artifact_materializer import (
    MCP_RESULTS_DIR_NAME,
    begin_artifact_accumulation,
    drain_materialized_artifacts,
    end_artifact_accumulation,
    materialize_mcp_data_result,
)
from yuxi.agents.mcp.execution import McpExecutionContext, reset_mcp_execution_context, set_mcp_execution_context

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

_LONG_TEXT = "A" * 2500
_SHORT_TEXT = "短结果"


@pytest.fixture(autouse=True)
def _no_artifact_registration(monkeypatch):
    """单测不直连 Postgres：run_artifacts 写侧行为由 test_run_artifacts_projection.py 用 SQLite 覆盖。"""

    async def _skip(*, context, entry):
        return False

    monkeypatch.setattr("yuxi.agents.mcp.artifact_materializer.register_run_artifact", _skip)


@pytest.fixture
def materialize_outputs(monkeypatch, tmp_path):
    """把物化落盘根指到临时目录，屏蔽真实线程目录与线程目录初始化。"""
    outputs_dir = tmp_path / "outputs"
    monkeypatch.setattr("yuxi.agents.backends.sandbox.ensure_thread_dirs", lambda thread_id, uid: None)
    monkeypatch.setattr("yuxi.agents.backends.sandbox.sandbox_outputs_dir", lambda thread_id: outputs_dir)
    return outputs_dir


@pytest.fixture
def run_scope():
    """同时开启 MCP 执行身份与产物累积器（对齐 run 边界的真实时序）。"""
    token = set_mcp_execution_context(
        McpExecutionContext(tenant_id=1, uid="u1", thread_id="t-mat", run_id="r-mat")
    )
    accumulation_token = begin_artifact_accumulation()
    try:
        yield accumulation_token
    finally:
        end_artifact_accumulation(accumulation_token)
        reset_mcp_execution_context(token)


async def test_structured_content_materializes_json(materialize_outputs, run_scope):
    entry = await materialize_mcp_data_result(
        server_slug="ricekb",
        tool_name="search_genes",
        result_text=_SHORT_TEXT,
        structured_content={"rows": [{"gene": "Os06g0133000"}]},
        is_error=False,
        mcp_call_audit_id=7,
    )
    assert entry is not None
    assert entry.virtual_path.startswith(f"/home/gem/user-data/outputs/{MCP_RESULTS_DIR_NAME}/")
    assert entry.virtual_path.endswith(".json")
    assert entry.media_type == "application/json"
    assert entry.origin["source"] == "mcp"
    assert entry.origin["mcp_call_audit_id"] == 7
    target = materialize_outputs / MCP_RESULTS_DIR_NAME / entry.name
    assert target.exists()
    assert entry.size_bytes == target.stat().st_size


async def test_long_text_materializes_markdown(materialize_outputs, run_scope):
    entry = await materialize_mcp_data_result(
        server_slug="bio-mcp",
        tool_name="blast",
        result_text=_LONG_TEXT,
        structured_content=None,
        is_error=False,
        mcp_call_audit_id=None,
    )
    assert entry is not None
    assert entry.virtual_path.endswith(".md")
    assert entry.media_type == "text/markdown"
    assert (materialize_outputs / MCP_RESULTS_DIR_NAME / entry.name).read_text(encoding="utf-8") == _LONG_TEXT


async def test_short_plain_result_and_error_result_are_not_materialized(materialize_outputs, run_scope):
    assert (
        await materialize_mcp_data_result(
            server_slug="s",
            tool_name="t",
            result_text=_SHORT_TEXT,
            structured_content=None,
            is_error=False,
            mcp_call_audit_id=None,
        )
        is None
    )
    assert (
        await materialize_mcp_data_result(
            server_slug="s",
            tool_name="t",
            result_text=_LONG_TEXT,
            structured_content=None,
            is_error=True,
            mcp_call_audit_id=None,
        )
        is None
    )
    assert not (materialize_outputs / MCP_RESULTS_DIR_NAME).exists()


async def test_missing_scope_or_context_is_tolerated(materialize_outputs):
    # 无累积器（run 边界外，如离线评估）：不物化、不抛异常
    token = set_mcp_execution_context(
        McpExecutionContext(tenant_id=1, uid="u1", thread_id="t-x", run_id="r-x")
    )
    try:
        assert (
            await materialize_mcp_data_result(
                server_slug="s",
                tool_name="t",
                result_text=_LONG_TEXT,
                structured_content=None,
                is_error=False,
                mcp_call_audit_id=None,
            )
            is None
        )
    finally:
        reset_mcp_execution_context(token)
    # 有累积器但无 MCP 执行身份（无线程上下文）：不物化
    accumulation_token = begin_artifact_accumulation()
    try:
        assert (
            await materialize_mcp_data_result(
                server_slug="s",
                tool_name="t",
                result_text=_LONG_TEXT,
                structured_content=None,
                is_error=False,
                mcp_call_audit_id=None,
            )
            is None
        )
    finally:
        end_artifact_accumulation(accumulation_token)


async def test_per_run_file_limit_blocks_excess(materialize_outputs, run_scope):
    from yuxi.agents.mcp.artifact_materializer import _MAX_FILES_PER_RUN

    entries = []
    for index in range(_MAX_FILES_PER_RUN + 2):
        entry = await materialize_mcp_data_result(
            server_slug="s",
            tool_name=f"t{index}",
            result_text=f"{index}" + _LONG_TEXT,
            structured_content=None,
            is_error=False,
            mcp_call_audit_id=None,
        )
        if entry is not None:
            entries.append(entry)
    assert len(entries) == _MAX_FILES_PER_RUN


async def test_accumulator_drain_feeds_state_middleware(materialize_outputs, run_scope):
    from yuxi.agents.middlewares.artifact_state import ArtifactStateMiddleware

    entry = await materialize_mcp_data_result(
        server_slug="s",
        tool_name="t",
        result_text=_LONG_TEXT,
        structured_content=None,
        is_error=False,
        mcp_call_audit_id=None,
    )
    assert entry is not None

    middleware = ArtifactStateMiddleware()
    update = middleware.after_model(state={}, runtime=None)
    assert update == {"artifacts": [entry.virtual_path]}

    # 排空后不再重复上报；after_model 返回 None 表示无 state 更新
    assert middleware.after_model(state={}, runtime=None) is None
    assert await middleware.aafter_model(state={}, runtime=None) is None


async def test_same_payload_dedupes_by_digest_filename(materialize_outputs, run_scope):
    first = await materialize_mcp_data_result(
        server_slug="s",
        tool_name="t",
        result_text=_LONG_TEXT,
        structured_content=None,
        is_error=False,
        mcp_call_audit_id=None,
    )
    second = await materialize_mcp_data_result(
        server_slug="s",
        tool_name="t",
        result_text=_LONG_TEXT,
        structured_content=None,
        is_error=False,
        mcp_call_audit_id=None,
    )
    assert first is not None and second is not None
    # 相同负载 → 相同摘要文件名（内容寻址幂等），累积器两条（merge_artifacts 归并去重）
    assert first.name == second.name
