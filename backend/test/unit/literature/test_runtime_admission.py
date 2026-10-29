"""Retired-job audits remain read-only and release-blocking in the lightweight app."""

import importlib.util
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.literature import runtime_admission as admission
from yuxi.storage.postgres.models_knowledge import LiteraturePdfModelCall, LiteratureTranslationJob

pytestmark = pytest.mark.unit
NOW = datetime(2026, 10, 26, 12, tzinfo=UTC)


@pytest_asyncio.fixture
async def audit_db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(LiteratureTranslationJob.__table__.create)
        await conn.run_sync(LiteraturePdfModelCall.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def sessions():
        async with factory() as db:
            yield db
            await db.commit()

    monkeypatch.setattr(admission.pg_manager, "get_async_session_context", sessions)
    monkeypatch.setattr(admission, "utc_now", lambda: NOW)
    yield sessions, engine
    await engine.dispose()


def job(identifier, **values):
    return LiteratureTranslationJob(**{
        "id": identifier, "tenant_id": 7, "paper_id": 1, "generation": identifier,
        "variant_kind": "pdf_zh", "status": "SUCCEEDED", "created_at": NOW,
        "updated_at": NOW - timedelta(minutes=6), "execution_json": {}, **values,
    })


async def test_unknown_usage_and_overdue_cleanup_block_without_mutating_audit_records(audit_db):
    sessions, engine = audit_db
    async with sessions() as db:
        db.add_all([
            job(1, execution_json={"artifact_cleanup_pending": True}),
            job(2, execution_json={"remote_id": "old-run", "cleaned": False}),
            job(3, execution_json={"remote_id": "cleaned-run", "cleaned": True}),
            job(4, updated_at=NOW, execution_json={"artifact_cleanup_pending": True}),
            job(5, status="RUNNING", execution_json={"artifact_cleanup_pending": True}),
            job(6, variant_kind="zh", execution_json={"artifact_cleanup_pending": True}),
            job(7, tenant_id=8, execution_json={"artifact_cleanup_pending": True}),
        ])
        for index, tenant in ((1, 7), (2, 8)):
            db.add(LiteraturePdfModelCall(
                id=str(index), job_id=index, run_id="historic-run", tenant_id=tenant,
                attempt=1, request_digest=str(index), status="uncertain", reserved_tokens=456,
            ))
    statements = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    state = await admission.operations(7)
    assert {alert["code"] for alert in state["alerts"]} == {"PDF_USAGE_UNRESOLVED", "PDF_CLEANUP_OVERDUE"}
    assert state["unknown_calls"] == 1 and state["cleanup_overdue"] == 2
    assert statements and all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    async with sessions() as db:
        call = await db.get(LiteraturePdfModelCall, "1")
        assert (call.status, call.reserved_tokens, call.input_tokens, call.output_tokens) == ("uncertain", 456, 0, 0)
        assert call.finished_at is None and call.resolution_json == {}
        assert (await db.get(LiteratureTranslationJob, 1)).execution_json == {"artifact_cleanup_pending": True}


@pytest.mark.parametrize("age,alert", [(None, False), (300, False), (301, True), (-10, False)])
async def test_queue_age_requires_an_overdue_pending_pdf_job(audit_db, age, alert):
    sessions, _engine = audit_db
    async with sessions() as db:
        db.add(job(1, tenant_id=8, status="PENDING", created_at=NOW - timedelta(hours=1)))
        db.add(job(2, variant_kind="zh", status="PENDING", created_at=NOW - timedelta(hours=1)))
        if age is not None:
            db.add(job(3, status="PENDING", created_at=NOW - timedelta(seconds=age)))
    state = await admission.operations(7)
    assert state["alerts"] == ([{"code": "PDF_QUEUE_DELAY"}] if alert else [])
    assert state["oldest_queue_seconds"] == max(0, age or 0)


@pytest.mark.parametrize("samples,failures,alert", [(19, 2, False), (20, 1, False), (20, 2, True)])
async def test_failure_slo_uses_current_tenant_recent_samples_and_original_thresholds(
    audit_db, samples, failures, alert,
):
    sessions, _engine = audit_db
    async with sessions() as db:
        db.add_all(job(index, status="FAILED" if index <= failures else "SUCCEEDED")
                   for index in range(1, samples + 1))
        db.add(job(100, status="FAILED", updated_at=NOW - timedelta(days=2)))
        db.add(job(101, status="FAILED", tenant_id=8))
        db.add(job(102, status="FAILED", variant_kind="zh"))
        db.add(job(103, status="CANCELLED"))
    state = await admission.operations(7)
    assert state["alerts"] == ([{"code": "PDF_FAILURE_RATE"}] if alert else [])
    assert state["terminal_samples"] == samples and state["failure_rate"] == failures / samples
    assert state["slo_evidence"] == ("available" if samples >= 20 else "insufficient_samples")


@pytest.fixture
def release_gate(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "literature_release_check_test", Path("scripts/literature_release_check.py"),
    )
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    user = SimpleNamespace(role="admin", is_disabled=False, is_deleted=False)
    db = SimpleNamespace(scalar=AsyncMock(return_value=user))

    @asynccontextmanager
    async def sessions():
        yield db

    monkeypatch.setattr(gate.pg_manager, "initialize", Mock())
    monkeypatch.setattr(gate.pg_manager, "get_async_session_context", sessions)
    monkeypatch.setattr(gate.pg_manager, "close", AsyncMock())
    monkeypatch.setattr(gate, "resolve_tenant_id", AsyncMock(return_value=7))
    monkeypatch.setattr(gate, "readiness", AsyncMock(return_value={"ready": True}))
    monkeypatch.setattr(gate, "operations", AsyncMock(return_value={
        "alerts": [], "terminal_samples": 0, "slo_evidence": "insufficient_samples",
    }))
    return gate, db, user


@pytest.mark.parametrize("invalid", ["missing", "member", "disabled", "deleted"])
async def test_release_gate_requires_an_existing_active_admin_before_audits(release_gate, invalid):
    gate, db, user = release_gate
    if invalid == "missing":
        db.scalar.return_value = None
    elif invalid == "member":
        user.role = "user"
    else:
        setattr(user, "is_" + invalid, True)
    with pytest.raises(PermissionError, match="管理员"):
        await gate.check("admin-uid")
    gate.resolve_tenant_id.assert_not_awaited()
    gate.readiness.assert_not_awaited()
    gate.operations.assert_not_awaited()
    gate.pg_manager.close.assert_awaited_once()


@pytest.mark.parametrize("ready,alerts,exit_code", [
    (True, [], 0), (False, [], 2), (True, [{"code": "PDF_USAGE_UNRESOLVED"}], 2),
    (True, [{"code": "PDF_CLEANUP_OVERDUE"}], 2),
])
async def test_release_gate_combines_infrastructure_and_historical_blockers(
    release_gate, capsys, ready, alerts, exit_code,
):
    gate, db, _user = release_gate
    gate.readiness.return_value = {"ready": ready}
    gate.operations.return_value = {"alerts": alerts, "terminal_samples": 23, "slo_evidence": "available"}
    assert await gate.check("admin-uid") == exit_code
    output = json.loads(capsys.readouterr().out)
    assert output["release_ready"] is (exit_code == 0)
    assert output["operational_alerts"] == [alert["code"] for alert in alerts]
    assert output["execution_samples"] == 23 and output["provider_calls_created"] == 0
    gate.resolve_tenant_id.assert_awaited_once_with(db, "admin-uid")
    gate.operations.assert_awaited_once_with(7)
    gate.pg_manager.close.assert_awaited_once()
