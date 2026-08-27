"""Runtime/build/long-compute provider contracts for server-grade MCP."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx

from yuxi.agents.mcp.domain import McpArtifactCandidate, McpRuntimeArtifact, McpRuntimeArtifactKind
from yuxi.agents.mcp.security import validate_oci_runtime_artifact


class McpProviderUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class McpRuntimeDeployment:
    runtime_ref: str
    endpoint: str | None
    state: str
    metadata: dict[str, Any] = field(default_factory=dict)


class McpArtifactBuilder(ABC):
    @abstractmethod
    async def build(self, candidate: McpArtifactCandidate) -> McpRuntimeArtifact: ...


class HttpMcpArtifactBuilder(McpArtifactBuilder):
    """Client for an external, authenticated BuildKit/Tekton-style builder."""

    def __init__(self, base_url: str | None = None, token: str | None = None) -> None:
        self.base_url = (base_url or os.getenv("YUXI_MCP_BUILDER_URL", "")).rstrip("/")
        self.token = token or os.getenv("YUXI_MCP_BUILDER_TOKEN")

    async def build(self, candidate: McpArtifactCandidate) -> McpRuntimeArtifact:
        if not self.base_url or not self.token:
            raise McpProviderUnavailable("MCP artifact builder is not configured")
        if not candidate.pinned or not candidate.is_build_source:
            raise ValueError("builder accepts only pinned package build sources")
        async with httpx.AsyncClient(timeout=120, follow_redirects=False, trust_env=False) as client:
            response = await client.post(
                f"{self.base_url}/v1/artifacts/build",
                headers={"Authorization": f"Bearer {self.token}"},
                json={"candidate": candidate.to_dict()},
            )
        response.raise_for_status()
        payload = response.json()
        digest = validate_oci_runtime_artifact(str(payload.get("image_digest") or ""))
        return McpRuntimeArtifact(
            kind=McpRuntimeArtifactKind.OCI_IMAGE,
            transport="stdio",
            image_digest=digest,
            source_digest=payload.get("source_digest"),
            provenance=dict(payload.get("provenance") or {}),
        )


class McpRuntimeProvider(ABC):
    @abstractmethod
    async def deploy(
        self, artifact: McpRuntimeArtifact, *, tenant_id: int, installation_id: int
    ) -> McpRuntimeDeployment: ...

    @abstractmethod
    async def stop(self, runtime_ref: str) -> None: ...


class HttpOciRuntimeProvider(McpRuntimeProvider):
    """Client for the isolated MCP runtime manager (Docker/Kubernetes backend)."""

    def __init__(self, base_url: str | None = None, token: str | None = None) -> None:
        self.base_url = (base_url or os.getenv("YUXI_MCP_RUNTIME_URL", "")).rstrip("/")
        self.token = token or os.getenv("YUXI_MCP_RUNTIME_TOKEN")

    def _headers(self) -> dict[str, str]:
        if not self.base_url or not self.token:
            raise McpProviderUnavailable("isolated MCP runtime provider is not configured")
        return {"Authorization": f"Bearer {self.token}"}

    async def deploy(
        self, artifact: McpRuntimeArtifact, *, tenant_id: int, installation_id: int
    ) -> McpRuntimeDeployment:
        if artifact.kind != McpRuntimeArtifactKind.OCI_IMAGE:
            raise ValueError("OCI runtime provider accepts only OCI artifacts")
        validate_oci_runtime_artifact(str(artifact.image_digest or ""))
        async with httpx.AsyncClient(timeout=60, follow_redirects=False, trust_env=False) as client:
            response = await client.post(
                f"{self.base_url}/v1/runtimes",
                headers=self._headers(),
                json={
                    "tenant_id": tenant_id,
                    "installation_id": installation_id,
                    "artifact": artifact.to_dict(),
                    "security": {
                        "read_only_rootfs": True,
                        "non_root": True,
                        "drop_capabilities": ["ALL"],
                        "no_new_privileges": True,
                        "network_policy": "deny_by_default",
                        "host_mounts": [],
                    },
                },
            )
        response.raise_for_status()
        payload = response.json()
        return McpRuntimeDeployment(
            runtime_ref=str(payload["runtime_ref"]),
            endpoint=payload.get("endpoint"),
            state=str(payload.get("state") or "DEPLOYED"),
            metadata=dict(payload.get("metadata") or {}),
        )

    async def stop(self, runtime_ref: str) -> None:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
            response = await client.delete(
                f"{self.base_url}/v1/runtimes/{runtime_ref}", headers=self._headers()
            )
        response.raise_for_status()


@dataclass(frozen=True)
class BioComputeJob:
    operation: str
    inputs: tuple[dict[str, Any], ...]
    parameters: dict[str, Any]
    tenant_id: int
    uid: str


class BioComputeBroker:
    """Submit heavy jobs instead of holding an MCP request open."""

    def __init__(self, base_url: str | None = None, token: str | None = None) -> None:
        self.base_url = (base_url or os.getenv("YUXI_BIOCOMPUTE_BROKER_URL", "")).rstrip("/")
        self.token = token or os.getenv("YUXI_BIOCOMPUTE_BROKER_TOKEN")

    async def submit(self, job: BioComputeJob) -> dict[str, Any]:
        if not self.base_url or not self.token:
            raise McpProviderUnavailable("BioComputeBroker is not configured")
        async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
            response = await client.post(
                f"{self.base_url}/v1/jobs",
                headers={"Authorization": f"Bearer {self.token}"},
                json={
                    "operation": job.operation,
                    "inputs": list(job.inputs),
                    "parameters": job.parameters,
                    "tenant_id": job.tenant_id,
                    "uid": job.uid,
                },
            )
        response.raise_for_status()
        return dict(response.json())


__all__ = [
    "McpProviderUnavailable",
    "McpRuntimeDeployment",
    "McpArtifactBuilder",
    "HttpMcpArtifactBuilder",
    "McpRuntimeProvider",
    "HttpOciRuntimeProvider",
    "BioComputeJob",
    "BioComputeBroker",
]
