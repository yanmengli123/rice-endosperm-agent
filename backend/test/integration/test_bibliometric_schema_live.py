"""只读检查运行库迁移、RLS 与 ORM 列；不修改用户文献或权限。"""

import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from yuxi.storage.postgres.models_knowledge import LiteraturePaper, LiteratureReferenceBatch, LiteratureReferenceMember

pytestmark = pytest.mark.integration


async def test_reference_batch_live_schema_matches_models():
    url = os.environ.get("POSTGRES_URL")
    if not url:
        pytest.skip("集成环境 POSTGRES_URL 未配置")
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            assert (
                await conn.scalar(
                    text("SELECT COUNT(*) FROM schema_migrations WHERE version = '0111_literature_reference_batches'")
                )
                == 1
            )
            columns = (
                await conn.execute(
                    text(
                        "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public' "
                        "AND table_name IN ('literature_papers', 'literature_reference_batches', "
                        "'literature_reference_members')"
                    )
                )
            ).all()
            actual = {}
            for table, column in columns:
                actual.setdefault(table, set()).add(column)
            for model in (LiteraturePaper, LiteratureReferenceBatch, LiteratureReferenceMember):
                assert actual.get(model.__tablename__) == set(model.__table__.columns.keys())
            rls = (
                await conn.execute(
                    text(
                        "SELECT relname, relrowsecurity FROM pg_class WHERE relnamespace = 'public'::regnamespace "
                        "AND relname IN ('literature_reference_batches', 'literature_reference_members')"
                    )
                )
            ).all()
            assert len(rls) == 2 and all(enabled for _, enabled in rls)
            policies = (
                (
                    await conn.execute(
                        text(
                            "SELECT policyname FROM pg_policies WHERE schemaname = 'public' "
                            "AND tablename IN ('literature_reference_batches', 'literature_reference_members')"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert set(policies) == {"reference_batches_owner", "reference_members_owner"}
    finally:
        await engine.dispose()
