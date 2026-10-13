"""PDF task governance: replay, unknown outcomes, cancellation, usage and provenance."""

import hashlib
import importlib
import json
import time
from datetime import timedelta
from contextlib import asynccontextmanager
from types import SimpleNamespace

import fitz
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.literature import pdf_translation as service
from yuxi.storage.postgres.models_business import AgentRun, UsageLedger
from yuxi.storage.postgres.models_knowledge import (
    LiteraturePaper,
    LiteraturePdfModelCall,
    LiteratureTranslationJob,
    LiteratureVariant,
)
from yuxi.utils.datetime_utils import utc_now

pytestmark = pytest.mark.unit
gateway = importlib.import_module("server.routers.literature_pdf_gateway")


@pytest_asyncio.fixture
async def database(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for model in (
            LiteraturePaper,
            LiteratureTranslationJob,
            LiteratureVariant,
            AgentRun,
            UsageLedger,
            LiteraturePdfModelCall,
        ):
            await conn.run_sync(model.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def context():
        async with factory() as db:
            yield db

    monkeypatch.setattr(service.pg_manager, "get_async_session_context", context)
    monkeypatch.setenv("SUPER_TRANSLATE_URL", "http://supertranslate:8000")
    monkeypatch.setenv("SUPER_TRANSLATE_TOKEN", "test-service-key-" + "x" * 40)
    async with factory() as db:
        paper = LiteraturePaper(
            id=1, tenant_id=7, rid="pdf-test", title="Rice", source_revision_id="rev1", reading_version=1
        )
        run = AgentRun(
            id="run1",
            uid="u1",
            tenant_id=7,
            agent_slug="literature",
            status="running",
            request_id="request1",
            conversation_thread_id="pdf-test",
            run_type="literature",
            input_payload={
                "operation": "translate_pdf_zh",
                "model_spec": "provider:model",
                "credential_source": "platform",
            },
        )
        job = LiteratureTranslationJob(
            id=1,
            paper_id=1,
            tenant_id=7,
            variant_kind="pdf_zh",
            status="RUNNING",
            source_revision_id="rev1",
            reading_version=1,
            generator_version=service.VERSION,
            requested_by="u1",
            llm_run_id="run1",
            model_spec="provider:model",
            credential_source="platform",
            execution_json={"lease": "lease1", "owner": "owner1", "deadline": int(time.time()) + 300},
            options_json={"token_budget": 100_000},
            result_json={},
        )
        db.add_all([paper, run, job])
        await db.commit()
        token = service.execution_token(job)

    async def current(db, job):
        return await db.get(LiteraturePaper, job.paper_id)

    monkeypatch.setattr(gateway, "require_current_input", current)
    monkeypatch.setattr(service, "require_current_input", current)
    app = FastAPI()
    app.include_router(gateway.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield factory, client, token
    await engine.dispose()


def request_body(**changes):
    body = {
        "model": "provider:model",
        "messages": [{"role": "user", "content": "Translate rice endosperm."}],
        "max_tokens": 64,
    }
    body.update(changes)
    body["request_id"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    return body


async def send(client, token, body=None):
    return await client.post(
        "/internal/literature-pdf/v1/chat/completions",
        json=body or request_body(),
        headers={"Authorization": f"Bearer {token}"},
    )


@pytest.mark.asyncio
async def test_duplicate_model_request_replays_once_and_settlement_is_append_only(database, monkeypatch):
    factory, client, token = database
    calls = []

    class Model:
        async def call(self, messages, **kwargs):
            calls.append(messages)
            return SimpleNamespace(content='["水稻胚乳"]', usage_metadata={"input_tokens": 12, "output_tokens": 8})

    monkeypatch.setattr("yuxi.models.chat.select_model", lambda **kwargs: Model())
    first, second = await send(client, token), await send(client, token)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() and len(calls) == 1
    async with factory() as db:
        run = await db.get(AgentRun, "run1")
        run.status = "completed"
        await db.commit()
    await service.settle_usage("run1")
    await service.settle_usage("run1")
    async with factory() as db:
        rows = (await db.execute(select(UsageLedger))).scalars().all()
        assert len(rows) == 1 and rows[0].total_tokens == 20 and not rows[0].estimated


@pytest.mark.asyncio
async def test_unknown_provider_outcome_never_retries_paid_call(database, monkeypatch):
    factory, client, token = database
    calls = []

    class Model:
        async def call(self, *_args, **_kwargs):
            calls.append(1)
            raise TimeoutError("lost supplier response")

    monkeypatch.setattr("yuxi.models.chat.select_model", lambda **kwargs: Model())
    assert (await send(client, token)).status_code == 502
    assert (await send(client, token)).status_code == 409
    changed = request_body(messages=[{"role": "user", "content": "Try another translation prompt."}])
    assert (await send(client, token, changed)).status_code == 409
    assert len(calls) == 1
    async with factory() as db:
        call = (await db.execute(select(LiteraturePdfModelCall))).scalar_one()
        assert call.status == "uncertain" and call.estimated


@pytest.mark.asyncio
async def test_cancelled_task_and_wrong_model_do_not_reserve_usage(database):
    factory, client, token = database
    assert (await send(client, token, request_body(model="other:model"))).status_code == 403
    async with factory() as db:
        job = await db.get(LiteratureTranslationJob, 1)
        job.cancel_requested = True
        await db.commit()
    assert (await send(client, token)).status_code == 403
    async with factory() as db:
        assert not (await db.execute(select(LiteraturePdfModelCall))).first()


@pytest.mark.asyncio
async def test_budget_and_tampered_token_rejected_before_supplier(database):
    factory, client, token = database
    assert (await send(client, token + "tampered")).status_code == 401
    async with factory() as db:
        job = await db.get(LiteratureTranslationJob, 1)
        job.options_json = {"token_budget": 1}
        await db.commit()
    assert (await send(client, token)).status_code == 429


@pytest.mark.asyncio
async def test_unresolved_other_pdf_task_still_reserves_monthly_budget(database):
    factory, client, token = database
    async with factory() as db:
        run = await db.get(AgentRun, "run1")
        run.input_payload = {**run.input_payload, "monthly_platform_token_limit": 1000}
        db.add(
            AgentRun(
                id="older-run",
                uid="u1",
                tenant_id=7,
                agent_slug="literature",
                status="failed",
                request_id="older-request",
                conversation_thread_id="older-pdf",
                run_type="literature",
                input_payload={"credential_source": "platform"},
            )
        )
        db.add(
            LiteratureTranslationJob(
                id=0,
                paper_id=1,
                tenant_id=7,
                variant_kind="pdf_zh",
                status="FAILED",
                source_revision_id="rev1",
                reading_version=1,
                requested_by="u1",
                llm_run_id="older-run",
            )
        )
        await db.flush()
        db.add(
            LiteraturePdfModelCall(
                id="unknown-call",
                job_id=0,
                run_id="older-run",
                tenant_id=7,
                attempt=0,
                request_digest="0" * 64,
                status="uncertain",
                reserved_tokens=1000,
                estimated=True,
            )
        )
        await db.commit()
    assert (await send(client, token)).status_code == 429


@pytest.mark.asyncio
async def test_revoked_source_acl_rejected_before_supplier(database, monkeypatch):
    _factory, client, token = database

    async def revoked(*_args):
        raise PermissionError("Source access revoked")

    monkeypatch.setattr(gateway, "require_current_input", revoked)
    assert (await send(client, token)).status_code == 403


@pytest.mark.asyncio
async def test_only_owner_can_cancel_and_old_worker_cannot_publish(database):
    _factory, _client, _token = database
    with pytest.raises(PermissionError):
        await service.cancel_pdf_translation(7, "pdf-test", "another-user", 1)
    with pytest.raises(service.StaleLiteratureInput):
        await service.update_execution(1, "old-owner", stage="发布")
    result = await service.cancel_pdf_translation(7, "pdf-test", "u1", 1)
    assert result["status"] == "CANCELLING"
    with pytest.raises(__import__("asyncio").CancelledError):
        await service.update_execution(1, "owner1")


def pdf_artifacts():
    with fitz.open() as document:
        document.new_page(width=600, height=800)
        source = document.tobytes()
        geometry = [[*document[0].mediabox, document[0].rotation]]
    with fitz.open() as document:
        document.new_page(width=600, height=800)
        document.new_page(width=600, height=800)
        dual = document.tobytes()
    artifacts = {"pdf_zh": source, "pdf_dual": dual, "qa": json.dumps({"status": "passed"}).encode()}
    source_sha = hashlib.sha256(source).hexdigest()
    manifest = {
        "source_sha256": source_sha,
        "page_count": 1,
        "geometry": geometry,
        "outputs": {k: {"sha256": hashlib.sha256(v).hexdigest(), "size": len(v)} for k, v in artifacts.items()},
    }
    artifacts["manifest"] = json.dumps(manifest).encode()
    return source, artifacts, source_sha


def test_pdf_publication_checks_source_digest_geometry_and_dual_page_count():
    source, artifacts, digest = pdf_artifacts()
    assert service.validate_outputs(source, artifacts, digest) == 1
    with pytest.raises(ValueError, match="来源"):
        service.validate_outputs(source, artifacts, "0" * 64)
    artifacts["pdf_dual"] = source
    with pytest.raises(ValueError, match="双语"):
        service.validate_outputs(source, artifacts, digest)


@pytest.mark.asyncio
async def test_worker_adopts_completed_remote_task_and_publishes_without_model_reexecution(database, monkeypatch):
    factory, _client, _token = database
    source, artifacts, digest = pdf_artifacts()
    requests, stored = [], {}
    async with factory() as db:
        job = await db.get(LiteratureTranslationJob, 1)
        job.updated_at = utc_now() - timedelta(seconds=130)
        job.options_json = {**job.options_json, "source_sha256": digest, "model_spec": job.model_spec}
        job.execution_json = {**job.execution_json, "remote_id": "existing-remote-task"}
        await db.commit()

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def request(self, method, path, **_kwargs):
            requests.append((method, path))
            return {"status": "completed", "progress": 1, "page_count": 1}

        async def download(self, _remote, kind):
            return artifacts[kind]

    class Storage:
        async def aupload_file(self, *, object_name, data, **_kwargs):
            stored[object_name] = data

    async def original(*_args):
        return source, "source/original.pdf"

    monkeypatch.setattr(service, "PdfClient", Client)
    monkeypatch.setattr(service, "get_minio_client", lambda: Storage())
    monkeypatch.setattr("yuxi.literature.reader_bundle.get_source_pdf", original)
    result = await service.process_pdf_translation({}, 1)
    assert result["status"] == "SUCCEEDED" and len(stored) == 4
    assert all("existing-remote-task" in path for _method, path in requests)
    async with factory() as db:
        job = await db.get(LiteratureTranslationJob, 1)
        assert job.execution_json["cleaned"] and job.execution_json["lease"] == "lease1"
        assert len((await db.execute(select(LiteratureVariant))).scalars().all()) == 2
        assert not (await db.execute(select(LiteraturePdfModelCall))).first()


@pytest.mark.asyncio
async def test_lost_remote_response_preserves_identity_for_reconciliation(database, monkeypatch):
    factory, _client, _token = database
    source, _artifacts, digest = pdf_artifacts()
    async with factory() as db:
        job = await db.get(LiteratureTranslationJob, 1)
        job.status = "PENDING"
        job.options_json = {**job.options_json, "source_sha256": digest}
        job.execution_json = {}
        await db.commit()

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def request(self, *_args, **_kwargs):
            raise httpx.ReadTimeout("response lost after remote commit")

    async def original(*_args):
        return source, "source/original.pdf"

    monkeypatch.setattr(service, "PdfClient", Client)
    monkeypatch.setattr("yuxi.literature.reader_bundle.get_source_pdf", original)
    result = await service.process_pdf_translation({}, 1)
    assert result["status"] == "RUNNING"
    async with factory() as db:
        job = await db.get(LiteratureTranslationJob, 1)
        assert job.status == "RUNNING" and job.execution_json["remote_id"]
        assert job.execution_json["stage"] == "连接中断，等待恢复"
        assert (await db.get(AgentRun, "run1")).status == "running"
