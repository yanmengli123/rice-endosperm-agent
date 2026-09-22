"""Authenticated knowledge asset serving.

Scientific PDF evidence pipelines store extracted figures in the private
``knowledgebases`` bucket.  Markdown must never carry MinIO URLs or presigned
URLs; instead it references a stable logical URI::

    kbasset://{file_id}/{revision_id}/{asset_name}

This service is the single storage access boundary:

    request -> authentication -> file/KB ACL -> revision ownership
            -> strict asset_name validation -> object key reconstruction
            -> prefix containment check -> MinIO stat -> stream

Any failure that could leak existence or tenant layout returns 404 so that
cross-tenant probing cannot enumerate resources.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from fastapi.responses import StreamingResponse
from sqlalchemy import select
from yuxi.knowledge.pdf_evidence.asset_paths import revision_image_prefix
from yuxi.knowledge.runtime import knowledge_base
from yuxi.storage.minio import get_minio_client
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import User
from yuxi.storage.postgres.models_knowledge import KnowledgeBase, KnowledgeFile, KnowledgeParseRevision

ASSET_BUCKET = "knowledgebases"
ASSET_IMAGE_MEDIA_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})
ASSET_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
ASSET_NAME_MAX_LENGTH = 255

# Animations / SVG are explicitly disallowed as inline preview content: SVG is
# XML with a large attack surface.  The asset endpoint only streams static
# raster images for now; SVG rasterization is deferred to V1.1.
_ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

KBASSET_URI_PREFIX = "kbasset://"


class KnowledgeAssetError(Exception):
    """Base error for asset resolution failures."""

    def __init__(self, *args: Any, status_code: int = 404, headers: dict[str, str] | None = None):
        super().__init__(*args)
        self.status_code = status_code
        self.headers = headers or {}


def _parse_kb_asset_uri(uri: str) -> tuple[str, str, str]:
    """Parse ``kbasset://{file_id}/{revision_id}/{asset_name}``.

    Returns ``(file_id, revision_id, asset_name)``.  Raises
    :class:`KnowledgeAssetError` when the shape is not the expected one.
    """
    if not uri or not uri.startswith(KBASSET_URI_PREFIX):
        raise KnowledgeAssetError("invalid asset URI", status_code=404)
    path = uri[len(KBASSET_URI_PREFIX) :]
    parts = path.split("/")
    if len(parts) != 3:
        raise KnowledgeAssetError("invalid asset URI", status_code=404)
    file_id, revision_id, asset_name = parts
    if not file_id or not revision_id or not asset_name:
        raise KnowledgeAssetError("invalid asset URI", status_code=404)
    return file_id, revision_id, asset_name


def _validate_asset_name(asset_name: str) -> str:
    """Strict allowlist validation for a single asset filename."""
    if not asset_name or len(asset_name) > ASSET_NAME_MAX_LENGTH:
        raise KnowledgeAssetError("invalid asset name", status_code=404)
    if not ASSET_NAME_PATTERN.match(asset_name):
        raise KnowledgeAssetError("invalid asset name", status_code=404)
    # Reject any path-ish variants regardless of OS separator.
    posix = PurePosixPath(asset_name)
    win = PureWindowsPath(asset_name)
    if posix.name != asset_name or win.name != asset_name:
        raise KnowledgeAssetError("invalid asset name", status_code=404)
    suffix = PurePosixPath(asset_name).suffix.lower()
    if suffix not in _ALLOWED_EXTENSIONS:
        raise KnowledgeAssetError("unsupported asset type", status_code=404)
    return asset_name


def _media_type_for_asset_name(asset_name: str) -> str:
    suffix = PurePosixPath(asset_name).suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }[suffix]


def build_kbasset_url(file_id: str, revision_id: str, asset_name: str) -> str:
    """Build the stable logical asset URI for Markdown.

    ``kbasset://{file_id}/{revision_id}/{asset_name}`` is the ONLY shape that
    may ever appear in Markdown.  It never contains a MinIO URL, a presigned
    token, a tenant id, a kb id or a storage layout.
    """
    if not file_id or not revision_id or not asset_name:
        raise ValueError("file_id, revision_id and asset_name are required")
    _validate_asset_name(asset_name)
    for identity in (file_id, revision_id):
        if "/" in identity or "\\" in identity or ".." in identity:
            raise ValueError(f"invalid identity: {identity!r}")
    return f"{KBASSET_URI_PREFIX}{file_id}/{revision_id}/{asset_name}"


def _image_object_prefix(revision: KnowledgeParseRevision) -> str:
    """Rebuild the images prefix owned by a parse revision.

    MUST mirror ``scientific_pdf_ingest_service``'s ``image_prefix``::
        ``tenants/{tenant_id}/documents/{source_sha256}/mineru/{revision_id}/images``
    """
    return revision_image_prefix(
        tenant_id=int(revision.tenant_id),
        source_sha256=str(revision.source_sha256),
        revision_id=str(revision.revision_id),
    )


async def materialize_reused_revision_assets(
    source_revision: KnowledgeParseRevision, target_revision: KnowledgeParseRevision
) -> list[dict[str, str]]:
    """Server-side copy of the canonical revision's images into the reusing revision.

    Identity-cache reuse loads the canonical Markdown verbatim, whose
    ``kbasset://`` URIs point at the canonical file/revision.  Copying each
    image into the target revision's own prefix (same asset name) lets the
    target Markdown reference its own identity and keeps the asset endpoint's
    file->KB->revision authorization chain intact for every tenant.

    Returns the list of copied assets; raises :class:`KnowledgeAssetError`
    when the canonical image set cannot be reproduced (caller then falls back
    to a full re-parse instead of serving broken image references).
    """
    if source_revision.tenant_id != target_revision.tenant_id:
        raise KnowledgeAssetError("cross-tenant reuse is not supported", status_code=404)
    source_prefix = _image_object_prefix(source_revision)
    target_prefix = _image_object_prefix(target_revision)
    source_names = await get_minio_client().alist_object_names_by_prefix(ASSET_BUCKET, f"{source_prefix}/")
    copied: list[dict[str, str]] = []
    for source_object_name in source_names:
        asset_name = source_object_name[len(source_prefix) + 1 :]
        if not asset_name:
            continue
        _validate_asset_name(asset_name)
        target_object_name = f"{target_prefix}/{asset_name}"
        if not await get_minio_client().acopy_object(ASSET_BUCKET, target_object_name, source_object_name):
            raise KnowledgeAssetError(f"canonical image missing during reuse: {asset_name}", status_code=404)
        copied.append(
            {
                "source": source_object_name,
                "target": target_object_name,
                "asset_name": asset_name,
            }
        )
    return copied


def rewrite_kbasset_uri(
    markdown: str,
    source_file_id: str,
    source_revision_id: str,
    target_file_id: str,
    target_revision_id: str,
) -> str:
    """Rewrite ``kbasset://`` URIs from the canonical identity to the reusing identity."""
    if not markdown:
        return markdown
    pattern = re.compile(r"kbasset://" + re.escape(source_file_id) + r"/" + re.escape(source_revision_id) + r"/")
    return pattern.sub(
        f"kbasset://{target_file_id}/{target_revision_id}/",
        markdown,
    )


async def _load_parse_revision(revision_id: str) -> KnowledgeParseRevision | None:
    async with pg_manager.get_async_session_context() as session:
        return (
            (
                await session.execute(
                    select(KnowledgeParseRevision).where(KnowledgeParseRevision.revision_id == revision_id)
                )
            )
            .scalars()
            .one_or_none()
        )


async def _load_knowledge_file(file_id: str) -> KnowledgeFile | None:
    async with pg_manager.get_async_session_context() as session:
        return (
            (await session.execute(select(KnowledgeFile).where(KnowledgeFile.file_id == file_id)))
            .scalars()
            .one_or_none()
        )


async def _load_knowledge_base(kb_id: str) -> KnowledgeBase | None:
    async with pg_manager.get_async_session_context() as session:
        return (
            (await session.execute(select(KnowledgeBase).where(KnowledgeBase.kb_id == kb_id))).scalars().one_or_none()
        )


async def _authorize_kb_read(user: User, kb_id: str) -> None:
    """Raise 404 (not 403) whenever the user cannot read the KB."""
    if user.role == "superadmin":
        return
    try:
        accessible = await knowledge_base.check_accessible(
            {"role": user.role, "uid": user.uid, "department_id": user.department_id}, kb_id
        )
    except Exception:  # noqa: BLE001 - normalize any KB lookup failure
        raise KnowledgeAssetError("not found", status_code=404) from None
    if not accessible:
        raise KnowledgeAssetError("not found", status_code=404)


async def _stat_object(bucket_name: str, object_name: str) -> Any | None:
    """Return MinIO stat info or None when the object does not exist."""
    client = get_minio_client()
    try:
        return await client.astat_object(bucket_name, object_name)
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "code", None)
        if code == "NoSuchKey":
            return None
        raise KnowledgeAssetError("object unavailable", status_code=404) from exc


async def resolve_asset(*, kb_id: str, file_id: str, revision_id: str, asset_name: str, user: User) -> dict[str, Any]:
    """Validated + authorized asset resolution.

    Returns a plain dict with ``object_key``/``media_type``/``stat`` when the
    asset is authorized and exists; raises :class:`KnowledgeAssetError`
    otherwise.  All forbidden/miss/forged outcomes collapse to 404.
    """
    asset_name = _validate_asset_name(asset_name)

    knowledge_file = await _load_knowledge_file(file_id)
    if knowledge_file is None or knowledge_file.kb_id != kb_id:
        raise KnowledgeAssetError("not found", status_code=404)

    knowledge_db = await _load_knowledge_base(kb_id)
    if knowledge_db is None:
        raise KnowledgeAssetError("not found", status_code=404)

    await _authorize_kb_read(user, kb_id)

    revision = await _load_parse_revision(revision_id)
    if revision is None or revision.file_id != file_id:
        raise KnowledgeAssetError("not found", status_code=404)
    if revision.kb_id != kb_id:
        raise KnowledgeAssetError("not found", status_code=404)
    if int(revision.tenant_id) != int(knowledge_db.tenant_id):
        raise KnowledgeAssetError("not found", status_code=404)

    prefix = _image_object_prefix(revision)
    object_key = f"{prefix}/{asset_name}"
    # Defense in depth: the reconstructed key must stay inside the revision images prefix.
    if not object_key.startswith(prefix + "/"):
        raise KnowledgeAssetError("invalid object key", status_code=404)

    stat = await _stat_object(ASSET_BUCKET, object_key)
    if stat is None:
        raise KnowledgeAssetError("not found", status_code=404)

    media_type = _media_type_for_asset_name(asset_name)
    stored_media_type = str(getattr(stat, "content_type", "") or "").split(";", 1)[0].strip().lower()
    if stored_media_type not in {"", "application/octet-stream", media_type}:
        raise KnowledgeAssetError("unsupported asset type", status_code=404)
    return {
        "bucket": ASSET_BUCKET,
        "object_key": object_key,
        "media_type": media_type,
        "stat": stat,
        "etag": str(getattr(stat, "etag", "") or ""),
        "size": int(getattr(stat, "size", 0) or 0),
    }


def asset_response_headers(resolved: dict[str, Any]) -> dict[str, str]:
    """Return the deterministic headers shared by 200 and 304 responses."""
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, max-age=300",
        "Content-Disposition": "inline",
        "Vary": "Authorization",
    }
    etag = str(resolved.get("etag") or "").strip().strip('"')
    if etag:
        headers["ETag"] = f'"{etag}"'
    return headers


def etag_matches(if_none_match: str | None, etag: str | None) -> bool:
    """Match a MinIO object ETag against a standards-compatible header list."""
    normalized_etag = str(etag or "").strip().strip('"')
    if not normalized_etag or not if_none_match:
        return False
    for candidate in if_none_match.split(","):
        normalized_candidate = candidate.strip()
        if normalized_candidate == "*":
            return True
        if normalized_candidate.startswith("W/"):
            normalized_candidate = normalized_candidate[2:].strip()
        if normalized_candidate.strip('"') == normalized_etag:
            return True
    return False


async def stream_asset(resolved: dict[str, Any]) -> StreamingResponse:
    """Build a media :class:`StreamingResponse` streaming the MinIO object.

    Deterministic security headers and the object body are set here; the
    caller only returns the response.  ``ETag`` comes from the MinIO object
    stat (not the source PDF), which is what the browser revalidates.
    """
    client = get_minio_client()
    object_stream = await client.adownload_response(resolved["bucket"], resolved["object_key"])

    async def _iter():
        try:
            while True:
                chunk = await asyncio.to_thread(object_stream.read, 1 << 16)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                object_stream.close()
                object_stream.release_conn()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass

    headers = asset_response_headers(resolved)
    if resolved["size"] is not None:
        headers["Content-Length"] = str(resolved["size"])
    return StreamingResponse(_iter(), media_type=resolved["media_type"], headers=headers)
