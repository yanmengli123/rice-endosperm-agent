from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from server.utils.auth_middleware import get_db, get_required_user
from sqlalchemy.ext.asyncio import AsyncSession
from yuxi.repositories.conversation_repository import ConversationRepository
from yuxi.services.mention_search_service import search_mention_files_in_index
from yuxi.storage.postgres.models_business import User

mention_router = APIRouter(prefix="/mention", tags=["mention"])


class MentionFileItem(BaseModel):
    """提及文件搜索结果条目"""

    name: str
    path: str
    is_dir: bool
    source: str


class MentionDocumentItem(BaseModel):
    """@doc 提及候选（知识库文档）：只含文档身份，不带页码/图片"""

    file_id: str
    kb_id: str
    filename: str
    figure_ready: bool = False


@mention_router.get("/documents", response_model=list[MentionDocumentItem])
async def search_mention_documents(
    query: str = Query("", description="文件名模糊搜索关键字"),
    kb_ids: str = Query("", description="逗号分隔的知识库 ID；为空时搜索用户可访问的全部知识库"),
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """@doc 提及候选：用户可访问知识库范围内的文档（确定性 ilike 过滤），供前端插入
    ``@doc:"<file_id>"`` 提及——文献作用域解析器把它转成定位硬约束。"""
    from sqlalchemy import func, select
    from yuxi.knowledge.evidence.verbatim import escape_like
    from yuxi.knowledge.runtime import knowledge_base
    from yuxi.storage.postgres.models_knowledge import KnowledgeFile, KnowledgeParseRevision

    accessible = await knowledge_base.get_databases_by_user(current_user)
    accessible_ids = {str(item.get("kb_id") or "") for item in accessible.get("databases") or [] if item.get("kb_id")}
    requested = [item.strip() for item in kb_ids.split(",") if item.strip()] if kb_ids else []
    scoped = [item for item in requested if item in accessible_ids] or sorted(accessible_ids)
    if not scoped:
        return []

    conditions = [
        KnowledgeFile.kb_id.in_(scoped[:20]),
        func.coalesce(KnowledgeFile.is_folder, False) == False,  # noqa: E712 - SQL 布尔比较
    ]
    keyword = str(query or "").strip()
    if keyword:
        conditions.append(KnowledgeFile.filename.ilike(f"%{escape_like(keyword)}%", escape="/"))
    rows = (
        await db.execute(
            select(
                KnowledgeFile.file_id,
                KnowledgeFile.kb_id,
                KnowledgeFile.filename,
                KnowledgeParseRevision.qa_report,
            )
            .outerjoin(
                KnowledgeParseRevision,
                KnowledgeParseRevision.revision_id == KnowledgeFile.active_parse_revision_id,
            )
            .where(*conditions)
            .order_by(KnowledgeFile.filename)
            .limit(20)
        )
    ).all()

    items: list[MentionDocumentItem] = []
    for file_id, kb_id, filename, qa_report in rows:
        figure_index = (qa_report or {}).get("figure_index") if isinstance(qa_report, dict) else None
        try:
            locator_ready = int((figure_index or {}).get("locator_ready_assets") or 0)
        except (TypeError, ValueError):
            locator_ready = 0
        items.append(
            MentionDocumentItem(
                file_id=str(file_id),
                kb_id=str(kb_id or ""),
                filename=str(filename or ""),
                figure_ready=locator_ready > 0,
            )
        )
    return items


@mention_router.get("/search", response_model=list[MentionFileItem])
async def search_mention_files(
    thread_id: str | None = Query(None, description="当前聊天会话 ID；为空时仅搜索用户工作区"),
    query: str = Query("", description="模糊搜索关键字"),
    sources: str | None = Query(None, description="搜索来源：workspace,thread；为空时自动选择"),
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """
    提及文件模糊搜索接口：未创建 thread 时只搜索用户 workspace；已有 thread 时可搜索当前对话文件。
    """
    uid = str(current_user.uid)
    effective_thread_id: str | None = None

    if thread_id:
        conv_repo = ConversationRepository(db)
        conversation = await conv_repo.get_conversation_by_thread_id(thread_id)
        if conversation:
            if conversation.uid != uid or conversation.status == "deleted":
                raise HTTPException(status_code=404, detail="对话线程不存在")
            effective_thread_id = thread_id
        else:
            try:
                from yuxi.agents.backends.sandbox.paths import validate_thread_id

                validate_thread_id(thread_id)
            except ValueError:
                raise HTTPException(status_code=400, detail="非法的 thread_id 格式")

    source_list = [item.strip() for item in sources.split(",")] if sources else None
    return await search_mention_files_in_index(
        thread_id=effective_thread_id,
        uid=uid,
        query=query,
        sources=source_list,
    )
