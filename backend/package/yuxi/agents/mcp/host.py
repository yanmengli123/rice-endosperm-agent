"""MCP Host 层：Agent 世界与 MCP 协议栈之间唯一的协议边界。

设计约束：
- Agent 业务代码只认识 `McpHost` 抽象与 `McpToolDescriptor`/`McpToolResult`
  这套自有 domain model，**不 import langchain_mcp_adapters**；
- 当前实现 `LegacyLangChainHost` 桥接存量 langchain-mcp-adapters 栈
  （主环境锁定 mcp 1.x，切换官方 SDK v2 时只需提供新实现，不动上层）；
- 连接配置由调用方（service 门面）注入，本层不做数据库访问，避免环依赖；
- 工具缓存与统计从旧 service.py 原样迁移至此：key = slug:config_hash，
  配置变更自然失效；失败不再静默吞掉，统一抛 `McpHostError`（带 stage/code）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from yuxi.agents.mcp.health import (
    CODE_CLIENT_INIT_FAILED,
    CODE_DISCOVERY_FAILED,
    STAGE_TRANSPORT,
    failure_from_exception,
)
from yuxi.agents.mcp.spec import to_camel_case
from yuxi.agents.mcp.execution import record_mcp_call
from yuxi.agents.mcp.security import (
    build_safe_httpx_client_factory,
    validate_remote_url_dns,
)

ADAPTER_LEGACY_HTTP_DIALECT = "streamable-http"
ADAPTER_MODERN_HTTP_DIALECT = "streamable_http"

#: 迁移到官方 SDK v2 前的兼容性说明，透出给健康诊断与管理界面
PROTOCOL_NOTE_PREFIX = "legacy-client-via-langchain-mcp-adapters"


class McpHostError(RuntimeError):
    """协议栈操作失败的统一异常。"""

    def __init__(self, message: str, *, stage: str, code: str | None = None):
        super().__init__(message)
        self.stage = stage
        self.code = code


@dataclass
class McpToolDescriptor:
    """一个 MCP tool 的稳定描述（不序列化、进程内流转）。"""

    server_slug: str
    name: str                                   # 原始 MCP tool name（disabled_tools 匹配依据）
    stable_id: str                              # mcp__{server_cc}__{tool_cc}
    description: str = ""
    args_model: type | None = None              # pydantic schema（adapter 构造），UI/LangChain 共用
    annotations: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] | None = None
    raw_tool: Any = field(default=None, repr=False, compare=False)


@dataclass
class McpToolResult:
    """一次工具调用的归一化结果。"""

    text: str
    is_error: bool = False
    structured_content: dict[str, Any] | None = None
    content_blocks: list[Any] = field(default_factory=list)
    resource_links: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "is_error": self.is_error,
            "structured_content": self.structured_content,
            "content_blocks": self.content_blocks,
            "resource_links": self.resource_links,
            "metadata": self.metadata,
            "provenance": self.provenance,
        }


@dataclass
class McpResourceDescriptor:
    server_slug: str
    uri: str
    name: str
    description: str = ""
    mime_type: str | None = None
    annotations: dict[str, Any] = field(default_factory=dict)


@dataclass
class McpPromptDescriptor:
    server_slug: str
    name: str
    description: str = ""
    arguments: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class McpDiscoveryInfo:
    server_slug: str
    duration_ms: int | None = None
    tool_count: int = 0
    resource_count: int = 0
    prompt_count: int = 0
    protocol_note: str | None = None


class McpHost(ABC):
    """Yuxi 自有的 MCP Host 接口（终态由 mcpd / official SDK v2 实现）。"""

    @abstractmethod
    async def discover(
        self,
        slug: str,
        config: dict[str, Any],
        *,
        force_refresh: bool = False,
        update_cache: bool = True,
    ) -> tuple[list[McpToolDescriptor], McpDiscoveryInfo]: ...

    @abstractmethod
    async def call_tool(
        self,
        slug: str,
        config: dict[str, Any],
        tool_name: str,
        arguments: dict[str, Any] | None,
    ) -> McpToolResult: ...

    @abstractmethod
    async def discover_resources(
        self, slug: str, config: dict[str, Any]
    ) -> list[McpResourceDescriptor]: ...

    @abstractmethod
    async def read_resource(
        self, slug: str, config: dict[str, Any], uri: str
    ) -> McpToolResult: ...

    @abstractmethod
    async def discover_prompts(
        self, slug: str, config: dict[str, Any]
    ) -> list[McpPromptDescriptor]: ...

    @abstractmethod
    async def get_prompt(
        self, slug: str, config: dict[str, Any], name: str, arguments: dict[str, str] | None
    ) -> McpToolResult: ...


def _streamable_http_dialect() -> str:
    """不同版本 langchain-mcp-adapters 对 streamable HTTP 的拼写不同。

    - <0.3 只认 "streamable-http"
    - >=0.3 双写并存，返回规范值 "streamable_http"
    这是 adapter 方言问题，DB 里永远只存规范值，翻译发生在本层。
    """
    global _HTTP_DIALECT_CACHE
    if _HTTP_DIALECT_CACHE is None:
        try:
            from importlib.metadata import version

            ver = version("langchain-mcp-adapters")
            major, minor = (int(part) for part in ver.split(".")[:2]) if "." in ver else (0, 0)
            _HTTP_DIALECT_CACHE = (
                ADAPTER_MODERN_HTTP_DIALECT if (major, minor) >= (0, 3) else ADAPTER_LEGACY_HTTP_DIALECT
            )
        except Exception:
            _HTTP_DIALECT_CACHE = ADAPTER_LEGACY_HTTP_DIALECT
    return _HTTP_DIALECT_CACHE


_HTTP_DIALECT_CACHE: str | None = None


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _normalize_tool_output(output: Any, *, provenance: dict[str, Any]) -> McpToolResult:
    artifact = None
    content = output
    if isinstance(output, tuple) and len(output) == 2:
        content, artifact = output
    blocks = _jsonable(content)
    block_list = blocks if isinstance(blocks, list) else [blocks]
    text_parts: list[str] = []
    resource_links: list[dict[str, Any]] = []
    for block in block_list:
        if isinstance(block, str):
            text_parts.append(block)
        elif isinstance(block, dict):
            if block.get("type") == "text" and block.get("text") is not None:
                text_parts.append(str(block["text"]))
            if block.get("url") or block.get("uri"):
                resource_links.append(block)
    text = "\n".join(part for part in text_parts if part)
    if not text:
        text = json.dumps(blocks, ensure_ascii=False, default=str)
    artifact_data = _jsonable(artifact) if artifact is not None else None
    structured = None
    if isinstance(artifact_data, dict):
        candidate = artifact_data.get("structured_content") or artifact_data.get("structuredContent")
        if isinstance(candidate, dict):
            structured = candidate
    is_error = text.lstrip().lower().startswith("error:")
    return McpToolResult(
        text=text,
        is_error=is_error,
        structured_content=structured,
        content_blocks=block_list,
        resource_links=resource_links,
        metadata={"artifact": artifact_data} if artifact_data is not None else {},
        provenance=provenance,
    )


class LegacyLangChainHost(McpHost):
    """基于 langchain-mcp-adapters 的过渡实现。

    缓存语义与旧 service.get_mcp_tools 完全一致，仅搬家到这里：
    - 全量工具缓存 `_tool_cache[key]`，key=slug:sha256(config)[:16]
    - 全局 asyncio 锁串行化重建
    - 配置变化时旧 key 自然失效并清理
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._tool_cache: dict[str, list[Any]] = {}
        self._raw_by_name: dict[str, dict[str, Any]] = {}
        self._stats: dict[str, dict[str, int]] = {}

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _adapter_transport(transport: str) -> str:
        if transport == "streamable_http":
            return _streamable_http_dialect()
        return transport

    @staticmethod
    def _client_config(config: dict[str, Any]) -> dict[str, Any]:
        payload = {k: v for k, v in config.items() if k != "disabled_tools"}
        if "transport" in payload:
            payload["transport"] = LegacyLangChainHost._adapter_transport(str(payload["transport"]))
        return payload

    @staticmethod
    def _cache_key(slug: str, config: dict[str, Any]) -> str:
        canonical = json.dumps(config, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        return f"{slug}:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:16]}"

    def _note_adapter_version(self) -> str:
        try:
            from importlib.metadata import version

            ver = version("langchain-mcp-adapters")
        except Exception:
            ver = "unknown"
        return f"{PROTOCOL_NOTE_PREFIX}-{ver}"

    async def _ensure_raw_tools(
        self,
        slug: str,
        config: dict[str, Any],
        *,
        force_refresh: bool = False,
        update_cache: bool = True,
    ) -> list[Any]:
        key = self._cache_key(slug, config)
        if not force_refresh:
            cached = self._tool_cache.get(key)
            if cached:
                return cached

        client_config = self._client_config(config)
        if client_config.get("transport") in {"sse", ADAPTER_LEGACY_HTTP_DIALECT, ADAPTER_MODERN_HTTP_DIALECT}:
            validated_url = await validate_remote_url_dns(str(client_config.get("url") or ""))
            client_config["url"] = validated_url
            client_config["httpx_client_factory"] = build_safe_httpx_client_factory(validated_url)
        started = time.perf_counter()
        try:
            from langchain_mcp_adapters.client import MultiServerMCPClient

            client = MultiServerMCPClient({slug: client_config})  # pyright: ignore[reportArgumentType]
            raw_tools = list(await client.get_tools())
        except Exception as e:  # noqa: BLE001 —— 统一转成带分类的领域异常
            raised = failure_from_exception(e, fallback_stage=STAGE_TRANSPORT)
            raise McpHostError(raised.message, stage=raised.stage, code=raised.code) from e

        # 与旧行为一致：吞掉 ToolException 使其不击穿服务
        processed: list[Any] = []
        by_name: dict[str, Any] = {}
        for tool in raw_tools:
            try:
                tool.handle_tool_error = True
            except Exception:  # noqa: BLE001 —— 非 LC 工具对象防御
                pass
            processed.append(tool)
            by_name[tool.name] = tool

        duration_ms = int((time.perf_counter() - started) * 1000)

        if update_cache:
            async with self._lock:
                stale = [k for k in self._tool_cache if k.startswith(f"{slug}:") and k != key]
                for item in stale:
                    self._tool_cache.pop(item, None)
                    self._raw_by_name.pop(item, None)
                self._tool_cache[key] = processed
                self._raw_by_name[key] = by_name
            self._stats[slug] = {"total": len(processed), "enabled": len(processed), "disabled": 0}

        self.last_discovery_duration_ms = duration_ms
        return processed

    def _descriptor_from_raw(self, slug: str, raw: Any) -> McpToolDescriptor:
        name = getattr(raw, "name", "") or ""
        args_model = getattr(raw, "args_schema", None)
        metadata = getattr(raw, "metadata", None)
        metadata = metadata if isinstance(metadata, dict) else {}
        return McpToolDescriptor(
            server_slug=slug,
            name=name,
            stable_id=f"mcp__{to_camel_case(slug)}__{to_camel_case(name)}",
            description=str(getattr(raw, "description", "") or ""),
            args_model=args_model if isinstance(args_model, type) else None,
            annotations=dict(metadata.get("annotations") or {}),
            output_schema=metadata.get("output_schema") if isinstance(metadata.get("output_schema"), dict) else None,
            raw_tool=raw,
        )

    # ------------------------------------------------------------------
    # McpHost 实现
    # ------------------------------------------------------------------

    async def discover(
        self,
        slug: str,
        config: dict[str, Any],
        *,
        force_refresh: bool = False,
        update_cache: bool = True,
    ) -> tuple[list[McpToolDescriptor], McpDiscoveryInfo]:
        self.last_discovery_duration_ms = None
        try:
            raw_tools = await self._ensure_raw_tools(
                slug, config, force_refresh=force_refresh, update_cache=update_cache
            )
        except McpHostError:
            raise
        except Exception as e:  # noqa: BLE001
            raise McpHostError(f"{type(e).__name__}: {e}", stage="discovery", code=CODE_DISCOVERY_FAILED) from e

        descriptors = [self._descriptor_from_raw(slug, raw) for raw in raw_tools]
        info = McpDiscoveryInfo(
            server_slug=slug,
            duration_ms=self.last_discovery_duration_ms,
            tool_count=len(descriptors),
            protocol_note=self._note_adapter_version(),
        )
        return descriptors, info

    async def call_tool(
        self,
        slug: str,
        config: dict[str, Any],
        tool_name: str,
        arguments: dict[str, Any] | None,
    ) -> McpToolResult:
        started = time.perf_counter()
        raw_tools = await self._ensure_raw_tools(slug, config, force_refresh=False)
        target = next((t for t in raw_tools if t.name == tool_name), None)
        if target is None:
            lowered = {str(t.name).lower(): t for t in raw_tools}
            target = lowered.get(tool_name.lower())
        if target is None:
            raise McpHostError(
                f"MCP server '{slug}' 上不存在工具 '{tool_name}'",
                stage="discovery",
                code=CODE_DISCOVERY_FAILED,
            )
        try:
            output = await target.ainvoke(dict(arguments or {}))
        except Exception as e:  # noqa: BLE001 —— 适配器会先拦截 ToolException 为字符串，这里兜运行期错误
            raised = failure_from_exception(e, fallback_stage=STAGE_TRANSPORT)
            if raised.code in (CODE_DISCOVERY_FAILED, CODE_CLIENT_INIT_FAILED):
                raise McpHostError(raised.message, stage=raised.stage, code=raised.code) from e
            result = McpToolResult(text=f"Error: {e}", is_error=True)
            await record_mcp_call(
                server_slug=slug,
                capability_type="tool",
                capability_name=tool_name,
                arguments=arguments or {},
                result=result.to_dict(),
                status="error",
                duration_ms=int((time.perf_counter() - started) * 1000),
                provenance={"protocol": self._note_adapter_version()},
            )
            return result
        result = _normalize_tool_output(
            output,
            provenance={"server_slug": slug, "tool": tool_name, "protocol": self._note_adapter_version()},
        )
        await record_mcp_call(
            server_slug=slug,
            capability_type="tool",
            capability_name=tool_name,
            arguments=arguments or {},
            result=result.to_dict(),
            status="error" if result.is_error else "success",
            duration_ms=int((time.perf_counter() - started) * 1000),
            provenance=result.provenance,
        )
        return result

    @asynccontextmanager
    async def _official_session(self, config: dict[str, Any]):
        """Open an official SDK session for resources/prompts without flattening."""
        from mcp import ClientSession, StdioServerParameters

        transport = str(config.get("transport") or "")
        if transport == "stdio":
            from mcp.client.stdio import stdio_client

            params = StdioServerParameters(
                command=str(config.get("command") or ""),
                args=[str(item) for item in config.get("args") or []],
                env={str(key): str(value) for key, value in (config.get("env") or {}).items()} or None,
            )
            async with stdio_client(params) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    yield session
            return

        url = await validate_remote_url_dns(str(config.get("url") or ""))
        factory = build_safe_httpx_client_factory(url)
        headers = dict(config.get("headers") or {}) or None
        if transport == "sse":
            from mcp.client.sse import sse_client

            client = sse_client(
                url,
                headers=headers,
                timeout=float(config.get("timeout") or 30),
                sse_read_timeout=float(config.get("sse_read_timeout") or 300),
                httpx_client_factory=factory,
            )
        else:
            from mcp.client.streamable_http import streamablehttp_client

            client = streamablehttp_client(
                url,
                headers=headers,
                timeout=float(config.get("timeout") or 30),
                sse_read_timeout=float(config.get("sse_read_timeout") or 300),
                httpx_client_factory=factory,
            )
        async with client as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                yield session

    async def discover_resources(
        self, slug: str, config: dict[str, Any]
    ) -> list[McpResourceDescriptor]:
        try:
            async with self._official_session(config) as session:
                response = await session.list_resources()
        except Exception as exc:  # noqa: BLE001
            raised = failure_from_exception(exc, fallback_stage=STAGE_TRANSPORT)
            raise McpHostError(raised.message, stage=raised.stage, code=raised.code) from exc
        return [
            McpResourceDescriptor(
                server_slug=slug,
                uri=str(item.uri),
                name=str(item.name or item.uri),
                description=str(item.description or ""),
                mime_type=getattr(item, "mimeType", None),
                annotations=_jsonable(getattr(item, "annotations", None)) or {},
            )
            for item in response.resources
        ]

    async def read_resource(
        self, slug: str, config: dict[str, Any], uri: str
    ) -> McpToolResult:
        started = time.perf_counter()
        try:
            async with self._official_session(config) as session:
                response = await session.read_resource(uri)
            blocks = [_jsonable(item) for item in response.contents]
            result = _normalize_tool_output(
                blocks,
                provenance={"server_slug": slug, "resource_uri": uri, "protocol": self._note_adapter_version()},
            )
            status = "success"
        except Exception as exc:  # noqa: BLE001
            result = McpToolResult(text=f"Error: {exc}", is_error=True)
            status = "error"
        await record_mcp_call(
            server_slug=slug,
            capability_type="resource",
            capability_name=uri,
            arguments={"uri": uri},
            result=result.to_dict(),
            status=status,
            duration_ms=int((time.perf_counter() - started) * 1000),
            provenance=result.provenance,
        )
        return result

    async def discover_prompts(
        self, slug: str, config: dict[str, Any]
    ) -> list[McpPromptDescriptor]:
        try:
            async with self._official_session(config) as session:
                response = await session.list_prompts()
        except Exception as exc:  # noqa: BLE001
            raised = failure_from_exception(exc, fallback_stage=STAGE_TRANSPORT)
            raise McpHostError(raised.message, stage=raised.stage, code=raised.code) from exc
        return [
            McpPromptDescriptor(
                server_slug=slug,
                name=str(item.name),
                description=str(item.description or ""),
                arguments=[_jsonable(argument) for argument in (item.arguments or [])],
            )
            for item in response.prompts
        ]

    async def get_prompt(
        self, slug: str, config: dict[str, Any], name: str, arguments: dict[str, str] | None
    ) -> McpToolResult:
        started = time.perf_counter()
        try:
            async with self._official_session(config) as session:
                response = await session.get_prompt(name, arguments or {})
            blocks = [_jsonable(item) for item in response.messages]
            result = _normalize_tool_output(
                blocks,
                provenance={"server_slug": slug, "prompt": name, "protocol": self._note_adapter_version()},
            )
            status = "success"
        except Exception as exc:  # noqa: BLE001
            result = McpToolResult(text=f"Error: {exc}", is_error=True)
            status = "error"
        await record_mcp_call(
            server_slug=slug,
            capability_type="prompt",
            capability_name=name,
            arguments=arguments or {},
            result=result.to_dict(),
            status=status,
            duration_ms=int((time.perf_counter() - started) * 1000),
            provenance=result.provenance,
        )
        return result

    # ------------------------------------------------------------------
    # 缓存管理（供门面/路由使用）
    # ------------------------------------------------------------------

    def clear_server_cache(self, slug: str) -> None:
        prefix = f"{slug}:"
        for key in [k for k in self._tool_cache if k.startswith(prefix)]:
            self._tool_cache.pop(key, None)
            self._raw_by_name.pop(key, None)
        self._stats.pop(slug, None)

    def clear_all(self) -> None:
        self._tool_cache.clear()
        self._raw_by_name.clear()
        self._stats.clear()

    def note_filter(self, slug: str, disabled_count: int) -> None:
        """外部过滤结果回填统计（保持旧 stats 字段含义）。"""
        stat = self._stats.setdefault(slug, {"total": 0, "enabled": 0, "disabled": 0})
        total = stat.get("total", 0)
        stat.update({"total": total, "disabled": disabled_count, "enabled": max(total - disabled_count, 0)})

    def get_stats(self, slug: str) -> dict[str, int] | None:
        return self._stats.get(slug)


_host_singleton: McpHost | None = None


def get_host() -> McpHost:
    """宿主单例入口。终态替换为 mcpd/official SDK 实现时仅需改这里。"""
    global _host_singleton
    if _host_singleton is None:
        _host_singleton = LegacyLangChainHost()
    return _host_singleton


def reset_host() -> None:
    """测试辅助：重置单例与缓存。"""
    global _host_singleton
    _host_singleton = None


__all__ = [
    "McpHost",
    "McpHostError",
    "McpToolDescriptor",
    "McpToolResult",
    "McpResourceDescriptor",
    "McpPromptDescriptor",
    "McpDiscoveryInfo",
    "LegacyLangChainHost",
    "get_host",
    "reset_host",
]
