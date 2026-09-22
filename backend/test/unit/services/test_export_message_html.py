"""单条回答 HTML 导出的行为契约：轮次抽取、文件名清洗、归属校验与审计。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup

from yuxi.services import conversation_export_service as export_module
from yuxi.services.conversation_export_service import (
    _pick_single_round,
    build_message_filename,
    export_message_html_view,
)
from yuxi.storage.postgres.models_business import User
from yuxi.utils.datetime_utils import utc_now_naive

pytestmark = [pytest.mark.unit]


def _msg(msg_id: int, msg_type: str, content: str = "") -> dict:
    return {"id": msg_id, "type": msg_type, "content": content, "created_at": "2026-09-22T08:30:00", "extra_metadata": {}}


def test_pick_single_round_takes_question_and_answer():
    history = [
        _msg(1, "human", "第一问"),
        _msg(2, "ai", "第一答"),
        _msg(3, "human", "第二问"),
        _msg(4, "tool", "工具消息不算轮次锚点"),
        _msg(5, "ai", "第二答"),
    ]
    picked = _pick_single_round(history, 5)
    assert picked is not None
    single, question_text = picked
    assert [msg["id"] for msg in single] == [3, 5]
    assert question_text == "第二问"


def test_pick_single_round_without_preceding_question():
    picked = _pick_single_round([_msg(9, "ai", "先导回答")], 9)
    assert picked is not None
    single, question_text = picked
    assert [msg["id"] for msg in single] == [9]
    assert question_text == ""


def test_pick_single_round_rejects_missing_or_non_ai_message():
    assert _pick_single_round([_msg(1, "ai")], 404) is None
    assert _pick_single_round([_msg(1, "human", "提问本身不可导出")], 1) is None


def test_message_filename_cleans_and_prefixes():
    from datetime import datetime

    name = build_message_filename("查询 Os06g0133000 的表达量/趋势?*", datetime(2026, 9, 22, 8, 0, 0))
    assert name.startswith("语析回答_查询 Os06g0133000 的表达量 趋势_")
    assert name.endswith(".html")
    assert "/" not in name and "?" not in name and "*" not in name
    assert build_message_filename("", datetime(2026, 9, 22, 8, 0, 0)).startswith("语析回答_未命名回答_")


class _FakeRepo:
    """按 ConversationRepository(db) 的实例化形态替换；conversation 属性可被子类覆盖。"""

    conversation: object = None

    def __init__(self, db):
        self.db = db

    async def get_conversation_by_thread_id(self, thread_id, uid=None):
        return type(self).conversation


def _conversation():
    return SimpleNamespace(
        uid="u1",
        status="active",
        title="胚乳问答",
        agent_id="default-chatbot",
        thread_id="th-1",
        created_at=utc_now_naive(),
        updated_at=utc_now_naive(),
    )


class _OwnedRepo(_FakeRepo):
    conversation = _conversation()


class _MissingRepo(_FakeRepo):
    conversation = None


def _user() -> User:
    return User(id=11, uid="u1", username="tester")


async def test_export_message_view_renders_single_round(monkeypatch):
    audit_calls: list[tuple] = []

    async def fake_log_operation(db, user_id, action, detail, request=None):
        audit_calls.append((user_id, action))

    monkeypatch.setattr(export_module, "ConversationRepository", _OwnedRepo)
    monkeypatch.setattr(export_module, "log_operation", fake_log_operation)

    async def fake_history_view(**kwargs):
        return {
            "history": [
                _msg(1, "human", "第一问"),
                _msg(2, "ai", "第一答"),
                _msg(3, "human", "目标提问"),
                _msg(4, "ai", "目标回答：GBSSI 受 **糖** 诱导"),
            ]
        }

    monkeypatch.setattr(export_module, "get_thread_history_view", fake_history_view)

    html, filename = await export_message_html_view(
        thread_id="th-1",
        message_id=4,
        current_user=_user(),
        db=None,
        request=None,
    )
    soup = BeautifulSoup(html, "html.parser")
    assert len(soup.select(".qa")) == 1
    assert "目标提问" in soup.select_one(".qa-q-text").get_text()
    assert "目标回答" in soup.select_one(".qa-a").get_text()
    assert "第一答" not in html
    assert filename.startswith("语析回答_目标提问_")
    assert audit_calls and audit_calls[0][1] == "导出单条回答HTML"


async def test_export_message_view_404_for_missing_message(monkeypatch):
    monkeypatch.setattr(export_module, "ConversationRepository", _OwnedRepo)

    async def fake_history_view(**kwargs):
        return {"history": [_msg(1, "human", "只有提问")]}

    monkeypatch.setattr(export_module, "get_thread_history_view", fake_history_view)

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        await export_message_html_view(
            thread_id="th-1",
            message_id=99,
            current_user=_user(),
            db=None,
            request=None,
        )
    assert excinfo.value.status_code == 404


async def test_export_message_view_404_for_foreign_thread(monkeypatch):
    monkeypatch.setattr(
        export_module,
        "ConversationRepository",
        _MissingRepo,
    )
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        await export_message_html_view(
            thread_id="th-other",
            message_id=1,
            current_user=_user(),
            db=None,
            request=None,
        )
    assert excinfo.value.status_code == 404
