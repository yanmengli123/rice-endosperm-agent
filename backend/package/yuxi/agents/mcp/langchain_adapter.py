"""LangChain 适配层：把 MCP 描述符装配成 Agent 可用的 BaseTool。

边界约定：
- 只消费 host 层的 `McpToolDescriptor` / `McpToolResult`，对 langchain_mcp_adapters
  零感知——adapters 换官方 SDK v2 时本层不改；
- 装配出的 BaseTool 元数据统一带：id（stable id）、server、mcp_tool_name，
  管理 UI 与工具开关语义由此保持稳定；
- 跨服务器同名冲突在此消解：后来者改名为 `{serverCamel}_{tool}`，并保留
  metadata 中的原始名，disabled_tools 过滤永远按原始名进行。
"""

from __future__ import annotations

from typing import Any

from yuxi.agents.mcp.host import McpHost, McpHostError, McpToolDescriptor
from yuxi.agents.mcp.spec import to_camel_case
from yuxi.utils import logger


def build_mcp_base_tool(
    descriptor: McpToolDescriptor,
    config: dict[str, Any],
    *,
    model_facing_name: str | None = None,
) -> Any:
    """descriptor → LangChain BaseTool。

    说明：
    - args_schema 直接复用 adapter 构造的 pydantic 模型（零损失），
      缺失时回退通用 dict 形参；
    - coroutine 绑定 host.call_tool，异常路径已在 host 层归一化为
      McpToolResult(is_error=True) 文本，与旧 handle_tool_error 行为等价。
    """
    from langchain_core.tools import StructuredTool
    from pydantic import Field, create_model

    host = _resolve_host()

    args_schema = descriptor.args_model
    # adapters 构造的 schema 在不同栈下可能是 pydantic v2 或 v1-compat 模型，
    # 只要有任一 schema 出口（model_json_schema / schema）就透传，避免误判降级。
    schema_capable = isinstance(args_schema, type) and (
        hasattr(args_schema, "model_json_schema") or hasattr(args_schema, "schema")
    )
    if not schema_capable:
        args_schema = create_model(
            f"{to_camel_case(descriptor.name).capitalize() or 'Mcp'}Args",
            arguments=(dict, Field(default_factory=dict, description="MCP 工具入参对象")),
        )

    async def _arun(**kwargs: Any) -> tuple[str, dict[str, Any]]:
        result = await host.call_tool(descriptor.server_slug, config, descriptor.name, kwargs)
        return result.text, result.to_dict()

    def _run(**kwargs: Any) -> str:  # 同步路径：不主动支持，防误用给出明确报错
        raise McpHostError(
            "MCP 工具仅支持异步调用；请通过 agent 异步执行链路使用",
            stage="transport",
        )

    tool_name = model_facing_name or descriptor.name
    structured = StructuredTool(
        name=tool_name,
        description=descriptor.description or f"MCP tool from {descriptor.server_slug}",
        args_schema=args_schema,
        func=_run,
        coroutine=_arun,
        handle_tool_error=True,
        response_format="content_and_artifact",
        metadata={
            "id": descriptor.stable_id,
            "server": descriptor.server_slug,
            "mcp_tool_name": descriptor.name,
            "model_facing_name": tool_name,
            "aliased": bool(model_facing_name and model_facing_name != descriptor.name),
            "mcp_annotations_untrusted": descriptor.annotations,
            "mcp_output_schema": descriptor.output_schema,
        },
    )
    return structured


def assemble_tools(
    server_groups: list[tuple[str, list[McpToolDescriptor], dict[str, Any]]],
) -> list[Any]:
    """多服务器描述符批量装配 + 跨服务器重名消解。

    Args:
        server_groups: [(slug, descriptors, runtime_config), ...] 按选择顺序排列；
            先出现的保持原名，后续同名者加服务器前缀别名。
    Returns:
        按 group 顺序拼接的 BaseTool 列表。
    """
    seen: set[str] = set()
    tools: list[Any] = []
    for slug, descriptors, config in server_groups:
        prefix = to_camel_case(slug)
        for descriptor in descriptors:
            facing = descriptor.name
            if facing in seen:
                facing = f"{prefix}_{descriptor.name}"
                logger.info(f"Renamed conflicting MCP tool '{descriptor.name}' ({slug}) -> '{facing}'")
            if facing in seen:
                logger.warning(f"工具名在去前缀后仍冲突，跳过：{slug}::{descriptor.name}")
                continue
            seen.add(facing)
            tools.append(build_mcp_base_tool(descriptor, config, model_facing_name=facing))
    return tools


_host_resolver: Any = None


def set_host_resolver(resolver: Any) -> None:
    """由 service 门面注入延迟的 host 解析器，避免模块级环依赖。"""
    global _host_resolver
    _host_resolver = resolver


def _resolve_host() -> McpHost:
    if _host_resolver is None:
        from yuxi.agents.mcp.host import get_host

        return get_host()
    return _host_resolver()


__all__ = ["build_mcp_base_tool", "assemble_tools", "set_host_resolver"]
