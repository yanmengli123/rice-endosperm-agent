"""飞书/Lark 官方 OpenAPI MCP（内置 "feishu"）接入契约测试。

守住五条不变量：
1. 内置条目合规：stdio + 绝对路径 command、凭据只存 ${} 引用不存明文、
   source_type=builtin（走 builtin 豁免而非用户 stdio 白名单）；
2. 版本钉死三处一致：source_ref、docker/api.Dockerfile 的全局安装、本测试
   的期望值——npm 供应链面必须单点升级（同 ricekb 的 sha256 互锁纪律）；
3. 策略面：builtin stdio 绕过用户 allowlist 也能过 assert_transport_allowed；
4. env 引用解析：平台环境有 LARK_MCP_APP_ID/SECRET 时展开为 APP_ID/APP_SECRET，
   缺失时键被剔除（显式 WARNING），绝不注入空值；
5. 启动产物存在：/usr/local/bin/lark-mcp（容器内）——MCP host 实际拉起的
   就是它（bin 跳过 keytar 原生编译属预期，App 身份不依赖）。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from yuxi.agents.mcp import service as mcp_service
from yuxi.agents.mcp.policy import assert_transport_allowed
from yuxi.agents.mcp.registry import SOURCE_TYPE_BUILTIN
from yuxi.agents.mcp.security import assert_no_inline_secrets

LARK_MCP_VERSION = "0.5.1"
_INSTALLED_BIN = Path("/usr/local/bin/lark-mcp")
_DOCKERFILE = Path(__file__).resolve().parents[4] / "docker" / "api.Dockerfile"


def _feishu_config() -> dict:
    config = mcp_service._DEFAULT_MCP_SERVERS.get("feishu")
    assert config is not None, "内置 MCP 'feishu' 未注册"
    return config


class TestBuiltinEntryInvariants:
    def test_stdio_absolute_command(self) -> None:
        config = _feishu_config()
        assert config["transport"] == "stdio"
        assert config["command"] == "/usr/local/bin/lark-mcp"
        assert config["args"][:1] == ["mcp"]
        assert "preset.default" in config["args"], "必须使用官方收敛工具集，不得放开全量 API"

    def test_source_type_and_pinned_ref(self) -> None:
        config = _feishu_config()
        assert config["source_type"] == SOURCE_TYPE_BUILTIN
        assert config["source_ref"] == f"builtin:feishu@{LARK_MCP_VERSION}"

    def test_env_uses_ref_syntax_only(self) -> None:
        config = _feishu_config()
        env = config.get("env") or {}
        assert set(env) == {"APP_ID", "APP_SECRET"}, "服务进程只应收到 APP_ID/APP_SECRET"
        for key, value in env.items():
            assert re.fullmatch(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}", value), f"env.{key} 必须是 ${{}} 引用"

    def test_no_inline_secrets(self) -> None:
        config = _feishu_config()
        assert_no_inline_secrets(config.get("env"), section="env")
        assert_no_inline_secrets(config.get("headers"), section="headers")

    def test_dockerfile_pin_matches_source_ref(self) -> None:
        """Dockerfile 全局安装的版本必须与 source_ref 一致（升级需两处同批改）。

        api 容器只挂载 backend/ 子目录，读不到仓库根的 Dockerfile 时跳过
        （宿主机/CI 全仓环境必须执行）；语义对齐 test_apisix_gateway_contract。
        """
        if not _DOCKERFILE.is_file():
            pytest.skip(f"读不到 {_DOCKERFILE}（当前环境未挂载完整仓库）")
        dockerfile = _DOCKERFILE.read_text(encoding="utf-8")
        pins = re.findall(r"@larksuiteoapi/lark-mcp@([0-9.]+)", dockerfile)
        assert pins, "api.Dockerfile 缺少 lark-mcp 全局安装（运行期零 npm 拉取的形态被破坏）"
        assert set(pins) == {LARK_MCP_VERSION}, f"Dockerfile 版本 {pins} 与契约 {LARK_MCP_VERSION} 漂移"
        assert "--ignore-scripts" in dockerfile, "必须跳过 keytar 原生编译（slim 镜像无编译链）"


class TestPolicyAndEnvResolution:
    def test_builtin_stdio_bypasses_user_allowlist(self) -> None:
        config = _feishu_config()
        assert_transport_allowed("stdio", source_type="builtin", command=config["command"])

    def test_env_refs_resolved_from_platform_env(self, monkeypatch) -> None:
        monkeypatch.setenv("LARK_MCP_APP_ID", "cli_test")
        monkeypatch.setenv("LARK_MCP_APP_SECRET", "secret_test")
        runtime = mcp_service.build_runtime_config("feishu", _feishu_config())
        assert runtime["env"] == {"APP_ID": "cli_test", "APP_SECRET": "secret_test"}

    def test_env_refs_dropped_when_missing(self, monkeypatch) -> None:
        monkeypatch.delenv("LARK_MCP_APP_ID", raising=False)
        monkeypatch.delenv("LARK_MCP_APP_SECRET", raising=False)
        runtime = mcp_service.build_runtime_config("feishu", _feishu_config())
        assert not runtime.get("env"), "缺凭据时键必须剔除（显式 WARNING），不得注入空值"


class TestInstalledArtifact:
    def test_global_bin_present(self) -> None:
        if not _INSTALLED_BIN.is_file():
            pytest.skip("/usr/local/bin/lark-mcp 未安装（非容器运行时；镜像构建由 Dockerfile RUN 断言）")
        assert os.access(_INSTALLED_BIN, os.X_OK)


class TestMissingCredentialsGuard:
    """缺凭据 = lark-mcp 启动即退出：spawn 边界必须给结构化错误而非 TaskGroup 500。"""

    def test_missing_env_refs_helper(self, monkeypatch) -> None:
        from yuxi.agents.mcp.service import missing_env_refs

        monkeypatch.delenv("LARK_MCP_APP_ID", raising=False)
        monkeypatch.delenv("LARK_MCP_APP_SECRET", raising=False)
        config = _feishu_config()
        assert missing_env_refs(config) == ["LARK_MCP_APP_ID", "LARK_MCP_APP_SECRET"]
        assert missing_env_refs({"transport": "stdio", "command": "x"}) == []  # 无 env 段不受守卫影响
        monkeypatch.setenv("LARK_MCP_APP_ID", "a")
        monkeypatch.setenv("LARK_MCP_APP_SECRET", "b")
        assert missing_env_refs(config) == []

    async def test_probe_reports_credentials_missing(self, monkeypatch) -> None:
        """连接测试在 config 阶段给出 CREDENTIALS_MISSING，而非 transport/UNKNOWN。"""
        from types import SimpleNamespace

        from yuxi.agents.mcp import service as svc

        row = SimpleNamespace(
            to_mcp_config=lambda: {
                "transport": "stdio",
                "command": _feishu_config()["command"],
                "args": _feishu_config()["args"],
                "env": dict(_feishu_config()["env"]),
            },
            lifecycle_status="ready",
            credential_id=None,
        )

        class _Ctx:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *args):
                return False

        async def _fake_get_server(db, slug, tenant_id=None):
            return row

        monkeypatch.setattr(svc, "get_mcp_server", _fake_get_server)
        monkeypatch.setattr("yuxi.storage.postgres.manager.pg_manager.get_async_session_context", lambda: _Ctx())
        monkeypatch.delenv("LARK_MCP_APP_ID", raising=False)
        monkeypatch.delenv("LARK_MCP_APP_SECRET", raising=False)

        result = await svc.probe_mcp_server("feishu", db=None, persist=False)
        assert result.status == "error"
        assert result.stage == "config"
        assert result.code == "CREDENTIALS_MISSING"
        assert "LARK_MCP_APP_ID" in result.message

    async def test_tools_listing_with_placeholder_credentials(self, monkeypatch) -> None:
        """凭据就位（占位值即可）→ host 真实路径 tools/list 返回 19 个工具。

        需要容器内的 /usr/local/bin/lark-mcp；非容器运行时跳过。
        config 必须是 DB 的 to_mcp_config() 闭集形态（无 name 等展示键）——
        与生产 get_enabled_mcp_server_config 的喂入形态一致。
        """
        if not _INSTALLED_BIN.is_file():
            pytest.skip("/usr/local/bin/lark-mcp 未安装")
        from yuxi.agents.mcp import service as svc

        config = _feishu_config()

        async def _fake_config(slug):
            # 与 DB to_mcp_config() 的 stdio 闭集形态一致：
            # 不含 name/timeout 等（新版 stdio adapter 会当作 unexpected kwarg）
            return {
                "transport": "stdio",
                "command": config["command"],
                "args": list(config["args"]),
                "env": dict(config["env"]),
            }

        monkeypatch.setattr(svc, "get_enabled_mcp_server_config", _fake_config)
        monkeypatch.setenv("LARK_MCP_APP_ID", "cli_placeholder")
        monkeypatch.setenv("LARK_MCP_APP_SECRET", "placeholder_secret")

        tools = await svc.get_all_mcp_tools("feishu")
        assert len(tools) == 19
        names = {tool.name for tool in tools}
        assert any("im" in name for name in names), "preset.default 应包含 IM 工具"
