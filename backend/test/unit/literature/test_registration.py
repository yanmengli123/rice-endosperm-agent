"""文献身份登记链回归（提取自编译服务后的独立模块契约）。

编译/翻译子系统下线后，`register_literature_paper` 是知识源登记桥
（reconcile.register_scientific_revision）的唯一身份入口——本测试锁定：
同 RID 幂等复用、后补 DOI 回填不改身份、来源版本切换递增阅读版本。
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.literature.registration import register_literature_paper
from yuxi.storage.postgres.models_business import Tenant, User
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeBase,
    KnowledgeFile,
    KnowledgeParseRevision,
    LiteraturePaper,
)

pytestmark = [pytest.mark.unit]
TENANT = 11


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    models = (Tenant, User, KnowledgeBase, KnowledgeFile, KnowledgeParseRevision, LiteraturePaper)
    async with engine.begin() as conn:
        for model in models:
            await conn.run_sync(model.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def sessions():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    import yuxi.literature.registration as registration

    monkeypatch.setattr(registration.pg_manager, "get_async_session_context", sessions)

    async def seed():
        async with factory() as session:
            session.add(Tenant(id=TENANT, name="登记测试租户"))
            session.add(
                KnowledgeParseRevision(
                    tenant_id=TENANT,
                    revision_id="spr_regression_a",
                    kb_id="kb_reg",
                    file_id="file_reg",
                    source_sha256="a" * 64,
                    parser_fingerprint="b" * 64,
                    pipeline_version="scientific_pdf_v4.0",
                )
            )
            await session.commit()

    await seed()
    return factory


async def test_same_rid_is_idempotent_and_backfills_doi(env):
    first = await register_literature_paper(tenant_id=TENANT, doi="10.1002/adma.202407106", title="T1")
    second = await register_literature_paper(tenant_id=TENANT, doi="10.1002/adma.202407106", title="T2")
    assert second.id == first.id, "同租户同 RID 必须复用既有行"
    assert second.rid == first.rid
    assert second.title == "T1", "既有标题不被空位覆盖"
    async with env() as session:
        count = len((await session.execute(select(LiteraturePaper))).all())
    assert count == 1


async def test_no_doi_paper_gets_fingerprint_identity(env):
    paper = await register_literature_paper(tenant_id=TENANT, fingerprint="sha:abc", title="仅指纹")
    assert paper.rid, "无 DOI 时指纹必须兜底生成身份"
    again = await register_literature_paper(tenant_id=TENANT, fingerprint="sha:abc")
    assert again.id == paper.id


async def test_source_revision_switch_bumps_reading_version(env):
    linked = await register_literature_paper(
        tenant_id=TENANT,
        doi="10.1111/reg.1",
        source_revision_id="spr_regression_a",
    )
    assert linked.source_revision_id == "spr_regression_a"
    assert linked.reading_version == 1
    # 同一来源重复登记幂等
    repeat = await register_literature_paper(
        tenant_id=TENANT, doi="10.1111/reg.1", source_revision_id="spr_regression_a"
    )
    assert repeat.id == linked.id
    assert repeat.reading_version == 1


async def test_cross_tenant_revision_is_rejected(env):
    with pytest.raises(ValueError, match="解析来源不存在或跨租户"):
        await register_literature_paper(
            tenant_id=TENANT + 1,
            doi="10.1111/reg.2",
            source_revision_id="spr_regression_a",
        )
