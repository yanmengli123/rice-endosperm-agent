"""MCP Service — 门面（Facade）层：统一入口，薄编排。

架构分层（详见 ARCHITECTURE.md「MCP 子系统」）：
- 本模块只负责：配置装载 → 策略闸门 → Host 探测/调用 → LangChain 装配 → 持久化；
- 协议栈细节在 `host.py`（对外仅暴露自有 domain model，业务代码不得直接
  import langchain_mcp_adapters）；
- 格式翻译在 `spec.py` / `registry.py`；策略在 `policy.py`；
  结构化诊断在 `health.py`；Agent 工具装配在 `langchain_adapter.py`。

公开函数签名与旧版完全兼容：dynamic_tool 中间件、toolkits 服务、mcp_router
均无需感知本次重构。
"""

import shutil
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.agents.mcp.health import (
    CODE_CLIENT_INIT_FAILED,
    CODE_CONFIG_MISSING,
    CODE_DISCOVERY_FAILED,
    STAGE_CONFIG,
    STAGE_DISCOVERY,
    STAGE_RUNTIME,
    McpHealthResult,
    error_result,
    ok_result,
)
from yuxi.agents.mcp.host import McpHostError, get_host
from yuxi.agents.mcp.langchain_adapter import assemble_tools, set_host_resolver
from yuxi.agents.mcp.policy import (
    PolicyError,
    assert_transport_allowed,
    expand_env_refs,
)
from yuxi.agents.mcp.registry import (
    SOURCE_TYPE_BUILTIN,
    SOURCE_TYPE_MANUAL,
    ImportFormatError,
    smart_parse,
)
from yuxi.agents.mcp.spec import (
    ARTIFACT_NPM,
    NormalizationError,
    SPEC_SCHEMA_VERSION,
    build_plan_from_legacy_fields,
    to_camel_case,
)
from yuxi.storage.postgres.models_business import MCPServer
from yuxi.utils import logger

set_host_resolver(get_host)

# =============================================================================
# === Builtin Servers ===
# =============================================================================

# Default MCP Server configurations (Imported to DB on first run)
# 环境变量引用语法：值形如 ${VAR} 时运行时从进程环境解析，缺失则剔除并告警，
# 数据库里永不保存明文凭据。
_DEFAULT_MCP_SERVERS = {
    "mcp-server-chart": {
        "name": "图表生成",
        "command": "npx",
        "args": ["-y", "@antv/mcp-server-chart"],
        "transport": "stdio",
        "description": "图表生成工具，支持生成各类图表（柱状图、折线图、饼图等）",
        "icon": "📊",
        "tags": ["内置", "图表"],
        "source_type": SOURCE_TYPE_BUILTIN,
        "source_ref": "builtin:mcp-server-chart",
    },
    # P0：BioMCP 以独立 uvx 环境接入（包内依赖 mcp 2.x，绝不 pip 进主环境）。
    # 版本必须 pin；UV_DEFAULT_INDEX 走 ${} 引用，国内网络可在容器环境配置。
    "bio-mcp": {
        "name": "BioMCP",
        "command": "uvx",
        "args": ["--from", "bio-mcp==0.5.0", "bio-mcp"],
        "transport": "stdio",
        "description": "生物信息学 MCP：覆盖 36 个公开数据库 / 68 个工具"
        "（文献、基因、变异、通路、植物科研等），来源 qgeng1465/bio-mcp",
        "icon": "🧬",
        "tags": ["内置", "生物信息"],
        "env": {"UV_DEFAULT_INDEX": "${UV_DEFAULT_INDEX}"},
        "source_type": SOURCE_TYPE_BUILTIN,
        "source_ref": "builtin:bio-mcp",
    },
}

_RETIRED_BUILTIN_MCP_SERVER_SLUGS = ("sequentialthinking",)

_SYNCED_MCP_FIELDS = (
    "description",
    "transport",
    "url",
    "command",
    "args",
    "env",
    "headers",
    "timeout",
    "sse_read_timeout",
    "tags",
    "icon",
    "source_type",
    "source_ref",
)


def _is_builtin_source(source_type: str | None, created_by: str | None = None, slug: str | None = None) -> bool:
    """内置判定的唯一口径：system 创建人或 builtin source_type。

    注意不能用"slug 在默认表里"来判定——那会让手工新建同 slug 的行绕过 stdio 白名单。
    """
    if created_by == "system":
        return True
    if source_type == SOURCE_TYPE_BUILTIN:
        return True
    # 兼容存量数据：迁移前创建的内置行没有 source_type，仅当行为与默认定义一致时认作内置
    return slug in _DEFAULT_MCP_SERVERS and created_by == "system"


_UNSET_SENTINEL = object()


# =============================================================================
# === 配置归一化（legacy 行 -> host 可用配置）===
# =============================================================================


def build_runtime_config(slug: str, server_config: dict[str, Any]) -> dict[str, Any]:
    """把数据库行配置转换为 host 可用配置：展开 env/headers 的 ${VAR} 引用。"""
    runtime = {k: v for k, v in server_config.items() if k != "disabled_tools"}
    missing_notes: list[str] = []

    for section in ("env", "headers"):
        value = runtime.get(section)
        if isinstance(value, dict) and value:
            resolved, missing = expand_env_refs(value)
            if missing:
                missing_notes.extend(f"{section}.{item}" for item in missing)
            runtime[section] = resolved or None
        elif value is not None:
            runtime[section] = value

    if missing_notes:
        logger.warning(
            f"MCP '{slug}' 引用的环境变量不存在，相关条目已剔除: {', '.join(missing_notes)}"
        )
    return runtime


def build_spec(*, transport: str, url: str | None, command: str | None, args: list | None) -> dict[str, Any]:
    """从扁平字段推导 InstallPlan spec；失败时返回未归一化标记而不是抛错。"""
    try:
        plan = build_plan_from_legacy_fields(transport=transport, url=url, command=command, args=args)
        return plan.to_dict()
    except NormalizationError as e:
        logger.warning(f"MCP spec 归一化失败（保留原始连接字段）: {e}")
        return {"schema_version": SPEC_SCHEMA_VERSION, "normalized": False, "error": str(e)}


async def _load_enabled_mcp_server_configs(
    *,
    names: list[str] | None = None,
    db: AsyncSession | None = None,
) -> dict[str, dict[str, Any]]:
    """Load enabled MCP server configs directly from the database."""
    if db is not None:
        stmt = select(MCPServer).where(MCPServer.enabled == 1)
        if names:
            stmt = stmt.where(MCPServer.slug.in_(names))
        result = await db.execute(stmt)
        servers = result.scalars().all()
        return {server.slug: server.to_mcp_config() for server in servers}

    from yuxi.storage.postgres.manager import pg_manager

    async with pg_manager.get_async_session_context() as session:
        return await _load_enabled_mcp_server_configs(names=names, db=session)


async def get_enabled_mcp_server_config(server_slug: str, *, db: AsyncSession | None = None) -> dict[str, Any] | None:
    """Get the latest enabled MCP server config from the database."""
    configs = await _load_enabled_mcp_server_configs(names=[server_slug], db=db)
    return configs.get(server_slug)


async def get_enabled_mcp_server_slugs(*, db: AsyncSession | None = None) -> list[str]:
    """Get enabled MCP server slugs from the database."""
    if db is not None:
        result = await db.execute(select(MCPServer.slug).where(MCPServer.enabled == 1))
        return [name for name in result.scalars().all() if isinstance(name, str)]

    from yuxi.storage.postgres.manager import pg_manager

    async with pg_manager.get_async_session_context() as session:
        return await get_enabled_mcp_server_slugs(db=session)


# =============================================================================
# === 内置服务器同步（Code <-> Database）===
# =============================================================================


async def ensure_builtin_mcp_servers_in_db() -> None:
    """Ensure built-in MCP server definitions exist in the database."""
    from yuxi.storage.postgres.manager import pg_manager

    try:
        async with pg_manager.get_async_session_context() as session:
            any_changed = False
            for slug in _RETIRED_BUILTIN_MCP_SERVER_SLUGS:
                result = await session.execute(
                    select(MCPServer).filter(MCPServer.slug == slug, MCPServer.created_by == "system")
                )
                retired = result.scalar_one_or_none()
                if retired:
                    await session.delete(retired)
                    clear_mcp_server_tools_cache(slug)
                    any_changed = True
                    logger.info(f"Removed retired built-in MCP server '{slug}' from database")

            for slug, config in _DEFAULT_MCP_SERVERS.items():
                result = await session.execute(select(MCPServer).filter(MCPServer.slug == slug))
                existing = result.scalar_one_or_none()
                if not existing:
                    session.add(MCPServer(**_builtin_row_values(slug, config)))
                    any_changed = True
                    logger.info(f"Added built-in MCP server '{slug}' to database")
                    continue

                server_changed = False
                for field in _SYNCED_MCP_FIELDS:
                    next_value = config.get(field)
                    if getattr(existing, field) != next_value:
                        setattr(existing, field, next_value)
                        server_changed = True
                if server_changed:
                    existing.updated_by = "system"
                    any_changed = True

            if any_changed:
                await session.commit()

    except Exception as e:
        logger.exception(f"Failed to ensure builtin MCP servers in database: {e}")


def _builtin_row_values(slug: str, config: dict[str, Any]) -> dict[str, Any]:
    """builtin 条目 → MCPServer 行字段。"""
    plan = build_plan_from_legacy_fields(
        transport=config["transport"], url=config.get("url"), command=config.get("command"), args=config.get("args")
    )
    values: dict[str, Any] = {
        "slug": slug,
        "name": config.get("name", slug),
        "description": config.get("description"),
        "transport": config["transport"],
        "url": config.get("url"),
        "command": config.get("command"),
        "args": config.get("args"),
        "env": config.get("env"),
        "headers": config.get("headers"),
        "timeout": config.get("timeout"),
        "sse_read_timeout": config.get("sse_read_timeout"),
        "tags": config.get("tags"),
        "icon": config.get("icon"),
        "enabled": 0,
        "disabled_tools": None,
        "source_type": SOURCE_TYPE_BUILTIN,
        "source_ref": config.get("source_ref", f"{SOURCE_TYPE_BUILTIN}:{slug}"),
        "spec": plan.to_dict(),
        "created_by": "system",
        "updated_by": "system",
    }
    return values


# =============================================================================
# === Agent 工具获取 ===
# =============================================================================


async def get_mcp_tools(
    server_slug: str,
    additional_servers: dict[str, dict[str, Any]] | None = None,
    disabled_tools: list[str] | None = None,
    cache: bool = True,
    force_refresh: bool = False,
) -> list[Any]:
    """Get MCP tools for a specific server（返回装配后的 LangChain BaseTool 列表）。

    Architecture:
    1. Discovering: 经 McpHost 连接 MCP server 获取全量描述符（缓存 key=slug:config_hash）
    2. Assembling: 只对过滤后存活的工具装配 BaseTool（模型可见名冲突自动加前缀别名）
    3. Filtering: disabled_tools 仅影响返回值（全局禁用由 get_enabled_mcp_tools 合入）

    失败返回空列表；结构化失败原因请使用 probe_mcp_server()。
    """
    if additional_servers and server_slug in additional_servers:
        server_config = additional_servers[server_slug]
    else:
        server_config = await get_enabled_mcp_server_config(server_slug)

    if server_config is None:
        logger.warning(f"MCP server '{server_slug}' not found in database or disabled")
        return []

    runtime_config = build_runtime_config(server_slug, server_config)

    try:
        descriptors, info = await get_host().discover(
            server_slug,
            runtime_config,
            force_refresh=force_refresh,
            update_cache=cache,
        )
    except McpHostError as e:
        logger.error(f"Failed to load tools from MCP server '{server_slug}': [{e.stage}/{e.code}] {e}")
        return []
    except Exception as e:  # noqa: BLE001 —— 保持旧版外层契约
        logger.exception(f"Failed to load tools from MCP server '{server_slug}': {e}")
        return []

    global_disabled = set(server_config.get("disabled_tools") or [])
    arg_disabled = set(disabled_tools or [])
    alive = [
        d
        for d in descriptors
        if d.name not in global_disabled and d.name not in arg_disabled
    ]

    if cache:
        get_host().note_filter(
            server_slug,
            len([d for d in descriptors if d.name in global_disabled]),
        )

    if not alive:
        logger.info(f"MCP server '{server_slug}' exposes 0 usable tools ({info.tool_count} discovered)")
        return []

    logger.debug(f"Assembling {len(alive)}/{len(descriptors)} MCP tools for '{server_slug}'")
    return assemble_tools([(server_slug, alive, runtime_config)])


async def get_tools_from_all_servers() -> list[Any]:
    """Get all tools from all configured MCP servers（跨服务器重名在此一次性消解）。"""
    server_configs = await _load_enabled_mcp_server_configs()
    groups: list[tuple[str, list[Any], dict[str, Any]]] = []
    for server_slug in server_configs:
        runtime_config = build_runtime_config(server_slug, server_configs[server_slug])
        try:
            descriptors, _info = await get_host().discover(server_slug, runtime_config)
        except Exception as e:  # noqa: BLE001 —— 含 McpHostError；单个服务器失败不阻塞整体装配
            logger.warning(f"Skip MCP '{server_slug}' during global tool load: {e}")
            continue
        groups.append((server_slug, descriptors, runtime_config))
    return assemble_tools(groups)


def clear_mcp_cache() -> None:
    """Clear the MCP tools cache (useful for testing)."""
    get_host().clear_all()


def clear_mcp_server_tools_cache(server_slug: str) -> None:
    """Clear the tools cache for a specific MCP server."""
    get_host().clear_server_cache(server_slug)


def get_mcp_tools_stats(server_slug: str) -> dict[str, int] | None:
    """Get tools statistics for a MCP server."""
    return get_host().get_stats(server_slug)


async def get_enabled_mcp_tools(server_slug: str) -> list[Any]:
    """Get MCP server tools (auto-filtering disabled_tools)。Agent 统一入口。"""
    config = await get_enabled_mcp_server_config(server_slug)
    if config is None:
        logger.warning(f"MCP server '{server_slug}' not found in database or disabled")
        return []

    disabled_tools = config.get("disabled_tools") or []
    return await get_mcp_tools(server_slug, additional_servers={server_slug: config}, disabled_tools=disabled_tools)


async def get_all_mcp_tools(server_slug: str) -> list[Any]:
    """Get all tools of an MCP server (no filtering)。管理 UI 用，不写缓存。"""
    config = await get_enabled_mcp_server_config(server_slug)
    if config is None:
        logger.warning(f"MCP server '{server_slug}' not found in database or disabled")
        return []

    runtime_config = build_runtime_config(server_slug, config)
    try:
        descriptors, _info = await get_host().discover(
            server_slug, runtime_config, force_refresh=True, update_cache=False
        )
    except McpHostError as e:
        logger.error(f"Failed to load tools from MCP server '{server_slug}': [{e.stage}/{e.code}] {e}")
        raise
    except Exception as e:  # noqa: BLE001
        logger.error(f"Failed to load tools from MCP server '{server_slug}': {e}")
        raise
    alive = [d for d in descriptors if d.name not in set(config.get("disabled_tools") or [])]
    return assemble_tools([(server_slug, alive, runtime_config)])


async def get_mcp_client(server_configs: dict[str, Any] | None = None) -> Any | None:
    """Deprecated: 旧直连适配器入口，仅为兼容保留；新代码一律走 McpHost。"""
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient

        return MultiServerMCPClient(server_configs)  # pyright: ignore[reportArgumentType]
    except Exception as e:
        logger.error("Failed to initialize MCP client: {}", e)
        return None


# =============================================================================
# === Server Config CRUD ===
# =============================================================================


def to_camel_case(s: str) -> str:
    """Convert string to lowerCamelCase。（规范实现位于 spec.py，此处兼容保留）"""
    return to_camel_case(s)


async def get_mcp_server(db: AsyncSession, slug: str) -> MCPServer | None:
    """Get single server configuration by slug."""
    result = await db.execute(select(MCPServer).filter(MCPServer.slug == slug))
    return result.scalar_one_or_none()


async def get_all_mcp_servers(db: AsyncSession) -> list[MCPServer]:
    """Get all server configurations."""
    result = await db.execute(select(MCPServer))
    return list(result.scalars().all())


def _validate_for_policy(
    *,
    transport: str,
    command: str | None,
    created_by: str | None = None,
    source_type: str | None = None,
) -> None:
    """创建/更新共用的策略闸门。"""
    is_builtin = _is_builtin_source(source_type, created_by)
    effective_source = SOURCE_TYPE_BUILTIN if is_builtin else (source_type or SOURCE_TYPE_MANUAL)
    assert_transport_allowed(transport, source_type=effective_source, command=command)


async def create_mcp_server(
    db: AsyncSession,
    slug: str,
    name: str,
    transport: str,
    url: str = None,
    command: str = None,
    args: list = None,
    env: dict = None,
    description: str = None,
    headers: dict = None,
    timeout: int = None,
    sse_read_timeout: int = None,
    tags: list = None,
    icon: str = None,
    created_by: str = None,
    source_type: str = None,
    source_ref: str = None,
) -> MCPServer:
    """Create server（先过策略闸门，落库时同步生成 InstallPlan spec）。"""
    existing = await get_mcp_server(db, slug)
    if existing:
        raise ValueError(f"Server slug '{slug}' already exists")

    _validate_for_policy(
        transport=transport, command=command, created_by=created_by, source_type=source_type
    )

    server = MCPServer(
        slug=slug,
        name=name,
        description=description,
        transport=transport,
        url=url,
        command=command,
        args=args,
        env=env,
        headers=headers,
        timeout=timeout,
        sse_read_timeout=sse_read_timeout,
        tags=tags,
        icon=icon,
        enabled=1,
        source_type=source_type or SOURCE_TYPE_MANUAL,
        source_ref=source_ref,
        spec=build_spec(transport=transport, url=url, command=command, args=args),
        created_by=created_by,
        updated_by=created_by,
    )
    db.add(server)
    await db.commit()
    await db.refresh(server)

    clear_mcp_server_tools_cache(slug)

    logger.info(f"Created MCP server '{slug}'")
    return server


async def update_mcp_server(
    db: AsyncSession,
    slug: str,
    name: str = None,
    description: str = None,
    transport: str = None,
    url: str = None,
    command: str = None,
    args: list = None,
    env: Any = _UNSET_SENTINEL,
    headers: dict = None,
    timeout: int = None,
    sse_read_timeout: int = None,
    tags: list = None,
    icon: str = None,
    updated_by: str = None,
) -> MCPServer:
    """Update server configuration。transport/command 变更会重新过策略闸门。"""
    server = await get_mcp_server(db, slug)
    if not server:
        raise ValueError(f"Server '{slug}' does not exist")

    if name is not None:
        server.name = name
    if description is not None:
        server.description = description
    if transport is not None:
        server.transport = transport
    if url is not None:
        server.url = url
    if command is not None:
        server.command = command
    if args is not None:
        server.args = args
    if env is not _UNSET_SENTINEL:
        server.env = env
    if headers is not None:
        server.headers = headers
    if timeout is not None:
        server.timeout = timeout
    if sse_read_timeout is not None:
        server.sse_read_timeout = sse_read_timeout
    if tags is not None:
        server.tags = tags
    if icon is not None:
        server.icon = icon
    if updated_by is not None:
        server.updated_by = updated_by

    _validate_for_policy(
        transport=server.transport,
        command=server.command,
        created_by=server.created_by,
        source_type=getattr(server, "source_type", None),
    )

    # spec 跟随连接字段重建；归一化失败时保留旧 spec
    refreshed_spec = build_spec(
        transport=server.transport, url=server.url, command=server.command, args=server.args
    )
    if refreshed_spec.get("normalized") is not False:
        server.spec = refreshed_spec

    await db.commit()
    await db.refresh(server)

    clear_mcp_server_tools_cache(slug)

    logger.info(f"Updated MCP server '{slug}'")
    return server


async def delete_mcp_server(db: AsyncSession, slug: str) -> bool:
    """Delete server."""
    server = await get_mcp_server(db, slug)
    if not server:
        return False

    await db.delete(server)
    await db.commit()

    clear_mcp_server_tools_cache(slug)

    logger.info(f"Deleted MCP server '{slug}'")
    return True


# =============================================================================
# === Tool Management ===
# =============================================================================


async def set_server_enabled(
    db: AsyncSession, slug: str, enabled: bool, updated_by: str = None
) -> tuple[bool, MCPServer]:
    """Set server enabled status."""
    server = await get_mcp_server(db, slug)
    if not server:
        raise ValueError(f"Server '{slug}' does not exist")

    server.enabled = 1 if enabled else 0
    if updated_by is not None:
        server.updated_by = updated_by
    await db.commit()

    is_enabled = bool(server.enabled)
    clear_mcp_server_tools_cache(slug)

    logger.info(f"Set MCP server '{slug}' enabled={is_enabled}")
    return is_enabled, server


async def toggle_tool_enabled(
    db: AsyncSession,
    server_slug: str,
    tool_name: str,
    updated_by: str = None,
) -> tuple[bool, MCPServer]:
    """Toggle single tool enabled status（tool_name 为原始 MCP tool name）。"""
    server = await get_mcp_server(db, server_slug)
    if not server:
        raise ValueError(f"Server '{server_slug}' does not exist")

    disabled_tools = list(server.disabled_tools or [])

    if tool_name in disabled_tools:
        disabled_tools.remove(tool_name)
        enabled = True
    else:
        disabled_tools.append(tool_name)
        enabled = False

    server.disabled_tools = disabled_tools
    if updated_by is not None:
        server.updated_by = updated_by
    await db.commit()

    clear_mcp_server_tools_cache(server_slug)

    logger.info(f"Toggled tool '{tool_name}' for server '{server_slug}' enabled={enabled}")
    return enabled, server


# =============================================================================
# === 健康探测（结构化诊断）===
# =============================================================================


async def probe_mcp_server(
    slug: str,
    *,
    db: AsyncSession | None = None,
    persist: bool = True,
) -> McpHealthResult:
    """对单个 MCP 做全链路探测：config → runtime → transport/discovery。

    结果持久化到 last_health 列并返回。管理端 /test 应使用本函数而非裸拉工具列表。
    """
    import time as _time

    started = _time.perf_counter()

    async def _finish(result: McpHealthResult) -> McpHealthResult:
        result.duration_ms = int((_time.perf_counter() - started) * 1000)
        if persist:
            await _persist_last_health(slug, result.to_dict())
        return result

    # ---- Stage: config ----
    if db is not None:
        server = await get_mcp_server(db, slug)
    else:
        from yuxi.storage.postgres.manager import pg_manager

        async with pg_manager.get_async_session_context() as session:
            server = await get_mcp_server(session, slug)

    if server is None:
        return await _finish(error_result(STAGE_CONFIG, CODE_CONFIG_MISSING, f"MCP '{slug}' 不存在"))

    server_config = server.to_mcp_config()
    runtime_config = build_runtime_config(slug, server_config)

    try:
        plan = build_plan_from_legacy_fields(
            transport=server.transport, url=server.url, command=server.command, args=server.args or []
        )
    except NormalizationError as e:
        return await _finish(error_result(STAGE_CONFIG, "CONFIG_INVALID", str(e), retryable=False))

    # ---- Stage: runtime ----
    # 按 runtime provider 探测执行前提（uvx/npx/裸命令在当前镜像里是否可用）
    required_binary: str | None = None
    if plan.runtime_provider == "uv":
        required_binary = "uvx"
    elif plan.runtime_provider == "node":
        required_binary = "npx" if plan.artifact_kind == ARTIFACT_NPM else "node"
    elif plan.runtime_provider != "none":
        required_binary = plan.entrypoint

    if required_binary and shutil.which(required_binary) is None:
        return await _finish(
            error_result(
                STAGE_RUNTIME,
                CODE_CLIENT_INIT_FAILED,
                f"命令 '{required_binary}' 在当前环境（api/worker 容器）不可用",
                retryable=False,
            )
        )

    source_type_attr = getattr(server, "source_type", None)
    if not _is_builtin_source(source_type_attr, getattr(server, "created_by", None)) and server.transport == "stdio":
        from yuxi.agents.mcp.policy import stdio_command_allowed

        if not stdio_command_allowed(server.command):
            return await _finish(
                error_result(
                    STAGE_RUNTIME,
                    "POLICY_REJECTED",
                    f"command '{server.command}' 不在 stdio 白名单中",
                    retryable=False,
                )
            )

    # ---- Stage: transport + discovery ----
    try:
        descriptors, info = await get_host().discover(slug, runtime_config, force_refresh=True, update_cache=False)
    except McpHostError as e:
        return await _finish(error_result(e.stage, e.code or CODE_DISCOVERY_FAILED, str(e)))

    result = ok_result(
        stage=STAGE_DISCOVERY,
        message=f"发现 {len(descriptors)} 个工具",
        retryable=False,
        tool_count=len(descriptors),
        protocol_note=info.protocol_note,
    )
    return await _finish(result)


async def _persist_last_health(slug: str, payload: dict[str, Any]) -> None:
    from yuxi.storage.postgres.manager import pg_manager

    try:
        async with pg_manager.get_async_session_context() as session:
            result = await session.execute(select(MCPServer).filter(MCPServer.slug == slug))
            server = result.scalar_one_or_none()
            if server is not None:
                server.last_health = payload
                await session.commit()
    except Exception as e:  # noqa: BLE001 —— 诊断失败不能反过来影响管理操作
        logger.warning(f"Failed to persist last_health for '{slug}': {e}")


async def get_last_health(slug: str, *, db: AsyncSession | None = None) -> dict[str, Any] | None:
    """读取最近一次结构化诊断结果（不做实时探测）。"""
    if db is not None:
        server = await get_mcp_server(db, slug)
        return getattr(server, "last_health", None) if server else None

    from yuxi.storage.postgres.manager import pg_manager

    async with pg_manager.get_async_session_context() as session:
        return await get_last_health(slug, db=session)


# =============================================================================
# === 导入（Registry server.json / Claude-Cursor / URL）===
# =============================================================================


async def import_mcp_servers(
    payload: Any,
    *,
    created_by: str | None = None,
) -> list[dict[str, Any]]:
    """批量导入外部格式的 MCP 定义；导入的服务器默认 disabled，人工确认后启用。

    Returns:
        [{slug, name, status(created|exists|failed), enabled, warnings:[...]}]
    """
    from yuxi.storage.postgres.manager import pg_manager

    records = smart_parse(payload)

    outcomes: list[dict[str, Any]] = []
    async with pg_manager.get_async_session_context() as session:
        for record in records:
            outcome: dict[str, Any] = {"slug": record.slug, "name": record.name}
            outcome["warnings"] = list(record.warnings)
            try:
                result = await session.execute(select(MCPServer).filter(MCPServer.slug == record.slug))
                if result.scalar_one_or_none() is not None:
                    outcome.update({"status": "exists", "enabled": None})
                    outcomes.append(outcome)
                    continue

                if record.unpinned:
                    outcome.update({"status": "rejected", "reason": "包版本未固定（需要精确版本号）"})
                    outcomes.append(outcome)
                    continue

                _validate_for_policy(
                    transport=record.transport,
                    command=record.command,
                    created_by=created_by or "system",
                    source_type=record.source_type,
                )

                spec_payload = record.plan.to_dict() if record.plan else {
                    "schema_version": SPEC_SCHEMA_VERSION,
                    "normalized": False,
                }
                session.add(
                    MCPServer(
                        slug=record.slug,
                        name=record.name[:100],
                        description=record.description,
                        transport=record.transport,
                        url=record.url,
                        command=record.command,
                        args=record.args,
                        env=record.env,
                        headers=record.headers,
                        tags=record.tags,
                        icon=record.icon,
                        enabled=0,
                        source_type=record.source_type,
                        source_ref=record.source_ref,
                        spec=spec_payload,
                        created_by=created_by or "system",
                        updated_by=created_by or "system",
                    )
                )
                outcome.update({"status": "created", "enabled": False})
            except PolicyError as e:
                outcome.update({"status": "rejected", "reason": str(e)})
            except (ImportFormatError, ValueError) as e:
                outcome.update({"status": "failed", "reason": str(e)})
            outcomes.append(outcome)
        await session.commit()

    for item in outcomes:
        if item.get("status") == "created":
            clear_mcp_server_tools_cache(item["slug"])

    return outcomes
