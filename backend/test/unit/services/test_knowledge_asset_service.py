from __future__ import annotations

from types import SimpleNamespace

import pytest

from yuxi.knowledge.pdf_evidence.contracts import ParserArtifact, PipelineResult
from yuxi.services import knowledge_asset_service as asset_service
from yuxi.services.knowledge_asset_service import (
    KnowledgeAssetError,
    _image_object_prefix,
    asset_response_headers,
    build_kbasset_url,
    etag_matches,
    materialize_reused_revision_assets,
    resolve_asset,
    rewrite_kbasset_uri,
)
from yuxi.knowledge.pdf_evidence.contracts import UnifiedArticle


# --- kbasset URI contract ---------------------------------------------------


def test_build_kbasset_url_produces_logical_uri_and_rejects_path_tricks():
    uri = build_kbasset_url("file_1", "rev_1", "fig_abc.jpg")
    assert uri == "kbasset://file_1/rev_1/fig_abc.jpg"

    for bad_name in (
        "../escape.png",
        "a/b.jpg",
        "a\\b.jpg",
        "photo.svg",
        "photo.bmp",
        "",
        "nul%00.png",
        "encoded%2fpath.png",
        "encoded%5cpath.png",
    ):
        with pytest.raises((ValueError, KnowledgeAssetError)):
            build_kbasset_url("file_1", "rev_1", bad_name)

    for bad_identity in ("a/b", "a\\b", ".."):
        with pytest.raises(ValueError):
            build_kbasset_url(bad_identity, "rev_1", "a.png")
        with pytest.raises(ValueError):
            build_kbasset_url("file_1", bad_identity, "a.png")


def test_image_object_prefix_matches_ingest_layout():
    class FakeRevision:
        tenant_id = 7
        source_sha256 = "a" * 64
        revision_id = "spr_x"

    assert _image_object_prefix(FakeRevision()) == (f"tenants/7/documents/{'a' * 64}/mineru/spr_x/images")


# --- reuse materialization --------------------------------------------------


class _FakeMinio:
    def __init__(self, names: list[str], copy_ok: bool = True):
        self.names = names
        self.copy_ok = copy_ok
        self.copy_calls: list[tuple[str, str]] = []

    async def alist_object_names_by_prefix(self, bucket_name: str, prefix: str) -> list[str]:
        return [name for name in self.names if name.startswith(prefix)]

    async def acopy_object(self, bucket_name: str, object_name: str, source_object_name: str) -> bool:
        self.copy_calls.append((object_name, source_object_name))
        return self.copy_ok


class _FakeRevision:
    def __init__(self, tenant_id: int, source_sha256: str, revision_id: str, file_id: str = "f1"):
        self.tenant_id = tenant_id
        self.source_sha256 = source_sha256
        self.revision_id = revision_id
        self.file_id = file_id


@pytest.mark.asyncio
async def test_materialize_copies_canonical_images_into_target_prefix(monkeypatch: pytest.MonkeyPatch):
    sha = "b" * 64
    source = _FakeRevision(7, sha, "spr_src")
    target = _FakeRevision(7, sha, "spr_tgt")
    fake = _FakeMinio([f"tenants/7/documents/{sha}/mineru/spr_src/images/1.jpg"])
    monkeypatch.setattr(asset_service, "get_minio_client", lambda: fake)

    copied = await materialize_reused_revision_assets(source, target)

    assert fake.copy_calls == [
        (
            f"tenants/7/documents/{sha}/mineru/spr_tgt/images/1.jpg",
            f"tenants/7/documents/{sha}/mineru/spr_src/images/1.jpg",
        )
    ]
    assert copied[0]["asset_name"] == "1.jpg"


@pytest.mark.asyncio
async def test_materialize_fails_when_canonical_image_missing(monkeypatch: pytest.MonkeyPatch):
    sha = "c" * 64
    fake = _FakeMinio([f"tenants/7/documents/{sha}/mineru/spr_src/images/1.jpg"], copy_ok=False)
    monkeypatch.setattr(asset_service, "get_minio_client", lambda: fake)
    with pytest.raises(KnowledgeAssetError):
        await materialize_reused_revision_assets(_FakeRevision(7, sha, "spr_src"), _FakeRevision(7, sha, "spr_tgt"))


@pytest.mark.asyncio
async def test_materialize_rejects_cross_tenant_reuse():
    sha = "d" * 64
    with pytest.raises(KnowledgeAssetError):
        await materialize_reused_revision_assets(_FakeRevision(7, sha, "s"), _FakeRevision(8, sha, "t"))


# --- reused markdown identity rewrite ---------------------------------------


def test_rewrite_kbasset_uri_targets_only_source_identity():
    source_file, source_rev = "file_src", "rev_src"
    target_file, target_rev = "file_tgt", "rev_tgt"
    markdown = f"![a](kbasset://{source_file}/{source_rev}/1.jpg)\n![b](kbasset://other_file/rev_other/2.png)\n"
    rewritten = rewrite_kbasset_uri(markdown, source_file, source_rev, target_file, target_rev)
    assert f"kbasset://{target_file}/{target_rev}/1.jpg" in rewritten
    # 不匹配的 URI 保持原样
    assert "kbasset://other_file/rev_other/2.png" in rewritten


def _fake_pipeline_result(markdown: str) -> PipelineResult:
    article = UnifiedArticle(
        schema_version="1.0",
        source_sha256="0" * 64,
        title="t",
        markdown=markdown,
        metadata={},
        sections=[],
        references=[],
        citation_mentions=[],
        anchors=[],
        parser_provenance={},
    )
    return PipelineResult(
        "INDEXED_FULL", article, {}, [ParserArtifact("x", "x.bin", b"", "application/octet-stream")], markdown, "fp"
    )


def test_ingest_service_rewrite_helper_rewrites_article_and_annotated_markdown():
    from yuxi.services.scientific_pdf_ingest_service import _rewrite_reused_markdown_identity

    result = _fake_pipeline_result("![f](kbasset://fs/rs/1.jpg)")
    source = _FakeRevision(7, "e" * 64, "rs", file_id="fs")
    target = _FakeRevision(7, "e" * 64, "rt", file_id="ft")

    rewritten = _rewrite_reused_markdown_identity(result, source, target)

    assert "kbasset://ft/rt/1.jpg" in rewritten.annotated_markdown
    assert rewritten.unified_article is not None
    assert "kbasset://ft/rt/1.jpg" in rewritten.unified_article.markdown


# --- asset name validation ---------------------------------------------------


def test_validate_asset_name_rejects_traversal_and_disallowed_types():
    for bad in ("", "..", "../x.png", "a/b.png", "x.svg", "x.bmp", "x.pdf", "x.exe"):
        with pytest.raises(KnowledgeAssetError):
            asset_service._validate_asset_name(bad)
    assert asset_service._validate_asset_name("170000_abc.jpg") == "170000_abc.jpg"


def test_asset_headers_and_etag_match_use_the_image_object_etag():
    resolved = {"etag": '"image-etag"'}
    assert asset_response_headers(resolved) == {
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, max-age=300",
        "Content-Disposition": "inline",
        "Vary": "Authorization",
        "ETag": '"image-etag"',
    }
    assert etag_matches('W/"old", "image-etag"', "image-etag")
    assert not etag_matches('"source-pdf-etag"', "image-etag")


@pytest.mark.asyncio
async def test_resolve_asset_reconstructs_private_key_and_rejects_mime_mismatch(
    monkeypatch: pytest.MonkeyPatch,
):
    revision = SimpleNamespace(
        revision_id="spr_2",
        file_id="file_1",
        kb_id="kb_9",
        tenant_id=7,
        source_sha256="a" * 64,
    )
    monkeypatch.setattr(
        asset_service,
        "_load_knowledge_file",
        lambda _file_id: _async_value(SimpleNamespace(file_id="file_1", kb_id="kb_9")),
    )
    monkeypatch.setattr(
        asset_service,
        "_load_knowledge_base",
        lambda _kb_id: _async_value(SimpleNamespace(kb_id="kb_9", tenant_id=7)),
    )
    monkeypatch.setattr(asset_service, "_load_parse_revision", lambda _revision_id: _async_value(revision))
    monkeypatch.setattr(asset_service, "_authorize_kb_read", lambda _user, _kb_id: _async_value(None))
    monkeypatch.setattr(
        asset_service,
        "_stat_object",
        lambda _bucket, _key: _async_value(SimpleNamespace(etag="asset-etag", size=42, content_type="image/jpeg")),
    )

    resolved = await resolve_asset(
        kb_id="kb_9",
        file_id="file_1",
        revision_id="spr_2",
        asset_name="figure.jpg",
        user=SimpleNamespace(role="user"),
    )

    assert resolved["object_key"] == (f"tenants/7/documents/{'a' * 64}/mineru/spr_2/images/figure.jpg")
    assert resolved["media_type"] == "image/jpeg"

    monkeypatch.setattr(
        asset_service,
        "_stat_object",
        lambda _bucket, _key: _async_value(SimpleNamespace(etag="asset-etag", size=42, content_type="text/html")),
    )
    with pytest.raises(KnowledgeAssetError):
        await resolve_asset(
            kb_id="kb_9",
            file_id="file_1",
            revision_id="spr_2",
            asset_name="figure.jpg",
            user=SimpleNamespace(role="user"),
        )


async def _async_value(value):
    return value
