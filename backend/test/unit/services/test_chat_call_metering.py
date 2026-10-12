"""/api/chat/call 直调计费：无 AgentRun 也必须落 usage_ledger（append-only 权威）。"""

from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.services.chat_service import persist_direct_call_usage
from yuxi.storage.postgres.models_business import (
    Base,
    Department,
    Tenant,
    TenantMembership,
    User,
    UsageLedger,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


@pytest_asyncio.fixture()
async def metering_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        department = Department(name="Metering Department", tenant_id=1)
        user = User(
            username="Metering User",
            uid="metering-user",
            password_hash="$argon2id$placeholder",
            role="user",
            department=department,
        )
        tenant = Tenant(id=1, name="测试租户", status="active")
        membership = TenantMembership(tenant_id=1, uid=user.uid, role="member", status="active")
        db.add_all([department, tenant, user, membership])
        await db.commit()
        yield db, user
    await engine.dispose()


async def _rows(db):
    return list((await db.execute(select(UsageLedger).order_by(UsageLedger.id))).scalars())


async def test_exact_usage_metadata_billed_without_estimation(metering_session):
    db, user = metering_session
    response = SimpleNamespace(
        content="answer",
        usage_metadata={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
        response_metadata={},
    )

    await persist_direct_call_usage(
        db,
        uid=user.uid,
        model_spec="provider:model",
        query_text="question",
        response=response,
        request_id="req-1",
    )

    (row,) = await _rows(db)
    assert row.run_id == "direct-call:req-1"
    assert row.uid == user.uid
    assert row.tenant_id == 1
    assert (row.input_tokens, row.output_tokens, row.total_tokens) == (11, 7, 18)
    assert row.estimated is False
    assert row.credential_source == "platform"


async def test_missing_usage_estimates_by_chars_and_marks_estimated(metering_session):
    db, user = metering_session
    response = SimpleNamespace(content="x" * 40, usage_metadata={}, response_metadata={})

    await persist_direct_call_usage(
        db,
        uid=user.uid,
        model_spec=None,
        query_text="q" * 100,
        response=response,
        request_id="req-2",
    )

    (row,) = await _rows(db)
    assert row.input_tokens == 25  # 100 chars // 4
    assert row.output_tokens == 10  # 40 chars // 4
    assert row.total_tokens == 35
    assert row.estimated is True
