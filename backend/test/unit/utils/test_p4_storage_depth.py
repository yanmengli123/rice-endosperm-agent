"""P4 存储纵深单元测试：usage_ledger 模型与写入助手、RLS 迁移登记。"""

from types import SimpleNamespace

import pytest

from yuxi.storage.postgres.models_business import UsageLedger


class _Result:
    def __init__(self, value=None):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _CaptureDB:
    def __init__(self, run=None):
        self.added = []
        self.run = run

    def add(self, item):
        self.added.append(item)

    async def execute(self, statement, *_a, **_k):
        text = str(statement)
        if "agent_runs" in text:
            return _Result(self.run)
        return _Result(None)

    async def flush(self):
        return None


class TestUsageLedgerModel:
    def test_columns_present(self):
        cols = {c.name for c in UsageLedger.__table__.columns}
        assert {"run_id", "uid", "tenant_id", "model_spec", "input_tokens",
                "output_tokens", "total_tokens", "estimated"} <= cols
        assert UsageLedger.__table__.columns["tenant_id"].nullable is False

    def test_indexes_for_query_dimensions(self):
        names = " ".join(idx.name or "" for idx in UsageLedger.__table__.indexes)
        for dim in ("run_id", "uid", "tenant_id", "created_at"):
            assert dim in names


@pytest.mark.asyncio
async def test_persist_writes_ledger_row():
    from yuxi.services.chat_service import _persist_run_total_tokens

    run = SimpleNamespace(id="run-1", uid="alice", tenant_id=7, total_tokens=None)
    db = _CaptureDB(run=run)
    await _persist_run_total_tokens(
        db, "run-1", 123, uid="alice", model_spec="deepseek:deepseek-v4-flash", estimated=True
    )
    ledgers = [item for item in db.added if isinstance(item, UsageLedger)]
    assert len(ledgers) == 1
    assert ledgers[0].total_tokens == 123
    assert ledgers[0].uid == "alice"
    assert ledgers[0].tenant_id == 7
    assert ledgers[0].estimated is True
    assert run.total_tokens == 123


@pytest.mark.asyncio
async def test_persist_without_uid_uses_authoritative_run_owner():
    from yuxi.services.chat_service import _persist_run_total_tokens

    run = SimpleNamespace(id="run-2", uid="alice", tenant_id=9, total_tokens=None)
    db = _CaptureDB(run=run)
    await _persist_run_total_tokens(db, "run-2", 5, uid=None)

    ledger = next(item for item in db.added if isinstance(item, UsageLedger))
    assert ledger.uid == "alice"
    assert ledger.tenant_id == 9
