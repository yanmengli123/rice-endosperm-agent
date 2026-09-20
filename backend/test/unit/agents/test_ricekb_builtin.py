"""Rice Source KB（内置 MCP "ricekb"）接入契约测试。

守住四条不变量：
1. 内置条目本身合规：stdio + 绝对路径 command、凭据只存 ${} 引用、source_type=builtin，
   因而走 builtin 豁免而不是用户 stdio 白名单；
2. 能力注册表覆盖 vendored 脚本声明的全部 15 个工具（否则 Knowledge-first 计划会把它们
   当作 UNCLASSIFIED，"无缝衔接"失效）；
3. 实际运行的服务器脚本与上游钉定 sha256 一致：容器内校验已安装的
   /usr/local/bin/ricekb-mcp（真正被 MCP host 启动的产物），宿主机校验 vendored 副本；
4. 缺省禁用、READY 仅在开发运行时策略允许时成立，与其它内置 stdio MCP 同一口径。

更新 vendored 脚本时：先在上游仓库跑通其测试，再重拷贝并同步 VENDORED_SHA256。
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import types
from pathlib import Path

import pytest

from yuxi.agents.mcp import service as mcp_service
from yuxi.agents.mcp.capability_registry import _TRUSTED_PROFILES, RICEKB_TOOL_NAMES
from yuxi.agents.mcp.policy import expand_env_refs
from yuxi.agents.mcp.registry import SOURCE_TYPE_BUILTIN
from yuxi.agents.mcp.security import assert_no_inline_secrets
from yuxi.knowledge.planning.turn_execution_plan import Capability

# 上游：Rice Research Agent 仓库 rice-kb-gateway/mcp/ricekb_mcp.py（gateway 2.2.0）。
# 与 docker/mcp/ricekb/VENDOR.md 记录一致；两处必须同时更新。
VENDORED_SHA256 = "0be31f3f6d59a9a2ea1c8d9acda95b8e3876099ecd8388a01bfa222d84ce0aa6"

_INSTALLED = Path("/usr/local/bin/ricekb-mcp")
_REPO_COPY = Path(__file__).resolve().parents[4] / "docker" / "mcp" / "ricekb" / "ricekb_mcp.py"


def _server_script() -> Path:
    for candidate in (_INSTALLED, _REPO_COPY):
        if candidate.is_file():
            return candidate
    pytest.skip("ricekb-mcp not installed in this runtime and repo copy not mounted")


def _load_vendored_tool_names(path: Path) -> frozenset[str]:
    # 已安装产物没有 .py 后缀，importlib 推断不出 loader；直接编译执行源码。
    # 脚本用 `from __future__ import annotations` + dataclass，要求模块已在 sys.modules。
    # mcp 是惰性导入，模块顶层只需要标准库。
    name = "ricekb_mcp_vendored"
    module = types.ModuleType(name)
    sys.modules[name] = module
    try:
        exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), module.__dict__)  # noqa: S102
        return frozenset(module.TOOL_NAMES)
    finally:
        sys.modules.pop(name, None)


@pytest.fixture(scope="module")
def ricekb_config() -> dict:
    return mcp_service._DEFAULT_MCP_SERVERS["ricekb"]


def test_builtin_entry_is_stdio_with_absolute_command(ricekb_config):
    assert ricekb_config["transport"] == "stdio"
    assert ricekb_config["command"] == str(_INSTALLED)
    assert ricekb_config["args"] == []
    assert ricekb_config["source_type"] == SOURCE_TYPE_BUILTIN
    assert "gateway-2." in ricekb_config["source_ref"]
    assert ricekb_config["timeout"] >= 30, "gateway statement timeout is 8s; session timeout must leave headroom"


def test_builtin_entry_passes_policy_gate_without_stdio_allowlist(ricekb_config, monkeypatch):
    # 把用户 stdio 白名单收窄到 npx：builtin 必须仍然放行，证明走的是 builtin 豁免路径。
    monkeypatch.setenv("YUXI_MCP_STDIO_COMMAND_ALLOWLIST", "npx")
    mcp_service._validate_for_policy(
        transport=ricekb_config["transport"],
        command=ricekb_config["command"],
        created_by="system",
        source_type=ricekb_config["source_type"],
    )


def test_credentials_are_env_references_only(ricekb_config, monkeypatch):
    env = ricekb_config["env"]
    assert set(env) == {"RICE_KB_GATEWAY_URL", "RICE_KB_API_TOKEN"}
    assert_no_inline_secrets(env, section="env")
    for value in env.values():
        assert re.fullmatch(r"\$\{[A-Z_]+\}", value), value

    monkeypatch.delenv("RICE_KB_API_TOKEN", raising=False)
    monkeypatch.delenv("RICE_KB_GATEWAY_URL", raising=False)
    resolved, missing = expand_env_refs(env)
    assert resolved == {}
    assert sorted(missing) == ["RICE_KB_API_TOKEN=${RICE_KB_API_TOKEN}", "RICE_KB_GATEWAY_URL=${RICE_KB_GATEWAY_URL}"]

    monkeypatch.setenv("RICE_KB_API_TOKEN", "t" * 40)
    resolved, missing = expand_env_refs(env)
    assert resolved == {"RICE_KB_API_TOKEN": "t" * 40}
    assert missing == ["RICE_KB_GATEWAY_URL=${RICE_KB_GATEWAY_URL}"], "URL 缺省由脚本回落到共享网络别名"


def test_builtin_row_is_development_stdio_runtime(ricekb_config, monkeypatch):
    monkeypatch.setenv("ALLOW_DEVELOPMENT_MCP_RUNTIME", "true")
    values = mcp_service._builtin_row_values("ricekb", ricekb_config)
    assert values["runtime_artifact"]["kind"] == "development_stdio"
    assert values["runtime_artifact"]["command"] == str(_INSTALLED)
    assert values["lifecycle_status"] == "READY"
    assert values["enabled"] == 0, "内置默认关闭，由管理员在管理页显式启用"
    assert values["created_by"] == "system"

    monkeypatch.setenv("ALLOW_DEVELOPMENT_MCP_RUNTIME", "false")
    assert mcp_service._builtin_row_values("ricekb", ricekb_config)["lifecycle_status"] == "BUILD_REQUIRED"


def test_capability_registry_covers_every_vendored_tool():
    vendored = _load_vendored_tool_names(_server_script())
    assert vendored == RICEKB_TOOL_NAMES, {
        "missing_profiles": sorted(vendored - RICEKB_TOOL_NAMES),
        "stale_profiles": sorted(RICEKB_TOOL_NAMES - vendored),
    }
    for name in vendored:
        profile = _TRUSTED_PROFILES[name]
        assert Capability.GENE_RECORD_LOOKUP in profile.capabilities, name
        assert profile.source_class == "AUTHORITATIVE_DATABASE", name
        assert profile.authority_level == "PRIMARY_DATABASE", name
        assert profile.citation_semantics == "DATA_PROVENANCE", name
        assert profile.fallback_policy == "FAIL_CLOSED", name
    assert Capability.VERBATIM_SEARCH in _TRUSTED_PROFILES["ricekb_search"].capabilities


SEQUENCE_TOOLS = ("ricekb_sequence", "ricekb_region", "ricekb_genome")


def test_sequence_tools_are_trusted_source_records():
    """序列/区间工具必须与行级工具同口径，否则"具体序列"会被降级为无来源事实。"""
    for name in SEQUENCE_TOOLS:
        assert name in RICEKB_TOOL_NAMES, name
        profile = _TRUSTED_PROFILES[name]
        assert profile.source_class == "AUTHORITATIVE_DATABASE", name
        assert profile.authority_level == "PRIMARY_DATABASE", name
        assert profile.citation_semantics == "DATA_PROVENANCE", name
        assert profile.fallback_policy == "FAIL_CLOSED", name
        assert Capability.GENE_RECORD_LOOKUP in profile.capabilities, name


def test_vendored_script_declares_the_sequence_tools():
    vendored = _load_vendored_tool_names(_server_script())
    assert set(SEQUENCE_TOOLS) <= set(vendored)


def test_server_script_matches_pinned_upstream_hash():
    script = _server_script()
    actual = hashlib.sha256(script.read_bytes()).hexdigest()
    assert actual == VENDORED_SHA256, f"{script} drifted from the pinned upstream; re-vendor and update the pin"


@pytest.mark.skipif(not _INSTALLED.exists(), reason="only meaningful inside the runtime image")
def test_installed_server_is_executable():
    assert os.access(_INSTALLED, os.X_OK)
    assert _INSTALLED.read_bytes().startswith(b"#!/usr/bin/env python3")
