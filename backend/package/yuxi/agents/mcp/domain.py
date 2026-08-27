"""Server-grade MCP domain types.

This module deliberately has no database or protocol SDK dependency.  It is the
stable boundary between manifest resolution, deployment policy and runtime
providers.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class McpLifecycleStatus(StrEnum):
    DISCOVERED = "DISCOVERED"
    RESOLVED = "RESOLVED"
    VERIFIED = "VERIFIED"
    BUILT = "BUILT"
    DEPLOYED = "DEPLOYED"
    CONNECTED = "CONNECTED"
    DISCOVERED_CAPABILITIES = "DISCOVERED_CAPABILITIES"
    READY = "READY"
    BUILD_REQUIRED = "BUILD_REQUIRED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


class McpRuntimeLevel(StrEnum):
    TRUSTED_REMOTE = "trusted_remote"
    MANAGED_OCI = "managed_oci"
    DEVELOPMENT = "development"


class McpRuntimeArtifactKind(StrEnum):
    REMOTE_HTTP = "remote_http"
    OCI_IMAGE = "oci_image"
    DEVELOPMENT_STDIO = "development_stdio"


class McpDataAccessLevel(StrEnum):
    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    CONTROLLED = "CONTROLLED"
    HUMAN_SENSITIVE = "HUMAN_SENSITIVE"


class McpDependencyMode(StrEnum):
    OPTIONAL = "OPTIONAL"
    REQUIRED = "REQUIRED"
    AUTHORITATIVE = "AUTHORITATIVE"


class McpArtifactSourceKind(StrEnum):
    REMOTE = "remote"
    OCI = "oci"
    PYPI = "pypi"
    NPM = "npm"
    CARGO = "cargo"
    NUGET = "nuget"
    MCPB = "mcpb"
    BIOCONDA = "bioconda"
    BINARY = "binary"


@dataclass(frozen=True)
class McpArtifactCandidate:
    kind: McpArtifactSourceKind
    identifier: str
    version: str | None = None
    transport: str = "stdio"
    endpoint: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def pinned(self) -> bool:
        if self.kind == McpArtifactSourceKind.REMOTE:
            return True
        if self.kind == McpArtifactSourceKind.OCI:
            return "@sha256:" in self.identifier or self.identifier.startswith("sha256:")
        return bool(self.version)

    @property
    def is_build_source(self) -> bool:
        return self.kind in {
            McpArtifactSourceKind.PYPI,
            McpArtifactSourceKind.NPM,
            McpArtifactSourceKind.CARGO,
            McpArtifactSourceKind.NUGET,
            McpArtifactSourceKind.MCPB,
            McpArtifactSourceKind.BIOCONDA,
            McpArtifactSourceKind.BINARY,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "identifier": self.identifier,
            "version": self.version,
            "pinned": self.pinned,
            "transport": self.transport,
            "endpoint": self.endpoint,
            "metadata": self.metadata,
            "build_source": self.is_build_source,
        }


@dataclass(frozen=True)
class McpRuntimeArtifact:
    kind: McpRuntimeArtifactKind
    transport: str
    endpoint: str | None = None
    image_digest: str | None = None
    command: str | None = None
    args: tuple[str, ...] = ()
    source_digest: str | None = None
    protocol_mode: str = "auto"
    provenance: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind == McpRuntimeArtifactKind.REMOTE_HTTP and not self.endpoint:
            raise ValueError("remote runtime artifact requires endpoint")
        if self.kind == McpRuntimeArtifactKind.OCI_IMAGE:
            if not self.image_digest or "@sha256:" not in self.image_digest:
                raise ValueError("OCI runtime artifact must use an immutable image@sha256 digest")
        if self.kind == McpRuntimeArtifactKind.DEVELOPMENT_STDIO and not self.command:
            raise ValueError("development stdio artifact requires command")

    @property
    def immutable(self) -> bool:
        return self.kind != McpRuntimeArtifactKind.DEVELOPMENT_STDIO

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "transport": self.transport,
            "endpoint": self.endpoint,
            "image_digest": self.image_digest,
            "command": self.command,
            "args": list(self.args),
            "source_digest": self.source_digest,
            "protocol_mode": self.protocol_mode,
            "immutable": self.immutable,
            "provenance": self.provenance,
        }


@dataclass(frozen=True)
class McpResolvedManifest:
    name: str
    display_name: str
    raw_manifest: dict[str, Any]
    normalized_manifest: dict[str, Any]
    candidates: tuple[McpArtifactCandidate, ...]
    schema_url: str | None = None
    warnings: tuple[str, ...] = ()

    @property
    def content_digest(self) -> str:
        payload = json.dumps(self.raw_manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class McpDeploymentDecision:
    status: McpLifecycleStatus
    runtime_level: McpRuntimeLevel | None
    candidate: McpArtifactCandidate | None
    runtime_artifact: McpRuntimeArtifact | None
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "runtime_level": self.runtime_level.value if self.runtime_level else None,
            "candidate": self.candidate.to_dict() if self.candidate else None,
            "runtime_artifact": self.runtime_artifact.to_dict() if self.runtime_artifact else None,
            "reason": self.reason,
        }


def development_runtime_allowed() -> bool:
    raw = os.getenv("ALLOW_DEVELOPMENT_MCP_RUNTIME")
    if raw is not None:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return os.getenv("YUXI_ENV", "development").strip().lower() not in {"production", "prod"}


def choose_deployment(
    candidates: list[McpArtifactCandidate] | tuple[McpArtifactCandidate, ...],
    *,
    allow_development: bool | None = None,
) -> McpDeploymentDecision:
    """Choose a deployable alternative without turning packages into runtime commands."""
    allow_dev = development_runtime_allowed() if allow_development is None else allow_development
    pinned = [candidate for candidate in candidates if candidate.pinned]

    for candidate in pinned:
        if candidate.kind == McpArtifactSourceKind.REMOTE and candidate.endpoint:
            artifact = McpRuntimeArtifact(
                kind=McpRuntimeArtifactKind.REMOTE_HTTP,
                transport=candidate.transport,
                endpoint=candidate.endpoint,
                provenance={"source": candidate.to_dict()},
            )
            return McpDeploymentDecision(
                McpLifecycleStatus.RESOLVED,
                McpRuntimeLevel.TRUSTED_REMOTE,
                candidate,
                artifact,
                "remote endpoint selected; security verification is required before enablement",
            )

    for candidate in pinned:
        if candidate.kind == McpArtifactSourceKind.OCI:
            artifact = McpRuntimeArtifact(
                kind=McpRuntimeArtifactKind.OCI_IMAGE,
                transport=candidate.transport,
                image_digest=candidate.identifier,
                provenance={"source": candidate.to_dict()},
            )
            return McpDeploymentDecision(
                McpLifecycleStatus.VERIFIED,
                McpRuntimeLevel.MANAGED_OCI,
                candidate,
                artifact,
                "immutable OCI artifact selected; isolated deployment is required",
            )

    if allow_dev:
        candidate = next(
            (
                item
                for item in pinned
                if item.metadata.get("development_requested") and item.metadata.get("command")
            ),
            None,
        )
        if candidate:
            artifact = McpRuntimeArtifact(
                kind=McpRuntimeArtifactKind.DEVELOPMENT_STDIO,
                transport="stdio",
                command=str(candidate.metadata["command"]),
                args=tuple(str(item) for item in candidate.metadata.get("args") or []),
                provenance={"source": candidate.to_dict()},
            )
            return McpDeploymentDecision(
                McpLifecycleStatus.RESOLVED,
                McpRuntimeLevel.DEVELOPMENT,
                candidate,
                artifact,
                "development stdio runtime selected",
            )

    build_source = next((candidate for candidate in pinned if candidate.is_build_source), None)
    if build_source is not None:
        return McpDeploymentDecision(
            McpLifecycleStatus.BUILD_REQUIRED,
            McpRuntimeLevel.MANAGED_OCI,
            build_source,
            None,
            "package candidate is a build source and must be converted to a verified OCI digest",
        )

    reason = "no pinned, deployable candidate"
    if candidates and not pinned:
        reason = "all package candidates are unpinned"
    return McpDeploymentDecision(McpLifecycleStatus.BLOCKED, None, None, None, reason)


__all__ = [
    name
    for name in globals()
    if name.startswith("Mcp") or name in {"choose_deployment", "development_runtime_allowed"}
]
