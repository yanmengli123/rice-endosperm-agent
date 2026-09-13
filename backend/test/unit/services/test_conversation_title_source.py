from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from yuxi.services import conversation_service as svc


def _conversation(title: str = svc.INITIAL_THREAD_TITLE, metadata: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        thread_id="thread-1",
        uid="u1",
        agent_id="ChatbotAgent",
        status="active",
        title=title,
        is_pinned=False,
        extra_metadata=dict(metadata or {}),
        created_at=datetime(2026, 9, 13, tzinfo=UTC),
        updated_at=datetime(2026, 9, 13, tzinfo=UTC),
    )


class FakeConversationRepository:
    def __init__(self, conversation: SimpleNamespace):
        self.conversation = conversation
        self.update_kwargs: dict = {}

    async def get_conversation_by_thread_id(self, thread_id: str, uid: str | None = None):
        if thread_id != self.conversation.thread_id:
            return None
        return self.conversation

    async def update_conversation(self, thread_id: str, **kwargs):
        self.update_kwargs = kwargs
        if kwargs.get("title") is not None:
            self.conversation.title = kwargs["title"]
        if kwargs.get("is_pinned") is not None:
            self.conversation.is_pinned = kwargs["is_pinned"]
        if kwargs.get("metadata"):
            self.conversation.extra_metadata = {
                **(self.conversation.extra_metadata or {}),
                **kwargs["metadata"],
            }
        return self.conversation


async def _run_update(
    monkeypatch: pytest.MonkeyPatch, conversation: SimpleNamespace, **kwargs
) -> tuple[FakeConversationRepository, dict]:
    repo = FakeConversationRepository(conversation)
    monkeypatch.setattr(svc, "ConversationRepository", lambda db: repo)
    response = await svc.update_thread_view(db=None, current_uid="u1", thread_id="thread-1", **kwargs)
    return repo, response


@pytest.mark.asyncio
async def test_bare_title_rename_writes_user_source(monkeypatch: pytest.MonkeyPatch):
    conversation = _conversation()
    repo, response = await _run_update(monkeypatch, conversation, title="我的会话")

    assert repo.update_kwargs["title"] == "我的会话"
    assert repo.update_kwargs["metadata"] == {"title_source": svc.TITLE_SOURCE_USER}
    assert response["title"] == "我的会话"
    assert response["metadata"]["title_source"] == svc.TITLE_SOURCE_USER


@pytest.mark.asyncio
async def test_fallback_naming_allowed_on_initial_title(monkeypatch: pytest.MonkeyPatch):
    conversation = _conversation()
    repo, response = await _run_update(
        monkeypatch,
        conversation,
        title="通过 MCP 查 Wx",
        metadata={"title_source": svc.TITLE_SOURCE_FALLBACK},
    )

    assert repo.update_kwargs["title"] == "通过 MCP 查 Wx"
    assert response["metadata"]["title_source"] == svc.TITLE_SOURCE_FALLBACK


@pytest.mark.asyncio
async def test_auto_can_refine_auto_title(monkeypatch: pytest.MonkeyPatch):
    conversation = _conversation(title="通过 MCP 查 Wx", metadata={"title_source": svc.TITLE_SOURCE_FALLBACK})
    repo, response = await _run_update(
        monkeypatch,
        conversation,
        title="Wx 与 MCP 查询",
        metadata={"title_source": svc.TITLE_SOURCE_AUTO},
    )

    assert repo.update_kwargs["title"] == "Wx 与 MCP 查询"
    assert response["metadata"]["title_source"] == svc.TITLE_SOURCE_AUTO


@pytest.mark.asyncio
async def test_auto_naming_rejected_after_user_rename(monkeypatch: pytest.MonkeyPatch):
    conversation = _conversation(title="我的会话", metadata={"title_source": svc.TITLE_SOURCE_USER})
    repo, response = await _run_update(
        monkeypatch,
        conversation,
        title="迟到的自动标题",
        metadata={"title_source": svc.TITLE_SOURCE_FALLBACK},
    )

    # 标题与 title_source 一并拒绝，其余 metadata 键仍允许透传
    assert repo.update_kwargs["title"] is None
    assert repo.update_kwargs["metadata"] is None
    assert response["title"] == "我的会话"
    assert response["metadata"]["title_source"] == svc.TITLE_SOURCE_USER


@pytest.mark.asyncio
async def test_auto_naming_rejected_on_legacy_named_thread(monkeypatch: pytest.MonkeyPatch):
    # 存量线程没有 title_source，但标题已不是初始值：视为已命名，自动写入必须被拒
    conversation = _conversation(title="旧标题", metadata={})
    repo, _ = await _run_update(
        monkeypatch,
        conversation,
        title="通过 MCP 查 Wx",
        metadata={"title_source": svc.TITLE_SOURCE_FALLBACK},
    )

    assert repo.update_kwargs["title"] is None
    assert repo.update_kwargs["metadata"] is None
    assert conversation.title == "旧标题"


@pytest.mark.asyncio
async def test_unknown_title_source_rejected_with_422(monkeypatch: pytest.MonkeyPatch):
    conversation = _conversation()
    repo = FakeConversationRepository(conversation)
    monkeypatch.setattr(svc, "ConversationRepository", lambda db: repo)

    with pytest.raises(HTTPException) as exc_info:
        await svc.update_thread_view(
            db=None,
            current_uid="u1",
            thread_id="thread-1",
            title="任意标题",
            metadata={"title_source": "SYSTEM"},
        )

    assert exc_info.value.status_code == 422
    assert repo.update_kwargs == {}


@pytest.mark.asyncio
async def test_metadata_without_title_passes_through(monkeypatch: pytest.MonkeyPatch):
    conversation = _conversation(metadata={"attachments": []})
    repo, response = await _run_update(monkeypatch, conversation, metadata={"custom_key": 1})

    assert repo.update_kwargs["title"] is None
    assert repo.update_kwargs["metadata"] == {"custom_key": 1}
    assert response["metadata"]["custom_key"] == 1
    assert response["metadata"]["attachments"] == []


@pytest.mark.asyncio
async def test_pin_only_update_does_not_touch_title(monkeypatch: pytest.MonkeyPatch):
    conversation = _conversation(title="已有标题", metadata={"title_source": svc.TITLE_SOURCE_USER})
    repo, response = await _run_update(monkeypatch, conversation, is_pinned=True)

    assert repo.update_kwargs["title"] is None
    assert repo.update_kwargs["metadata"] is None
    assert repo.update_kwargs["is_pinned"] is True
    assert response["is_pinned"] is True
    assert response["title"] == "已有标题"
