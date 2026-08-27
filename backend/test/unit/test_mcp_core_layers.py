"""MCP 子系统核心层单元测试：spec / policy / health / domain / security / registry。

六个模块只依赖标准库或相互引用（经 yuxi.agents.mcp 包路径），参照
test_chunking_token_limit.py 的 sys.modules 隔离模式加载，
避免触发 yuxi 重依赖链（langchain / sqlalchemy 等）。
跑完后清理 sys.modules，避免污染其他测试。
"""

from __future__ import annotations

import asyncio
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
    "yuxi.agents.mcp.domain",
    "yuxi.agents.mcp.security",
    "yuxi.agents.mcp.registry",
]

# 由 _isolated_modules fixture 在运行时注入
spec_mod = None  # type: ignore[assignment]
policy_mod = None  # type: ignore[assignment]
health_mod = None  # type: ignore[assignment]
domain_mod = None  # type: ignore[assignment]
security_mod = None  # type: ignore[assignment]
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

    global spec_mod, policy_mod, health_mod, domain_mod, security_mod, registry_mod  # noqa: PLW0603
    spec_mod = _load("yuxi.agents.mcp.spec", "yuxi/agents/mcp/spec.py")
    policy_mod = _load("yuxi.agents.mcp.policy", "yuxi/agents/mcp/policy.py")
    health_mod = _load("yuxi.agents.mcp.health", "yuxi/agents/mcp/health.py")
    domain_mod = _load("yuxi.agents.mcp.domain", "yuxi/agents/mcp/domain.py")
    security_mod = _load("yuxi.agents.mcp.security", "yuxi/agents/mcp/security.py")
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


class TestInstallPlan:
    def test_classify_uvx_pinned(self):
        plan = spec_mod.classify_command("uvx", ["--from", "bio-mcp==0.5.0", "bio-mcp"])
        assert plan.artifact_kind == "pypi"
        assert plan.version == "0.5.0"
        assert plan.pinned is True

    def test_materialize_pypi_requires_pin(self):
        plan = spec_mod.classify_command("uvx", ["biomcp-cli"])
        with pytest.raises(spec_mod.PolicyUnpinnedArtifact):
            spec_mod.materialize(plan)

    def test_materialize_remote_passthrough(self):
        plan = spec_mod.classify_url("http://biomcp:8080/mcp")
        assert spec_mod.materialize(plan) == {
            "transport": "streamable_http",
            "url": "http://biomcp:8080/mcp",
        }


# ── policy ─────────────────────────────────────────────────────────


class TestPolicy:
    def test_stdio_gate_and_env_refs(self, monkeypatch):
        monkeypatch.delenv(policy_mod.ENV_STDIO_ALLOWLIST, raising=False)
        with pytest.raises(policy_mod.PolicyError):
            policy_mod.assert_transport_allowed("stdio", source_type="manual", command="bash")
        policy_mod.assert_transport_allowed("stdio", source_type="manual", command="npx")
        policy_mod.assert_transport_allowed("stdio", source_type="builtin", command="anything")

        monkeypatch.setenv("MY_TOKEN", "tok-123")
        resolved, missing = policy_mod.expand_env_refs(
            {"A": "${MY_TOKEN}", "B": "${NOT_SET_VAR_XYZ}", "C": "plain"}
        )
        assert resolved == {"A": "tok-123", "C": "plain"}
        assert missing == ["B=${NOT_SET_VAR_XYZ}"]


# ── health ─────────────────────────────────────────────────────────


class TestHealth:
    def test_timeout_classified_retryable(self):
        result = health_mod.failure_from_exception(TimeoutError("read timed out"), fallback_stage="discovery")
        assert result.code == "TIMEOUT" and result.retryable is True

    def test_missing_binary_maps_runtime_stage(self):
        err = FileNotFoundError(2, "No such file or directory")
        result = health_mod.failure_from_exception(err, fallback_stage="discovery")
        assert result.code == "RUNTIME_SPAWN_FAILED" and result.stage == "runtime"

    def test_chained_cause_dns_failure(self):
        try:
            try:
                raise OSError("getaddrinfo failed: api.example.com")
            except OSError as inner:
                raise RuntimeError("client init failed") from inner
        except RuntimeError as outer:
            result = health_mod.failure_from_exception(outer, fallback_stage="transport")
        assert result.code == "TRANSPORT_CONNECT_FAILED"


# ── domain：部署决策矩阵 ───────────────────────────────────────────


class TestDeploymentMatrix:
    def _remote(self):
        return domain_mod.McpArtifactCandidate(
            kind=domain_mod.McpArtifactSourceKind.REMOTE,
            identifier="https://acme.io/mcp",
            endpoint="https://acme.io/mcp",
            transport="streamable_http",
        )

    def _pypi(self, version=None):
        return domain_mod.McpArtifactCandidate(
            kind=domain_mod.McpArtifactSourceKind.PYPI,
            identifier="acme-mcp",
            version=version,
        )

    def _digest_oci(self):
        return domain_mod.McpArtifactCandidate(
            kind=domain_mod.McpArtifactSourceKind.OCI,
            identifier="ghcr.io/acme/acme-mcp@sha256:" + "0" * 64,
        )

    def test_remote_wins_over_packages(self, monkeypatch):
        monkeypatch.delenv("ALLOW_DEVELOPMENT_MCP_RUNTIME", raising=False)
        decision = domain_mod.choose_deployment([self._pypi("1.0"), self._remote()])
        assert decision.status is domain_mod.McpLifecycleStatus.RESOLVED
        assert decision.runtime_level is domain_mod.McpRuntimeLevel.TRUSTED_REMOTE
        assert decision.runtime_artifact.kind is domain_mod.McpRuntimeArtifactKind.REMOTE_HTTP

    def test_digest_pinned_oci_preferred_before_package(self):
        decision = domain_mod.choose_deployment([self._pypi("1.0"), self._digest_oci()])
        assert decision.status is domain_mod.McpLifecycleStatus.VERIFIED
        assert decision.runtime_level is domain_mod.McpRuntimeLevel.MANAGED_OCI

    def test_pinned_package_is_build_source_not_command(self, monkeypatch):
        monkeypatch.setenv("ALLOW_DEVELOPMENT_MCP_RUNTIME", "false")
        decision = domain_mod.choose_deployment([self._pypi("2.1.3")])
        assert decision.status is domain_mod.McpLifecycleStatus.BUILD_REQUIRED
        assert decision.runtime_artifact is None
        assert "OCI" in decision.reason or "build" in decision.reason.lower()

    def test_dev_gate_allows_ephemeral_stdio(self, monkeypatch):
        monkeypatch.setenv("ALLOW_DEVELOPMENT_MCP_RUNTIME", "true")
        dev_candidate = domain_mod.McpArtifactCandidate(
            kind=domain_mod.McpArtifactSourceKind.NPM,
            identifier="@scope/pkg",
            version="1.4.0",
            metadata={"development_requested": True, "command": "npx", "args": ["-y", "@scope/pkg@1.4.0"]},
        )
        decision = domain_mod.choose_deployment([dev_candidate])
        assert decision.runtime_level is domain_mod.McpRuntimeLevel.DEVELOPMENT
        assert decision.runtime_artifact.command == "npx"
        assert decision.runtime_artifact.args == ("-y", "@scope/pkg@1.4.0")

    def test_unpinned_only_blocked(self, monkeypatch):
        monkeypatch.delenv("ALLOW_DEVELOPMENT_MCP_RUNTIME", raising=False)
        decision = domain_mod.choose_deployment([self._pypi(None)])
        assert decision.status is domain_mod.McpLifecycleStatus.BLOCKED
        assert decision.runtime_artifact is None


# ── security：SSRF 门禁与凭据卫生 ──────────────────────────────────


class TestSecurityGuards:
    def test_static_rejects_private_and_metadata_targets(self, monkeypatch):
        monkeypatch.delenv("YUXI_MCP_ALLOW_INSECURE_HTTP", raising=False)
        for bad in [
            "http://127.0.0.1:8080/mcp",
            "https://localhost/mcp",
            "https://metadata.google.internal/computeMetadata/v1/",
            "https://169.254.169.254/latest/meta-data",
            "http://10.1.2.3/mcp",
            "https://user:pw@public.io/mcp",
            "ftp://public.io/x",
        ]:
            with pytest.raises(security_mod.McpSecurityError):
                security_mod.validate_remote_url_static(bad)

    def test_static_accepts_public_https(self):
        url = security_mod.validate_remote_url_static("https://mcp.example.com/mcp?a=1")
        assert url.startswith("https://mcp.example.com")

    def test_plain_http_requires_explicit_flag(self, monkeypatch):
        with pytest.raises(security_mod.McpSecurityError):
            security_mod.validate_remote_url_static("http://mcp.example.com/mcp")
        monkeypatch.setenv("YUXI_MCP_ALLOW_INSECURE_HTTP", "true")
        assert security_mod.validate_remote_url_static("http://mcp.example.com/mcp").startswith("http://")

    def test_dns_rebinding_to_private_ip_rejected(self, monkeypatch):
        import socket as _socket

        real_getaddrinfo = _socket.getaddrinfo

        def fake_getaddrinfo(host, port, **kwargs):
            if host.endswith("rebind.example.com"):
                return [(2, 1, 6, "", ("10.0.0.5", port))]
            return real_getaddrinfo(host, port, **kwargs)

        monkeypatch.setattr(_socket, "getaddrinfo", fake_getaddrinfo)
        with pytest.raises(security_mod.McpSecurityError):
            asyncio.run(security_mod.validate_remote_url_dns("https://rebind.example.com/mcp"))

    def test_inline_secrets_blocked_but_refs_allowed(self):
        with pytest.raises(security_mod.McpSecurityError):
            security_mod.assert_no_inline_secrets({"Authorization": "Bearer sk-live"}, section="headers")
        security_mod.assert_no_inline_secrets({"Authorization": "${MCP_TOKEN}"}, section="headers")
        security_mod.assert_no_inline_secrets({"X-Trace-Id": "plain-ok"}, section="headers")


# ── registry 导入器 ────────────────────────────────────────────────


class TestRegistryImporters:
    def test_smart_parse_bare_url_string(self):
        records = registry_mod.smart_parse("https://mcp.example.com/mcp")
        (record,) = records
        assert record.url == "https://mcp.example.com/mcp"
        assert record.transport == "streamable_http"
        assert record.deployment.runtime_level is domain_mod.McpRuntimeLevel.TRUSTED_REMOTE

    def test_parse_claude_cursor_mixed_entries(self):
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
        assert any(candidate.kind is domain_mod.McpArtifactSourceKind.NPM for candidate in pkg.candidates)
        broken = records["broken-z"]
        assert broken.warnings  # 缺字段条目降级为 warning，不阻断整批

    def test_official_registry_prefers_remote_over_packages(self):
        server_json = {
            "name": "io.github.acme/toolkit",
            "version": "2.0.1",
            "packages": [
                {"registryType": "pypi", "identifier": "acme-mcp", "version": "2.0.1",
                 "transport": {"type": "stdio"}}
            ],
            "remotes": [{"type": "streamable-http", "url": "https://acme.io/mcp"}],
        }
        record = registry_mod.parse_official_registry(server_json)
        assert record.plan.artifact_kind == "remote"
        assert record.deployment.status is domain_mod.McpLifecycleStatus.RESOLVED
        assert record.raw_manifest["name"] == "io.github.acme/toolkit"
        assert record.normalized_manifest["schema_version"] == 2

    def test_official_registry_digest_pinned_oci_selected(self):
        server_json = {
            "name": "io.github.acme/native",
            "packages": [
                {
                    "registryType": "oci",
                    "identifier": "ghcr.io/acme/native-mcp@sha256:" + "1" * 64,
                    "version": "1.0.0",
                    "transport": {"type": "stdio"},
                }
            ],
        }
        record = registry_mod.parse_official_registry(server_json)
        assert record.deployment.status is domain_mod.McpLifecycleStatus.VERIFIED
        assert record.deployment.runtime_level is domain_mod.McpRuntimeLevel.MANAGED_OCI

    def test_official_registry_tag_only_oci_blocked_honestly(self):
        """tag 引用不可变语义不足：必须 BLOCKED，而不是伪装成可运行。"""
        server_json = {
            "name": "io.github.acme/native",
            "packages": [
                {
                    "registryType": "oci",
                    "identifier": "ghcr.io/acme/native-mcp:latest",
                    "version": "1.0.0",
                    "transport": {"type": "stdio"},
                }
            ],
        }
        record = registry_mod.parse_official_registry(server_json)
        assert record.deployment.status is domain_mod.McpLifecycleStatus.BLOCKED

    def test_pypi_only_manifest_requires_build(self, monkeypatch):
        monkeypatch.setenv("ALLOW_DEVELOPMENT_MCP_RUNTIME", "false")
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
        assert record.deployment.status is domain_mod.McpLifecycleStatus.BUILD_REQUIRED
        assert record.unpinned is False
        assert record.normalized_manifest["deployment"]["runtime_level"] == "managed_oci"

    def test_unknown_format_raises_actionable_error(self):
        with pytest.raises(registry_mod.ImportFormatError):
            registry_mod.smart_parse({"hello": "world"})
