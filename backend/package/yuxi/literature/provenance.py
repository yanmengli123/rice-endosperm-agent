"""Frozen input identity and current-output predicates for derived literature."""

from sqlalchemy import and_, func, select, update

from yuxi.storage.postgres.models_knowledge import LiteraturePaper

COMPILE_VERSION = "literature-compile-v1"
TRANSLATE_VERSION = "literature-translate-v1"


def current_job(job_model, paper=LiteraturePaper):
    version = COMPILE_VERSION if job_model.__tablename__ == "literature_compile_jobs" else TRANSLATE_VERSION
    return and_(
        job_model.card_revision == paper.card_revision if version == COMPILE_VERSION else True,
        job_model.source_revision_id == func.coalesce(paper.source_revision_id, ""),
        job_model.reading_version == paper.reading_version,
        job_model.generator_version.in_((version, "paperagent-import-v4")),
    )


def source_matches(job, paper) -> bool:
    return (
        getattr(paper, "trashed_at", None) is None
        and job.source_revision_id == (paper.source_revision_id or "")
        and job.reading_version == paper.reading_version
        and (not hasattr(job, "card_revision") or job.card_revision == paper.card_revision)
    )


async def require_current_input(session, job):
    paper = (
        await session.execute(select(LiteraturePaper).where(LiteraturePaper.id == job.paper_id).with_for_update())
    ).scalar_one()
    if job.requested_by and not job.requested_by.startswith("system:"):
        from yuxi.literature.access import build_access
        from yuxi.storage.postgres.models_business import User

        user = (await session.execute(select(User).where(User.uid == job.requested_by))).scalar_one_or_none()
        allowed = user is not None and not user.is_disabled and not user.is_deleted
        if allowed:
            from yuxi.services.principal import PrincipalResolutionError, resolve_tenant_id

            try:
                allowed = await resolve_tenant_id(session, user.uid) == paper.tenant_id
            except PrincipalResolutionError:
                allowed = False
        if allowed:
            scope = await build_access(user, int(paper.tenant_id))
            allowed = paper.rid in scope.rids
        if not allowed:
            await session.execute(
                update(type(job))
                .where(type(job).id == job.id)
                .values(status="STALE", error="来源访问权已撤销；任务未发布")
            )
            await session.commit()
            raise StaleLiteratureInput("来源访问权已撤销；任务未发布")
    if not source_matches(job, paper):
        job.status = "STALE"
        job.error = "来源解析或阅读修订已变更；此任务保留为历史记录"
        await session.execute(update(type(job)).where(type(job).id == job.id).values(status="STALE", error=job.error))
        await session.commit()
        raise StaleLiteratureInput(job.error)
    return paper


class StaleLiteratureInput(ValueError):
    """An in-flight historical task must never publish as the current output."""


async def invalidate_note_publications(session, revision_id: str) -> None:
    from yuxi.storage.postgres.models_knowledge import (
        KnowledgeParseRevision,
        KnowledgeWiki,
        WikiPublication,
        WikiSourceBinding,
    )

    sources = select(KnowledgeParseRevision.kb_id).where(KnowledgeParseRevision.revision_id == revision_id)
    wikis = select(WikiSourceBinding.wiki_id).where(
        WikiSourceBinding.source_kb_id.in_(sources), WikiSourceBinding.wiki_id.like("literature-wiki-%")
    )
    await session.execute(
        update(WikiPublication)
        .where(WikiPublication.wiki_id.in_(wikis), WikiPublication.status == "ACTIVE")
        .values(status="SUPERSEDED")
    )
    await session.execute(
        update(KnowledgeWiki)
        .where(KnowledgeWiki.wiki_id.in_(wikis))
        .values(current_publication_id=None, status="DRAFT")
    )
