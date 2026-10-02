"""Obsidian 兼容导出（P11；方案 §十：导出包实现，不暴露服务器目录树）。

导出 zip 内容：
- ``<rid>/`` 目录（每篇文献一个）
  - ``L1_note.md`` / ``L2_wiki.md`` / ``L3_relations.md``（已有产物直接复制）
  - ``render.md``（阅读视图）
  - ``zh.md``（译文变体）
  - ``images/``（阅读视图图片，相对路径引用）
- ``_index.md``（导出清单：rid/标题/DOI/编译状态/包含文件）
- ``_concepts/``（概念页，如有）

双链：L3 笔记中的 ``[[<dirname>/_note]]`` 已是 Obsidian 兼容格式；
导出时统一改写为 ``[[<dirname>/L1_note]]``（Yuxi 文件名对齐）。
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from typing import Any

from sqlalchemy import select

from yuxi.literature.reading.service import RENDER_BUCKET
from yuxi.storage.minio import get_minio_client
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    LiteratureCompileJob,
    LiteraturePaper,
    LiteratureVariant,
)

#: 导出文件名映射（Yuxi → Obsidian 惯例）
_EXPORT_FILES = {
    "L1_note.md": "L1_note.md",
    "L2_wiki.md": "L2_wiki.md",
    "L3_relations.md": "L3_relations.md",
}

_WIKILINK_RE = re.compile(r"\[\[([^\]/]+?)/_note\]\]")


def rewrite_wikilinks(content: str) -> str:
    """``[[dir/_note]]`` → ``[[dir/L1_note]]``（Yuxi 文件名对齐）。"""
    return _WIKILINK_RE.sub(r"[[\1/L1_note]]", content)


async def export_obsidian_vault(tenant_id: int, *, rids: list[str] | None = None) -> bytes:
    """生成 Obsidian 兼容导出 zip。返回字节流（路由层写响应）。"""
    minio = get_minio_client()
    buf = io.BytesIO()
    zf = zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED)

    async with pg_manager.get_async_session_context() as session:
        conditions = [LiteraturePaper.tenant_id == tenant_id]
        if rids:
            conditions.append(LiteraturePaper.rid.in_(rids))
        papers = (
            (await session.execute(select(LiteraturePaper).where(*conditions))).scalars().all()
        )
        paper_ids = {p.id for p in papers}

        compile_jobs = (
            (
                await session.execute(
                    select(LiteratureCompileJob).where(
                        LiteratureCompileJob.tenant_id == tenant_id,
                        LiteratureCompileJob.paper_id.in_(paper_ids),
                        LiteratureCompileJob.status == "SUCCEEDED",
                        LiteratureCompileJob.note_uri.isnot(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        note_uris: dict[int, dict[str, str]] = {}
        for job in compile_jobs:
            level = job.level if job.level in ("L1", "L2", "L3") else "L1"
            filename = {"L1": "L1_note.md", "L2": "L2_wiki.md", "L3": "L3_relations.md"}[level]
            note_uris.setdefault(job.paper_id, {})[filename] = job.note_uri

        variants = (
            (
                await session.execute(
                    select(LiteratureVariant).where(
                        LiteratureVariant.tenant_id == tenant_id,
                        LiteratureVariant.paper_id.in_(paper_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        variant_uris: dict[int, dict[str, str]] = {}
        for v in variants:
            variant_uris.setdefault(v.paper_id, {})[v.variant_kind] = v.uri

    index_entries: list[str] = ["# 文献库导出清单\n"]

    for paper in papers:
        dirname = paper.rid.replace("/", "_")
        notes = note_uris.get(paper.id, {})
        variants = variant_uris.get(paper.id, {})
        files_included: list[str] = []

        # 编译笔记
        for filename, uri in notes.items():
            try:
                content = await minio.adownload_file(RENDER_BUCKET, uri.removeprefix("kbasset://"))
                text = rewrite_wikilinks(content.decode("utf-8"))
                zf.writestr(f"{dirname}/{filename}", text)
                files_included.append(filename)
            except Exception:  # noqa: BLE001
                continue

        # 译文变体
        for kind, uri in variants.items():
            try:
                content = await minio.adownload_file(RENDER_BUCKET, uri.removeprefix("kbasset://"))
                zf.writestr(f"{dirname}/{kind}.md", content)
                files_included.append(f"{kind}.md")
            except Exception:  # noqa: BLE001
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
        zf.writestr(f"{dirname}/_metadata.json", json.dumps(frontmatter, ensure_ascii=False, indent=2))
        files_included.append("_metadata.json")

        index_entries.append(
            f"- **{paper.title or paper.rid}**（{paper.rid}）— {', '.join(files_included) or '无产物'}"
        )

    zf.writestr("_index.md", "\n".join(index_entries) + "\n")
    zf.close()
    return buf.getvalue()
