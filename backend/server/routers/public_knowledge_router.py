"""Read-only knowledge tools using the same APP and user authority as Sessions."""

from typing import Literal

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from server.utils.auth_middleware import get_db
from server.utils.public_auth import authenticate_public_request
from yuxi.services.knowledge_mcp_service import execute_knowledge_tool
from yuxi.storage.postgres.models_business import User

public_knowledge = APIRouter(prefix="/v1/knowledge/tools", tags=["Public Knowledge"])


@public_knowledge.get("/list_kbs")
async def list_kbs(actor=Depends(authenticate_public_request), db=Depends(get_db)):
    user = await db.scalar(select(User).where(User.uid == actor.uid))
    return await execute_knowledge_tool(db, user, "list_kbs", {})


@public_knowledge.post("/{name}")
async def execute(
    name: Literal["search_file", "query_kb", "open_kb_document", "find_kb_document"],
    arguments: dict = Body(...),
    actor=Depends(authenticate_public_request),
    db=Depends(get_db),
):
    user = await db.scalar(select(User).where(User.uid == actor.uid))
    try:
        return await execute_knowledge_tool(db, user, name, arguments)
    except ValidationError as error:
        raise HTTPException(422, error.errors(include_url=False, include_context=False)) from None
