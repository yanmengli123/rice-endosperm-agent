from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from yuxi.services import feedback_service as svc


class _FakeResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _FakeSession:
    def __init__(self, results, commit_error: Exception | None = None):
        self.results = list(results)
        self.added = []
        self.committed = False
        self.rolled_back = False
        self.commit_error = commit_error

    async def execute(self, _query):
        return _FakeResult(self.results.pop(0))

    def add(self, item):
        self.added.append(item)

    async def commit(self):
        if self.commit_error is not None:
            raise self.commit_error
        self.committed = True

    async def refresh(self, item):
        item.id = 9
        item.created_at = datetime(2026, 1, 2, 3, 4, 5)

    async def rollback(self):
        self.rolled_back = True


@pytest.mark.asyncio
async def test_submit_message_feedback_syncs_langfuse_score(monkeypatch: pytest.MonkeyPatch):
    message = SimpleNamespace(
        id=3,
        conversation_id=7,
        extra_metadata={"langfuse_trace_id": "trace-1"},
    )
    conversation = SimpleNamespace(id=7, uid="user-1")
    db = _FakeSession([message, conversation])
    calls = []

    monkeypatch.setattr(svc, "submit_user_feedback_score", lambda **kwargs: calls.append(kwargs) or True)

    result = await svc.submit_message_feedback_view(
        message_id=3,
        rating="like",
        reason=None,
        db=db,
        current_uid="user-1",
    )

    assert result == {
        "id": 9,
        "message_id": 3,
        "rating": "like",
        "reason": None,
        "created_at": "2026-01-02T03:04:05",
    }
    assert db.committed is True
    assert db.rolled_back is False
    assert calls == [
        {
            "trace_id": "trace-1",
            "feedback_id": 9,
            "message_id": 3,
            "conversation_id": 7,
            "uid": "user-1",
            "rating": "like",
            "reason": None,
        }
    ]


@pytest.mark.asyncio
async def test_submit_message_feedback_skips_langfuse_without_trace_id(monkeypatch: pytest.MonkeyPatch):
    message = SimpleNamespace(id=3, conversation_id=7, extra_metadata={})
    conversation = SimpleNamespace(id=7, uid="user-1")
    db = _FakeSession([message, conversation])
    calls = []

    monkeypatch.setattr(svc, "submit_user_feedback_score", lambda **kwargs: calls.append(kwargs) or True)

    result = await svc.submit_message_feedback_view(
        message_id=3,
        rating="dislike",
        reason="不相关",
        db=db,
        current_uid="user-1",
    )

    assert result["rating"] == "dislike"
    assert result["reason"] == "不相关"
    assert calls == []


@pytest.mark.asyncio
async def test_submit_message_feedback_unique_conflict_is_idempotent_success(
    monkeypatch: pytest.MonkeyPatch,
):
    """唯一索引冲突（并发/重复提交）按幂等成功处理：返回既有记录，不重复推送 langfuse。"""
    message = SimpleNamespace(id=3, conversation_id=7, extra_metadata={"langfuse_trace_id": "trace-1"})
    conversation = SimpleNamespace(id=7, uid="user-1")
    existing = SimpleNamespace(
        id=5,
        message_id=3,
        rating="like",
        reason=None,
        created_at=datetime(2026, 1, 1, 0, 0, 0),
    )
    db = _FakeSession(
        [message, conversation, existing],
        commit_error=IntegrityError("dup", None, Exception("unique")),
    )
    calls = []
    monkeypatch.setattr(svc, "submit_user_feedback_score", lambda **kwargs: calls.append(kwargs) or True)

    result = await svc.submit_message_feedback_view(
        message_id=3,
        rating="like",
        reason=None,
        db=db,
        current_uid="user-1",
    )

    assert result == {
        "id": 5,
        "message_id": 3,
        "rating": "like",
        "reason": None,
        "created_at": "2026-01-01T00:00:00",
    }
    assert db.committed is False
    assert db.rolled_back is True
    assert calls == []
