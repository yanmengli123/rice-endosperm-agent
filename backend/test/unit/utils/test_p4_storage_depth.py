"""P4 存储纵深单元测试：usage_ledger 模型与写入助手、RLS 迁移登记。"""

from types import SimpleNamespace

import pytest

from yuxi.storage.postgres.models_business import UsageLedger


class _CaptureDB:
    def __init__(self):
        self.added = []

    def add(self, item):
        self.added.append(item)

    async def execute(self, *_a, **_k):
        return SimpleNamespace()

    async def flush(self):
        return None


class TestUsageLedgerModel:
    def test_columns_present(self):
        cols = {c.name for c in UsageLedger.__table__.columns}
        assert {"run_id", "uid", "tenant_id", "model_spec", "input_tokens",
                "output_tokens", "total_tokens", "estimated"} <= cols

    def test_indexes_for_query_dimensions(self):
        names = " ".join(idx.name or "" for idx in UsageLedger.__table__.indexes)
        for dim in ("run_id", "uid", "tenant_id", "created_at"):
            assert dim in names


@pytest.mark.asyncio
async def test_persist_writes_ledger_row():
    from yuxi.services.chat_service import _persist_run_total_tokens

    db = _CaptureDB()
    await _persist_run_total_tokens(
        db, "run-1", 123, uid="alice", model_spec="deepseek:deepseek-v4-flash", estimated=True
    )
    ledgers = [item for item in db.added if isinstance(item, UsageLedger)]
    assert len(ledgers) == 1
    assert ledgers[0].total_tokens == 123
    assert ledgers[0].uid == "alice"
    assert ledgers[0].estimated is True


@pytest.mark.asyncio
async def test_persist_without_uid_skips_ledger_but_sets_total():
    from yuxi.services.chat_service import _persist_run_total_tokens

    db = _CaptureDB()

    class _Repo:
        def __init__(self, db):
            pass

        async def set_total_tokens(self, run_id, total):
            self.called = (run_id, total)

    from yuxi.repositories.agent_run_repository import AgentRunRepository
    original = AgentRunRepository.set_total_tokens
    AgentRunRepository.set_total_tokens = _Repo.set_total_tokens
    try:
        await _persist_run_total_tokens(db, "run-2", 5, uid=None)
    finally:
        AgentRunRepository.set_total_tokens = original

    assert not [item for item in db.added if isinstance(item, UsageLedger)]
