"""Owner-scoped parse-only reading, deliberately outside LiteraturePaper/evidence."""

from __future__ import annotations

import hashlib
from sqlalchemy import select

from yuxi.knowledge.manager import KnowledgeBaseManager
from yuxi.knowledge.utils import parse_minio_url
from yuxi.literature.access import current_access
from yuxi.storage.minio import get_minio_client
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeBase,
    KnowledgeFile,
    KnowledgeParseRevision,
    KnowledgeParseArtifact,
)


async def list_drafts(tenant_id: int, *, revision_id: str = "") -> list[dict]:
    scope = current_access()
    if scope is None or scope.tenant_id != tenant_id:
        raise PermissionError("草稿需要已认证的用户范围")
    async with pg_manager.get_async_session_context() as db:
        query = (
            select(KnowledgeParseRevision, KnowledgeFile, KnowledgeBase)
            .join(KnowledgeFile, KnowledgeFile.file_id == KnowledgeParseRevision.file_id)
            .join(KnowledgeBase, KnowledgeBase.kb_id == KnowledgeParseRevision.kb_id)
            .where(
                KnowledgeParseRevision.tenant_id == tenant_id,
                KnowledgeParseRevision.created_by == scope.user.uid,
                KnowledgeParseRevision.processing_mode == "parse_only",
                KnowledgeFile.kb_id == KnowledgeBase.kb_id,
                KnowledgeBase.tenant_id == tenant_id,
                KnowledgeBase.governance_status.not_in(("ARCHIVED", "DELETED")),
            )
            .order_by(KnowledgeParseRevision.created_at.desc(), KnowledgeParseRevision.id.desc())
        )
        if revision_id:
            query = query.where(KnowledgeParseRevision.revision_id == revision_id)
        rows = (await db.execute(query.limit(100))).all()
    principal = {
        "uid": scope.user.uid,
        "tenant_id": tenant_id,
        "role": getattr(scope.user, "role", "user"),
        "department_id": getattr(scope.user, "department_id", None),
    }
    return [
        {
            "revision_id": r.revision_id,
            "kb_id": r.kb_id,
            "file_id": r.file_id,
            "title": (r.article_summary or {}).get("title") or f.original_filename or f.filename,
            "status": r.status,
            "ready": r.status == "PARSED_ONLY",
            "evidence": False,
        }
        for r, f, kb in rows
        if KnowledgeBaseManager._database_info_accessible(
            principal, {"tenant_id": kb.tenant_id, "created_by": kb.created_by, "share_config": kb.share_config}
        )
    ]


async def read_draft(tenant_id: int, revision_id: str, *, pdf: bool = False):
    rows = await list_drafts(tenant_id, revision_id=revision_id)
    if not rows or not rows[0]["ready"]:
        raise ValueError("草稿不存在、尚未完成或无权访问")
    async with pg_manager.get_async_session_context() as db:
        artifact = (
            await db.execute(
                select(KnowledgeParseArtifact).where(
                    KnowledgeParseArtifact.revision_id == revision_id,
                    KnowledgeParseArtifact.kind == ("original_pdf" if pdf else "indexed_markdown"),
                )
            )
        ).scalar_one_or_none()
    if artifact is None:
        raise ValueError("草稿产物不存在")
    bucket, name = parse_minio_url(artifact.object_uri)
    data = await get_minio_client().adownload_file(bucket, name)
    if hashlib.sha256(data).hexdigest() != artifact.sha256:
        raise ValueError("草稿产物校验失败")
    if not await list_drafts(tenant_id, revision_id=revision_id):
        raise PermissionError("草稿权限已撤销")
    return data if pdf else {**rows[0], "markdown": data.decode("utf-8")}
