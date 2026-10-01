"""阅读包服务（P8：让 P1/P3 产物真正可读——前端消费入口的后端侧）。

- 阅读包：render（修正版优先）markdown + **图片 URL 重写**（``![](images/X)``
  → ``/api/literature/assets?uri=kbasset://...`` 鉴权端点，前端零重写逻辑）；
- 译文内容：variants 直接返回文本（对照阅读由前端按空行分段对齐）；
- 源 PDF：经 KnowledgeParseRevision.source_sha256 定位对象，专用代理端点。
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from sqlalchemy import select

from yuxi.literature.review import RESOLVED_MD_NAME
from yuxi.literature.reading.service import RENDER_BUCKET
from yuxi.storage.minio import get_minio_client
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeParseRevision,
    LiteraturePaper,
    LiteratureReaderRender,
    LiteratureVariant,
)

_IMAGE_REF_RE = re.compile(r"!\[([^\]]*)\]\((images/[^)\s]+)\)")


def rewrite_image_refs(markdown: str, base: str) -> str:
    """``![](images/X)`` → 鉴权资产端点 URL（base 为 MinIO 对象前缀，无 kbasset://）。"""

    def _replace(match: re.Match) -> str:
        alt, rel = match.group(1), match.group(2)
        uri = quote(f"kbasset://{base}{rel}", safe="")
        return f"![{alt}](/api/literature/assets?uri={uri})"

    return _IMAGE_REF_RE.sub(_replace, markdown)


async def get_reader_bundle(tenant_id: int, rid: str) -> dict[str, Any] | None:
    """阅读包：markdown（图片已重写）+ 是否修正版标记。"""
    async with pg_manager.get_async_session_context() as session:
        paper = (
            await session.execute(
                select(LiteraturePaper).where(LiteraturePaper.tenant_id == tenant_id, LiteraturePaper.rid == rid)
            )
        ).scalar_one_or_none()
        if paper is None or not paper.source_revision_id:
            return None
        render = (
            await session.execute(
                select(LiteratureReaderRender)
                .where(
                    LiteratureReaderRender.revision_id == paper.source_revision_id,
                    LiteratureReaderRender.status == "SUCCEEDED",
                )
                .order_by(LiteratureReaderRender.render_version.desc())
            )
        ).scalar_one_or_none()
        if render is None or not render.markdown_uri:
            return {"rid": rid, "ready": False, "reason": "阅读渲染未完成"}
    base = render.markdown_uri.removeprefix("kbasset://").removesuffix("render.md")
    minio = get_minio_client()
    markdown = ""
    resolved = False
    for name in (RESOLVED_MD_NAME, "render.md"):
        try:
            markdown = (await minio.adownload_file(RENDER_BUCKET, f"{base}{name}")).decode("utf-8")
            resolved = name == RESOLVED_MD_NAME
            break
        except Exception:  # noqa: BLE001 - 候选缺失试下一个
            continue
    if not markdown.strip():
        return {"rid": rid, "ready": False, "reason": "渲染产物不可读"}
    return {
        "rid": rid,
        "ready": True,
        "resolved": resolved,
        "title": paper.title or rid,
        "markdown": rewrite_image_refs(markdown, base),
        "markdown_uri": render.markdown_uri,
        "image_count": render.image_count,
    }


async def get_variant_content(tenant_id: int, rid: str, variant_kind: str) -> dict[str, Any] | None:
    """译文内容（zh/en_zh/summary；直接文本返回）。"""
    async with pg_manager.get_async_session_context() as session:
        paper = (
            await session.execute(
                select(LiteraturePaper).where(LiteraturePaper.tenant_id == tenant_id, LiteraturePaper.rid == rid)
            )
        ).scalar_one_or_none()
        if paper is None:
            return None
        variant = (
            await session.execute(
                select(LiteratureVariant)
                .where(LiteratureVariant.paper_id == paper.id, LiteratureVariant.variant_kind == variant_kind)
                .order_by(LiteratureVariant.created_at.desc())
            )
        ).scalar_one_or_none()
    if variant is None:
        return {"rid": rid, "variant_kind": variant_kind, "ready": False, "reason": "译文尚未生成"}
    content = (await get_minio_client().adownload_file(RENDER_BUCKET, variant.uri.removeprefix("kbasset://"))).decode(
        "utf-8"
    )
    return {
        "rid": rid,
        "variant_kind": variant_kind,
        "lang": variant.lang,
        "ready": True,
        "content": content,
        "uri": variant.uri,
    }


async def get_source_pdf(tenant_id: int, rid: str) -> tuple[bytes, str] | None:
    """源 PDF（原始上传件）：revision.source_sha256 → MinIO 对象。返回 (bytes, object_name)。"""
    async with pg_manager.get_async_session_context() as session:
        paper = (
            await session.execute(
                select(LiteraturePaper).where(LiteraturePaper.tenant_id == tenant_id, LiteraturePaper.rid == rid)
            )
        ).scalar_one_or_none()
        if paper is None or not paper.source_revision_id:
            return None
        revision = (
            await session.execute(
                select(KnowledgeParseRevision).where(KnowledgeParseRevision.revision_id == paper.source_revision_id)
            )
        ).scalar_one_or_none()
    if revision is None:
        return None
    object_name = f"tenants/{tenant_id}/documents/{revision.source_sha256}/source/original.pdf"
    try:
        data = await get_minio_client().adownload_file(RENDER_BUCKET, object_name)
    except Exception:  # noqa: BLE001 - 对象缺失
        return None
    return data, object_name
