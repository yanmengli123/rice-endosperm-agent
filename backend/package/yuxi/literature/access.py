"""Request/Agent access scope inherited from authoritative knowledge sources.

Background compilation has no user scope. HTTP and Agent entry points must open
this scope before invoking domain services; filtering happens before pagination
and aggregation, and object downloads resolve their owning resource.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from sqlalchemy import String, and_, any_, literal, or_, select
from sqlalchemy.dialects.postgresql import ARRAY

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
    # Internal version authority also covers bibliography without a PDF source.
    reading_versions: dict[str, int] = field(default_factory=dict)


_access: ContextVar[LiteratureAccess | None] = ContextVar("literature_access", default=None)


def paper_visibility(tenant_id: int):
    scope = _access.get()
    condition = and_(LiteraturePaper.tenant_id == tenant_id, LiteraturePaper.trashed_at.is_(None))
    if scope is None:  # trusted background use, never an unauthenticated HTTP entry
        return condition
    rids = list(scope.rids if scope.tenant_id == tenant_id else ())
    # Large export corpora exceed asyncpg's 32767 expanded-bind limit. One typed
    # array retains exactly the same ACL predicate without loosening visibility.
    engine = pg_manager.async_engine
    if engine is not None and engine.dialect.name == "postgresql":
        return and_(condition, LiteraturePaper.rid == any_(literal(rids, type_=ARRAY(String(160)))))
    return and_(condition, LiteraturePaper.rid.in_(rids))


def current_access() -> LiteratureAccess | None:
    return _access.get()


async def build_access(user, tenant_id: int, *, include_trashed: bool = False) -> LiteratureAccess:
    from yuxi.knowledge.manager import KnowledgeBaseManager

    principal = {
        "uid": user.uid,
        "role": getattr(user, "role", "user"),
        "department_id": getattr(user, "department_id", None),
        "tenant_id": tenant_id,
    }
    async with pg_manager.get_async_session_context() as session:
        # Check each KB once, then filter sources in SQL. Never cache an ACL
        # verdict across requests: revocation must apply to the next read.
        databases = (
            await session.execute(
                select(KnowledgeBase.kb_id, KnowledgeBase.created_by, KnowledgeBase.share_config)
                .where(KnowledgeBase.tenant_id == tenant_id)
                .where(KnowledgeBase.governance_status.notin_(("ARCHIVED", "DELETED")))
            )
        ).all()
        allowed_kbs = [
            kb_id
            for kb_id, created_by, share_config in databases
            if KnowledgeBaseManager._database_info_accessible(
                principal, {"tenant_id": tenant_id, "created_by": created_by, "share_config": share_config}
            )
        ]
        rows = (
            await session.execute(
                select(
                    LiteraturePaper.id,
                    LiteraturePaper.rid,
                    LiteraturePaper.reading_version,
                    KnowledgeParseRevision.revision_id,
                    KnowledgeParseRevision.source_sha256,
                    KnowledgeParseRevision.kb_id,
                    KnowledgeParseRevision.file_id,
                    KnowledgeBase.name,
                    KnowledgeBase.contract_key,
                    KnowledgeParseRevision.status,
                    KnowledgeParseRevision.capabilities,
                    KnowledgeParseRevision.parser_fingerprint,
                    KnowledgeParseRevision.pipeline_version,
                )
                .outerjoin(
                    KnowledgeParseRevision, KnowledgeParseRevision.revision_id == LiteraturePaper.source_revision_id
                )
                .outerjoin(KnowledgeBase, KnowledgeBase.kb_id == KnowledgeParseRevision.kb_id)
                .outerjoin(KnowledgeFile, KnowledgeFile.file_id == KnowledgeParseRevision.file_id)
                .where(LiteraturePaper.tenant_id == tenant_id)
                .where(True if include_trashed else LiteraturePaper.trashed_at.is_(None))
                .where(
                    or_(
                        LiteraturePaper.source_revision_id.is_(None),
                        and_(
                            KnowledgeParseRevision.tenant_id == tenant_id,
                            KnowledgeBase.kb_id.in_(allowed_kbs),
                            KnowledgeFile.kb_id == KnowledgeBase.kb_id,
                            KnowledgeFile.active_parse_revision_id == KnowledgeParseRevision.revision_id,
                        ),
                    )
                )
            )
        ).all()
    revisions = {}
    sources = {}
    for row in rows:
        if row.revision_id is not None:
            revisions[row.revision_id] = row.source_sha256
            sources[row.rid] = {
                "kb_id": row.kb_id,
                "file_id": row.file_id,
                "name": row.name,
                "contract_key": row.contract_key,
                "parse_revision_id": row.revision_id,
                "reading_version": row.reading_version,
                "evidence_status": row.status,
                "capabilities": row.capabilities or {},
                "parser_fingerprint": row.parser_fingerprint,
                "pipeline_version": row.pipeline_version,
                "imported": row.pipeline_version == "paperagent_import_v4",
            }
    return LiteratureAccess(
        tenant_id,
        frozenset(row.rid for row in rows),
        frozenset(row.id for row in rows),
        revisions,
        sources,
        user,
        {row.rid: row.reading_version for row in rows},
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
    if report.report_kind == "historical":
        return report.tenant_id == scope.tenant_id and report.created_by == scope.user.uid
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
    if namespace in {"notes", "variants", "artifacts"}:
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
