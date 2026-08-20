from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.models_knowledge import (
    AgentKnowledgeScopeConfig,
    KnowledgeScope,
    KnowledgeScopeAudit,
    KnowledgeScopeMember,
)
from yuxi.utils.datetime_utils import utc_now_naive

DEFAULT_QA_SCOPE_ID = "scope_default_qa"
DEFAULT_QA_SCOPE_SLUG = "default-qa"


class ScopeVersionConflictError(ValueError):
    def __init__(self, current_version: int):
        super().__init__(f"知识范围已被其他用户更新，当前版本为 {current_version}")
        self.current_version = current_version


def serialize_scope(scope: KnowledgeScope) -> dict[str, Any]:
    return {
        "scope_id": scope.scope_id,
        "slug": scope.slug,
        "name": scope.name,
        "description": scope.description,
        "retrieval_mode": scope.retrieval_mode,
        "allow_web": bool(scope.allow_web),
        "version": int(scope.version or 1),
        "created_by": scope.created_by,
        "updated_by": scope.updated_by,
        "created_at": scope.created_at.isoformat() if scope.created_at else None,
        "updated_at": scope.updated_at.isoformat() if scope.updated_at else None,
    }


def serialize_member(member: KnowledgeScopeMember) -> dict[str, Any]:
    return {
        "kb_id": member.kb_id,
        "enabled": bool(member.enabled),
        "document_enabled": bool(member.document_enabled),
        "graph_enabled": bool(member.graph_enabled),
        "structured_enabled": bool(member.structured_enabled),
        "evidence_strict": bool(member.evidence_strict),
        "evidence_supporting": bool(member.evidence_supporting),
        "evidence_candidate": bool(member.evidence_candidate),
        "evidence_rejected": bool(member.evidence_rejected),
        "priority": int(member.priority or 100),
        "health_status": member.health_status or "VALIDATING",
        "health_details": member.health_details or {},
        "last_validated_at": member.last_validated_at.isoformat() if member.last_validated_at else None,
        "created_by": member.created_by,
        "updated_by": member.updated_by,
        "created_at": member.created_at.isoformat() if member.created_at else None,
        "updated_at": member.updated_at.isoformat() if member.updated_at else None,
    }


def serialize_audit(audit: KnowledgeScopeAudit) -> dict[str, Any]:
    return {
        "audit_id": audit.audit_id,
        "scope_id": audit.scope_id,
        "action": audit.action,
        "old_version": int(audit.old_version),
        "new_version": int(audit.new_version),
        "before": audit.before_json,
        "after": audit.after_json,
        "updated_by": audit.updated_by,
        "created_at": audit.created_at.isoformat() if audit.created_at else None,
    }


class KnowledgeScopeRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def ensure_default_scope(self, *, actor_uid: str | None = None) -> KnowledgeScope:
        result = await self.db.execute(select(KnowledgeScope).where(KnowledgeScope.scope_id == DEFAULT_QA_SCOPE_ID))
        scope = result.scalar_one_or_none()
        if scope:
            return scope
        scope = KnowledgeScope(
            scope_id=DEFAULT_QA_SCOPE_ID,
            slug=DEFAULT_QA_SCOPE_SLUG,
            name="默认问答范围",
            description="由管理员显式纳入、供继承该范围的智能体使用的知识源集合。",
            retrieval_mode="KB_ONLY",
            allow_web=False,
            version=1,
            created_by=actor_uid,
            updated_by=actor_uid,
        )
        self.db.add(scope)
        await self.db.flush()
        return scope

    async def get_scope(self, scope_id: str, *, for_update: bool = False) -> KnowledgeScope | None:
        stmt = select(KnowledgeScope).where(KnowledgeScope.scope_id == scope_id)
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def list_members(self, scope_id: str, *, enabled_only: bool = False) -> list[KnowledgeScopeMember]:
        stmt = select(KnowledgeScopeMember).where(KnowledgeScopeMember.scope_id == scope_id)
        if enabled_only:
            stmt = stmt.where(KnowledgeScopeMember.enabled.is_(True))
        result = await self.db.execute(
            stmt.order_by(KnowledgeScopeMember.priority.asc(), KnowledgeScopeMember.id.asc())
        )
        return list(result.scalars().all())

    async def list_audits(
        self,
        scope_id: str,
        *,
        limit: int = 100,
        max_version: int | None = None,
        ascending: bool = False,
    ) -> list[KnowledgeScopeAudit]:
        stmt = select(KnowledgeScopeAudit).where(KnowledgeScopeAudit.scope_id == scope_id)
        if max_version is not None:
            stmt = stmt.where(KnowledgeScopeAudit.new_version <= max_version)
        ordering = KnowledgeScopeAudit.new_version.asc() if ascending else KnowledgeScopeAudit.new_version.desc()
        result = await self.db.execute(stmt.order_by(ordering).limit(min(max(limit, 1), 500)))
        return list(result.scalars().all())

    async def get_member(self, scope_id: str, kb_id: str) -> KnowledgeScopeMember | None:
        result = await self.db.execute(
            select(KnowledgeScopeMember).where(
                KnowledgeScopeMember.scope_id == scope_id,
                KnowledgeScopeMember.kb_id == kb_id,
            )
        )
        return result.scalar_one_or_none()

    async def upsert_member(
        self,
        *,
        scope_id: str,
        kb_id: str,
        values: dict[str, Any],
        expected_version: int,
        actor_uid: str,
    ) -> tuple[KnowledgeScope, KnowledgeScopeMember]:
        scope = await self.get_scope(scope_id, for_update=True)
        if not scope:
            raise LookupError("知识范围不存在")
        current_version = int(scope.version or 1)
        if current_version != expected_version:
            raise ScopeVersionConflictError(current_version)

        member = await self.get_member(scope_id, kb_id)
        before = serialize_member(member) if member else None
        now = utc_now_naive()
        if member is None:
            member = KnowledgeScopeMember(
                scope_id=scope_id,
                kb_id=kb_id,
                created_by=actor_uid,
                created_at=now,
            )
            self.db.add(member)

        for key, value in values.items():
            if hasattr(member, key):
                setattr(member, key, value)
        member.updated_by = actor_uid
        member.updated_at = now
        await self.db.flush()

        scope.version = current_version + 1
        scope.updated_by = actor_uid
        scope.updated_at = now
        after = serialize_member(member)
        self.db.add(
            KnowledgeScopeAudit(
                audit_id=f"ksa_{uuid.uuid4().hex}",
                scope_id=scope_id,
                action="UPSERT_MEMBER",
                old_version=current_version,
                new_version=scope.version,
                before_json=before,
                after_json=after,
                updated_by=actor_uid,
                created_at=now,
            )
        )
        await self.db.flush()
        return scope, member

    async def get_agent_config(self, agent_slug: str) -> AgentKnowledgeScopeConfig | None:
        result = await self.db.execute(
            select(AgentKnowledgeScopeConfig).where(AgentKnowledgeScopeConfig.agent_slug == agent_slug)
        )
        return result.scalar_one_or_none()

    async def ensure_agent_config(
        self,
        *,
        agent_slug: str,
        scope_mode: str,
        actor_uid: str | None = None,
    ) -> AgentKnowledgeScopeConfig:
        config = await self.get_agent_config(agent_slug)
        if config:
            return config
        await self.ensure_default_scope(actor_uid=actor_uid)
        config = AgentKnowledgeScopeConfig(
            agent_slug=agent_slug,
            scope_id=DEFAULT_QA_SCOPE_ID,
            scope_mode=scope_mode,
            created_by=actor_uid,
            updated_by=actor_uid,
        )
        self.db.add(config)
        await self.db.flush()
        return config

    async def update_agent_config(
        self,
        *,
        agent_slug: str,
        scope_mode: str,
        knowledge_strategy: str,
        retrieval_mode: str | None,
        retrieval_policy: dict[str, Any] | None,
        allow_web: bool | None,
        actor_uid: str,
    ) -> AgentKnowledgeScopeConfig:
        config = await self.ensure_agent_config(
            agent_slug=agent_slug,
            scope_mode="INHERIT_GLOBAL",
            actor_uid=actor_uid,
        )
        config.scope_mode = scope_mode
        config.knowledge_strategy = knowledge_strategy
        config.retrieval_mode = retrieval_mode
        config.retrieval_policy = retrieval_policy or {}
        config.allow_web = allow_web
        config.updated_by = actor_uid
        config.updated_at = utc_now_naive()
        await self.db.flush()
        return config
