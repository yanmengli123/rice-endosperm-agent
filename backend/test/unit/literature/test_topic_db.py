"""Production fail-closed and actor fencing; no live database required."""

import io
from types import SimpleNamespace

import pytest

from yuxi.literature import topic_db
from yuxi.literature.access import _access

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("variable", ["YUXI_ENV", "YUXI_TOPIC_REQUIRE_RLS"])
async def test_production_never_falls_back_to_admin_pool(monkeypatch, variable):
    monkeypatch.delenv("YUXI_TOPIC_DATABASE_URL", raising=False)
    monkeypatch.delenv("YUXI_TOPIC_SCHEDULER_DATABASE_URL", raising=False)
    monkeypatch.setenv(variable, "production" if variable == "YUXI_ENV" else "true")
    assert not (await topic_db.readiness())["ready"]
    with pytest.raises(RuntimeError, match="独立"):
        async with topic_db.session():
            pytest.fail("Must not reach admin pool")
    with pytest.raises(RuntimeError):
        async with topic_db.session(scheduler=True):
            pytest.fail("Must not reach admin scheduler")


def test_actor_context_is_nested_and_restored():
    assert topic_db._identity.get() is None
    with topic_db.actor(1, "alice"):
        assert topic_db._identity.get() == (1, "alice")
        with topic_db.actor(2, "bob"):
            assert topic_db._identity.get() == (2, "bob")
        assert topic_db._identity.get() == (1, "alice")
    assert topic_db._identity.get() is None
    with pytest.raises(PermissionError):
        with topic_db.actor(1, "system"):
            pass


async def test_invalid_dsn_is_never_disclosed(monkeypatch):
    secret = "private-dsn-secret"
    monkeypatch.setenv("YUXI_TOPIC_DATABASE_URL", f"invalid://{secret}")
    with topic_db.actor(1, "alice"), pytest.raises(RuntimeError) as failure:
        async with topic_db.session():
            pass
    assert secret not in str(failure.value)


async def test_production_rejects_archive_before_cpu_parsing(monkeypatch):
    from yuxi.literature import topic_legacy

    monkeypatch.setenv("YUXI_TOPIC_REQUIRE_RLS", "true")
    monkeypatch.delenv("YUXI_TOPIC_DATABASE_URL", raising=False)
    monkeypatch.delenv("YUXI_TOPIC_SCHEDULER_DATABASE_URL", raising=False)
    monkeypatch.setattr(topic_legacy, "read_archive", lambda *args: pytest.fail("Must not parse blocked uploads"))
    token = _access.set(SimpleNamespace(tenant_id=1, user=SimpleNamespace(uid="alice")))
    try:
        with pytest.raises(RuntimeError, match="未解析"):
            await topic_legacy.import_archive(1, io.BytesIO())
    finally:
        _access.reset(token)
