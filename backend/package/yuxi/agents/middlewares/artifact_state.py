"""MCP 物化产物的确定性 state 注入中间件。"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware.types import AgentMiddleware

from yuxi.agents.mcp.artifact_materializer import drain_materialized_artifacts


class ArtifactStateMiddleware(AgentMiddleware):
    """把 host 层确定性物化的产物路径合并进 LangGraph ``state.artifacts``。

    ``state.artifacts`` 此前唯一写方是 ``present_artifacts`` 工具（依赖模型
    自觉调用），MCP 查询结果默认不产生任何产物。本中间件在每次模型调用后
    排空 run 级物化累积器（``begin_artifact_accumulation`` 管理、host 层写入），
    经 ``merge_artifacts`` reducer 归并去重——SSE ``agent_state`` 与
    ``/state`` 对 MCP 数据产物恒可见，不再取决于模型行为。
    """

    def after_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        entries = drain_materialized_artifacts()
        if not entries:
            return None
        return {"artifacts": [entry.virtual_path for entry in entries]}

    async def aafter_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        return self.after_model(state, runtime)
