"""MCP 子系统核心层单元测试：spec / policy / health / registry。

四个模块只依赖标准库（相互经 yuxi.agents.mcp 包路径引用），参照
test_chunking_token_limit.py 的 sys.modules 隔离模式加载，
避免触发 yuxi 重依赖链（langchain / pydantic 等）。
跑完后清理 sys.modules，避免污染其他测试。
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_PKG = Path(__file__).resolve().parents[2] / "package"

_STUB_NAMES = [
    "yuxi",
    "yuxi.agents",
    "yuxi.agents.mcp",
    "yuxi.agents.mcp.spec",
    "yuxi.agents.mcp.policy",
    "yuxi.agents.mcp.health",
    "yuxi.agents.mcp.registry",
]

# 由 _isolated_modules fixture 在运行时注入
spec_mod = None  # type: ignore[assignment]
policy_mod = None  # type: ignore[assignment]
health_mod = None  # type: ignore[assignment]
registry_mod = None  # type: ignore[assignment]


@pytest.fixture(autouse=True, scope="module")
def _isolated_modules():
    saved = {name: sys.modules.get(name) for name in _STUB_NAMES}

    for name in _STUB_NAMES[:3]:
        sys.modules.setdefault(name, types.ModuleType(name))

    def _load(name: str, rel: str):
        spec = importlib.util.spec_from_file_location(name, _PKG / rel)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod

    global spec_mod, policy_mod, health_mod, registry_mod  # noqa: PLW0603
    spec_mod = _load("yuxi.agents.mcp.spec", "yuxi/agents/mcp/spec.py")
    policy_mod = _load("yuxi.agents.mcp.policy", "yuxi/agents/mcp/policy.py")
    health_mod = _load("yuxi.agents.mcp.health", "yuxi/agents/mcp/health.py")
    registry_mod = _load("yuxi.agents.mcp.registry", "yuxi/agents/mcp/registry.py")

    yield

    for name in _STUB_NAMES:
        if saved[name] is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = saved[name]


# ── spec.transport / 命名 ──────────────────────────────────────────


class TestSpecTransportAndNaming:
    def test_transport_aliases_normalize(self):
        assert spec_mod.normalize_transport("http") == "streamable_http"
        assert spec_mod.normalize_transport("Streamable-HTTP") == "streamable_http"
        assert spec_mod.normalize_transport("sse") == "sse"
        assert spec_mod.normalize_transport("stdio") == "stdio"

    def test_transport_unknown_rejected(self):
        with pytest.raises(spec_mod.NormalizationError):
            spec_mod.normalize_transport("websocket_custom")

    def test_split_name_version(self):
        assert spec_mod.split_name_version("bio-mcp==0.5.0") == ("bio-mcp", "0.5.0")
        assert spec_mod.split_name_version("@scope/pkg@1.2.3") == ("@scope/pkg", "1.2.3")
        assert spec_mod.split_name_version("@antv/mcp-server-chart") == ("@antv/mcp-server-chart", None)

    def test_slugify_registry_namespace(self):
        assert spec_mod.slugify("io.github.example/Foo Bar") == "example-foo-bar"
        assert spec_mod.slugify("Some MCP!") == "some-mcp"

    def test_to_camel_case_compat(self):
        assert spec_mod.to_camel_case("mcp-server-chart") == "mcpServerChart"
        assert spec_mod.to_camel_case("search_pubmed") == "searchPubmed"


class TestInstallPlan:
    def test_classify_uvx_pinned(self):
        plan = spec_mod.classify_command("uvx", ["--from", "bio-mcp==0.5.0", "bio-mcp"])
        assert plan.artifact_kind == "pypi"
        assert plan.identifier == "bio-mcp"
        assert plan.version == "0.5.0"
        assert plan.runtime_provider == "uv"
        assert plan.entrypoint == "bio-mcp"
        assert plan.pinned is True

    def test_classify_npx_scoped_package_unpinned(self):
        plan = spec_mod.classify_command("npx", ["-y", "@antv/mcp-server-chart"])
        assert plan.artifact_kind == "npm"
        assert plan.identifier == "@antv/mcp-server-chart"
        assert plan.version is None

    def test_classify_binary_absolute_path(self):
        plan = spec_mod.classify_command("/opt/mcp/bio/bin/bio-mcp", [])
        assert plan.artifact_kind == "binary"
        assert plan.runtime_provider == "preinstalled"
        assert plan.entrypoint == "/opt/mcp/bio/bin/bio-mcp"

    def test_remote_plan_requires_url(self):
        with pytest.raises(spec_mod.NormalizationError):
            spec_mod.build_plan_from_legacy_fields(transport="streamable_http", url=None)

    def test_materialize_pypi_requires_pin(self):
        plan = spec_mod.classify_command("uvx", ["biomcp-cli"])
        assert plan.pinned is False
        with pytest.raises(spec_mod.PolicyUnpinnedArtifact):
            spec_mod.materialize(plan)

    def test_materialize_pypi_pinned_argv(self):
        plan = spec_mod.classify_command("uvx", ["--from", "bio-mcp==0.5.0", "bio-mcp"])
        cfg = spec_mod.materialize(plan)
        assert cfg["command"] == "uvx"
        assert cfg["args"] == ["--from", "bio-mcp==0.5.0", "bio-mcp"]
        assert cfg["transport"] == "stdio"

    def test_materialize_remote_passthrough(self):
        plan = spec_mod.classify_url("http://biomcp:8080/mcp")
        cfg = spec_mod.materialize(plan)
        assert cfg == {"transport": "streamable_http", "url": "http://biomcp:8080/mcp"}


# ── policy ─────────────────────────────────────────────────────────


class TestPolicy:
    def test_default_allowlist_contains_launchers(self, monkeypatch):
        monkeypatch.delenv(policy_mod.ENV_STDIO_ALLOWLIST, raising=False)
        rules = policy_mod.get_stdio_allowlist()
        assert set(rules) >= {"npx", "uvx"}

    def test_stdio_command_allowed_basename_and_prefix(self, monkeypatch):
        monkeypatch.setenv(policy_mod.ENV_STDIO_ALLOWLIST, "npx,/opt/mcp/")
        assert policy_mod.stdio_command_allowed("npx")
        assert policy_mod.stdio_command_allowed("/opt/mcp/foo/bin/srv")
        assert not policy_mod.stdio_command_allowed("/usr/bin/bash")
        assert not policy_mod.stdio_command_allowed(None)

    def test_assert_transport_user_stdio_gated(self, monkeypatch):
        monkeypatch.delenv(policy_mod.ENV_STDIO_ALLOWLIST, raising=False)
        # bash 不在默认白名单 → 拒绝
        with pytest.raises(policy_mod.PolicyError):
            policy_mod.assert_transport_allowed(
                "stdio", source_type="manual", command="bash"
            )
        # npx 默认放行
        policy_mod.assert_transport_allowed("stdio", source_type="manual", command="npx")
        # builtin 来源完全绕过
        policy_mod.assert_transport_allowed(
            "stdio", source_type="builtin", command="anything-else"
        )

    def test_expand_env_refs(self, monkeypatch):
        monkeypatch.setenv("MY_TOKEN", "tok-123")
        resolved, missing = policy_mod.expand_env_refs(
            {"A": "${MY_TOKEN}", "B": "${NOT_SET_VAR_XYZ}", "C": "plain-value"}
        )
        assert resolved == {"A": "tok-123", "C": "plain-value"}
        assert missing == ["B=${NOT_SET_VAR_XYZ}"]


# ── health ─────────────────────────────────────────────────────────


class TestHealth:
    def test_error_result_codes_with_retryable_defaults(self):
        r = health_mod.error_result("runtime", "CLIENT_INIT_FAILED", "缺少 uvx", retryable=False)
        d = r.to_dict()
        assert d["status"] == "error" and d["code"] == "CLIENT_INIT_FAILED"
        assert d["retryable"] is False

    def test_timeout_exception_classified_retryable(self):
        result = health_mod.failure_from_exception(TimeoutError("read timed out"), fallback_stage="discovery")
        assert result.code == "TIMEOUT"
        assert result.retryable is True

    def test_missing_binary_maps_runtime_stage(self):
        err = FileNotFoundError(2, "No such file or directory")
        result = health_mod.failure_from_exception(err, fallback_stage="discovery")
        assert result.code == "RUNTIME_SPAWN_FAILED"
        assert result.stage == "runtime"

    def test_chained_cause_dns_failure(self):
        try:
            try:
                raise OSError("getaddrinfo failed: api.example.com")
            except OSError as inner:
                raise RuntimeError("client init failed") from inner
        except RuntimeError as outer:
            result = health_mod.failure_from_exception(outer, fallback_stage="transport")
        assert result.code == "TRANSPORT_CONNECT_FAILED"


# ── registry 导入器 ────────────────────────────────────────────────


class TestRegistryImporters:
    def test_smart_parse_bare_url_string(self):
        records = registry_mod.smart_parse("https://mcp.example.com/mcp")
        (record,) = records
        assert record.url == "https://mcp.example.com/mcp"
        assert record.transport == "streamable_http"
        assert record.plan.artifact_kind == "remote"

    def test_parse_claude_cursor_config(self):
        payload = {
            "mcpServers": {
                "remote-x": {"url": "https://x.io/mcp"},
                "pkg-y": {"command": "npx", "args": ["-y", "@scope/pkg@1.4.0"]},
                "broken-z": {},
            }
        }
        records = {r.slug: r for r in registry_mod.parse_claude_cursor(payload)}
        assert records["remote-x"].transport == "streamable_http"
        pkg = records["pkg-y"]
        assert pkg.unpinned is False  # 参数里带精确版本
        assert pkg.plan.version == "1.4.0"
        broken = records["broken-z"]
        assert broken.warnings  # 缺字段条目降级为 warning，不阻断整批

    def test_claude_cursor_unversioned_flagged_not_blocked(self):
        records = registry_mod.parse_claude_cursor({"mcpServers": {"c": {"command": "npx", "args": ["-y", "pkg"]}}})
        (record,) = records
        assert record.unpinned is True
        assert any("版本" in w for w in record.warnings)

    def test_official_registry_prefers_remote_over_pypi(self):
        server_json = {
            "name": "io.github.acme/toolkit",
            "version": "2.0.1",
            "description": "demo",
            "packages": [
                {
                    "registryType": "pypi",
                    "identifier": "acme-mcp",
                    "version": "2.0.1",
                    "transport": {"type": "stdio"},
                }
            ],
            "remotes": [{"type": "streamable-http", "url": "https://acme.io/mcp"}],
        }
        record = registry_mod.parse_official_registry(server_json)
        assert record.plan.artifact_kind == "remote"
        assert record.source_type == "registry"
        assert record.url == "https://acme.io/mcp"

    def test_official_registry_pypi_fallback_when_no_remote(self):
        server_json = {
            "name": "io.github.acme/toolkit",
            "packages": [
                {
                    "registryType": "pypi",
                    "identifier": "acme-mcp",
                    "version": "2.0.1",
                    "transport": {"type": "stdio"},
                }
            ],
        }
        record = registry_mod.parse_official_registry(server_json)
        assert record.plan.artifact_kind == "pypi"
        assert record.plan.pinned is True
        assert record.unpinned is False

    def test_official_registry_unsupported_only_raises(self):
        server_json = {
            "name": "io.github.acme/native",
            "packages": [
                {"registryType": "oci", "identifier": "ghcr.io/acme/mcp", "version": "1.0.0",
                 "transport": {"type": "stdio"}}
            ],
        }
        with pytest.raises(registry_mod.ImportFormatError) as excinfo:
            registry_mod.parse_official_registry(server_json)
        assert "oci" in str(excinfo.value)

    def test_official_registry_headers_list_form_annotated(self):
        server_json = {
            "name": "io.github.acme/authed",
            "remotes": [
                {
                    "type": "streamable-http",
                    "url": "https://acme.io/mcp",
                    "headers": [{"name": "Authorization", "value": "Bearer xyz"}],
                }
            ],
        }
        record = registry_mod.parse_official_registry(server_json)
        assert record.headers == {"Authorization": "Bearer xyz"}

    def test_unknown_format_raises_actionable_error(self):
        with pytest.raises(registry_mod.ImportFormatError):
            registry_mod.smart_parse({"hello": "world"})
