"""Network and secret policy for remotely hosted MCP servers."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import socket
from collections.abc import Iterable
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class McpSecurityError(ValueError):
    pass


_METADATA_HOSTS = {
    "metadata",
    "metadata.google.internal",
    "metadata.azure.internal",
    "instance-data.ec2.internal",
}
_BLOCKED_PORTS = {0, 21, 22, 23, 25, 110, 135, 139, 445, 2375, 2376, 3306, 5432, 6379, 7687, 11211}
_SECRET_KEY_MARKERS = ("authorization", "api-key", "apikey", "token", "secret", "password", "cookie")


def allow_insecure_remote_http() -> bool:
    return os.getenv("YUXI_MCP_ALLOW_INSECURE_HTTP", "false").strip().lower() in {"1", "true", "yes", "on"}


#: 受信私网 MCP 端点清单环境变量：逗号分隔的 ``host`` 或 ``host:port``。
#: 专为内网 sidecar（如 genoffice-mcp）放行 —— 命中者豁免"私网地址拒绝"与
#: "HTTPS 强制"两条；敏感端口、credentials-in-URL、metadata 主机与重定向/
#: 代理禁用永不豁免。默认空 = 现行为 fail-closed。
TRUSTED_PRIVATE_HOSTS_ENV = "YUXI_MCP_TRUSTED_PRIVATE_HOSTS"

_trusted_log_marker: str | None = None


def trusted_private_endpoints() -> tuple[frozenset[str], frozenset[tuple[str, int]]]:
    """解析受信端点清单：返回 (任意端口主机集, (主机, 端口) 集)。

    条目 ``host`` 匹配该主机的任意端口；``host:port`` 只匹配显式端口。
    IPv6 字面量建议写成 ``[::1]:3093``，裸 ``::1`` 视为无端口主机。
    """
    raw = os.getenv(TRUSTED_PRIVATE_HOSTS_ENV, "")
    any_port_hosts: set[str] = set()
    host_ports: set[tuple[str, int]] = set()
    for entry in raw.split(","):
        cleaned = entry.strip().lower().rstrip(".")
        if not cleaned:
            continue
        if cleaned.startswith("["):
            closing = cleaned.find("]")
            if closing == -1:
                continue
            host = cleaned[1:closing]
            rest = cleaned[closing + 1 :]
            if rest.startswith(":") and rest[1:].isdigit():
                host_ports.add((host, int(rest[1:])))
            else:
                any_port_hosts.add(host)
        elif cleaned.count(":") == 1:
            host, _, port_text = cleaned.rpartition(":")
            if host and port_text.isdigit():
                host_ports.add((host, int(port_text)))
        else:
            any_port_hosts.add(cleaned)
    global _trusted_log_marker
    if raw.strip() and raw != _trusted_log_marker:
        logger.warning("MCP SSRF policy: trusted private endpoints active → %s", raw.strip())
        _trusted_log_marker = raw
    return frozenset(any_port_hosts), frozenset(host_ports)


def _is_trusted_private_endpoint(host: str, port: int | None) -> bool:
    any_port_hosts, host_ports = trusted_private_endpoints()
    if host in any_port_hosts:
        return True
    return port is not None and (host, port) in host_ports


def _assert_public_address(address: str) -> None:
    ip = ipaddress.ip_address(address)
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        raise McpSecurityError(f"MCP endpoint resolves to a non-public address: {ip}")


def validate_remote_url_static(raw_url: str, *, allow_insecure_http: bool | None = None) -> str:
    """Validate syntax and literal IPs without performing network I/O."""
    parsed = urlparse(str(raw_url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise McpSecurityError("MCP endpoint must be an absolute http(s) URL")
    if parsed.username or parsed.password or parsed.fragment:
        raise McpSecurityError("MCP endpoint must not contain credentials or a URL fragment")
    host = parsed.hostname.rstrip(".").lower()
    trusted = _is_trusted_private_endpoint(host, parsed.port)
    insecure_allowed = allow_insecure_remote_http() if allow_insecure_http is None else allow_insecure_http
    if parsed.scheme != "https" and not (insecure_allowed or trusted):
        raise McpSecurityError("remote MCP endpoints must use HTTPS")
    # metadata 主机是云凭据边界，永不豁免——即使被误列进受信清单。
    if host in _METADATA_HOSTS:
        raise McpSecurityError("MCP endpoint host is blocked by SSRF policy")
    if not trusted and (host == "localhost" or host.endswith(".localhost")):
        raise McpSecurityError("MCP endpoint host is blocked by SSRF policy")
    if parsed.port in _BLOCKED_PORTS:
        raise McpSecurityError(f"MCP endpoint port {parsed.port} is blocked")
    if not trusted:
        try:
            # 仅容忍"主机名不是 IP 字面量"的解析失败；McpSecurityError 必须
            # 穿透（它也是 ValueError 子类，裸 except 会放行 127.0.0.1/169.254.x 等）。
            _assert_public_address(host.strip("[]"))
        except McpSecurityError:
            raise
        except ValueError:
            pass
    return parsed.geturl()


async def validate_remote_url_dns(raw_url: str, *, allow_insecure_http: bool | None = None) -> str:
    """Resolve every A/AAAA answer and reject DNS rebinding to private networks."""
    normalized = validate_remote_url_static(raw_url, allow_insecure_http=allow_insecure_http)
    parsed = urlparse(normalized)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    trusted = _is_trusted_private_endpoint(host.rstrip(".").lower(), parsed.port)
    try:
        answers = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise McpSecurityError(f"MCP endpoint DNS resolution failed: {exc}") from exc
    addresses = {answer[4][0] for answer in answers if answer and answer[4]}
    if not addresses:
        raise McpSecurityError("MCP endpoint DNS returned no addresses")
    if not trusted:
        for address in addresses:
            _assert_public_address(address)
    return normalized


def assert_no_inline_secrets(mapping: dict | None, *, section: str) -> None:
    """Credentials must be referenced by credential_id, never embedded in JSON."""
    for key, value in (mapping or {}).items():
        lowered = str(key).lower()
        if not any(marker in lowered for marker in _SECRET_KEY_MARKERS):
            continue
        text = str(value or "").strip()
        if text.startswith("${") and text.endswith("}"):
            continue
        raise McpSecurityError(
            f"{section}.{key} looks like a credential; store it in MCP credentials and use credential_id"
        )


def redact_secret_mapping(mapping: dict | None) -> dict:
    redacted = {}
    for key, value in (mapping or {}).items():
        lowered = str(key).lower()
        redacted[str(key)] = "******" if any(marker in lowered for marker in _SECRET_KEY_MARKERS) else value
    return redacted


def build_safe_httpx_client_factory(validated_url: str):
    """Return an MCP SDK factory that refuses redirects and unexpected proxies.

    DNS is checked before this factory is used.  Redirect following is disabled so
    a public endpoint cannot bounce the client to a metadata/private address.
    """
    validate_remote_url_static(validated_url)

    def factory(*, headers=None, timeout=None, auth=None):
        import httpx

        return httpx.AsyncClient(
            headers=headers,
            timeout=timeout,
            auth=auth,
            follow_redirects=False,
            trust_env=False,
        )

    return factory


def validate_oci_runtime_artifact(
    image_digest: str,
    *,
    mounts: Iterable[str] = (),
    privileged: bool = False,
    host_network: bool = False,
) -> str:
    if "@sha256:" not in str(image_digest):
        raise McpSecurityError("OCI image must be pinned as image@sha256:digest")
    if privileged or host_network:
        raise McpSecurityError("privileged and host-network MCP runtimes are forbidden")
    forbidden_mounts = ("/var/run/docker.sock", "/", "/etc", "/proc", "/sys")
    for mount in mounts:
        source = str(mount).split(":", 1)[0].rstrip("/") or "/"
        if source in forbidden_mounts:
            raise McpSecurityError(f"forbidden OCI MCP mount: {source}")
    return image_digest


__all__ = [
    "McpSecurityError",
    "TRUSTED_PRIVATE_HOSTS_ENV",
    "allow_insecure_remote_http",
    "trusted_private_endpoints",
    "validate_remote_url_static",
    "validate_remote_url_dns",
    "assert_no_inline_secrets",
    "redact_secret_mapping",
    "build_safe_httpx_client_factory",
    "validate_oci_runtime_artifact",
]
