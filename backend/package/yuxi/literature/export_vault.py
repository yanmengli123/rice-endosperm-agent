"""Obsidian 兼容导出（P11；方案 §十：导出包实现，不暴露服务器目录树）。

导出 zip 内容：
- ``<rid>/`` 目录（每篇文献一个）
  - ``L1_note.md`` / ``L2_wiki.md`` / ``L3_relations.md``（已有产物直接复制）
  - ``render.md``（阅读视图）
  - ``zh.md``（译文变体）
  - ``images/``（阅读视图图片，相对路径引用）
- ``_index.md``（导出清单：rid/标题/DOI/编译状态/包含文件）
- ``_manifest.json``（来源版本、文件清单、缺失产物提示）

这不是整机备份：当前不包含卡片、报告、日记、会话历史和支撑附件。

双链：L3 笔记中的 ``[[<dirname>/_note]]`` 已是 Obsidian 兼容格式；
导出时统一改写为 ``[[<dirname>/L1_note]]``（Yuxi 文件名对齐）。
"""

from __future__ import annotations

import io
import hashlib
import json
import re
import zipfile
from urllib.parse import parse_qs, urlparse

from sqlalchemy import func, select

from yuxi.literature.access import paper_visibility
from yuxi.literature.provenance import current_job, TRANSLATE_VERSION
from yuxi.literature.reading.service import RENDER_BUCKET
from yuxi.storage.minio import get_minio_client
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    LiteratureCompileJob,
    LiteraturePaper,
    LiteratureVariant,
    LiteratureArtifactRevision,
)

#: 导出文件名映射（Yuxi → Obsidian 惯例）
_EXPORT_FILES = {
    "L1_note.md": "L1_note.md",
    "L2_wiki.md": "L2_wiki.md",
    "L3_relations.md": "L3_relations.md",
}

_WIKILINK_RE = re.compile(r"\[\[([^\]/]+?)/_note\]\]")
MAX_EXPORT_PAPERS = 1_000
MAX_EXPORT_BYTES = 128 * 1024 * 1024
MAX_EXPORT_ENTRIES = 10_000


class ExportLimitError(ValueError):
    """The archive cannot safely be delivered as a complete snapshot."""


def rewrite_wikilinks(content: str) -> str:
    """``[[dir/_note]]`` → ``[[dir/L1_note]]``（Yuxi 文件名对齐）。"""
    return _WIKILINK_RE.sub(r"[[\1/L1_note]]", content)


def _paper_snapshot(paper):
    return (
        paper.id,
        paper.source_revision_id,
        paper.reading_version,
        paper.card_revision,
        json.dumps(paper.artifact_pointers or {}, sort_keys=True),
        paper.title,
        paper.doi,
        paper.kind,
        paper.journal,
        paper.pub_year,
    )


async def export_obsidian_vault(tenant_id: int, *, rids: list[str] | None = None) -> bytes:
    """生成 Obsidian 兼容导出 zip。返回字节流（路由层写响应）。"""
    minio = get_minio_client()
    buf = io.BytesIO()
    zf = zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED)
    written = 0
    names = set()

    def write_member(name, content):
        nonlocal written
        content = content.encode("utf-8") if isinstance(content, str) else content
        if name in names:
            raise ExportLimitError("导出文件名冲突，未交付不完整归档")
        if written + len(content) > MAX_EXPORT_BYTES or len(names) >= MAX_EXPORT_ENTRIES:
            raise ExportLimitError("导出超过单批安全预算，请缩小文献范围")
        written += len(content)
        names.add(name)
        zf.writestr(name, content)

    async with pg_manager.get_async_session_context() as session:
        conditions = [paper_visibility(tenant_id)]
        if rids:
            conditions.append(LiteraturePaper.rid.in_(rids))
        papers = (
            (await session.execute(select(LiteraturePaper).where(*conditions).limit(MAX_EXPORT_PAPERS + 1)))
            .scalars()
            .all()
        )
        if len(papers) > MAX_EXPORT_PAPERS:
            raise ExportLimitError("导出文献数超过单批上限，请缩小文献范围")
        paper_ids = {p.id for p in papers}
        frozen_papers = {_paper_snapshot(p) for p in papers}

        compile_jobs = (
            (
                await session.execute(
                    select(LiteratureCompileJob)
                    .join(LiteraturePaper, LiteraturePaper.id == LiteratureCompileJob.paper_id)
                    .where(
                        LiteratureCompileJob.tenant_id == tenant_id,
                        LiteratureCompileJob.paper_id.in_(paper_ids),
                        LiteratureCompileJob.status == "SUCCEEDED",
                        LiteratureCompileJob.note_uri.isnot(None),
                        current_job(LiteratureCompileJob),
                    )
                    .order_by(LiteratureCompileJob.updated_at, LiteratureCompileJob.id)
                )
            )
            .scalars()
            .all()
        )
        note_uris: dict[int, dict[str, str]] = {}
        frozen_notes = {job.id: job.note_uri for job in compile_jobs}
        for job in compile_jobs:
            level = job.level if job.level in ("L1", "L2", "L3") else "L1"
            filename = {"L1": "L1_note.md", "L2": "L2_wiki.md", "L3": "L3_relations.md"}[level]
            note_uris.setdefault(job.paper_id, {})[filename] = job.note_uri

        variants = (
            (
                await session.execute(
                    select(LiteratureVariant)
                    .join(LiteraturePaper, LiteraturePaper.id == LiteratureVariant.paper_id)
                    .where(
                        LiteratureVariant.tenant_id == tenant_id,
                        LiteratureVariant.paper_id.in_(paper_ids),
                        LiteratureVariant.parse_revision_id == func.coalesce(LiteraturePaper.source_revision_id, ""),
                        LiteratureVariant.reading_version == LiteraturePaper.reading_version,
                        LiteratureVariant.generator_version.in_((TRANSLATE_VERSION, "paperagent-import-v4")),
                    )
                    .order_by(LiteratureVariant.created_at, LiteratureVariant.id)
                )
            )
            .scalars()
            .all()
        )
        variant_uris: dict[int, dict[str, str]] = {}
        frozen_variants = {variant.id: variant.uri for variant in variants}
        for v in variants:
            variant_uris.setdefault(v.paper_id, {})[v.variant_kind] = v.uri

        # A newer automatic candidate must never replace the online frozen
        # product in an export. Resolve all pointers in one bounded SQL query.
        pointer_ids = {
            revision
            for paper in papers
            for kind, revision in (paper.artifact_pointers or {}).items()
            if kind.startswith(("note_", "variant_"))
        }
        revisions = {
            row.revision_id: row
            for row in (
                await session.execute(
                    select(LiteratureArtifactRevision).where(
                        LiteratureArtifactRevision.tenant_id == tenant_id,
                        LiteratureArtifactRevision.paper_id.in_(paper_ids),
                        LiteratureArtifactRevision.revision_id.in_(pointer_ids),
                    )
                )
            ).scalars()
        }
        expected_digests = {}
        for paper in papers:
            for kind, pointer in (paper.artifact_pointers or {}).items():
                if not kind.startswith(("note_", "variant_")):
                    continue
                revision = revisions.get(pointer)
                if revision is None or (
                    revision.paper_id,
                    revision.kind,
                    revision.source_revision_id,
                    revision.input_reading_version,
                    revision.input_card_revision,
                ) != (paper.id, kind, paper.source_revision_id, paper.reading_version, paper.card_revision):
                    raise ValueError("人工产物指针损坏或来源已变更，请确认重存后导出")
                expected_digests[revision.object_uri] = revision.content_digest
                if kind in {"note_L1", "note_L2", "note_L3"}:
                    filename = {"note_L1": "L1_note.md", "note_L2": "L2_wiki.md", "note_L3": "L3_relations.md"}[kind]
                    note_uris.setdefault(paper.id, {})[filename] = revision.object_uri
                elif kind in {"variant_zh", "variant_en_zh", "variant_summary"}:
                    variant_uris.setdefault(paper.id, {})[kind[8:]] = revision.object_uri

    index_entries: list[str] = ["# 文献库导出清单\n"]
    export_manifest = {"schema_version": 1, "product_type": "literature_navigation", "papers": [], "warnings": []}

    for paper in papers:
        dirname = re.sub(r"[^\w.-]", "_", paper.rid)
        if dirname.startswith("."):
            dirname = f"paper_{dirname}"
        notes = note_uris.get(paper.id, {})
        variants = variant_uris.get(paper.id, {})
        files_included: list[str] = []

        # Read the same effective version as the online reader. Protected URLs
        # are copied into the vault, never left as localhost/auth-only links.
        from yuxi.literature.reader_bundle import get_reader_bundle, get_source_pdf

        reader = (
            await get_reader_bundle(tenant_id, paper.rid)
            if paper.source_revision_id or paper.artifact_pointers
            else None
        )
        if reader and reader.get("ready"):
            markdown = reader["markdown"]
            image_members = {}
            for match in list(re.finditer(r"!\[[^\]]*\]\((/api/literature/assets\?[^)]+)\)", markdown)):
                url = match.group(1)
                uri = (parse_qs(urlparse(url).query).get("uri") or [""])[0]
                from yuxi.literature.access import authorize_asset
                from yuxi.literature.readops import validate_literature_asset_uri

                object_name = validate_literature_asset_uri(uri, tenant_id)
                # Human Markdown can contain arbitrary proxy links. Validate
                # ownership before object IO, just like the online asset route.
                if not await authorize_asset(object_name):
                    raise ValueError("导出图片不存在或无权访问")
                if uri in image_members:
                    markdown = markdown.replace(url, image_members[uri])
                    continue
                # Include object identity so equally named images in distinct
                # directories cannot silently replace one another in the ZIP.
                image_name = re.sub(r"[^A-Za-z0-9_.-]", "_", object_name.rsplit("/", 1)[-1])
                if not image_name or image_name in {".", ".."}:
                    raise ValueError("阅读图片文件名非法")
                data = await minio.adownload_file(RENDER_BUCKET, object_name)
                relative = f"images/{hashlib.sha256(object_name.encode()).hexdigest()[:16]}-{image_name}"
                if relative not in files_included:
                    write_member(f"{dirname}/{relative}", data)
                    files_included.append(relative)
                markdown = markdown.replace(url, relative)
                image_members[uri] = relative
            write_member(f"{dirname}/render.md", markdown)
            files_included.append("render.md")
        pdf = await get_source_pdf(tenant_id, paper.rid) if paper.source_revision_id else None
        if pdf:
            write_member(f"{dirname}/source.pdf", pdf[0])
            files_included.append("source.pdf")

        # 编译笔记
        for filename, uri in notes.items():
            try:
                content = await minio.adownload_file(RENDER_BUCKET, uri.removeprefix("kbasset://"))
                if uri in expected_digests and hashlib.sha256(content).hexdigest() != expected_digests[uri]:
                    raise ExportLimitError("人工笔记摘要不一致，未交付损坏归档")
                text = rewrite_wikilinks(content.decode("utf-8"))
                write_member(f"{dirname}/{filename}", text)
                files_included.append(filename)
            except ExportLimitError:
                raise
            except Exception as exc:  # noqa: BLE001
                if uri in expected_digests:
                    raise ExportLimitError("人工笔记不可读，未交付不完整归档") from exc
                export_manifest["warnings"].append(
                    {"rid": paper.rid, "file": filename, "reason": "artifact_unavailable"}
                )
                continue

        # 译文变体
        for kind, uri in variants.items():
            try:
                content = await minio.adownload_file(RENDER_BUCKET, uri.removeprefix("kbasset://"))
                if uri in expected_digests and hashlib.sha256(content).hexdigest() != expected_digests[uri]:
                    raise ExportLimitError("人工译文摘要不一致，未交付损坏归档")
                write_member(f"{dirname}/{kind}.md", content)
                files_included.append(f"{kind}.md")
            except ExportLimitError:
                raise
            except Exception as exc:  # noqa: BLE001
                if uri in expected_digests:
                    raise ExportLimitError("人工译文不可读，未交付不完整归档") from exc
                export_manifest["warnings"].append(
                    {"rid": paper.rid, "file": f"{kind}.md", "reason": "artifact_unavailable"}
                )
                continue

        # 元数据 frontmatter 文件（Obsidian 属性面板）
        frontmatter = {
            "rid": paper.rid,
            "doi": paper.doi or "",
            "title": paper.title or "",
            "kind": paper.kind,
            "journal": paper.journal or "",
            "pub_year": paper.pub_year,
            "tags": ["literature", paper.kind],
        }
        write_member(f"{dirname}/_metadata.json", json.dumps(frontmatter, ensure_ascii=False, indent=2))
        files_included.append("_metadata.json")
        frontmatter_text = (
            "---\n"
            + "\n".join(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in frontmatter.items())
            + "\n---\n"
        )
        links = "\n".join(
            f"- [[{filename.removesuffix('.md')}]]" for filename in files_included if filename.endswith(".md")
        )
        write_member(f"{dirname}/index.md", frontmatter_text + f"\n# {paper.title or paper.rid}\n\n{links}\n")
        files_included.append("index.md")
        export_manifest["papers"].append(
            {
                "rid": paper.rid,
                "source_revision_id": paper.source_revision_id,
                "reading_version": paper.reading_version,
                "files": files_included,
            }
        )

        index_entries.append(
            f"- **{paper.title or paper.rid}**（{paper.rid}）— {', '.join(files_included) or '无产物'}"
        )

    # Revalidate before delivering bytes: a long download must not finish with
    # sources revoked or an obsolete reading/card generation labelled current.
    from yuxi.literature.access import build_access, current_access

    scope = current_access()
    if scope is not None:
        fresh = await build_access(scope.user, tenant_id)
        if not {p.rid for p in papers} <= fresh.rids:
            raise ValueError("导出期间来源权限已撤销，未交付归档")
    async with pg_manager.get_async_session_context() as session:
        current = list(
            (
                await session.execute(
                    select(LiteraturePaper)
                    .where(paper_visibility(tenant_id), LiteraturePaper.id.in_(paper_ids))
                    .execution_options(populate_existing=True)
                )
            ).scalars()
        )
        if {_paper_snapshot(p) for p in current} != frozen_papers:
            raise ValueError("导出期间文献版本已变更，请重新导出")
        notes_now = dict(
            (
                await session.execute(
                    select(LiteratureCompileJob.id, LiteratureCompileJob.note_uri).where(
                        LiteratureCompileJob.id.in_(frozen_notes), LiteratureCompileJob.status == "SUCCEEDED"
                    )
                )
            ).all()
        )
        variants_now = dict(
            (
                await session.execute(
                    select(LiteratureVariant.id, LiteratureVariant.uri).where(LiteratureVariant.id.in_(frozen_variants))
                )
            ).all()
        )
        if notes_now != frozen_notes or variants_now != frozen_variants:
            raise ValueError("导出期间产物已重编译或重译，请重新导出")
    write_member("_index.md", "\n".join(index_entries) + "\n")
    write_member("_manifest.json", json.dumps(export_manifest, ensure_ascii=False, indent=2))
    zf.close()
    return buf.getvalue()
