"""文献身份登记（从编译服务提取的登记链，供知识源登记桥与清单导入复用）。

幂等语义：同租户同 RID 复用既有行；跨来源 ACL 的同名 DOI 不合并文档；
来源版本更新时使旧笔记发布失效并递增阅读版本。RID 语义经 P0 身份模块。
"""

from __future__ import annotations

import hashlib

from sqlalchemy import select

from yuxi.literature.contracts import ResourceKind
from yuxi.literature.identity.rid import make_rid
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeParseRevision,
    LiteraturePaper,
)


async def register_literature_paper(
    *,
    tenant_id: int,
    kind: ResourceKind = "paper",
    doi: str = "",
    isbn: str = "",
    cnki: str = "",
    arxiv: str = "",
    fingerprint: str = "",
    title: str = "",
    source_revision_id: str | None = None,
) -> LiteraturePaper:
    """登记/更新文献身份（幂等：同租户同 RID 复用既有行）。RID 语义经 P0 身份模块。"""
    rid = make_rid(kind, doi=doi, isbn=isbn, cnki=cnki, arxiv=arxiv, fingerprint=fingerprint)
    async with pg_manager.get_async_session_context() as session:
        row = None
        revision = None
        if source_revision_id:
            revision = (
                await session.execute(
                    select(KnowledgeParseRevision).where(
                        KnowledgeParseRevision.revision_id == source_revision_id,
                        KnowledgeParseRevision.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if revision is None:
                raise ValueError("解析来源不存在或跨租户")
            row = (
                await session.execute(
                    select(LiteraturePaper)
                    .join(
                        KnowledgeParseRevision, KnowledgeParseRevision.revision_id == LiteraturePaper.source_revision_id
                    )
                    .where(
                        LiteraturePaper.tenant_id == tenant_id,
                        KnowledgeParseRevision.file_id == revision.file_id,
                        KnowledgeParseRevision.kb_id == revision.kb_id,
                    )
                )
            ).scalar_one_or_none()
        if row is None:
            row = (
                await session.execute(
                    select(LiteraturePaper).where(LiteraturePaper.tenant_id == tenant_id, LiteraturePaper.rid == rid)
                )
            ).scalar_one_or_none()
        if row is not None and revision is not None and row.source_revision_id:
            prior = (
                await session.execute(
                    select(KnowledgeParseRevision).where(KnowledgeParseRevision.revision_id == row.source_revision_id)
                )
            ).scalar_one_or_none()
            if prior is None or prior.kb_id != revision.kb_id:
                # A shared DOI must not merge documents across source ACLs.
                rid += "__source-" + hashlib.sha256(revision.kb_id.encode()).hexdigest()[:12]
                row = (
                    await session.execute(
                        select(LiteraturePaper).where(
                            LiteraturePaper.tenant_id == tenant_id, LiteraturePaper.rid == rid
                        )
                    )
                ).scalar_one_or_none()
        if row is None:
            row = LiteraturePaper(
                tenant_id=tenant_id,
                rid=rid,
                kind=kind,
                doi=(doi or None),
                title=title or None,
                source_revision_id=source_revision_id,
            )
            session.add(row)
        else:
            if title and not row.title:
                row.title = title
            if doi and not row.doi:
                # 复用既有行时回填后补的 DOI（PDF 链先前未抽取）；RID 身份
                # 已定，不因补 DOI 而改写，避免既有引用/产物悬空。
                row.doi = doi
            if source_revision_id and row.source_revision_id != source_revision_id:
                from yuxi.literature.provenance import invalidate_note_publications

                if row.source_revision_id:
                    await invalidate_note_publications(session, row.source_revision_id)
                row.source_revision_id = source_revision_id
                row.reading_version = int(row.reading_version or 1) + 1
                row.ai_value_score = None
                row.topic_score = None
                row.value_score = 0.0
        await session.flush()
        await session.refresh(row)
        await session.commit()
        return row
