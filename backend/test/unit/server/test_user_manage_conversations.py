from datetime import datetime
from importlib import import_module
from types import SimpleNamespace

import pytest

user_router = import_module("server.routers.user_router")


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar(self):
        return self.value


class _RowsResult:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


@pytest.mark.asyncio
async def test_managed_conversation_list_returns_total(monkeypatch):
    target = SimpleNamespace(uid="member-1", username="研究员")
    current = SimpleNamespace(id=1, uid="admin", role="superadmin", department_id=None)
    conversation = SimpleNamespace(
        thread_id="thread-1",
        title="多行问答",
        is_pinned=False,
        created_at=datetime(2026, 8, 28, 9, 0),
        updated_at=datetime(2026, 8, 28, 9, 1),
    )

    class DB:
        calls = 0

        async def execute(self, _stmt):
            self.calls += 1
            return SimpleNamespace(scalar_one_or_none=lambda: target) if self.calls == 1 else _ScalarResult(1)

    class Repo:
        def __init__(self, _db):
            pass

        async def list_conversations(self, **_kwargs):
            return [conversation]

    import yuxi.repositories.conversation_repository as repository

    monkeypatch.setattr(repository, "ConversationRepository", Repo)
    monkeypatch.setattr(user_router, "_admin_guard", lambda *_args: None)
    result = await user_router.list_user_conversations_for_admin(
        "member-1",
        page=1,
        page_size=20,
        current_user=current,
        db=DB(),
    )
    assert result["total"] == 1
    assert result["conversations"][0]["thread_id"] == "thread-1"


@pytest.mark.asyncio
async def test_csv_export_wraps_formula_cells_and_records_audit(monkeypatch):
    target = SimpleNamespace(uid="member-1", username="研究员")
    current = SimpleNamespace(id=1, uid="admin")
    conversation = SimpleNamespace(
        id=9,
        thread_id="thread-1",
        title="=危险标题",
        created_at=datetime(2026, 8, 28, 9, 0),
    )
    chat_message = SimpleNamespace(
        role="assistant",
        content="+第二行\n正文",
        created_at=datetime(2026, 8, 28, 9, 1),
    )

    class DB:
        def __init__(self):
            self.added = []
            self.committed = False

        async def execute(self, _stmt):
            return _RowsResult([(conversation, chat_message)])

        def add(self, value):
            self.added.append(value)

        async def commit(self):
            self.committed = True

    async def fake_target(*_args, **_kwargs):
        return target

    async def fake_tenant(*_args, **_kwargs):
        return 3

    db = DB()
    monkeypatch.setattr(user_router, "_load_manage_target", fake_target)
    monkeypatch.setattr(user_router, "resolve_operator_tenant_id", fake_tenant)
    response = await user_router.export_user_conversations_for_admin(
        "member-1",
        current_user=current,
        db=db,
    )

    decoded = response.body.decode("utf-8-sig")
    assert "'=危险标题" in decoded
    assert "'+第二行" in decoded
    assert response.media_type.startswith("text/csv")
    assert db.committed is True
    assert db.added[0].tenant_id == 3
