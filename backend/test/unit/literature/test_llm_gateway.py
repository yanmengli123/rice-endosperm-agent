from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.literature import llm_gateway
from yuxi.services import agent_run_service
from yuxi.storage.postgres.models_business import AgentRun, UserModelPreference
from yuxi.storage.postgres.models_knowledge import LiteratureCompileJob


pytestmark = pytest.mark.unit


class _SessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, *_args):
        if exc_type is None:
            await self.session.commit()
        else:
            await self.session.rollback()
        return False


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(AgentRun.__table__.create)
        await connection.run_sync(UserModelPreference.__table__.create)
        await connection.run_sync(LiteratureCompileJob.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        monkeypatch.setattr(
            llm_gateway.pg_manager,
            "get_async_session_context",
            lambda: _SessionContext(session),
        )
        yield session
    await engine.dispose()


async def test_retry_gets_a_fresh_append_only_accounting_run(db, monkeypatch):
    original_id = "run-original"
    db.add(
        AgentRun(
            id=original_id,
            tenant_id=7,
            conversation_thread_id="literature:paper-1",
            agent_slug="literature",
            uid="user-1",
            status="failed",
            request_id="request-original",
            run_type="literature",
            input_payload={"monthly_platform_token_limit": 1000},
        )
    )
    job = LiteratureCompileJob(
        id=1,
        tenant_id=7,
        paper_id=11,
        level="L1",
        status="RUNNING",
        attempt=1,
        requested_by="user-1",
        llm_run_id=original_id,
        model_spec="provider:model",
        credential_source="platform",
        provider_id="provider",
        policy_version=3,
    )
    db.add(job)
    await db.commit()

    async def _entitlement(**_kwargs):
        return {"tenant_id": 7}

    async def _platform_quota(**_kwargs):
        return None

    monkeypatch.setattr(agent_run_service, "_enforce_user_quota", _entitlement)
    monkeypatch.setattr(agent_run_service, "_enforce_platform_token_quota", _platform_quota)

    policy = await llm_gateway.policy_for_job_attempt(job, operation="compile_l1")

    assert policy.run_id != original_id
    runs = list((await db.execute(select(AgentRun).order_by(AgentRun.id))).scalars())
    assert len(runs) == 2
    assert next(item for item in runs if item.id == policy.run_id).input_payload["retry_of"] == original_id
    await db.refresh(job)
    assert job.llm_run_id == policy.run_id


async def test_freeze_supersedes_stale_pending_run_for_same_resource(db, monkeypatch):
    """worker 中断遗留的 pending run 不得阻塞同资源的再次提交（500 回归）。"""
    db.add(
        AgentRun(
            id="run-stuck",
            tenant_id=7,
            conversation_thread_id="literature:paper-1:L1",
            agent_slug="literature",
            uid="user-1",
            status="pending",
            request_id="request-stuck",
            run_type="literature",
            input_payload={},
        )
    )
    await db.commit()

    async def _entitlement(**_kwargs):
        return {"tenant_id": 7}

    async def _platform_quota(**_kwargs):
        return None

    async def _custom_specs(*_args, **_kwargs):
        return []

    monkeypatch.setattr(agent_run_service, "_enforce_user_quota", _entitlement)
    monkeypatch.setattr(agent_run_service, "_enforce_platform_token_quota", _platform_quota)
    from yuxi.services import user_credential_service

    monkeypatch.setattr(user_credential_service, "list_active_custom_model_specs", _custom_specs)

    policy = await llm_gateway.freeze_literature_policy(
        db, tenant_id=7, uid="user-1", operation="compile_l1", resource_ref="paper-1:L1"
    )

    assert policy.run_id != "run-stuck"
    stuck = (await db.execute(select(AgentRun).where(AgentRun.id == "run-stuck"))).scalar_one()
    assert stuck.status == "failed"
    assert stuck.error_type == "LiteratureSuperseded"
    fresh = (await db.execute(select(AgentRun).where(AgentRun.id == policy.run_id))).scalar_one()
    assert fresh.status == "pending"


def test_token_usage_prefers_provider_metadata():
    response = SimpleNamespace(
        content="result",
        usage_metadata={"input_tokens": 12, "output_tokens": 5},
        response_metadata={},
    )
    usage = llm_gateway._token_usage(response, [{"content": "prompt"}])
    assert (usage.input_tokens, usage.output_tokens, usage.estimated) == (12, 5, False)


def test_token_usage_has_conservative_fallback():
    response = SimpleNamespace(content="eight chars", usage_metadata={}, response_metadata={})
    usage = llm_gateway._token_usage(response, [{"content": "four"}])
    assert usage.input_tokens >= 1
    assert usage.output_tokens >= 1
    assert usage.estimated is True
