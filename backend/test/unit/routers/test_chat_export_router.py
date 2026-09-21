"""会话 HTML 导出端点级单测：SQLite 内存库直调路由函数，覆盖 200/404 与审计写入。"""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.routers.chat_router import export_thread_html
from yuxi.storage.postgres.models_business import Base, Conversation, Department, Message, OperationLog, User

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


@pytest_asyncio.fixture()
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        department = Department(name="Chat Export Dept", tenant_id=1)
        owner = User(
            username="导出用户",
            uid="export_owner",
            password_hash="$argon2id$placeholder",
            role="user",
            department=department,
        )
        db.add_all([department, owner])
        await db.commit()
        await db.refresh(owner)
        yield db, owner
    await engine.dispose()


async def _seed_conversation(db, *, uid: str = "export_owner", status: str = "active") -> Conversation:
    conversation = Conversation(
        thread_id="thread-export-1",
        uid=uid,
        agent_id="sci-agent",
        title="导出 <测试>",
        status=status,
        tenant_id=1,
    )
    db.add(conversation)
    await db.flush()
    db.add_all(
        [
            Message(conversation_id=conversation.id, role="user", content="什么是Ⅰ型胚乳？"),
            Message(
                conversation_id=conversation.id,
                role="assistant",
                content="**Ⅰ型胚乳**是细胞化游离核类型。\n\n```python\nprint('x')\n```",
            ),
        ]
    )
    await db.commit()
    return conversation


async def test_export_thread_returns_html_response_and_writes_audit(session):
    db, owner = session
    await _seed_conversation(db)

    response = await export_thread_html("thread-export-1", None, current_user=owner, db=db)

    assert response.status_code == 200
    assert response.media_type == "text/html; charset=utf-8"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment; filename*=UTF-8''")

    html = response.body.decode("utf-8")
    assert html.startswith("<!DOCTYPE html>")
    assert "导出 &lt;测试&gt;" in html  # 标题转义
    assert "什么是Ⅰ型胚乳？" in html
    assert "code-lang" in html
    assert "%E8%AF%AD%E6%9E%90%E5%AF%B9%E8%AF%9D" in disposition  # 语析对话_… URL 编码文件名
    assert "<script" not in html

    audit = (await db.execute(select(OperationLog))).scalars().all()
    assert len(audit) == 1
    assert audit[0].operation == "导出会话HTML"
    assert "thread-export-1" in audit[0].details


async def test_export_thread_404_for_missing_thread(session):
    db, owner = session

    with pytest.raises(HTTPException) as exc_info:
        await export_thread_html("thread-missing", None, current_user=owner, db=db)
    assert exc_info.value.status_code == 404


async def test_export_thread_404_for_foreign_owner(session):
    db, owner = session
    await _seed_conversation(db, uid="someone_else")

    with pytest.raises(HTTPException) as exc_info:
        await export_thread_html("thread-export-1", None, current_user=owner, db=db)
    assert exc_info.value.status_code == 404


async def test_export_thread_404_for_deleted_conversation(session):
    db, owner = session
    await _seed_conversation(db, status="deleted")

    with pytest.raises(HTTPException) as exc_info:
        await export_thread_html("thread-export-1", None, current_user=owner, db=db)
    assert exc_info.value.status_code == 404
