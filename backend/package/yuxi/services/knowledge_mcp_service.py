"""Read-only knowledge use cases shared by the public MCP transport."""

from __future__ import annotations

import inspect

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from yuxi.knowledge.schemas import FindInputSchema, OpenInputSchema, SearchInputSchema


class ListArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QueryArguments(SearchInputSchema):
    model_config = ConfigDict(extra="forbid")
    kb_id: str = Field(min_length=1, max_length=80)
    query_text: str = Field(min_length=1, max_length=16000)


class FileArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kb_id: str | None = Field(default=None, max_length=80)
    kb_name: str | None = Field(default=None, max_length=256)
    query: str | None = Field(default=None, max_length=512)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=1000)


TOOLS = {
    "list_kbs": (ListArguments, "列出当前身份可见的知识库 ID、名称和描述。"),
    "query_kb": (QueryArguments, "检索知识库正文片段；候选结果需通过原文核验，不能当作已验证科研证据。"),
    "search_file": (FileArguments, "按知识库 ID、名称或文件名查找文件；offset/limit 分页。"),
    "open_kb_document": (OpenInputSchema, "按知识库和文件 ID 阅读带行号的原文，使用 next_offset 继续。"),
    "find_kb_document": (FindInputSchema, "在指定文档内定位关键词并返回上下文窗口。"),
}


async def execute_knowledge_tool(db, user, name, arguments):
    from yuxi.agents.toolkits.kbs.tools import _build_query_output
    from yuxi.knowledge.runtime import knowledge_base
    from yuxi.repositories.knowledge_file_repository import KnowledgeFileRepository
    from yuxi.services.principal import resolve_principal
    from yuxi.storage.postgres.models_knowledge import KnowledgeFile

    if name not in TOOLS:
        raise ValueError("未知只读知识工具")
    parameters = TOOLS[name][0].model_validate(arguments)
    await resolve_principal(db, user)
    visible = (await knowledge_base.get_databases_by_uid(str(user.uid))).get("databases") or []
    by_id = {str(item["kb_id"]): item for item in visible}
    if name == "list_kbs":
        return [{key: item.get(key) for key in ("kb_id", "name", "description")} for item in visible]
    if name == "search_file":
        if not any((parameters.kb_id, parameters.kb_name, parameters.query)):
            raise ValueError("至少指定知识库或文件名关键词")
        ids = [kb_id for kb_id, item in by_id.items() if (parameters.kb_id is None or kb_id == parameters.kb_id) and (parameters.kb_name is None or item.get("name") == parameters.kb_name)]
        if (parameters.kb_id or parameters.kb_name) and not ids:
            raise HTTPException(404, "知识库不存在或无权访问")
        conditions = [KnowledgeFile.kb_id.in_(ids), KnowledgeFile.is_folder.is_(False)]
        if parameters.query:
            from yuxi.knowledge.evidence.verbatim import escape_like

            conditions.append(KnowledgeFile.filename.ilike("%" + escape_like(parameters.query) + "%", escape="/"))
        total = (await db.execute(select(func.count()).select_from(KnowledgeFile).where(*conditions))).scalar_one()
        files = (await db.execute(select(KnowledgeFile).where(*conditions).order_by(KnowledgeFile.updated_at.desc(), KnowledgeFile.id).offset(parameters.offset).limit(parameters.limit))).scalars().all()
        return {"files": [{"kb_id": item.kb_id, "file_id": item.file_id, "filename": item.filename, "file_type": item.file_type, "status": item.status} for item in files], "total": total, "offset": parameters.offset, "limit": parameters.limit, "has_more": parameters.offset + len(files) < total}
    if parameters.kb_id not in by_id:
        raise HTTPException(404, "知识库不存在或无权访问")
    if name == "query_kb":
        target = knowledge_base.get_retrievers().get(parameters.kb_id)
        if target is None:
            raise HTTPException(404, "知识库当前不可检索")
        retriever = target["retriever"]
        result = retriever(parameters.query_text, **({"file_name": parameters.file_name} if parameters.file_name else {}))
        if inspect.isawaitable(result):
            result = await result
        output = await _build_query_output(parameters.kb_id, result)
        output["verification"] = "retrieval_candidates"
        return output
    file = await KnowledgeFileRepository(db).get_by_file_id(parameters.file_id)
    if file is None or file.kb_id != parameters.kb_id or file.is_folder:
        raise HTTPException(404, "知识文件不存在或无权访问")
    if name == "open_kb_document":
        offset = parameters.line - 1 if parameters.line is not None else parameters.offset or 0
        result = await knowledge_base.open_file_content(parameters.kb_id, parameters.file_id, offset=offset, limit=parameters.window_size)
    else:
        if not parameters.patterns or len(parameters.patterns) > 20 or any(len(pattern) > 512 for pattern in parameters.patterns):
            raise ValueError("需提供 1–20 个不超过 512 字符的检索模式")
        result = await knowledge_base.find_file_content(parameters.kb_id, parameters.file_id, parameters.patterns, use_regex=parameters.use_regex, case_sensitive=parameters.case_sensitive, max_windows=parameters.max_windows, window_size=parameters.window_size)
    return {"kb_id": parameters.kb_id, "file_id": parameters.file_id, **result}
