"""Trusted request identity, reapplied on every transaction (including after commit)."""

from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

_identity = ContextVar("postgres_runtime_identity", default=None)


def bind_identity(tenant_id, uid):
    if type(tenant_id) is not int or tenant_id < 1 or not isinstance(uid, str) or not uid:
        raise PermissionError("Missing trusted database identity")
    return _identity.set((tenant_id, uid))


@contextmanager
def actor(tenant_id, uid):
    token = bind_identity(tenant_id, uid)
    try:
        yield
    finally:
        _identity.reset(token)


class RuntimeSyncSession(Session):
    pass


class RuntimeAsyncSession(AsyncSession):
    sync_session_class = RuntimeSyncSession


@event.listens_for(RuntimeSyncSession, "after_begin")
def transaction_identity(_session, _transaction, connection):
    if connection.dialect.name != "postgresql":
        return
    identity = _identity.get()
    # Always set both values, even with no actor. Never inherit a pooled setting.
    connection.execute(
        text("SELECT set_config('yuxi.tenant_id', :tenant, true), set_config('yuxi.uid', :uid, true)"),
        {"tenant": str(identity[0]) if identity else "", "uid": identity[1] if identity else ""},
    )


class RuntimeIdentityMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        token = _identity.set(None)
        try:
            await self.app(scope, receive, send)
        finally:
            _identity.reset(token)


async def pdf_identity(db, job_id):
    if db.get_bind().dialect.name == "postgresql":
        row = (await db.execute(text("SELECT * FROM yuxi_pdf_job_identity(:job)"), {"job": job_id})).first()
    else:
        from yuxi.storage.postgres.models_knowledge import LiteratureTranslationJob

        job = await db.get(LiteratureTranslationJob, job_id)
        row = (job.tenant_id, job.requested_by) if job and job.variant_kind == "pdf_zh" else None
    if not row or not row[1]:
        raise PermissionError("PDF job identity is unavailable")
    return int(row[0]), str(row[1])
