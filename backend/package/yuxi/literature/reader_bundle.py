"""阅读包服务（P8：让 P1/P3 产物真正可读——前端消费入口的后端侧）。

- 阅读包：render（修正版优先）markdown + **图片 URL 重写**（``![](images/X)``
  → ``/api/literature/assets?uri=kbasset://...`` 鉴权端点，前端零重写逻辑）；
- 译文内容：variants 直接返回文本（对照阅读由前端按空行分段对齐）；
- 源 PDF：经 KnowledgeParseRevision.source_sha256 定位对象，专用代理端点。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import quote

from sqlalchemy import select

from yuxi.literature.access import paper_visibility
from yuxi.literature.reading.service import RENDER_BUCKET
from yuxi.literature.review import RESOLVED_MD_NAME
from yuxi.storage.minio import get_minio_client
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeParseArtifact,
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
                select(LiteraturePaper).where(paper_visibility(tenant_id), LiteraturePaper.rid == rid)
            )
        ).scalar_one_or_none()
        if paper is None:
            return None
        render = (
            await session.execute(
                select(LiteratureReaderRender)
                .where(
                    LiteratureReaderRender.revision_id == paper.source_revision_id,
                    LiteratureReaderRender.status == "SUCCEEDED",
                )
                .order_by(LiteratureReaderRender.updated_at.desc(), LiteratureReaderRender.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        missing_render = (render is None or not render.markdown_uri) and not (paper.artifact_pointers or {}).get(
            "document"
        )
    if missing_render:
        return await _scientific_reading_fallback(tenant_id, paper) or {
            "rid": rid,
            "ready": False,
            "reason": "阅读渲染未完成",
        }
    base = (
        render.markdown_uri.removeprefix("kbasset://").removesuffix("render.md")
        if render and render.markdown_uri
        else ""
    )
    minio = get_minio_client()
    markdown = ""
    resolved = False
    revision_id = None
    frozen = False
    source = "rendered"
    if (paper.artifact_pointers or {}).get("document"):
        from yuxi.literature.artifacts import resolve_effective_document
        from yuxi.literature.review import para_text

        effective = await resolve_effective_document(tenant_id, rid, paper=paper, storage=minio)
        document = json.loads(effective["content"])
        # Image references belong to the document's generation, not whichever
        # render happens to be newest after an asynchronous re-render.
        if document.get("_image_base"):
            base = str(document["_image_base"])
            allowed_prefix = f"tenants/{tenant_id}/documents/"
            if not base.startswith(allowed_prefix) or ".." in base.split("/"):
                raise ValueError("正文图片来源前缀无效")
        markdown = "\n\n".join(para_text(p) for p in document.get("paragraphs", []))
        revision_id = effective["revision_id"]
        frozen = effective["frozen"]
        source = effective["source"]
        resolved = source in {"manual_frozen", "decided"}
    else:
        for name in (RESOLVED_MD_NAME, "render.md"):
            try:
                markdown = (await minio.adownload_file(RENDER_BUCKET, f"{base}{name}")).decode("utf-8")
                resolved = name == RESOLVED_MD_NAME
                source = "decided" if resolved else "rendered"
                break
            except Exception:  # noqa: BLE001 - legacy missing candidate
                continue
    if not markdown.strip():
        return {"rid": rid, "ready": False, "reason": "渲染产物不可读"}
    return {
        "rid": rid,
        "ready": True,
        "resolved": resolved,
        "title": paper.title or rid,
        "markdown": rewrite_image_refs(markdown, base),
        "markdown_uri": render.markdown_uri if render else None,
        "image_count": render.image_count if render else 0,
        "image_base": base,
        "revision_id": revision_id,
        "frozen": frozen,
        "source": source,
    }


async def _scientific_reading_fallback(tenant_id: int, paper: LiteraturePaper) -> dict | None:
    """Read canonical parse without pretending review/render succeeded."""
    if not paper.source_revision_id:
        return None
    from yuxi.knowledge.utils import parse_minio_url
    from yuxi.literature.access import build_access, current_access

    async with pg_manager.get_async_session_context() as db:
        revision = (
            await db.execute(
                select(KnowledgeParseRevision).where(
                    KnowledgeParseRevision.revision_id == paper.source_revision_id,
                    KnowledgeParseRevision.tenant_id == tenant_id,
                    KnowledgeParseRevision.status.in_(
                        ("INDEXED_FULL", "INDEXED_CONTENT_ONLY", "INDEXED_TEXT_ONLY", "ACTIVE")
                    ),
                )
            )
        ).scalar_one_or_none()
        if revision is None:
            return None
        artifact = (
            await db.execute(
                select(KnowledgeParseArtifact).where(
                    KnowledgeParseArtifact.revision_id == revision.revision_id,
                    KnowledgeParseArtifact.kind == "indexed_markdown",
                )
            )
        ).scalar_one_or_none()
        if artifact is None:
            return None
        frozen_input = (paper.id, paper.source_revision_id, paper.reading_version)
        revision_state = (revision.id, revision.status)
    bucket, name = parse_minio_url(artifact.object_uri)
    content = await get_minio_client().adownload_file(bucket, name)
    if hashlib.sha256(content).hexdigest() != artifact.sha256:
        raise ValueError("科学解析正文完整性校验失败")
    scope = current_access()
    if scope is not None and paper.rid not in (await build_access(scope.user, tenant_id)).rids:
        raise PermissionError("读取期间来源权限已撤销")
    async with pg_manager.get_async_session_context() as db:
        now = (
            await db.execute(
                select(LiteraturePaper)
                .where(paper_visibility(tenant_id), LiteraturePaper.id == frozen_input[0])
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if now is None or (now.id, now.source_revision_id, now.reading_version) != frozen_input:
            raise ValueError("读取期间来源版本已变更")
        if (now.artifact_pointers or {}).get("document"):
            raise ValueError("读取期间人工正文已生效，请重新加载")
        current_revision = await db.get(KnowledgeParseRevision, revision_state[0], populate_existing=True)
        if current_revision is None or current_revision.status != revision_state[1]:
            raise ValueError("读取期间解析状态已变更")
    markdown = content.decode("utf-8")
    from yuxi.knowledge.utils.text_utils import validate_markdown_quality

    validate_markdown_quality(markdown)
    return {
        "rid": paper.rid,
        "ready": True,
        "resolved": False,
        "frozen": False,
        "title": paper.title or paper.rid,
        "markdown": markdown,
        "source": "scientific_parse",
        "revision_id": revision.revision_id,
        "knowledge_base_id": revision.kb_id,
        "markdown_uri": None,
        "image_count": len(re.findall(r"!\[[^\]]*\]\(", markdown)),
        "notice": "当前显示科学 PDF 解析正文；专用阅读渲染/人工复核尚未完成，未改变证据资格。",
    }


async def get_templated_variant(tenant_id: int, rid: str, variant_kind: str, template: str) -> dict[str, Any] | None:
    """按模板重排既有译文——**纯渲染，零 LLM 调用**。

    对标上游 ``rerender_paper``：上游重渲染会重跑翻译链（再次计费），Yuxi 只读已落盘
    的段落对齐 JSON 重新排版，因此切换模板不产生 token 消耗与新副作用。

    与 ``get_variant_content`` 同口径做租户/来源校验；产物是派生物，只进
    NAVIGATION_ONLY 阅读通道（ADR-0010 决议 2/3）。
    """
    from yuxi.literature.templates import rerender_from_alignment, resolve_template

    if variant_kind != "en_zh":
        # 只有双语变体存在原文/译文对齐结果；其他变体无重渲染语义。
        return None
    spec = resolve_template(template)
    async with pg_manager.get_async_session_context() as session:
        paper = (
            await session.execute(
                select(LiteraturePaper).where(paper_visibility(tenant_id), LiteraturePaper.rid == rid)
            )
        ).scalar_one_or_none()
        if paper is None:
            return None
        variant = (
            (
                await session.execute(
                    select(LiteratureVariant)
                    .where(
                        LiteratureVariant.paper_id == paper.id,
                        LiteratureVariant.variant_kind == variant_kind,
                        LiteratureVariant.parse_revision_id == (paper.source_revision_id or ""),
                        LiteratureVariant.reading_version == paper.reading_version,
                        LiteratureVariant.generator_version.in_(("literature-translate-v1", "paperagent-import-v4")),
                    )
                    .order_by(LiteratureVariant.created_at.desc(), LiteratureVariant.id.desc())
                )
            )
            .scalars()
            .first()
        )
        if variant is None:
            return {"rid": rid, "variant_kind": variant_kind, "ready": False, "reason": "译文尚未生成"}
        title = paper.title or ""
    object_name = variant.uri.removeprefix("kbasset://").removesuffix(".md")
    try:
        raw = await get_minio_client().adownload_file(RENDER_BUCKET, f"{object_name}.json")
    except Exception:  # noqa: BLE001 - 无对齐结果时不能假装能重渲染
        return {
            "rid": rid,
            "variant_kind": variant_kind,
            "ready": False,
            "reason": "缺少段落对齐结果，无法零成本重渲染（需重新翻译）",
        }
    try:
        alignment = json.loads(raw.decode("utf-8")).get("paragraphs") or []
    except (ValueError, AttributeError):
        return {
            "rid": rid,
            "variant_kind": variant_kind,
            "ready": False,
            "reason": "段落对齐结果损坏，无法重渲染",
        }
    return {
        "rid": rid,
        "variant_kind": variant_kind,
        "lang": variant.lang,
        "ready": True,
        "template": spec.key,
        "template_label": spec.label,
        "content": rerender_from_alignment(alignment, template=spec.key, title=title),
        "rerendered": True,
        "llm_invoked": False,
    }


async def get_variant_content(tenant_id: int, rid: str, variant_kind: str) -> dict[str, Any] | None:
    """译文内容（zh/en_zh/summary；直接文本返回）。"""
    async with pg_manager.get_async_session_context() as session:
        paper = (
            await session.execute(
                select(LiteraturePaper).where(paper_visibility(tenant_id), LiteraturePaper.rid == rid)
            )
        ).scalar_one_or_none()
        if paper is None:
            return None
        variant = (
            (
                await session.execute(
                    select(LiteratureVariant)
                    .where(
                        LiteratureVariant.paper_id == paper.id,
                        LiteratureVariant.variant_kind == variant_kind,
                        LiteratureVariant.parse_revision_id == (paper.source_revision_id or ""),
                        LiteratureVariant.reading_version == paper.reading_version,
                        LiteratureVariant.generator_version.in_(("literature-translate-v1", "paperagent-import-v4")),
                    )
                    .order_by(LiteratureVariant.created_at.desc(), LiteratureVariant.id.desc())
                )
            )
            .scalars()
            .first()
        )
    if variant is None:
        return {"rid": rid, "variant_kind": variant_kind, "ready": False, "reason": "译文尚未生成"}
    async with pg_manager.get_async_session_context() as session:
        from yuxi.literature.products import frozen_uri

        uri = await frozen_uri(session, paper, f"variant_{variant_kind}", fallback=variant.uri)
    content = (await get_minio_client().adownload_file(RENDER_BUCKET, uri.removeprefix("kbasset://"))).decode("utf-8")
    return {
        "rid": rid,
        "variant_kind": variant_kind,
        "lang": variant.lang,
        "ready": True,
        "content": content,
        "uri": uri,
        "frozen": bool((paper.artifact_pointers or {}).get(f"variant_{variant_kind}")),
    }


async def get_parallel_content(tenant_id: int, rid: str) -> dict[str, Any] | None:
    """Return paragraph-identity aligned source/translation rows.

    New translations persist a JSON sidecar with stable ``Pxxx`` identities.
    Historical imports remain readable through a deterministic positional
    fallback, explicitly marking missing translations instead of collapsing
    empty paragraphs.
    """
    async with pg_manager.get_async_session_context() as session:
        paper = (
            await session.execute(
                select(LiteraturePaper).where(paper_visibility(tenant_id), LiteraturePaper.rid == rid)
            )
        ).scalar_one_or_none()
        if paper is None:
            return None
        variants = list(
            (
                await session.execute(
                    select(LiteratureVariant)
                    .where(
                        LiteratureVariant.paper_id == paper.id,
                        LiteratureVariant.variant_kind.in_(("zh", "en_zh")),
                        LiteratureVariant.parse_revision_id == (paper.source_revision_id or ""),
                        LiteratureVariant.reading_version == paper.reading_version,
                        LiteratureVariant.generator_version.in_(("literature-translate-v1", "paperagent-import-v4")),
                    )
                    .order_by(LiteratureVariant.created_at.desc(), LiteratureVariant.id.desc())
                )
            ).scalars()
        )
        # Prefer the paragraph-preserving Chinese translation.  Historical
        # en_zh imports are a readable fallback but are not guaranteed to have
        # one translated paragraph per source paragraph.
        variant = next((item for item in variants if item.variant_kind == "zh"), None)
        variant = variant or (variants[0] if variants else None)
    if variant is None:
        return {"rid": rid, "ready": False, "reason": "译文尚未生成", "paragraphs": []}

    reader = await get_reader_bundle(tenant_id, rid)
    translated = await get_variant_content(tenant_id, rid, variant.variant_kind)
    if not reader or not reader.get("ready") or not translated or not translated.get("ready"):
        return {"rid": rid, "ready": False, "reason": "原文或译文不可读", "paragraphs": []}

    minio = get_minio_client()
    sidecar_name = re.sub(r"\.md$", ".json", variant.uri.removeprefix("kbasset://"))
    # An automatic sidecar must never override a manually frozen translation.
    if not translated.get("frozen"):
        try:
            sidecar = json.loads((await minio.adownload_file(RENDER_BUCKET, sidecar_name)).decode("utf-8"))
            rows = [
                {
                    "para_id": str(row.get("para_id") or ""),
                    "original": rewrite_image_refs(str(row.get("original") or ""), reader["image_base"])
                    if reader.get("image_base")
                    else str(row.get("original") or ""),
                    "translated": rewrite_image_refs(str(row.get("translated") or ""), reader["image_base"])
                    if reader.get("image_base")
                    else str(row.get("translated") or ""),
                }
                for row in (sidecar.get("paragraphs") or [])
                if isinstance(row, dict)
            ]
            if rows:
                return {"rid": rid, "ready": True, "alignment": "identity", "paragraphs": rows}
        except Exception:  # noqa: BLE001 - pre-sidecar variants use the fallback below
            pass

    def _paragraphs(value: str) -> list[str]:
        return [part.strip() for part in re.split(r"\n\s*\n", value or "") if part.strip()]

    originals = _paragraphs(str(reader.get("markdown") or ""))
    translations = _paragraphs(str(translated.get("content") or ""))
    total = max(len(originals), len(translations))
    rows = [
        {
            "para_id": f"legacy-{index + 1:03d}",
            "original": originals[index] if index < len(originals) else "",
            "translated": translations[index] if index < len(translations) else "",
        }
        for index in range(total)
    ]
    return {
        "rid": rid,
        "ready": True,
        "alignment": "manual-positional" if translated.get("frozen") else "legacy-positional",
        "paragraphs": rows,
    }


async def get_source_pdf(tenant_id: int, rid: str) -> tuple[bytes, str] | None:
    """源 PDF（原始上传件）：revision.source_sha256 → MinIO 对象。返回 (bytes, object_name)。"""
    async with pg_manager.get_async_session_context() as session:
        paper = (
            await session.execute(
                select(LiteraturePaper).where(paper_visibility(tenant_id), LiteraturePaper.rid == rid)
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
