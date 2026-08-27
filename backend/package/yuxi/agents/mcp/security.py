"""Network and secret policy for remotely hosted MCP servers."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from collections.abc import Iterable
from urllib.parse import urlparse


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


def _assert_public_address(address: str) -> None:
    ip = ipaddress.ip_address(address)
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        raise McpSecurityError(f"MCP endpoint resolves to a non-public address: {ip}")


def validate_remote_url_static(raw_url: str, *, allow_insecure_http: bool | None = None) -> str:
    """Validate syntax and literal IPs without performing network I/O."""
    parsed = urlparse(str(raw_url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise McpSecurityError("MCP endpoint must be an absolute http(s) URL")
    if parsed.username or parsed.password or parsed.fragment:
        raise McpSecurityError("MCP endpoint must not contain credentials or a URL fragment")
    insecure_allowed = allow_insecure_remote_http() if allow_insecure_http is None else allow_insecure_http
    if parsed.scheme != "https" and not insecure_allowed:
        raise McpSecurityError("remote MCP endpoints must use HTTPS")
    host = parsed.hostname.rstrip(".").lower()
    if host in _METADATA_HOSTS or host == "localhost" or host.endswith(".localhost"):
        raise McpSecurityError("MCP endpoint host is blocked by SSRF policy")
    if parsed.port in _BLOCKED_PORTS:
        raise McpSecurityError(f"MCP endpoint port {parsed.port} is blocked")
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
    try:
        answers = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise McpSecurityError(f"MCP endpoint DNS resolution failed: {exc}") from exc
    addresses = {answer[4][0] for answer in answers if answer and answer[4]}
    if not addresses:
        raise McpSecurityError("MCP endpoint DNS returned no addresses")
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
    "allow_insecure_remote_http",
    "validate_remote_url_static",
    "validate_remote_url_dns",
    "assert_no_inline_secrets",
    "redact_secret_mapping",
    "build_safe_httpx_client_factory",
    "validate_oci_runtime_artifact",
]
