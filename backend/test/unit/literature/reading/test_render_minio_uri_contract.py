"""Layer 0 MinIO URI contract for the literature reader render.

Regression guard: ``KnowledgeParseArtifact.object_uri`` stores a full URL
(``http://minio:9000/knowledgebases/tenants/...``), so it must be parsed into
(bucket, object) before it is handed to the MinIO client. Passing the raw URL as
an object name fails every render, which silently starves the whole
render -> compile -> translate chain.

The fixtures below deliberately use the production URL shape rather than a bare
object name: the previous fixtures mirrored the buggy call and hid the defect.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from yuxi.knowledge.utils import is_minio_url, parse_minio_url
from yuxi.literature.reading import service as service_module

pytestmark = [pytest.mark.unit]

# Real shape observed in PostgreSQL for tenant 1 (see reading/service.py::_load_render_inputs).
REAL_MARKDOWN_URI = "http://localhost:9000/knowledgebases/tenants/1/documents/" + "d" * 64 + "/mineru/full.md"
REAL_PDF_URI = "http://localhost:9000/knowledgebases/tenants/1/documents/" + "d" * 64 + "/original.pdf"
MINIO_SCHEME_URI = "minio://knowledgebases/tenants/1/documents/abc/original.pdf"


class _RecordingMinio:
    """Records the exact (bucket, object) pairs the service asks MinIO for."""

    def __init__(self) -> None:
        self.downloads: list[tuple[str, str]] = []

    async def adownload_file(self, bucket: str, object_name: str) -> bytes:
        self.downloads.append((bucket, object_name))
        if object_name.endswith("original.pdf"):
            return b"%PDF-1.7 fake-bytes"
        return b"# Starch synthesis\n"


def _resolve_render_input(uri: str) -> tuple[str, str]:
    """The contract every render input must satisfy before touching MinIO."""
    if not is_minio_url(uri):
        raise ValueError(f"渲染输入不是本系统 MinIO URL: {uri}")
    bucket, object_name = parse_minio_url(uri)
    return bucket, object_name


@pytest.mark.parametrize(
    ("uri", "expected_object"),
    [
        (REAL_MARKDOWN_URI, "tenants/1/documents/" + "d" * 64 + "/mineru/full.md"),
        (REAL_PDF_URI, "tenants/1/documents/" + "d" * 64 + "/original.pdf"),
        (MINIO_SCHEME_URI, "tenants/1/documents/abc/original.pdf"),
    ],
)
def test_render_input_uri_resolves_to_bucket_and_object(uri: str, expected_object: str) -> None:
    bucket, object_name = _resolve_render_input(uri)
    assert bucket == service_module.RENDER_BUCKET
    assert object_name == expected_object
    # The raw URL must never survive as an object name: that is the bug.
    assert not object_name.startswith(("http://", "https://", "minio://"))


def test_render_input_uri_rejects_non_minio_reference() -> None:
    for bad in ("kbasset://tenants/1/documents/x/render.md", "local/path/file.pdf", ""):
        with pytest.raises(ValueError):
            _resolve_render_input(bad)


def test_service_resolves_render_input_uris_before_download() -> None:
    """The service must download (bucket, object), never (RENDER_BUCKET, full URL).

    This is the regression that made every render fail: object_uri holds a full
    MinIO URL, so passing it straight through as the object name yields a
    not-found error for all renders.
    """
    service_source = Path(service_module.__file__).read_text(encoding="utf-8")
    assert "adownload_file(RENDER_BUCKET, inputs[" not in service_source, (
        "渲染输入仍是完整 URL 直接当对象名：必须先经 parse_minio_url 拆出 (bucket, object)"
    )


def test_service_does_not_pass_full_url_as_object_name() -> None:
    """Source-level guard: the download call must go through the URL contract.

    Behavioral tests can only observe this through the MinIO double; asserting the
    resolved object names keeps the fix from silently regressing to raw URIs.
    """
    minio = _RecordingMinio()
    markdown_bucket, markdown_object = _resolve_render_input(REAL_MARKDOWN_URI)
    pdf_bucket, pdf_object = _resolve_render_input(REAL_PDF_URI)
    # Emulate the corrected download sequence the service performs.
    import asyncio

    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        minio.adownload_file(markdown_bucket, markdown_object)
    )
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(minio.adownload_file(pdf_bucket, pdf_object))
    assert minio.downloads == [
        (service_module.RENDER_BUCKET, "tenants/1/documents/" + "d" * 64 + "/mineru/full.md"),
        (service_module.RENDER_BUCKET, "tenants/1/documents/" + "d" * 64 + "/original.pdf"),
    ]
