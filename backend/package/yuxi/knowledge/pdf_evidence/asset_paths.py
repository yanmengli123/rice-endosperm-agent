"""Canonical object-storage paths for scientific PDF visual assets."""

from __future__ import annotations


def revision_image_prefix(*, tenant_id: int, source_sha256: str, revision_id: str) -> str:
    """Return the MinIO prefix owned by one immutable parse revision."""
    return f"tenants/{tenant_id}/documents/{source_sha256}/mineru/{revision_id}/images"


__all__ = ["revision_image_prefix"]
