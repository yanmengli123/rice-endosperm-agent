from __future__ import annotations

from types import SimpleNamespace

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.agents.mcp import service as mcp_service
from yuxi.storage.postgres import manager as postgres_manager
from yuxi.storage.postgres.models_business import MCPServer


class _AsyncSessionContext:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, *_args):
        return False


@pytest_asyncio.fixture
async def mcp_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MCPServer.__table__.create)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session

    await engine.dispose()


class _FakeClient:
    """兼容保留：旧直连 adapters 的桩，新用例请使用 _FakeHost。"""

    def __init__(self, tools):
        self._tools = tools

    async def get_tools(self):
        return self._tools


def _host_noop_methods(cls):
    def clear_all(self):
        pass

    def clear_server_cache(self, slug):
        del slug

    cls.clear_all = clear_all
    cls.clear_server_cache = clear_server_cache
    return cls


async def test_ensure_builtin_mcp_servers_removes_retired_system_server(monkeypatch, mcp_session):
    retired_server = MCPServer(
        slug="sequentialthinking",
        name="sequentialthinking",
        description="old builtin",
        transport="streamable_http",
        url="https://remote.mcpservers.org/sequentialthinking/mcp",
        enabled=1,
        created_by="system",
        updated_by="system",
    )
    mcp_session.add(retired_server)
    await mcp_session.commit()

    monkeypatch.setattr(
        postgres_manager.pg_manager,
        "get_async_session_context",
        lambda: _AsyncSessionContext(mcp_session),
    )

    await mcp_service.ensure_builtin_mcp_servers_in_db()

    retired = await mcp_session.scalar(select(MCPServer).where(MCPServer.slug == "sequentialthinking"))
    chart = await mcp_session.scalar(select(MCPServer).where(MCPServer.slug == "mcp-server-chart"))
    assert retired is None
    assert chart is not None


async def test_ensure_builtin_mcp_servers_preserves_user_server_with_retired_slug(monkeypatch, mcp_session):
    user_server = MCPServer(
        slug="sequentialthinking",
        name="用户自定义 MCP",
        description="user managed",
        transport="streamable_http",
        url="https://example.com/mcp",
        enabled=1,
        created_by="admin",
        updated_by="admin",
    )
    mcp_session.add(user_server)
    await mcp_session.commit()

    monkeypatch.setattr(
        postgres_manager.pg_manager,
        "get_async_session_context",
        lambda: _AsyncSessionContext(mcp_session),
    )

    await mcp_service.ensure_builtin_mcp_servers_in_db()

    server = await mcp_session.scalar(select(MCPServer).where(MCPServer.slug == "sequentialthinking"))
    assert server is not None
    assert server.created_by == "admin"


async def test_get_enabled_mcp_tools_loads_latest_config_from_db(monkeypatch):
    captured: list[dict] = []

    async def fake_get_enabled_mcp_server_config(server_name: str, db=None):
        del db
        assert server_name == "demo"
        return {"transport": "stdio", "command": "demo", "disabled_tools": ["tool_b"]}

    async def fake_get_mcp_tools(server_name: str, additional_servers=None, disabled_tools=None, **kwargs):
        del kwargs
        captured.append(
            {
                "server_name": server_name,
                "additional_servers": additional_servers,
                "disabled_tools": list(disabled_tools or []),
            }
        )
        return ["tool-a"]

    monkeypatch.setattr(mcp_service, "get_enabled_mcp_server_config", fake_get_enabled_mcp_server_config)
    monkeypatch.setattr(mcp_service, "get_mcp_tools", fake_get_mcp_tools)

    tools = await mcp_service.get_enabled_mcp_tools("demo")

    assert tools == ["tool-a"]
    assert captured == [
        {
            "server_name": "demo",
            "additional_servers": {
                "demo": {"transport": "stdio", "command": "demo", "disabled_tools": ["tool_b"]}
            },
            "disabled_tools": ["tool_b"],
        }
    ]


async def test_get_mcp_tools_rebuilds_cache_when_config_hash_changes(monkeypatch):
    mcp_service.clear_mcp_cache()

    configs = [
        {"transport": "stdio", "command": "demo-v1", "disabled_tools": []},
        {"transport": "stdio", "command": "demo-v2", "disabled_tools": []},
    ]
    build_calls: list[str] = []

    async def fake_get_enabled_mcp_server_config(server_name: str, db=None):
        del db
        assert server_name == "demo"
        return configs[0]

    def make_descriptor(name):
        return SimpleNamespace(
            server_slug="demo",
            name=name,
            stable_id=f"mcp__demo__{name}",
            description="",
            args_model=None,
            raw_tool=None,
        )

    @_host_noop_methods
    class _FakeHost:
        def __init__(self):
            self._cache: dict[str, str] = {}

        async def discover(self, slug, config, *, force_refresh=False, update_cache=True):
            del force_refresh
            key = repr(sorted(config.items()))
            command = config["command"]
            if self._cache.get(key) != command:
                build_calls.append(command)
                self._cache[key] = command
            return (
                [make_descriptor(f"tool_for_{command}")],
                SimpleNamespace(server_slug=slug, duration_ms=1, tool_count=1, protocol_note="fake"),
            )

        async def call_tool(self, *_a, **_k):  # pragma: no cover - 本用例不触发
            raise AssertionError

        def note_filter(self, slug, disabled_count):
            del slug, disabled_count

        def get_stats(self, slug):
            return None

    fake_host = _FakeHost()
    monkeypatch.setattr(mcp_service, "get_enabled_mcp_server_config", fake_get_enabled_mcp_server_config)
    monkeypatch.setattr(mcp_service, "get_host", lambda: fake_host)

    tools_v1_first = await mcp_service.get_mcp_tools("demo")
    names_v1_first = [t.metadata["mcp_tool_name"] for t in tools_v1_first]
    tools_v1_second = await mcp_service.get_mcp_tools("demo")

    configs[0] = configs[1]
    tools_v2 = await mcp_service.get_mcp_tools("demo")

    assert names_v1_first == ["tool_for_demo-v1"]
    assert [t.metadata["mcp_tool_name"] for t in tools_v1_second] == ["tool_for_demo-v1"]
    assert [t.metadata["mcp_tool_name"] for t in tools_v2] == ["tool_for_demo-v2"]
    # 配置未变时命中缓存只建一次；配置变化后重建
    assert build_calls == ["demo-v1", "demo-v2"]

    mcp_service.clear_mcp_cache()


async def test_get_tools_from_all_servers_loads_names_from_db_once(monkeypatch):
    server_configs = {
        "alpha": {"transport": "stdio", "command": "cmd-a", "disabled_tools": []},
        "beta": {"transport": "stdio", "command": "cmd-b", "disabled_tools": []},
    }
    load_calls: list[list[str]] = []
    discover_slugs: list[str] = []

    async def fake_load_enabled_mcp_server_configs(*, names=None, db=None):
        del names, db
        load_calls.append(list(server_configs))
        return server_configs

    def make_descriptor(name):
        return SimpleNamespace(
            server_slug=name.split("_")[0],
            name=name,
            stable_id=name,
            description="",
            args_model=None,
            raw_tool=None,
        )

    @_host_noop_methods
    class _FakeHost:
        async def discover(self, slug, config, *, force_refresh=False, update_cache=True):
            del config, force_refresh, update_cache
            discover_slugs.append(slug)
            return (
                [make_descriptor(f"{slug}_tool")],
                SimpleNamespace(server_slug=slug, duration_ms=1, tool_count=1, protocol_note="fake"),
            )

        async def call_tool(self, *_a, **_k):  # pragma: no cover - 本用例不触发
            raise AssertionError

        def note_filter(self, slug, disabled_count):
            del slug, disabled_count

        def get_stats(self, slug):
            return None

    monkeypatch.setattr(mcp_service, "_load_enabled_mcp_server_configs", fake_load_enabled_mcp_server_configs)
    monkeypatch.setattr(mcp_service, "get_host", lambda: _FakeHost())

    tools = await mcp_service.get_tools_from_all_servers()

    assert [t.metadata["server"] for t in tools] == ["alpha", "beta"]
    assert [t.name for t in tools] == ["alpha_tool", "beta_tool"]
    assert load_calls == [list(server_configs)]
    assert sorted(discover_slugs) == ["alpha", "beta"]


async def test_get_mcp_tools_sets_handle_tool_error(monkeypatch):
    mcp_service.clear_mcp_cache()

    config = {"transport": "stdio", "command": "demo-tool", "disabled_tools": []}

    async def fake_get_enabled_mcp_server_config(server_name: str, db=None):
        del db
        return config

    descriptor = SimpleNamespace(
        server_slug="demo",
        name="demo_tool",
        stable_id="mcp__demo__demoTool",
        description="",
        args_model=None,
        raw_tool=None,
    )

    @_host_noop_methods
    class _FakeHost:
        async def discover(self, slug, cfg, *, force_refresh=False, update_cache=True):
            del slug, cfg, force_refresh, update_cache
            return (
                [descriptor],
                SimpleNamespace(server_slug="demo", duration_ms=1, tool_count=1, protocol_note="fake"),
            )

        async def call_tool(self, *_a, **_k):  # pragma: no cover - 本用例不触发
            raise AssertionError

        def note_filter(self, slug, disabled_count):
            del slug, disabled_count

        def get_stats(self, slug):
            return None

    monkeypatch.setattr(mcp_service, "get_enabled_mcp_server_config", fake_get_enabled_mcp_server_config)
    monkeypatch.setattr(mcp_service, "get_host", lambda: _FakeHost())

    tools = await mcp_service.get_mcp_tools("demo")
    assert len(tools) == 1
    # 装配后的工具保留旧版语义：错误以文本返回而非击穿 Agent 服务
    assert tools[0].handle_tool_error is True
    assert tools[0].metadata["id"] == "mcp__demo__demoTool"
    assert tools[0].metadata["mcp_tool_name"] == "demo_tool"

    mcp_service.clear_mcp_cache()
