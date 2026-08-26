from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.services.agent_run_service import _enforce_user_quota
from yuxi.storage.postgres.models_business import AgentRun, Base, Conversation, Department, Tenant, TenantMembership, TenantUserEntitlement, User
from yuxi.utils.datetime_utils import utc_now_naive

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


@pytest_asyncio.fixture()
async def quota_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        department = Department(name="Quota Department", tenant_id=1)
        user = User(
            username="Quota User",
            uid="quota-user",
            password_hash="$argon2id$placeholder",
            role="user",
            department=department,
        )
        conversation = Conversation(
            thread_id="quota-thread",
            uid=user.uid,
            agent_id="chatbot",
            title="Quota test",
            status="active",
        )
        tenant = Tenant(id=1, name="测试租户", status="active")
        membership = TenantMembership(tenant_id=1, uid=user.uid, role="member", status="active")
        quota = TenantUserEntitlement(tenant_id=1, uid=user.uid)
        db.add_all([department, tenant, user, conversation, membership, quota])
        await db.commit()
        await db.refresh(conversation)
        yield db, user, quota, conversation
    await engine.dispose()


def _run(*, user: User, conversation: Conversation, run_type: str, status: str, total_tokens: int | None = None):
    run_id = str(uuid.uuid4())
    return AgentRun(
        id=run_id,
        conversation_thread_id=conversation.thread_id,
        conversation_id=conversation.id,
        agent_slug="chatbot",
        uid=user.uid,
        total_tokens=total_tokens,
        status=status,
        request_id=run_id,
        run_type=run_type,
        input_payload={},
        created_at=utc_now_naive(),
    )


async def test_daily_quota_counts_only_user_initiated_chat_runs(quota_session):
    db, user, quota, conversation = quota_session
    quota.daily_run_limit = 1
    db.add(_run(user=user, conversation=conversation, run_type="subagent", status="completed"))
    await db.commit()

    await _enforce_user_quota(db=db, uid=user.uid)

    db.add(_run(user=user, conversation=conversation, run_type="chat", status="completed"))
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await _enforce_user_quota(db=db, uid=user.uid)
    assert exc.value.status_code == 429
    assert "运行次数" in str(exc.value.detail)


async def test_monthly_token_quota_rejects_second_concurrent_top_level_run(quota_session):
    db, user, quota, conversation = quota_session
    quota.monthly_platform_token_limit = 1_000
    db.add(_run(user=user, conversation=conversation, run_type="chat", status="running"))
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await _enforce_user_quota(db=db, uid=user.uid)

    assert exc.value.status_code == 429
    assert "正在计量" in str(exc.value.detail)


async def test_monthly_token_quota_rejects_exhausted_usage(quota_session):
    db, user, quota, conversation = quota_session
    quota.monthly_platform_token_limit = 1_000
    db.add(
        _run(
            user=user,
            conversation=conversation,
            run_type="subagent",
            status="completed",
            total_tokens=1_000,
        )
    )
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await _enforce_user_quota(db=db, uid=user.uid)

    assert exc.value.status_code == 429
    assert "token 用量" in str(exc.value.detail)
