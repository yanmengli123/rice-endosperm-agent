"""自定义工具数据访问层：纯查询边界，调用方负责事务提交。

跟随 ``agents/mcp/credentials.py`` 的函数式仓储先例：模块级 async 函数 +
显式传入 ``AsyncSession``；运行时装配（无请求级会话）由 service 层
经 ``pg_manager.get_async_session_context()`` 自管会话后调用本层。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.models_business import Agent, CustomTool


async def get_by_slug(db: AsyncSession, *, tenant_id: int, slug: str) -> CustomTool | None:
    result = await db.execute(select(CustomTool).where(CustomTool.tenant_id == tenant_id, CustomTool.slug == slug))
    return result.scalar_one_or_none()


async def list_for_tenant(db: AsyncSession, *, tenant_id: int) -> list[CustomTool]:
    result = await db.execute(select(CustomTool).where(CustomTool.tenant_id == tenant_id).order_by(CustomTool.id))
    return list(result.scalars().all())


async def list_runtime_ready(db: AsyncSession, *, tenant_id: int, slugs: list[str] | None = None) -> list[CustomTool]:
    """READY + enabled 的可装配定义；slugs 为空直接短路，避免空轮询。"""
    if not slugs:
        return []
    stmt = select(CustomTool).where(
        CustomTool.tenant_id == tenant_id,
        CustomTool.lifecycle_status == "READY",
        CustomTool.enabled.is_(True),
        CustomTool.slug.in_(slugs),
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def slug_exists(db: AsyncSession, *, tenant_id: int, slug: str) -> bool:
    result = await db.execute(select(CustomTool.id).where(CustomTool.tenant_id == tenant_id, CustomTool.slug == slug))
    return result.scalar_one_or_none() is not None


async def find_agent_references(db: AsyncSession, *, tenant_id: int, slug: str) -> list[str]:
    """引用了该工具的智能体 slug 列表（config_json["context"]["tools"]）。"""
    result = await db.execute(select(Agent).where(Agent.tenant_id == tenant_id))
    referenced: list[str] = []
    for agent in result.scalars().all():
        context = (agent.config_json or {}).get("context") or {}
        tools = context.get("tools")
        if isinstance(tools, list) and slug in tools:
            referenced.append(agent.slug)
    return referenced


async def hard_delete(db: AsyncSession, tool: CustomTool) -> None:
    await db.delete(tool)


__all__ = [
    "find_agent_references",
    "get_by_slug",
    "hard_delete",
    "list_for_tenant",
    "list_runtime_ready",
    "slug_exists",
]
