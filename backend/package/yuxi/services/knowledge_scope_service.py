from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from yuxi.knowledge.runtime import knowledge_base
from yuxi.repositories.knowledge_scope_repository import (
    DEFAULT_QA_SCOPE_ID,
    KnowledgeScopeRepository,
    serialize_audit,
    serialize_member,
    serialize_scope,
)
from yuxi.storage.postgres.models_business import Agent, User
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeFile,
    KnowledgeGraphEntity,
    KnowledgeGraphRelationEvidence,
    KnowledgeGraphTriple,
)
from yuxi.utils.datetime_utils import utc_now_naive

SCOPE_MODES = {"LEGACY", "INHERIT_GLOBAL", "CUSTOM", "GLOBAL_PLUS_CUSTOM", "DISABLED"}
RETRIEVAL_MODES = {"KB_ONLY", "KB_PLUS_WEB"}
KNOWLEDGE_STRATEGIES = {"KNOWLEDGE_FIRST", "MODEL_DECIDES", "DISABLED"}
HEALTH_STATUSES = {"HEALTHY", "DEGRADED", "UNAVAILABLE", "VALIDATING"}

DEFAULT_RETRIEVAL_POLICY = {
    "exact_first": True,
    "enumeration_exhaustive": True,
    "narrative_evidence_limit": 10,
    "display_limit": 20,
    "allow_secondary_retrieval": True,
}

DEFAULT_MEMBER_POLICY = {
    "enabled": True,
    "document_enabled": True,
    "graph_enabled": True,
    "structured_enabled": True,
    "evidence_strict": True,
    "evidence_supporting": True,
    "evidence_candidate": False,
    "evidence_rejected": False,
    "priority": 100,
    "health_status": "VALIDATING",
    "health_details": {},
    "last_validated_at": None,
}


def replay_scope_member_audits(audits: list[dict[str, Any]], *, target_version: int) -> list[dict[str, Any]]:
    """从不可变审计记录重建指定版本的成员策略，用于回答复现与调试。"""
    members: dict[str, dict[str, Any]] = {}
    for audit in sorted(audits, key=lambda item: int(item.get("new_version") or 0)):
        if int(audit.get("new_version") or 0) > target_version:
            break
        after = audit.get("after")
        if not isinstance(after, dict) or not after.get("kb_id"):
            continue
        members[str(after["kb_id"])] = dict(after)
    return sorted(members.values(), key=lambda item: (int(item.get("priority") or 100), str(item.get("kb_id"))))


async def get_default_scope_history(*, db: AsyncSession, limit: int = 100) -> dict[str, Any]:
    repo = KnowledgeScopeRepository(db)
    scope = await repo.ensure_default_scope()
    audits = [serialize_audit(item) for item in await repo.list_audits(scope.scope_id, limit=limit)]
    return {"scope": serialize_scope(scope), "audits": audits}


async def replay_default_scope_version(*, db: AsyncSession, version: int) -> dict[str, Any]:
    repo = KnowledgeScopeRepository(db)
    scope = await repo.ensure_default_scope()
    current_version = int(scope.version or 1)
    if version < 1 or version > current_version:
        raise LookupError(f"知识范围版本必须在 1 到 {current_version} 之间")
    serialized_audits = [
        serialize_audit(item)
        for item in await repo.list_audits(
            scope.scope_id,
            limit=500,
            max_version=version,
            ascending=True,
        )
    ]
    return {
        "scope_id": scope.scope_id,
        "scope_slug": scope.slug,
        "scope_version": version,
        "current_version": current_version,
        "members": replay_scope_member_audits(serialized_audits, target_version=version),
        "audit_count": len(serialized_audits),
    }


def _serialize_agent_scope_config(config, *, fallback_mode: str) -> dict[str, Any]:
    return {
        "agent_slug": getattr(config, "agent_slug", None),
        "scope_id": getattr(config, "scope_id", None) or DEFAULT_QA_SCOPE_ID,
        "scope_mode": getattr(config, "scope_mode", None) or fallback_mode,
        "knowledge_strategy": getattr(config, "knowledge_strategy", None)
        or ("KNOWLEDGE_FIRST" if fallback_mode == "INHERIT_GLOBAL" else "MODEL_DECIDES"),
        "retrieval_mode": getattr(config, "retrieval_mode", None),
        "retrieval_policy": getattr(config, "retrieval_policy", None) or dict(DEFAULT_RETRIEVAL_POLICY),
        "allow_web": getattr(config, "allow_web", None),
        "created_at": config.created_at.isoformat() if config and config.created_at else None,
        "updated_at": config.updated_at.isoformat() if config and config.updated_at else None,
    }


def _legacy_knowledge_ids(agent: Agent, accessible_ids: set[str]) -> set[str]:
    raw_context = (agent.config_json or {}).get("context") or {}
    raw_ids = raw_context.get("knowledges")
    if raw_ids is None:
        return set(accessible_ids)
    if not isinstance(raw_ids, list):
        return set()
    return {str(value).strip() for value in raw_ids if str(value).strip()}


def compute_effective_scope_ids(
    *,
    mode: str,
    accessible_ids: set[str],
    global_ids: set[str],
    custom_ids: set[str],
    session_kb_ids: set[str] | None,
) -> set[str]:
    """执行经过评审的集合代数；SessionNarrowing 只能做交集。"""
    if mode == "INHERIT_GLOBAL":
        base_ids = global_ids
    elif mode == "CUSTOM":
        base_ids = custom_ids
    elif mode == "GLOBAL_PLUS_CUSTOM":
        base_ids = global_ids | custom_ids
    elif mode == "DISABLED":
        base_ids = set()
    else:
        base_ids = custom_ids
    effective_ids = accessible_ids & base_ids
    if session_kb_ids is not None:
        effective_ids &= session_kb_ids
    return effective_ids


def _default_policy(kb_id: str, kb_info: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"kb_id": kb_id, "kb_name": (kb_info or {}).get("name") or kb_id, **DEFAULT_MEMBER_POLICY}


async def _accessible_knowledge_bases(user: User) -> list[dict[str, Any]]:
    result = await knowledge_base.get_databases_by_user(user)
    return [item for item in (result.get("databases") or []) if isinstance(item, dict) and item.get("kb_id")]


async def resolve_effective_knowledge_scope(
    *,
    db: AsyncSession,
    user: User,
    agent_slug: str,
    session_kb_ids: list[str] | None = None,
) -> dict[str, Any]:
    """唯一的知识范围解析器。

    Effective = Accessible ∩ Base ∩ SessionNarrowing。会话层只能缩小，永远不能扩大范围。
    """
    repo = KnowledgeScopeRepository(db)
    scope = await repo.ensure_default_scope(actor_uid=str(user.uid))
    agent = (await db.execute(select(Agent).where(Agent.slug == agent_slug))).scalar_one_or_none()
    if not agent:
        raise LookupError("智能体不存在")

    accessible = await _accessible_knowledge_bases(user)
    accessible_by_id = {str(item["kb_id"]): item for item in accessible}
    accessible_ids = set(accessible_by_id)
    members = await repo.list_members(scope.scope_id)
    enabled_members = {member.kb_id: member for member in members if member.enabled}
    global_ids = set(enabled_members)
    custom_ids = _legacy_knowledge_ids(agent, accessible_ids)

    config = await repo.get_agent_config(agent_slug)
    fallback_mode = "INHERIT_GLOBAL" if agent_slug == "default-chatbot" else "LEGACY"
    mode = str(getattr(config, "scope_mode", None) or fallback_mode).upper()
    if mode not in SCOPE_MODES:
        mode = fallback_mode

    fallback_strategy = "KNOWLEDGE_FIRST" if agent_slug == "default-chatbot" else "MODEL_DECIDES"
    knowledge_strategy = str(getattr(config, "knowledge_strategy", None) or fallback_strategy).upper()
    if knowledge_strategy not in KNOWLEDGE_STRATEGIES:
        knowledge_strategy = fallback_strategy
    if mode == "DISABLED":
        knowledge_strategy = "DISABLED"
    retrieval_policy = dict(DEFAULT_RETRIEVAL_POLICY)
    configured_policy = getattr(config, "retrieval_policy", None)
    if isinstance(configured_policy, dict):
        retrieval_policy.update(configured_policy)

    if mode == "INHERIT_GLOBAL":
        base_ids = global_ids
    elif mode == "CUSTOM":
        base_ids = custom_ids
    elif mode == "GLOBAL_PLUS_CUSTOM":
        base_ids = global_ids | custom_ids
    elif mode == "DISABLED":
        base_ids = set()
    else:
        base_ids = custom_ids

    narrowed_ids = None
    if session_kb_ids is not None:
        narrowed_ids = {str(value).strip() for value in session_kb_ids if str(value).strip()}
    effective_ids = compute_effective_scope_ids(
        mode=mode,
        accessible_ids=accessible_ids,
        global_ids=global_ids,
        custom_ids=custom_ids,
        session_kb_ids=narrowed_ids,
    )

    effective_members: list[dict[str, Any]] = []
    for kb_id in effective_ids:
        member = enabled_members.get(kb_id)
        policy = serialize_member(member) if member else _default_policy(kb_id, accessible_by_id[kb_id])
        policy["kb_id"] = kb_id
        policy["kb_name"] = accessible_by_id[kb_id].get("name") or kb_id
        policy["kb_type"] = accessible_by_id[kb_id].get("kb_type")
        policy["included_via"] = "GLOBAL" if kb_id in global_ids else "CUSTOM"
        effective_members.append(policy)
    effective_members.sort(key=lambda item: (int(item.get("priority") or 100), item["kb_id"]))

    retrieval_mode = str(getattr(config, "retrieval_mode", None) or scope.retrieval_mode or "KB_ONLY").upper()
    if retrieval_mode not in RETRIEVAL_MODES:
        retrieval_mode = "KB_ONLY"
    configured_allow_web = getattr(config, "allow_web", None)
    allow_web = bool(scope.allow_web if configured_allow_web is None else configured_allow_web)
    if retrieval_mode == "KB_ONLY":
        allow_web = False

    filtered_out = []
    for kb_id in sorted(base_ids - accessible_ids):
        filtered_out.append({"kb_id": kb_id, "reason": "NO_ACCESS"})
    if narrowed_ids is not None:
        for kb_id in sorted((accessible_ids & base_ids) - narrowed_ids):
            filtered_out.append({"kb_id": kb_id, "reason": "SESSION_NARROWED"})

    return {
        "scope_id": scope.scope_id,
        "scope_slug": scope.slug,
        "scope_version": int(scope.version or 1),
        "scope_mode": mode,
        "source": "AGENT_RUN_SNAPSHOT",
        "authoritative_for_this_run": True,
        "agent_slug": agent_slug,
        "knowledge_strategy": knowledge_strategy,
        "retrieval_mode": retrieval_mode,
        "retrieval_policy": retrieval_policy,
        "allow_web": allow_web,
        "effective_kb_ids": [item["kb_id"] for item in effective_members],
        "members": effective_members,
        "filtered_out": filtered_out,
        "resolved_at": utc_now_naive().isoformat(),
    }


async def validate_member_health(
    *,
    db: AsyncSession,
    kb_id: str,
    policy: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    kb = (await db.execute(select(KnowledgeBase).where(KnowledgeBase.kb_id == kb_id))).scalar_one_or_none()
    if not kb:
        raise LookupError("知识库不存在")

    async def count(model, *conditions) -> int:
        return int((await db.execute(select(func.count()).select_from(model).where(*conditions))).scalar_one() or 0)

    file_count = await count(KnowledgeFile, KnowledgeFile.kb_id == kb_id, KnowledgeFile.is_folder.is_(False))
    chunk_count = await count(KnowledgeChunk, KnowledgeChunk.kb_id == kb_id)
    entity_count = await count(KnowledgeGraphEntity, KnowledgeGraphEntity.kb_id == kb_id)
    triple_count = await count(KnowledgeGraphTriple, KnowledgeGraphTriple.kb_id == kb_id)
    evidence_count = await count(KnowledgeGraphRelationEvidence, KnowledgeGraphRelationEvidence.kb_id == kb_id)

    channel_health = {
        "document": (not policy.get("document_enabled")) or chunk_count > 0 or str(kb.kb_type).lower() == "dify",
        "graph": (not policy.get("graph_enabled")) or entity_count > 0 or triple_count > 0,
        "structured": (not policy.get("structured_enabled")) or evidence_count > 0,
    }
    enabled_channels = [
        name
        for name, flag in (
            ("document", policy.get("document_enabled")),
            ("graph", policy.get("graph_enabled")),
            ("structured", policy.get("structured_enabled")),
        )
        if flag
    ]
    ready_channels = [name for name in enabled_channels if channel_health[name]]
    if not enabled_channels or not ready_channels:
        status = "UNAVAILABLE"
    elif len(ready_channels) == len(enabled_channels):
        status = "HEALTHY"
    else:
        status = "DEGRADED"

    details = {
        "kb_type": kb.kb_type,
        "files": file_count,
        "chunks": chunk_count,
        "entities": entity_count,
        "triples": triple_count,
        "evidence": evidence_count,
        "channels": {
            name: {"enabled": name in enabled_channels, "ready": channel_health[name]} for name in channel_health
        },
    }
    return status, details


async def get_default_scope_view(*, db: AsyncSession, user: User) -> dict[str, Any]:
    repo = KnowledgeScopeRepository(db)
    scope = await repo.ensure_default_scope(actor_uid=str(user.uid))
    members = {member.kb_id: member for member in await repo.list_members(scope.scope_id)}
    databases = await _accessible_knowledge_bases(user)
    items = []
    for kb in databases:
        kb_id = str(kb["kb_id"])
        member = members.get(kb_id)
        item = serialize_member(member) if member else {"kb_id": kb_id, **DEFAULT_MEMBER_POLICY, "enabled": False}
        item.update(
            {
                "kb_id": kb_id,
                "name": kb.get("name") or kb_id,
                "description": kb.get("description") or "",
                "kb_type": kb.get("kb_type"),
            }
        )
        items.append(item)
    return {"scope": serialize_scope(scope), "members": items}


async def update_default_scope_member(
    *,
    db: AsyncSession,
    user: User,
    kb_id: str,
    expected_version: int,
    values: dict[str, Any],
) -> dict[str, Any]:
    accessible_ids = {str(item["kb_id"]) for item in await _accessible_knowledge_bases(user)}
    if kb_id not in accessible_ids:
        raise PermissionError("知识库不存在或无权访问")
    repo = KnowledgeScopeRepository(db)
    scope = await repo.ensure_default_scope(actor_uid=str(user.uid))
    policy = {**DEFAULT_MEMBER_POLICY, **values}
    status, details = await validate_member_health(db=db, kb_id=kb_id, policy=policy)
    values = {
        **values,
        "health_status": status,
        "health_details": details,
        "last_validated_at": utc_now_naive(),
    }
    scope, member = await repo.upsert_member(
        scope_id=scope.scope_id,
        kb_id=kb_id,
        values=values,
        expected_version=expected_version,
        actor_uid=str(user.uid),
    )
    await db.commit()
    return {"scope": serialize_scope(scope), "member": serialize_member(member)}
