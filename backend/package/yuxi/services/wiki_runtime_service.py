"""Frozen Wiki navigation, always checked against current authority-source access.

No page body is evidence. All sessions used by runtime navigation are independent
read-only transactions so a failed/timeout query cannot poison retrieval audit.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from yuxi.knowledge.products.contracts import WikiNavigationHit
from yuxi.knowledge.products.registry import is_derived_product
from yuxi.services.wiki_service import (
    WikiServiceError,
    _digest,
    _enabled_source_ids,
    _entry_score,
    assert_wiki_source_access,
)
from yuxi.storage.postgres.models_business import User
from yuxi.storage.postgres.models_knowledge import KnowledgeBase, KnowledgeWiki, WikiPublication

VERSION_FIELDS = ("wiki_id", "publication_id", "manifest_hash", "snapshot_id", "wiki_source_kb_ids")
NAVIGATION_DEFAULTS = {
    "wiki_navigation_enabled": True,
    "wiki_navigation_timeout_seconds": 2.0,
    "wiki_guided_timeout_seconds": 5.0,
    "wiki_navigation_max_terms": 12,
    "wiki_navigation_max_wikis": 4,
    "wiki_navigation_top_k": 6,
}


def navigation_policy(policy: dict[str, Any] | None) -> dict[str, Any]:
    result = {**NAVIGATION_DEFAULTS, **(policy or {})}
    result["wiki_navigation_enabled"] = result["wiki_navigation_enabled"] is True
    for key, low, high in (
        ("wiki_navigation_timeout_seconds", 0.05, 5.0),
        ("wiki_guided_timeout_seconds", 0.05, 10.0),
        ("wiki_navigation_max_terms", 1, 12),
        ("wiki_navigation_max_wikis", 1, 8),
        ("wiki_navigation_top_k", 1, 20),
    ):
        try:
            value = float(result[key])
            if not low <= value <= high:
                value = min(high, max(low, value))
            result[key] = value if "seconds" in key else int(value)
        except (TypeError, ValueError, OverflowError):
            result[key] = NAVIGATION_DEFAULTS[key]
    return result


@asynccontextmanager
async def navigation_session():
    from yuxi.storage.postgres.manager import pg_manager

    async with pg_manager.get_async_session_context() as db:
        if db.get_bind().dialect.name == "postgresql":
            await db.execute(text("SET TRANSACTION READ ONLY"))
        try:
            yield db
        finally:
            await db.rollback()


async def current_source_access(db: AsyncSession, *, uid: str, tenant_id: int, kb_ids: set[str]) -> set[str]:
    """Read current user/membership/KB ACL directly, never adapter caches."""
    from yuxi.knowledge.manager import KnowledgeBaseManager
    from yuxi.services.principal import PrincipalResolutionError, resolve_tenant_id

    user = (
        await db.execute(select(User).where(User.uid == uid).execution_options(populate_existing=True))
    ).scalar_one_or_none()
    if user is None or user.is_disabled or user.is_deleted or user.deleted_at is not None:
        return set()
    try:
        if await resolve_tenant_id(db, uid) != tenant_id:
            return set()
    except PrincipalResolutionError:
        return set()
    rows = (
        await db.execute(
            select(KnowledgeBase)
            .where(
                KnowledgeBase.kb_id.in_(kb_ids),
                KnowledgeBase.tenant_id == tenant_id,
            )
            .execution_options(populate_existing=True)
        )
    ).scalars()
    identity = {"uid": uid, "role": user.role, "department_id": user.department_id, "tenant_id": tenant_id}
    return {
        row.kb_id
        for row in rows
        if not (row.additional_params or {}).get("deleted_at")
        and KnowledgeBaseManager._database_info_accessible(
            identity,
            {
                "tenant_id": row.tenant_id,
                "created_by": row.created_by,
                "share_config": row.share_config,
            },
        )
    }


async def authorized_publication(
    db: AsyncSession,
    *,
    tenant_id: int,
    member: dict[str, Any],
    permitted_source_ids: set[str],
    freeze: bool = False,
) -> tuple[KnowledgeWiki, WikiPublication]:
    """Select current only on first resolution; any supplied identity must be preserved."""
    select_current = freeze and not any(field in member for field in VERSION_FIELDS)
    wiki = (
        await db.execute(
            select(KnowledgeWiki)
            .where(
                KnowledgeWiki.kb_id == member["kb_id"],
                KnowledgeWiki.tenant_id == tenant_id,
                KnowledgeWiki.deleted_at.is_(None),
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if wiki is None or wiki.status in {"DELETED", "REVOKED", "DISABLED"}:
        raise WikiServiceError("WIKI_UNAVAILABLE")
    if not select_current and (
        any(not member.get(field) for field in VERSION_FIELDS) or member["wiki_id"] != wiki.wiki_id
    ):
        raise WikiServiceError("WIKI_VERSION_NOT_FROZEN")
    publication_id = wiki.current_publication_id if select_current else member["publication_id"]
    publication = (
        await db.execute(
            select(WikiPublication)
            .where(
                WikiPublication.publication_id == publication_id,
                WikiPublication.wiki_id == wiki.wiki_id,
                WikiPublication.tenant_id == tenant_id,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if publication is None or publication.status not in ({"ACTIVE"} if select_current else {"ACTIVE", "SUPERSEDED"}):
        raise WikiServiceError("WIKI_PUBLICATION_UNAVAILABLE")
    manifest = publication.manifest_json or {}
    if not publication.snapshot_id or publication.manifest_hash != _digest(manifest):
        raise WikiServiceError("WIKI_MANIFEST_INVALID")
    if not select_current and (
        member["manifest_hash"] != publication.manifest_hash or member["snapshot_id"] != publication.snapshot_id
    ):
        raise WikiServiceError("WIKI_VERSION_MISMATCH")
    source_ids = {str(item) for item in manifest.get("source_kb_ids") or []}
    if not select_current and (
        not isinstance(member["wiki_source_kb_ids"], list) or set(member["wiki_source_kb_ids"]) != source_ids
    ):
        raise WikiServiceError("WIKI_VERSION_MISMATCH")
    if (
        not source_ids
        or not source_ids <= permitted_source_ids
        or not source_ids <= set(await _enabled_source_ids(db, wiki.wiki_id))
    ):
        raise WikiServiceError("WIKI_SOURCE_CLOSURE_DENIED")
    if not select_current:
        # Initial resolution receives current accessible sources from the resolver.
        # Frozen scopes must recheck liveness, not trust those historical IDs.
        source_rows = (
            await db.execute(
                select(KnowledgeBase)
                .where(
                    KnowledgeBase.kb_id.in_(source_ids),
                    KnowledgeBase.tenant_id == tenant_id,
                )
                .execution_options(populate_existing=True)
            )
        ).scalars()
        live_source_ids = {
            row.kb_id
            for row in source_rows
            if not (row.additional_params or {}).get("deleted_at") and not is_derived_product(row.kb_type)
        }
        if live_source_ids != source_ids:
            raise WikiServiceError("WIKI_SOURCE_CLOSURE_DENIED")
    await assert_wiki_source_access(
        db,
        wiki_id=wiki.wiki_id,
        tenant_id=tenant_id,
        permitted_source_kb_ids=permitted_source_ids,
        publication_id=publication.publication_id,
    )
    if manifest.get("security_domain") != wiki.security_domain:
        raise WikiServiceError("WIKI_ACL_CHANGED")
    return wiki, publication


async def freeze_scope_wikis(
    db: AsyncSession, *, snapshot: dict[str, Any], require_frozen: bool = False
) -> dict[str, Any]:
    """First resolution may bind current; revalidation only removes Wiki members."""
    members = [dict(item) for item in snapshot.get("members") or []]
    raw_ids = {
        str(item["kb_id"])
        for item in members
        if not is_derived_product(str(item.get("kb_type") or "")) and item.get("enabled", True)
    }
    kept, filtered = [], list(snapshot.get("filtered_out") or [])
    for member in members:
        if not is_derived_product(str(member.get("kb_type") or "")):
            kept.append(member)
            continue
        try:
            if not member.get("wiki_navigation_enabled") or snapshot.get("tenant_id") is None:
                raise WikiServiceError("WIKI_NAVIGATION_DISABLED")
            first_resolution = not require_frozen and not any(field in member for field in VERSION_FIELDS)
            wiki, publication = await authorized_publication(
                db,
                tenant_id=int(snapshot["tenant_id"]),
                member=member,
                permitted_source_ids=raw_ids,
                freeze=first_resolution,
            )
            if first_resolution:
                member.update(
                    wiki_id=wiki.wiki_id,
                    publication_id=publication.publication_id,
                    manifest_hash=publication.manifest_hash,
                    snapshot_id=publication.snapshot_id,
                    wiki_source_kb_ids=sorted(str(item) for item in publication.manifest_json["source_kb_ids"]),
                )
            kept.append(member)
        except WikiServiceError as exc:
            filtered.append({"kb_id": member["kb_id"], "reason": str(exc)})
    return {**snapshot, "members": kept, "effective_kb_ids": [item["kb_id"] for item in kept], "filtered_out": filtered}


async def reauthorize_scope(*, snapshot: dict[str, Any], uid: str) -> dict[str, Any]:
    """Preserve versions on execution/resume; current revocation only narrows."""
    async with navigation_session() as db:
        ids = {str(item["kb_id"]) for item in snapshot.get("members") or []}
        allowed = await current_source_access(db, uid=uid, tenant_id=int(snapshot.get("tenant_id") or 0), kb_ids=ids)
        result = {
            **snapshot,
            "members": [item for item in snapshot.get("members") or [] if item["kb_id"] in allowed],
            "filtered_out": [
                *(snapshot.get("filtered_out") or []),
                *({"kb_id": kb_id, "reason": "CURRENT_ACCESS_DENIED"} for kb_id in sorted(ids - allowed)),
            ],
        }
        return await freeze_scope_wikis(db, snapshot=result, require_frozen=True)


async def navigate_frozen_scope(*, uid: str, snapshot: dict[str, Any], question: str) -> tuple[list[dict], list[dict]]:
    policy = navigation_policy(snapshot.get("retrieval_policy"))
    if not policy["wiki_navigation_enabled"]:
        return [], []
    members = snapshot.get("members") or []
    raw_ids = {str(item["kb_id"]) for item in members if not is_derived_product(str(item.get("kb_type") or ""))}
    wikis = [
        item
        for item in members
        if is_derived_product(str(item.get("kb_type") or "")) and item.get("wiki_navigation_enabled")
    ]
    hits, statuses = [], []
    async with navigation_session() as db:
        permitted = await current_source_access(
            db,
            uid=uid,
            tenant_id=int(snapshot.get("tenant_id") or 0),
            kb_ids=raw_ids | {item["kb_id"] for item in wikis},
        )
        for member in wikis[: policy["wiki_navigation_max_wikis"]]:
            status = {
                "kb_id": member["kb_id"],
                "kb_name": member.get("kb_name") or member["kb_id"],
                "source": "WIKI_NAVIGATION",
                "capability_status": "UNAVAILABLE",
                "query_status": "NOT_QUERIED",
                "hit_count": 0,
            }
            statuses.append(status)
            try:
                if member["kb_id"] not in permitted:
                    raise WikiServiceError("WIKI_ACCESS_DENIED")
                wiki, publication = await authorized_publication(
                    db, tenant_id=int(snapshot["tenant_id"]), member=member, permitted_source_ids=raw_ids & permitted
                )
                source_ids = set(publication.manifest_json["source_kb_ids"])
                entries = [
                    entry
                    for entry in publication.manifest_json.get("navigation_entries") or []
                    if isinstance(entry, dict)
                    and entry.get("source_kb_ids")
                    and set(entry["source_kb_ids"]) <= source_ids
                ]
                scored = sorted(
                    ((_entry_score(question, entry), entry) for entry in entries),
                    key=lambda pair: (-pair[0], str(pair[1].get("page_key") or "")),
                )
                for score, entry in scored[: policy["wiki_navigation_top_k"]]:
                    if score <= 0:
                        continue
                    hits.append(
                        WikiNavigationHit(
                            wiki_id=wiki.wiki_id,
                            publication_id=publication.publication_id,
                            channel="ENTITY" if entry.get("entity_id") else "PAGE_PATH",
                            entity_id=str(entry.get("entity_id") or ""),
                            entity_name=str(entry.get("title") or ""),
                            aliases=tuple(entry.get("aliases") or []),
                            expansion_terms=tuple(
                                str(term)[:180]
                                for term in (entry.get("expansion_terms") or [])[: policy["wiki_navigation_max_terms"]]
                            ),
                            page_path=(str(entry.get("title") or ""),),
                            score=score,
                        ).to_dict()
                    )
                    status["hit_count"] += 1
                status.update(
                    capability_status="AVAILABLE", query_status="SUCCESS", publication_id=publication.publication_id
                )
            except WikiServiceError as exc:
                status["error_code"] = str(exc)
    hits.sort(key=lambda item: (-item["score"], item["wiki_id"], item["entity_name"]))
    return hits[:20], statuses
