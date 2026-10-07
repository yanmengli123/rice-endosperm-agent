"""协作成员不能跨过知识库、成员行、当前 PrincipalContext 的租户边界。"""

from contextlib import asynccontextmanager

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.repositories.knowledge_graph_review_repository import KnowledgeGraphReviewRepository
from yuxi.services import principal
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import KnowledgeBase, KnowledgeBaseMember


@pytest.mark.asyncio
@pytest.mark.parametrize("user_tenant,member_tenant,expected", [(10, 10, "viewer"), (20, 10, None), (10, 20, None)])
async def test_member_capability_requires_all_three_tenants(monkeypatch, user_tenant, member_tenant, expected):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def session_context():
        async with factory.begin() as session:
            yield session

    async def resolve_tenant(session, uid):
        assert uid == "viewer"
        return user_tenant

    try:
        async with engine.begin() as connection:
            for table in (KnowledgeBase.__table__, KnowledgeBaseMember.__table__):
                await connection.run_sync(table.create)
        async with session_context() as session:
            session.add(KnowledgeBase(id=1, kb_id="owned", name="Owned", kb_type="milvus", tenant_id=10))
            session.add(
                KnowledgeBaseMember(id=1, kb_id="owned", uid="viewer", tenant_id=member_tenant, capability="viewer")
            )
        monkeypatch.setattr(pg_manager, "get_async_session_context", session_context)
        monkeypatch.setattr(principal, "resolve_tenant_id", resolve_tenant)
        repo = KnowledgeGraphReviewRepository()
        assert await repo.get_member_capability("owned", "viewer") == expected
        await repo.delete_member("owned", "viewer")
        assert await repo.get_member_capability("owned", "viewer") is None
    finally:
        await engine.dispose()
