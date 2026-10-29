"""Opt-in current-library read benchmark; no task submission or source mutations."""

import os
import statistics
import time

import pytest
from sqlalchemy import select

from yuxi.literature.access import access_scope, build_access
from yuxi.literature.readops import list_papers, list_paper_tasks
from yuxi.services.principal import resolve_tenant_id
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import User

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("YUXI_LITERATURE_BENCHMARK_UID"),
        reason="Requires explicit current-library read benchmark identity",
    ),
]


async def test_current_library_bounded_reads_and_sql_pagination(record_property):
    pg_manager.initialize()
    try:
        async with pg_manager.get_async_session_context() as db:
            user = await db.scalar(select(User).where(User.uid == os.environ["YUXI_LITERATURE_BENCHMARK_UID"]))
            assert user is not None and not user.is_disabled and not user.is_deleted
            tenant = await resolve_tenant_id(db, user.uid)
        timings = {"full_acl": [], "one_paper_acl": [], "catalog_page": [], "task_poll": []}
        for _ in range(5):
            started = time.perf_counter()
            full = await build_access(user, tenant)
            timings["full_acl"].append(time.perf_counter() - started)
            assert full.rids, "Benchmark requires an existing accessible corpus"
            rid = next(iter(full.rids))
            started = time.perf_counter()
            bounded = await build_access(user, tenant, rids=(rid,))
            timings["one_paper_acl"].append(time.perf_counter() - started)
            assert bounded.rids == {rid} and len(bounded.sources) <= 1
            started = time.perf_counter()
            async with access_scope(user, tenant, catalog_only=True) as scope:
                assert not scope.rids and not scope.sources
                page = await list_papers(tenant, page_size=20)
                timings["catalog_page"].append(time.perf_counter() - started)
                started = time.perf_counter()
                assert len(await list_paper_tasks(tenant, limit=20)) <= 20
                timings["task_poll"].append(time.perf_counter() - started)
            assert page["total"] == len(full.rids) and len(page["items"]) <= 20
        record_property("accessible_papers", len(full.rids))
        for name, values in timings.items():
            record_property(name + "_median_ms", round(statistics.median(values) * 1000, 2))
            record_property(name + "_max_ms", round(max(values) * 1000, 2))
        if len(full.rids) >= 10000:
            assert statistics.median(timings["one_paper_acl"]) < statistics.median(timings["full_acl"])
        assert max(timings["one_paper_acl"]) < 5 and max(timings["catalog_page"]) < 5
        assert max(timings["task_poll"]) < 5
    finally:
        await pg_manager.close()
