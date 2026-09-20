from __future__ import annotations

from typing import Any

from sqlalchemy import select

from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import KnowledgeBase


class KnowledgeBaseRepository:
    async def get_all(self) -> list[KnowledgeBase]:
        async with pg_manager.get_async_session_context() as session:
            result = await session.execute(select(KnowledgeBase))
            return list(result.scalars().all())

    async def get_by_kb_id(self, kb_id: str) -> KnowledgeBase | None:
        async with pg_manager.get_async_session_context() as session:
            result = await session.execute(select(KnowledgeBase).where(KnowledgeBase.kb_id == kb_id))
            return result.scalar_one_or_none()

    async def get_by_idempotency_key(self, creation_idempotency_key: str) -> KnowledgeBase | None:
        """幂等重放查询：同 Idempotency-Key 的建库请求返回首次结果，不重复创建。"""
        if not creation_idempotency_key:
            return None
        async with pg_manager.get_async_session_context() as session:
            result = await session.execute(
                select(KnowledgeBase).where(KnowledgeBase.creation_idempotency_key == creation_idempotency_key)
            )
            return result.scalar_one_or_none()

    async def get_by_tenant_normalized_name(self, tenant_id: int | None, normalized_name: str) -> KnowledgeBase | None:
        """名称唯一约束的读侧查询（应用层友好提示用；硬约束在数据库 0054 唯一索引）。"""
        async with pg_manager.get_async_session_context() as session:
            result = await session.execute(
                select(KnowledgeBase).where(
                    KnowledgeBase.normalized_name == normalized_name,
                    KnowledgeBase.tenant_id == tenant_id
                    if tenant_id is not None
                    else KnowledgeBase.tenant_id.is_(None),
                )
            )
            return result.scalar_one_or_none()

    async def create(self, data: dict[str, Any]) -> KnowledgeBase:
        from yuxi.services.principal import resolve_tenant_id

        async with pg_manager.get_async_session_context() as session:
            tenant_id = await resolve_tenant_id(session, str(data.get("created_by") or ""))
        kb = KnowledgeBase(**data, tenant_id=tenant_id)
        async with pg_manager.get_async_session_context() as session:
            session.add(kb)
        return kb

    async def update(self, kb_id: str, data: dict[str, Any]) -> KnowledgeBase | None:
        async with pg_manager.get_async_session_context() as session:
            result = await session.execute(select(KnowledgeBase).where(KnowledgeBase.kb_id == kb_id))
            kb = result.scalar_one_or_none()
            if kb is None:
                return None
            for key, value in data.items():
                setattr(kb, key, value)
        return kb

    async def delete(self, kb_id: str) -> None:
        async with pg_manager.get_async_session_context() as session:
            result = await session.execute(select(KnowledgeBase).where(KnowledgeBase.kb_id == kb_id))
            kb = result.scalar_one_or_none()
            if kb is not None:
                await session.delete(kb)
