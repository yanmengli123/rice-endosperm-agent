"""Request/Agent access scope inherited from authoritative knowledge sources.

Background compilation has no user scope. HTTP and Agent entry points must open
this scope before invoking domain services; filtering happens before pagination
and aggregation, and object downloads resolve their owning resource.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from sqlalchemy import and_, select

from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeBase,
    KnowledgeFile,
    KnowledgeParseRevision,
    LiteraturePaper,
    LiteratureReport,
)


@dataclass(frozen=True)
class LiteratureAccess:
    tenant_id: int
    rids: frozenset[str]
    paper_ids: frozenset[int]
    revisions: dict[str, str]
    sources: dict[str, dict]
    user: object


_access: ContextVar[LiteratureAccess | None] = ContextVar("literature_access", default=None)


def paper_visibility(tenant_id: int):
    scope = _access.get()
    condition = LiteraturePaper.tenant_id == tenant_id
    if scope is None:  # trusted background use, never an unauthenticated HTTP entry
        return condition
    return and_(condition, LiteraturePaper.rid.in_(scope.rids if scope.tenant_id == tenant_id else ()))


def current_access() -> LiteratureAccess | None:
    return _access.get()


async def build_access(user, tenant_id: int) -> LiteratureAccess:
    from yuxi.knowledge.manager import KnowledgeBaseManager

    async with pg_manager.get_async_session_context() as session:
        rows = (
            await session.execute(
                select(LiteraturePaper, KnowledgeParseRevision, KnowledgeBase, KnowledgeFile)
                .outerjoin(
                    KnowledgeParseRevision, KnowledgeParseRevision.revision_id == LiteraturePaper.source_revision_id
                )
                .outerjoin(KnowledgeBase, KnowledgeBase.kb_id == KnowledgeParseRevision.kb_id)
                .outerjoin(KnowledgeFile, KnowledgeFile.file_id == KnowledgeParseRevision.file_id)
                .where(LiteraturePaper.tenant_id == tenant_id)
            )
        ).all()
    principal = {
        "uid": user.uid,
        "role": getattr(user, "role", "user"),
        "department_id": getattr(user, "department_id", None),
        "tenant_id": tenant_id,
    }
    visible = []
    revisions = {}
    sources = {}
    for paper, revision, kb, source_file in rows:
        # Independent Bib/Markdown records are shared bibliography within the
        # tenant. A source-linked record always inherits that source's ACL.
        allowed = paper.source_revision_id is None
        if revision is not None and kb is not None and source_file is not None:
            allowed = (
                revision.tenant_id == tenant_id
                and kb.tenant_id == tenant_id
                and (
                    source_file.kb_id == kb.kb_id
                    and source_file.active_parse_revision_id == revision.revision_id
                    and kb.governance_status not in {"ARCHIVED", "DELETED"}
                    and KnowledgeBaseManager._database_info_accessible(
                        principal,
                        {
                            "tenant_id": kb.tenant_id,
                            "created_by": kb.created_by,
                            "share_config": kb.share_config,
                        },
                    )
                )
            )
        if allowed:
            visible.append(paper)
            if revision is not None:
                revisions[revision.revision_id] = revision.source_sha256
                sources[paper.rid] = {
                    "kb_id": kb.kb_id,
                    "name": kb.name,
                    "contract_key": kb.contract_key,
                    "parse_revision_id": revision.revision_id,
                    "reading_version": paper.reading_version,
                    "evidence_status": revision.status,
                    "capabilities": revision.capabilities or {},
                    "parser_fingerprint": revision.parser_fingerprint,
                    "pipeline_version": revision.pipeline_version,
                    "imported": revision.pipeline_version == "paperagent_import_v4",
                }
    return LiteratureAccess(
        tenant_id, frozenset(p.rid for p in visible), frozenset(p.id for p in visible), revisions, sources, user
    )


@asynccontextmanager
async def access_scope(user, tenant_id: int):
    scope = await build_access(user, tenant_id)
    token = _access.set(scope)
    try:
        yield scope
    finally:
        _access.reset(token)


def report_visible(report: LiteratureReport) -> bool:
    scope = _access.get()
    if scope is None:
        return True
    sources = report.source_rids_json or []
    # Historical reports without a source manifest cannot prove an ACL closure.
    return bool(sources) and report.tenant_id == scope.tenant_id and set(sources) <= scope.rids


async def authorize_asset(object_name: str) -> bool:
    scope = _access.get()
    if scope is None:
        return False
    parts = object_name.split("/")
    if len(parts) < 5 or parts[:2] != ["tenants", str(scope.tenant_id)]:
        return False
    if parts[2] == "documents":
        return len(parts) >= 7 and parts[4] == "reader_render" and scope.revisions.get(parts[5]) == parts[3]
    if parts[2] != "literature":
        return False
    namespace, owner = parts[3:5]
    if namespace in {"notes", "variants"}:
        return owner in scope.rids or f"doi-{owner}" in scope.rids
    if namespace == "attachments":
        return owner.isdigit() and int(owner) in scope.paper_ids
    if namespace == "reports":
        report_id = owner.removesuffix(".md")
        if not report_id.isdigit():
            return False
        async with pg_manager.get_async_session_context() as session:
            report = (
                await session.execute(
                    select(LiteratureReport).where(
                        LiteratureReport.id == int(report_id), LiteratureReport.tenant_id == scope.tenant_id
                    )
                )
            ).scalar_one_or_none()
        return report is not None and report_visible(report)
    return False
