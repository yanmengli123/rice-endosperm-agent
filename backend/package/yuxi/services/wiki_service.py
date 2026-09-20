"""Dynamic LLM-Wiki control, compilation, publication and navigation services.

The implementation deliberately keeps the first compiler deterministic.  Wiki
pages are derived from the canonical PostgreSQL authority plane and may improve
recall, but their text is never returned as answer evidence.  Every published
claim has at least one currently eligible authority evidence reference.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import defaultdict
from collections.abc import Iterable
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from yuxi.knowledge.products.contracts import WikiNavigationHit
from yuxi.knowledge.products.registry import is_derived_product
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeDoclexDefinition,
    KnowledgeFile,
    KnowledgeGraphConflict,
    KnowledgeGraphEntity,
    KnowledgeGraphEntityAlias,
    KnowledgeGraphRelationEvidence,
    KnowledgeGraphTriple,
    KnowledgeScopeMember,
    KnowledgeWiki,
    WikiAuditEvent,
    WikiBuildArtifact,
    WikiBuildRun,
    WikiBuildSnapshot,
    WikiBuildSnapshotItem,
    WikiClaim,
    WikiClaimEvidence,
    WikiClaimRevision,
    WikiOutboxEvent,
    WikiPage,
    WikiPageRevision,
    WikiPublication,
    WikiPublicationIndex,
    WikiPublicationPage,
    WikiSourceBinding,
    WikiVerificationRun,
)
from yuxi.utils.datetime_utils import utc_now
from yuxi.utils.logging_config import logger

COMPILER_VERSION = "deterministic-authority-compiler/1.2"  # 1.2: 实体页定义口径并陈（R4a）
VERIFICATION_POLICY_VERSION = "authority-evidence-required/1.0"
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{1,63}|[\u4e00-\u9fff]{2,12}")
_NAVIGATION_STOP_WORDS = {
    "about",
    "after",
    "also",
    "and",
    "analysis",
    "based",
    "between",
    "during",
    "figure",
    "for",
    "from",
    "in",
    "into",
    "method",
    "of",
    "paragraph",
    "results",
    "rice",
    "scientific_pdf",
    "study",
    "table",
    "that",
    "the",
    "their",
    "these",
    "this",
    "to",
    "using",
    "were",
    "with",
}


class WikiServiceError(ValueError):
    """A safe, user-facing Wiki domain error."""


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _document_navigation_terms(file: KnowledgeFile, chunks: list[KnowledgeChunk]) -> list[str]:
    """Derive recall-only terms; no chunk text or quote leaves the authority plane."""
    scores: dict[str, int] = defaultdict(int)

    def add(value: Any, weight: int) -> None:
        text = re.sub(r"\s+", " ", str(value or "")).strip(" ._-\t\r\n")
        if not text or len(text) > 180:
            return
        folded = text.casefold()
        if folded in _NAVIGATION_STOP_WORDS:
            return
        scores[text] = max(scores[text], weight)
        for match in _WORD_RE.finditer(text):
            token = match.group(0)
            if token.casefold() in _NAVIGATION_STOP_WORDS:
                continue
            looks_like_identifier = (
                any(char.isdigit() for char in token)
                or (any(char.isupper() for char in token[1:]) and any(char.islower() for char in token))
                or (token.isupper() and len(token) <= 16)
            )
            if not looks_like_identifier and token.isascii() and len(token) < 5:
                continue
            scores[token] += max(1, weight // 10)

    filename = file.original_filename or file.filename
    add(re.sub(r"\.[A-Za-z0-9]{1,8}$", "", filename or ""), 1000)
    for chunk in chunks:
        provenance = chunk.source_provenance if isinstance(chunk.source_provenance, dict) else {}
        for section in provenance.get("section_path") or []:
            add(section, 600)
        for value in chunk.tags or []:
            add(value, 200)
        for value in chunk.ent_ids or []:
            add(value, 800)
        for match in _WORD_RE.finditer(str(chunk.content or "")):
            token = match.group(0)
            folded = token.casefold()
            looks_like_identifier = (
                any(char.isdigit() for char in token)
                or (any(char.isupper() for char in token[1:]) and any(char.islower() for char in token))
                or (token.isupper() and len(token) <= 16)
            )
            if looks_like_identifier and folded not in _NAVIGATION_STOP_WORDS:
                scores[token] += 20
    return [term for term, _ in sorted(scores.items(), key=lambda item: (-item[1], item[0].casefold(), item[0]))[:60]]


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


def _normalized_share_config(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    access_level = str(source.get("access_level") or "global").lower()
    return {
        "access_level": access_level,
        "department_ids": sorted({str(item) for item in source.get("department_ids") or []}),
        "user_uids": sorted({str(item) for item in source.get("user_uids") or []}),
    }


def _security_domain(tenant_id: int, share_config: Any) -> str:
    normalized = _normalized_share_config(share_config)
    return f"tenant:{tenant_id}:acl:{_digest(normalized)[:20]}"


def _wiki_dict(wiki: KnowledgeWiki, base: KnowledgeBase | None = None) -> dict[str, Any]:
    return {
        "wiki_id": wiki.wiki_id,
        "kb_id": wiki.kb_id,
        "tenant_id": wiki.tenant_id,
        "name": base.name if base else wiki.wiki_id,
        "description": (base.description or "") if base else "",
        "kb_type": "llmwiki",
        "trust_class": wiki.trust_class,
        "authority_class": wiki.authority_class,
        "security_domain": wiki.security_domain,
        "status": wiki.status,
        "update_mode": wiki.update_mode,
        "debounce_seconds": wiki.debounce_seconds,
        "policy": wiki.policy_json or {},
        "current_publication_id": wiki.current_publication_id,
        "last_content_snapshot_hash": wiki.last_content_snapshot_hash,
        "last_retrieval_snapshot_hash": wiki.last_retrieval_snapshot_hash,
        "created_by": wiki.created_by,
        "created_at": _iso(wiki.created_at),
        "updated_at": _iso(wiki.updated_at),
        "share_config": (base.share_config or {}) if base else {},
    }


def _build_dict(build: WikiBuildRun) -> dict[str, Any]:
    return {
        "build_id": build.build_id,
        "wiki_id": build.wiki_id,
        "snapshot_id": build.snapshot_id,
        "build_key": build.build_key,
        "compiler_fingerprint": build.compiler_fingerprint,
        "verification_policy_version": build.verification_policy_version,
        "status": build.status,
        "error_code": build.error_code,
        "error_detail": build.error_detail,
        "metrics": build.metrics_json or {},
        "created_by": build.created_by,
        "created_at": _iso(build.created_at),
        "started_at": _iso(build.started_at),
        "completed_at": _iso(build.completed_at),
    }


def _publication_dict(publication: WikiPublication) -> dict[str, Any]:
    return {
        "publication_id": publication.publication_id,
        "wiki_id": publication.wiki_id,
        "snapshot_id": publication.snapshot_id,
        "build_id": publication.build_id,
        "status": publication.status,
        "previous_publication_id": publication.previous_publication_id,
        "manifest_hash": publication.manifest_hash,
        "manifest": publication.manifest_json or {},
        "created_at": _iso(publication.created_at),
        "published_at": _iso(publication.published_at),
    }


def _audit(
    db: AsyncSession,
    *,
    wiki: KnowledgeWiki,
    event_type: str,
    actor_uid: str | None,
    payload: dict[str, Any] | None = None,
) -> None:
    db.add(
        WikiAuditEvent(
            event_id=_new_id("wae"),
            wiki_id=wiki.wiki_id,
            tenant_id=wiki.tenant_id,
            event_type=event_type,
            actor_uid=actor_uid,
            payload_json=payload or {},
        )
    )


def _outbox(
    db: AsyncSession,
    *,
    wiki: KnowledgeWiki,
    event_type: str,
    payload: dict[str, Any] | None = None,
    processed: bool = True,
    available_at: Any | None = None,
) -> None:
    now = utc_now()
    db.add(
        WikiOutboxEvent(
            event_id=_new_id("woe"),
            wiki_id=wiki.wiki_id,
            tenant_id=wiki.tenant_id,
            event_type=event_type,
            payload_json=payload or {},
            status="PROCESSED" if processed else "PENDING",
            attempts=1 if processed else 0,
            available_at=available_at or now,
            processed_at=now if processed else None,
        )
    )


async def _get_wiki(
    db: AsyncSession,
    wiki_id: str,
    tenant_id: int,
    *,
    for_update: bool = False,
) -> KnowledgeWiki:
    statement = select(KnowledgeWiki).where(
        KnowledgeWiki.wiki_id == wiki_id,
        KnowledgeWiki.tenant_id == tenant_id,
        KnowledgeWiki.deleted_at.is_(None),
    )
    if for_update:
        statement = statement.with_for_update()
    wiki = (await db.execute(statement)).scalar_one_or_none()
    if wiki is None:
        raise WikiServiceError("动态 Wiki 不存在或无权访问")
    return wiki


async def _source_rows(
    db: AsyncSession,
    *,
    source_kb_ids: Iterable[str],
    tenant_id: int,
) -> list[KnowledgeBase]:
    source_ids = list(dict.fromkeys(str(item).strip() for item in source_kb_ids if str(item).strip()))
    if not source_ids:
        raise WikiServiceError("至少选择一个权威知识源")
    rows = list(
        (
            await db.execute(
                select(KnowledgeBase).where(
                    KnowledgeBase.kb_id.in_(source_ids),
                    KnowledgeBase.tenant_id == tenant_id,
                )
            )
        ).scalars()
    )
    by_id = {row.kb_id: row for row in rows}
    missing = [source_id for source_id in source_ids if source_id not in by_id]
    if missing:
        raise WikiServiceError(f"知识源不存在、跨租户或无权访问: {', '.join(missing)}")
    derived = [row.kb_id for row in rows if is_derived_product(row.kb_type)]
    if derived:
        raise WikiServiceError(f"派生知识产品不能作为 Wiki 权威源: {', '.join(derived)}")
    return [by_id[source_id] for source_id in source_ids]


def _require_one_security_domain(rows: list[KnowledgeBase], tenant_id: int) -> tuple[str, dict[str, Any]]:
    domains = {_security_domain(tenant_id, row.share_config): row for row in rows}
    if len(domains) != 1:
        raise WikiServiceError("所选知识源的共享权限域不一致，请拆分创建 Wiki，禁止权限并集造成越权")
    domain, first = next(iter(domains.items()))
    return domain, _normalized_share_config(first.share_config)


def _source_access_allowed(
    *,
    source_ids: set[str],
    permitted_source_ids: set[str],
    expected_security_domain: str,
    actual_security_domains: dict[str, str],
) -> bool:
    """Fail closed unless every frozen source is still visible in the same ACL domain."""
    return (
        bool(source_ids)
        and source_ids.issubset(permitted_source_ids)
        and all(actual_security_domains.get(source_id) == expected_security_domain for source_id in source_ids)
    )


async def assert_wiki_source_access(
    db: AsyncSession,
    *,
    wiki_id: str,
    tenant_id: int,
    permitted_source_kb_ids: set[str],
    build_id: str | None = None,
    publication_id: str | None = None,
    use_current_publication: bool = False,
) -> KnowledgeWiki:
    """Authorize a Wiki from its authority sources, never from derived page metadata."""
    wiki = await _get_wiki(db, wiki_id, tenant_id)
    if use_current_publication and not publication_id:
        publication_id = wiki.current_publication_id
    source_ids: set[str]
    if publication_id:
        publication = (
            await db.execute(
                select(WikiPublication).where(
                    WikiPublication.publication_id == publication_id,
                    WikiPublication.wiki_id == wiki_id,
                    WikiPublication.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if publication is None:
            raise WikiServiceError("动态 Wiki 不存在或无权访问")
        source_ids = {str(item) for item in (publication.manifest_json or {}).get("source_kb_ids") or []}
    elif build_id:
        snapshot_id = (
            await db.execute(
                select(WikiBuildRun.snapshot_id).where(
                    WikiBuildRun.build_id == build_id,
                    WikiBuildRun.wiki_id == wiki_id,
                    WikiBuildRun.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if not snapshot_id:
            raise WikiServiceError("动态 Wiki 不存在或无权访问")
        source_ids = set(
            (
                await db.execute(
                    select(WikiBuildSnapshotItem.source_kb_id).where(WikiBuildSnapshotItem.snapshot_id == snapshot_id)
                )
            ).scalars()
        )
    else:
        source_ids = set(await _enabled_source_ids(db, wiki_id))

    source_rows = list(
        (
            await db.execute(
                select(KnowledgeBase).where(
                    KnowledgeBase.kb_id.in_(source_ids),
                    KnowledgeBase.tenant_id == tenant_id,
                )
            )
        ).scalars()
    )
    actual_domains = {row.kb_id: _security_domain(tenant_id, row.share_config) for row in source_rows}
    if not _source_access_allowed(
        source_ids=source_ids,
        permitted_source_ids=permitted_source_kb_ids,
        expected_security_domain=wiki.security_domain,
        actual_security_domains=actual_domains,
    ):
        raise WikiServiceError("动态 Wiki 不存在或无权访问")
    return wiki


async def create_wiki(
    db: AsyncSession,
    *,
    tenant_id: int,
    actor_uid: str,
    name: str,
    description: str,
    source_kb_ids: list[str],
    accessible_source_ids: set[str],
    update_mode: str = "MANUAL",
    debounce_seconds: int = 300,
    auto_publish: bool = False,
) -> dict[str, Any]:
    clean_name = str(name or "").strip()
    if not clean_name:
        raise WikiServiceError("Wiki 名称不能为空")
    source_ids = list(dict.fromkeys(str(item).strip() for item in source_kb_ids if str(item).strip()))
    denied = [source_id for source_id in source_ids if source_id not in accessible_source_ids]
    if denied:
        raise WikiServiceError(f"没有绑定以下知识源的权限: {', '.join(denied)}")
    sources = await _source_rows(db, source_kb_ids=source_ids, tenant_id=tenant_id)
    domain, share_config = _require_one_security_domain(sources, tenant_id)
    duplicate = (
        await db.execute(
            select(KnowledgeBase.kb_id).where(
                KnowledgeBase.tenant_id == tenant_id,
                func.lower(KnowledgeBase.name) == clean_name.lower(),
            )
        )
    ).scalar_one_or_none()
    if duplicate:
        raise WikiServiceError("同一租户内已存在同名知识产品")

    normalized_mode = str(update_mode or "MANUAL").upper()
    if normalized_mode not in {"MANUAL", "ON_SOURCE_CHANGE", "SCHEDULED"}:
        raise WikiServiceError("update_mode 仅支持 MANUAL、ON_SOURCE_CHANGE 或 SCHEDULED")
    wiki_id = _new_id("wiki")
    kb_id = _new_id("kbw")
    base = KnowledgeBase(
        kb_id=kb_id,
        tenant_id=tenant_id,
        name=clean_name,
        description=str(description or "").strip(),
        kb_type="llmwiki",
        additional_params={"product_category": "derived_product", "wiki_id": wiki_id},
        share_config=share_config,
        created_by=actor_uid,
    )
    wiki = KnowledgeWiki(
        wiki_id=wiki_id,
        kb_id=kb_id,
        tenant_id=tenant_id,
        security_domain=domain,
        update_mode=normalized_mode,
        debounce_seconds=max(30, min(int(debounce_seconds or 300), 86400)),
        policy_json={
            "auto_publish": bool(auto_publish),
            "compiler_version": COMPILER_VERSION,
            "verification_policy_version": VERIFICATION_POLICY_VERSION,
        },
        created_by=actor_uid,
    )
    db.add_all([base, wiki])
    await db.flush()
    for source in sources:
        db.add(
            WikiSourceBinding(
                binding_id=_new_id("wsb"),
                wiki_id=wiki_id,
                source_kb_id=source.kb_id,
                security_domain=domain,
                enabled=True,
                selector_json={"mode": "ALL_ACTIVE"},
                created_by=actor_uid,
            )
        )
    _audit(db, wiki=wiki, event_type="WIKI_CREATED", actor_uid=actor_uid, payload={"sources": source_ids})
    _outbox(db, wiki=wiki, event_type="WIKI_CREATED", payload={"sources": source_ids})
    await db.flush()
    return _wiki_dict(wiki, base)


async def list_wikis(
    db: AsyncSession,
    *,
    tenant_id: int,
    permitted_source_kb_ids: set[str],
) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(KnowledgeWiki, KnowledgeBase)
            .join(KnowledgeBase, KnowledgeBase.kb_id == KnowledgeWiki.kb_id)
            .where(KnowledgeWiki.tenant_id == tenant_id, KnowledgeWiki.deleted_at.is_(None))
            .order_by(KnowledgeWiki.updated_at.desc())
        )
    ).all()
    wiki_ids = [wiki.wiki_id for wiki, _ in rows]
    if not wiki_ids:
        return []
    binding_rows = (
        await db.execute(
            select(WikiSourceBinding.wiki_id, KnowledgeBase)
            .join(KnowledgeBase, KnowledgeBase.kb_id == WikiSourceBinding.source_kb_id)
            .where(
                WikiSourceBinding.wiki_id.in_(wiki_ids),
                WikiSourceBinding.enabled.is_(True),
                KnowledgeBase.tenant_id == tenant_id,
            )
        )
    ).all()
    sources_by_wiki: dict[str, set[str]] = defaultdict(set)
    domains_by_wiki: dict[str, dict[str, str]] = defaultdict(dict)
    for bound_wiki_id, source in binding_rows:
        sources_by_wiki[bound_wiki_id].add(source.kb_id)
        domains_by_wiki[bound_wiki_id][source.kb_id] = _security_domain(tenant_id, source.share_config)
    return [
        _wiki_dict(wiki, base)
        for wiki, base in rows
        if _source_access_allowed(
            source_ids=sources_by_wiki[wiki.wiki_id],
            permitted_source_ids=permitted_source_kb_ids,
            expected_security_domain=wiki.security_domain,
            actual_security_domains=domains_by_wiki[wiki.wiki_id],
        )
    ]


async def get_wiki_detail(db: AsyncSession, *, wiki_id: str, tenant_id: int) -> dict[str, Any]:
    wiki = await _get_wiki(db, wiki_id, tenant_id)
    base = (await db.execute(select(KnowledgeBase).where(KnowledgeBase.kb_id == wiki.kb_id))).scalar_one_or_none()
    bindings = list(
        (
            await db.execute(
                select(WikiSourceBinding, KnowledgeBase)
                .join(KnowledgeBase, KnowledgeBase.kb_id == WikiSourceBinding.source_kb_id)
                .where(WikiSourceBinding.wiki_id == wiki_id)
                .order_by(WikiSourceBinding.created_at)
            )
        ).all()
    )
    result = _wiki_dict(wiki, base)
    result["sources"] = [
        {
            "binding_id": binding.binding_id,
            "kb_id": source.kb_id,
            "name": source.name,
            "kb_type": source.kb_type,
            "enabled": binding.enabled,
            "security_domain": binding.security_domain,
            "selector": binding.selector_json or {},
        }
        for binding, source in bindings
    ]
    result["builds"] = [
        _build_dict(item)
        for item in (
            (
                await db.execute(
                    select(WikiBuildRun)
                    .where(WikiBuildRun.wiki_id == wiki_id)
                    .order_by(WikiBuildRun.created_at.desc())
                    .limit(20)
                )
            )
            .scalars()
            .all()
        )
    ]
    result["publications"] = [
        _publication_dict(item)
        for item in (
            (
                await db.execute(
                    select(WikiPublication)
                    .where(WikiPublication.wiki_id == wiki_id)
                    .order_by(WikiPublication.created_at.desc())
                    .limit(20)
                )
            )
            .scalars()
            .all()
        )
    ]
    result["pending_update_events"] = int(
        (
            await db.execute(
                select(func.count(WikiOutboxEvent.id)).where(
                    WikiOutboxEvent.wiki_id == wiki_id,
                    WikiOutboxEvent.status.in_(("PENDING", "PROCESSING")),
                )
            )
        ).scalar_one()
        or 0
    )
    return result


async def update_wiki_settings(
    db: AsyncSession,
    *,
    wiki_id: str,
    tenant_id: int,
    actor_uid: str,
    update_mode: str,
    debounce_seconds: int,
    auto_publish: bool,
) -> dict[str, Any]:
    wiki = await _get_wiki(db, wiki_id, tenant_id, for_update=True)
    normalized_mode = str(update_mode or "MANUAL").upper()
    if normalized_mode not in {"MANUAL", "ON_SOURCE_CHANGE", "SCHEDULED"}:
        raise WikiServiceError("update_mode 仅支持 MANUAL、ON_SOURCE_CHANGE 或 SCHEDULED")
    previous = {
        "update_mode": wiki.update_mode,
        "debounce_seconds": wiki.debounce_seconds,
        "auto_publish": bool((wiki.policy_json or {}).get("auto_publish")),
    }
    wiki.update_mode = normalized_mode
    wiki.debounce_seconds = max(30, min(int(debounce_seconds or 300), 86400))
    policy = dict(wiki.policy_json or {})
    policy.update(
        {
            "auto_publish": bool(auto_publish),
            "compiler_version": COMPILER_VERSION,
            "verification_policy_version": VERIFICATION_POLICY_VERSION,
        }
    )
    wiki.policy_json = policy
    _audit(
        db,
        wiki=wiki,
        event_type="WIKI_SETTINGS_UPDATED",
        actor_uid=actor_uid,
        payload={
            "previous": previous,
            "current": {
                "update_mode": wiki.update_mode,
                "debounce_seconds": wiki.debounce_seconds,
                "auto_publish": bool(auto_publish),
            },
        },
    )
    await db.flush()
    return await get_wiki_detail(db, wiki_id=wiki_id, tenant_id=tenant_id)


async def list_wiki_pages(
    db: AsyncSession,
    *,
    wiki_id: str,
    tenant_id: int,
    build_id: str | None = None,
) -> list[dict[str, Any]]:
    wiki = await _get_wiki(db, wiki_id, tenant_id)
    query = (
        select(WikiPage, WikiPageRevision)
        .join(WikiPageRevision, WikiPageRevision.page_id == WikiPage.page_id)
        .where(WikiPage.wiki_id == wiki_id)
    )
    if build_id:
        query = query.where(WikiPageRevision.build_id == build_id)
    elif wiki.current_publication_id:
        query = query.join(
            WikiPublicationPage,
            WikiPublicationPage.page_revision_id == WikiPageRevision.page_revision_id,
        ).where(WikiPublicationPage.publication_id == wiki.current_publication_id)
    else:
        query = query.where(WikiPage.current_revision_id == WikiPageRevision.page_revision_id)
    rows = (await db.execute(query.order_by(WikiPage.title).limit(500))).all()
    return [
        {
            "page_id": page.page_id,
            "page_revision_id": revision.page_revision_id,
            "page_key": page.page_key,
            "title": page.title,
            "status": revision.status,
            "build_id": revision.build_id,
            "content_markdown": revision.content_markdown,
            "content_sha256": revision.content_sha256,
            "created_at": _iso(revision.created_at),
        }
        for page, revision in rows
    ]


async def list_wiki_claims(
    db: AsyncSession,
    *,
    wiki_id: str,
    tenant_id: int,
    build_id: str | None = None,
) -> list[dict[str, Any]]:
    wiki = await _get_wiki(db, wiki_id, tenant_id)
    query = (
        select(WikiClaim, WikiClaimRevision)
        .join(WikiClaimRevision, WikiClaimRevision.claim_id == WikiClaim.claim_id)
        .where(WikiClaim.wiki_id == wiki_id)
    )
    if build_id:
        query = query.where(WikiClaimRevision.build_id == build_id)
    elif wiki.current_publication_id:
        publication = (
            await db.execute(
                select(WikiPublication).where(
                    WikiPublication.publication_id == wiki.current_publication_id,
                    WikiPublication.status == "ACTIVE",
                )
            )
        ).scalar_one_or_none()
        if publication is None:
            return []
        query = query.where(WikiClaimRevision.build_id == publication.build_id)
    else:
        return []
    rows = (await db.execute(query.order_by(WikiClaimRevision.created_at.desc()).limit(1000))).all()
    revision_ids = [revision.claim_revision_id for _, revision in rows]
    evidence_by_revision: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if revision_ids:
        evidence_rows = list(
            (
                await db.execute(select(WikiClaimEvidence).where(WikiClaimEvidence.claim_revision_id.in_(revision_ids)))
            ).scalars()
        )
        for evidence in evidence_rows:
            evidence_by_revision[evidence.claim_revision_id].append(
                {
                    "evidence_ref_type": evidence.evidence_ref_type,
                    "evidence_ref_id": evidence.evidence_ref_id,
                    "source_kb_id": evidence.source_kb_id,
                    "relation": evidence.relation,
                    "locator": evidence.locator_json or {},
                }
            )
    return [
        {
            "claim_id": claim.claim_id,
            "claim_revision_id": revision.claim_revision_id,
            "claim_text": revision.claim_text,
            "subject": revision.subject,
            "predicate": revision.predicate,
            "object": revision.object,
            "verification_status": revision.verification_status,
            "publication_status": revision.publication_status,
            "freshness_status": revision.freshness_status,
            "conflict_status": revision.conflict_status,
            "security_status": revision.security_status,
            "evidence": evidence_by_revision.get(revision.claim_revision_id, []),
        }
        for claim, revision in rows
    ]


async def replace_wiki_sources(
    db: AsyncSession,
    *,
    wiki_id: str,
    tenant_id: int,
    actor_uid: str,
    source_kb_ids: list[str],
    accessible_source_ids: set[str],
) -> dict[str, Any]:
    wiki = await _get_wiki(db, wiki_id, tenant_id, for_update=True)
    source_ids = list(dict.fromkeys(str(item).strip() for item in source_kb_ids if str(item).strip()))
    denied = [source_id for source_id in source_ids if source_id not in accessible_source_ids]
    if denied:
        raise WikiServiceError(f"没有绑定以下知识源的权限: {', '.join(denied)}")
    sources = await _source_rows(db, source_kb_ids=source_ids, tenant_id=tenant_id)
    domain, share_config = _require_one_security_domain(sources, tenant_id)
    if domain != wiki.security_domain:
        raise WikiServiceError("不能把 Wiki 绑定到不同权限域；请新建独立 Wiki")
    existing = {
        row.source_kb_id: row
        for row in (
            (await db.execute(select(WikiSourceBinding).where(WikiSourceBinding.wiki_id == wiki_id))).scalars().all()
        )
    }
    for row in existing.values():
        row.enabled = row.source_kb_id in source_ids
    for source in sources:
        if source.kb_id not in existing:
            db.add(
                WikiSourceBinding(
                    binding_id=_new_id("wsb"),
                    wiki_id=wiki_id,
                    source_kb_id=source.kb_id,
                    security_domain=domain,
                    enabled=True,
                    selector_json={"mode": "ALL_ACTIVE"},
                    created_by=actor_uid,
                )
            )
    base = (await db.execute(select(KnowledgeBase).where(KnowledgeBase.kb_id == wiki.kb_id))).scalar_one()
    base.share_config = share_config
    wiki.status = "SOURCE_CHANGED"
    _audit(db, wiki=wiki, event_type="WIKI_SOURCES_REPLACED", actor_uid=actor_uid, payload={"sources": source_ids})
    _outbox(
        db,
        wiki=wiki,
        event_type="WIKI_SOURCE_CHANGED",
        payload={"sources": source_ids},
        processed=wiki.update_mode != "ON_SOURCE_CHANGE",
        available_at=utc_now() + timedelta(seconds=wiki.debounce_seconds),
    )
    await db.flush()
    return await get_wiki_detail(db, wiki_id=wiki_id, tenant_id=tenant_id)


async def delete_wiki(
    db: AsyncSession,
    *,
    wiki_id: str,
    tenant_id: int,
    actor_uid: str,
) -> None:
    wiki = await _get_wiki(db, wiki_id, tenant_id, for_update=True)
    kb_id = wiki.kb_id
    _audit(db, wiki=wiki, event_type="WIKI_DELETE_REQUESTED", actor_uid=actor_uid)
    now = utc_now()
    await db.execute(update(KnowledgeScopeMember).where(KnowledgeScopeMember.kb_id == kb_id).values(enabled=False))
    await db.execute(update(WikiSourceBinding).where(WikiSourceBinding.wiki_id == wiki_id).values(enabled=False))
    base = (
        await db.execute(
            select(KnowledgeBase).where(KnowledgeBase.kb_id == kb_id, KnowledgeBase.tenant_id == tenant_id)
        )
    ).scalar_one()
    params = dict(base.additional_params or {})
    params["deleted_at"] = now.isoformat()
    base.additional_params = params
    wiki.status = "DELETED"
    wiki.deleted_at = now
    wiki.current_publication_id = None
    _audit(db, wiki=wiki, event_type="WIKI_DELETED", actor_uid=actor_uid)
    await db.flush()


async def _enabled_source_ids(db: AsyncSession, wiki_id: str) -> list[str]:
    return list(
        (
            await db.execute(
                select(WikiSourceBinding.source_kb_id)
                .where(WikiSourceBinding.wiki_id == wiki_id, WikiSourceBinding.enabled.is_(True))
                .order_by(WikiSourceBinding.source_kb_id)
            )
        ).scalars()
    )


async def _capture_snapshot(
    db: AsyncSession,
    *,
    wiki: KnowledgeWiki,
) -> tuple[WikiBuildSnapshot, list[KnowledgeFile]]:
    source_ids, files, content_manifest, retrieval_manifest = await _current_source_manifests(db, wiki=wiki)
    snapshot = WikiBuildSnapshot(
        snapshot_id=_new_id("wss"),
        wiki_id=wiki.wiki_id,
        tenant_id=wiki.tenant_id,
        content_snapshot_hash=_digest(content_manifest),
        retrieval_snapshot_hash=_digest(retrieval_manifest),
        manifest_json=content_manifest,
    )
    db.add(snapshot)
    await db.flush()
    for item in files:
        db.add(
            WikiBuildSnapshotItem(
                snapshot_id=snapshot.snapshot_id,
                source_kb_id=item.kb_id,
                file_id=item.file_id,
                source_sha256=item.content_hash,
                parse_revision_id=item.active_parse_revision_id,
                index_revision_id=item.active_index_revision_id,
                parser_semantic_version="active-revision",
            )
        )
    for source_id in source_ids:
        if not any(item.kb_id == source_id for item in files):
            db.add(
                WikiBuildSnapshotItem(
                    snapshot_id=snapshot.snapshot_id,
                    source_kb_id=source_id,
                    file_id=None,
                    parser_semantic_version="graph-only",
                )
            )
    await db.flush()
    return snapshot, files


async def _current_source_manifests(
    db: AsyncSession,
    *,
    wiki: KnowledgeWiki,
) -> tuple[list[str], list[KnowledgeFile], dict[str, Any], dict[str, Any]]:
    """Compute source fingerprints without mutating the build/release plane."""
    source_ids = await _enabled_source_ids(db, wiki.wiki_id)
    if not source_ids:
        raise WikiServiceError("Wiki 没有启用的权威知识源")
    files = list(
        (
            await db.execute(
                select(KnowledgeFile)
                .where(KnowledgeFile.kb_id.in_(source_ids), KnowledgeFile.is_folder.is_(False))
                .order_by(KnowledgeFile.kb_id, KnowledgeFile.file_id)
            )
        ).scalars()
    )
    entity_stats = (
        await db.execute(
            select(
                KnowledgeGraphEntity.kb_id,
                func.count(KnowledgeGraphEntity.id),
                func.max(KnowledgeGraphEntity.updated_at),
            )
            .where(KnowledgeGraphEntity.kb_id.in_(source_ids))
            .group_by(KnowledgeGraphEntity.kb_id)
        )
    ).all()
    triple_stats = (
        await db.execute(
            select(
                KnowledgeGraphTriple.kb_id,
                func.count(KnowledgeGraphTriple.id),
                func.max(KnowledgeGraphTriple.updated_at),
            )
            .where(KnowledgeGraphTriple.kb_id.in_(source_ids))
            .group_by(KnowledgeGraphTriple.kb_id)
        )
    ).all()
    evidence_stats = (
        await db.execute(
            select(
                KnowledgeGraphRelationEvidence.kb_id,
                func.count(KnowledgeGraphRelationEvidence.id),
                func.max(KnowledgeGraphRelationEvidence.updated_at),
            )
            .where(KnowledgeGraphRelationEvidence.kb_id.in_(source_ids))
            .group_by(KnowledgeGraphRelationEvidence.kb_id)
        )
    ).all()
    file_manifest = [
        {
            "source_kb_id": item.kb_id,
            "file_id": item.file_id,
            "source_sha256": item.content_hash,
            "parse_revision_id": item.active_parse_revision_id,
            "index_revision_id": item.active_index_revision_id,
            "status": item.status,
            "updated_at": _iso(item.updated_at),
        }
        for item in files
    ]
    alias_rows = (
        await db.execute(
            select(
                KnowledgeGraphEntityAlias.kb_id,
                KnowledgeGraphEntityAlias.entity_id,
                KnowledgeGraphEntityAlias.alias,
                KnowledgeGraphEntityAlias.normalized_alias,
            )
            .where(KnowledgeGraphEntityAlias.kb_id.in_(source_ids))
            .order_by(
                KnowledgeGraphEntityAlias.kb_id,
                KnowledgeGraphEntityAlias.entity_id,
                KnowledgeGraphEntityAlias.normalized_alias,
                KnowledgeGraphEntityAlias.alias,
            )
        )
    ).all()
    content_manifest = {
        "sources": source_ids,
        "files": file_manifest,
        "aliases": [tuple(row) for row in alias_rows],
        "entities": [(kb_id, count, _iso(updated_at)) for kb_id, count, updated_at in entity_stats],
        "triples": [(kb_id, count, _iso(updated_at)) for kb_id, count, updated_at in triple_stats],
        "evidence": [(kb_id, count, _iso(updated_at)) for kb_id, count, updated_at in evidence_stats],
    }
    retrieval_manifest = {
        "files": [
            (item["source_kb_id"], item["file_id"], item["index_revision_id"], item["status"]) for item in file_manifest
        ],
        "compiler": COMPILER_VERSION,
    }
    return source_ids, files, content_manifest, retrieval_manifest


def _graph_dependency_complete(content_manifest: dict[str, Any]) -> bool:
    """A build may only reuse/publish with full graph dependency visibility.

    The manifest freezes entity/triple/evidence presence; partial dependency
    visibility forces a full recompilation instead of a partial delta.
    """
    return bool(content_manifest.get("sources")) and not content_manifest.get("dependencies_incomplete", False)


async def _upsert_page_revision(
    db: AsyncSession,
    *,
    wiki_id: str,
    build_id: str,
    page_key: str,
    title: str,
    markdown: str,
) -> tuple[WikiPage, WikiPageRevision]:
    page = (
        await db.execute(select(WikiPage).where(WikiPage.wiki_id == wiki_id, WikiPage.page_key == page_key))
    ).scalar_one_or_none()
    if page is None:
        page = WikiPage(page_id=_new_id("wpg"), wiki_id=wiki_id, page_key=page_key, title=title)
        db.add(page)
        await db.flush()
    else:
        page.title = title
    revision = WikiPageRevision(
        page_revision_id=_new_id("wpr"),
        page_id=page.page_id,
        build_id=build_id,
        content_markdown=markdown,
        content_sha256=_digest(markdown),
        status="VERIFIED",
    )
    db.add(revision)
    await db.flush()
    page.current_revision_id = revision.page_revision_id
    return page, revision


async def _compile_build(
    db: AsyncSession,
    *,
    wiki: KnowledgeWiki,
    build: WikiBuildRun,
    files: list[KnowledgeFile],
) -> dict[str, int]:
    source_ids = await _enabled_source_ids(db, wiki.wiki_id)
    entities = list(
        (
            await db.execute(
                select(KnowledgeGraphEntity)
                .where(KnowledgeGraphEntity.kb_id.in_(source_ids))
                .order_by(KnowledgeGraphEntity.kb_id, KnowledgeGraphEntity.normalized_name)
            )
        ).scalars()
    )
    aliases = list(
        (
            await db.execute(select(KnowledgeGraphEntityAlias).where(KnowledgeGraphEntityAlias.kb_id.in_(source_ids)))
        ).scalars()
    )
    aliases_by_entity: dict[str, list[str]] = defaultdict(list)
    for alias in aliases:
        if alias.alias not in aliases_by_entity[alias.entity_id]:
            aliases_by_entity[alias.entity_id].append(alias.alias)

    source_entity = KnowledgeGraphEntity.__table__.alias("wiki_source_entity")
    target_entity = KnowledgeGraphEntity.__table__.alias("wiki_target_entity")
    relation_rows = (
        await db.execute(
            select(
                KnowledgeGraphTriple,
                KnowledgeGraphRelationEvidence,
                source_entity.c.name.label("source_name"),
                target_entity.c.name.label("target_name"),
            )
            .join(
                KnowledgeGraphRelationEvidence,
                KnowledgeGraphRelationEvidence.triple_id == KnowledgeGraphTriple.triple_id,
            )
            .join(source_entity, source_entity.c.entity_id == KnowledgeGraphTriple.source_entity_id)
            .join(target_entity, target_entity.c.entity_id == KnowledgeGraphTriple.target_entity_id)
            .where(
                KnowledgeGraphTriple.kb_id.in_(source_ids),
                KnowledgeGraphRelationEvidence.claim_eligible.is_(True),
                KnowledgeGraphRelationEvidence.evidence_alignment_status == "ALIGNED",
                func.lower(KnowledgeGraphRelationEvidence.assertion_status) != "rejected",
            )
            .order_by(KnowledgeGraphTriple.triple_id, KnowledgeGraphRelationEvidence.evidence_id)
        )
    ).all()
    relations_by_entity: dict[str, list[tuple[Any, Any, str, str]]] = defaultdict(list)
    relation_groups: dict[str, list[tuple[Any, Any, str, str]]] = defaultdict(list)
    for triple, evidence, source_name, target_name in relation_rows:
        row = (triple, evidence, str(source_name), str(target_name))
        relations_by_entity[triple.source_entity_id].append(row)
        relations_by_entity[triple.target_entity_id].append(row)
        relation_groups[triple.triple_id].append(row)

    # R4a 定义并陈：发育期实体的区间定义断言 + 未处置的定义冲突提示。
    # 页面只引用区间/文献名/definition id，不复制引文正文（派生产品不回流证据）。
    definitions_by_entity: dict[str, list[str]] = defaultdict(list)
    definition_conflict_entities: set[str] = set()
    stage_entity_names = {entity.normalized_name for entity in entities if entity.label == "DevelopmentStage"}
    if stage_entity_names:
        definition_rows = (
            await db.execute(
                select(KnowledgeDoclexDefinition, KnowledgeFile.filename, KnowledgeFile.original_filename)
                .join(KnowledgeFile, KnowledgeFile.file_id == KnowledgeDoclexDefinition.file_id)
                .where(
                    KnowledgeDoclexDefinition.kb_id.in_(source_ids),
                    KnowledgeDoclexDefinition.entity_normalized.in_(stage_entity_names),
                )
                .order_by(KnowledgeDoclexDefinition.entity_normalized, KnowledgeDoclexDefinition.file_id)
            )
        ).all()
        for definition, filename, original_filename in definition_rows:
            line = (
                f"- {definition.interval_start}–{definition.interval_end} {definition.interval_unit or ''}"
                f"（{definition.ref_event or '?'} 后）· 文献《{original_filename or filename}》"
                f"· definition `{definition.definition_id}`"
            )
            definitions_by_entity[definition.entity_normalized].append(line)
        open_definition_conflicts = (
            (
                await db.execute(
                    select(KnowledgeGraphConflict.subject_ref).where(
                        KnowledgeGraphConflict.kb_id.in_(source_ids),
                        KnowledgeGraphConflict.kind == "DEFINITION",
                        KnowledgeGraphConflict.status == "OPEN",
                        KnowledgeGraphConflict.subject_ref.in_(stage_entity_names),
                    )
                )
            )
            .scalars()
            .all()
        )
        definition_conflict_entities = set(open_definition_conflicts)

    page_revisions: dict[str, WikiPageRevision] = {}
    for entity in entities:
        entity_relations = relations_by_entity.get(entity.entity_id, [])
        alias_values = aliases_by_entity.get(entity.entity_id, [])
        lines = [
            f"# {entity.name}",
            "",
            f"- 规范实体：`{entity.canonical_identity}`",
            f"- 类型：`{entity.label}`",
            f"- 权威来源：`{entity.kb_id}`",
        ]
        if alias_values:
            lines.append(f"- 别名：{', '.join(alias_values[:30])}")
        entity_definitions = definitions_by_entity.get(entity.normalized_name, [])
        if entity_definitions:
            lines.append("")
            lines.append("## 定义口径（区间断言，跨文献并陈）")
            if entity.normalized_name in definition_conflict_entities:
                lines.append("> ⚠ 该概念在文献间存在多种互不一致的定义口径，回答时须并陈而非取其一。")
            lines.extend(entity_definitions[:10])
        lines.extend(["", "## 已验证关系导航"])
        if entity_relations:
            for triple, evidence, source_name, target_name in entity_relations[:200]:
                lines.append(
                    f"- {source_name} — `{triple.relation_type}` → {target_name} "
                    f"（authority evidence `{evidence.evidence_id}`）"
                )
        else:
            lines.append("- 当前快照没有满足发布策略的关系证据。")
        _, revision = await _upsert_page_revision(
            db,
            wiki_id=wiki.wiki_id,
            build_id=build.build_id,
            page_key=f"entity:{entity.entity_id}",
            title=entity.name,
            markdown="\n".join(lines),
        )
        page_revisions[entity.entity_id] = revision

    file_ids = [file.file_id for file in files]
    chunks_by_file: dict[str, list[KnowledgeChunk]] = defaultdict(list)
    if file_ids:
        chunk_rows = list(
            (
                await db.execute(
                    select(KnowledgeChunk)
                    .where(KnowledgeChunk.file_id.in_(file_ids))
                    .order_by(KnowledgeChunk.file_id, KnowledgeChunk.chunk_index)
                )
            )
            .scalars()
            .all()
        )
        for chunk in chunk_rows:
            chunks_by_file[chunk.file_id].append(chunk)

    document_pages = 0
    document_navigation_terms: dict[str, list[str]] = {}
    for file in files:
        file_chunks = chunks_by_file.get(file.file_id, [])
        chunk_count = len(file_chunks)
        navigation_terms = _document_navigation_terms(file, file_chunks)
        document_navigation_terms[file.file_id] = navigation_terms
        markdown = "\n".join(
            [
                f"# {file.original_filename or file.filename}",
                "",
                f"- 权威知识库：`{file.kb_id}`",
                f"- 文件 ID：`{file.file_id}`",
                f"- 内容哈希：`{file.content_hash or 'unknown'}`",
                f"- 活跃解析版本：`{file.active_parse_revision_id or 'none'}`",
                f"- 活跃索引版本：`{file.active_index_revision_id or 'none'}`",
                f"- 可导航分块数：{chunk_count}",
                f"- 导航词：{', '.join(navigation_terms[:20]) or 'none'}",
                "",
                "> 此页面是导航产物；回答必须回源到原始文献锚点或规范证据。",
            ]
        )
        await _upsert_page_revision(
            db,
            wiki_id=wiki.wiki_id,
            build_id=build.build_id,
            page_key=f"document:{file.file_id}",
            title=file.original_filename or file.filename,
            markdown=markdown,
        )
        document_pages += 1

    verified_claims = 0
    for triple_id, rows in relation_groups.items():
        triple, first_evidence, source_name, target_name = rows[0]
        page_revision = page_revisions.get(triple.source_entity_id) or page_revisions.get(triple.target_entity_id)
        if page_revision is None:
            continue
        canonical_key = _digest(
            {
                "source": source_name.casefold(),
                "predicate": triple.relation_type.casefold(),
                "target": target_name.casefold(),
            }
        )
        claim = (
            await db.execute(
                select(WikiClaim).where(
                    WikiClaim.wiki_id == wiki.wiki_id,
                    WikiClaim.canonical_claim_key == canonical_key,
                )
            )
        ).scalar_one_or_none()
        if claim is None:
            claim = WikiClaim(
                claim_id=_new_id("wcl"),
                wiki_id=wiki.wiki_id,
                canonical_claim_key=canonical_key,
            )
            db.add(claim)
            await db.flush()
        revision = WikiClaimRevision(
            claim_revision_id=_new_id("wcr"),
            claim_id=claim.claim_id,
            page_revision_id=page_revision.page_revision_id,
            build_id=build.build_id,
            claim_text=f"{source_name} {triple.relation_type} {target_name}",
            subject=source_name,
            predicate=triple.relation_type,
            object=target_name,
            scope_json={"source_kb_id": triple.kb_id, "triple_id": triple_id},
            verification_status="VERIFIED",
            publication_status="DRAFT",
            freshness_status="FRESH",
            conflict_status="NONE",
            security_status="ACTIVE",
            effective_acl_json={"security_domain": wiki.security_domain},
        )
        db.add(revision)
        await db.flush()
        for _, evidence, _, _ in rows:
            db.add(
                WikiClaimEvidence(
                    claim_revision_id=revision.claim_revision_id,
                    evidence_ref_type="GRAPH_RELATION_EVIDENCE",
                    evidence_ref_id=evidence.evidence_id,
                    source_kb_id=evidence.kb_id,
                    relation="SUPPORTS",
                    locator_json={
                        "evidence_id": evidence.evidence_id,
                        "triple_id": evidence.triple_id,
                        "pmid": evidence.pmid,
                        "doi": evidence.doi,
                        "sentence_id": evidence.sentence_id,
                    },
                    source_acl_json={"security_domain": wiki.security_domain},
                )
            )
        db.add(
            WikiVerificationRun(
                verification_id=_new_id("wvr"),
                claim_revision_id=revision.claim_revision_id,
                verifier_model="deterministic-authority-gate",
                verifier_fingerprint=_digest(COMPILER_VERSION),
                policy_version=VERIFICATION_POLICY_VERSION,
                verdict="VERIFIED",
                reason_code="AUTHORITATIVE_EVIDENCE_BOUND",
                details_json={"evidence_count": len(rows), "first_evidence_id": first_evidence.evidence_id},
            )
        )
        verified_claims += 1

    page_count = len(entities) + document_pages
    claim_revision_ids = [
        revision.claim_revision_id
        for revision in (
            (await db.execute(select(WikiClaimRevision).where(WikiClaimRevision.build_id == build.build_id))).scalars()
        )
    ]
    _, _, content_manifest_now, _ = await _current_source_manifests(db, wiki=wiki)
    artifact_payload = {
        "wiki_id": wiki.wiki_id,
        "build_id": build.build_id,
        "pages": page_count,
        "verified_claims": verified_claims,
        "document_navigation_terms": document_navigation_terms,
        "claim_revision_ids": sorted(claim_revision_ids),
        "navigation_entries": await _publication_navigation_entries(
            db,
            wiki_id=wiki.wiki_id,
            build_id=build.build_id,
            source_ids=source_ids,
            document_terms=document_navigation_terms,
        ),
        "content_snapshot_digest": _digest(content_manifest_now),
        "dependencies_complete": _graph_dependency_complete(content_manifest_now),
    }
    db.add(
        WikiBuildArtifact(
            artifact_id=_new_id("war"),
            build_id=build.build_id,
            kind="POSTGRES_CANONICAL_MANIFEST",
            object_uri=f"postgresql://wiki/{wiki.wiki_id}/builds/{build.build_id}",
            sha256=_digest(artifact_payload),
            metadata_json=artifact_payload,
        )
    )
    await db.flush()
    return {
        "source_count": len(source_ids),
        "file_count": len(files),
        "entity_page_count": len(entities),
        "document_page_count": document_pages,
        "page_count": page_count,
        "verified_claim_count": verified_claims,
        "authority_evidence_count": len(relation_rows),
        "dependencies_complete": artifact_payload["dependencies_complete"],
    }


async def build_wiki(
    db: AsyncSession,
    *,
    wiki_id: str,
    tenant_id: int,
    actor_uid: str,
    force: bool = False,
) -> dict[str, Any]:
    wiki = await _get_wiki(db, wiki_id, tenant_id, for_update=True)
    snapshot, files = await _capture_snapshot(db, wiki=wiki)
    compiler_fingerprint = _digest(
        {
            "compiler": COMPILER_VERSION,
            "policy": wiki.policy_json or {},
            "verification": VERIFICATION_POLICY_VERSION,
        }
    )
    build_key = _digest(
        {
            "wiki_id": wiki.wiki_id,
            "content_snapshot_hash": snapshot.content_snapshot_hash,
            "retrieval_snapshot_hash": snapshot.retrieval_snapshot_hash,
            "compiler_fingerprint": compiler_fingerprint,
        }
    )
    existing = (await db.execute(select(WikiBuildRun).where(WikiBuildRun.build_key == build_key))).scalar_one_or_none()
    if existing is not None and existing.status == "COMPLETED" and not force:
        await db.delete(snapshot)
        return {**_build_dict(existing), "reused": True}
    if existing is not None:
        build_key = f"{build_key}:{uuid.uuid4().hex[:12]}"

    now = utc_now()
    build = WikiBuildRun(
        build_id=_new_id("wb"),
        wiki_id=wiki.wiki_id,
        tenant_id=tenant_id,
        snapshot_id=snapshot.snapshot_id,
        build_key=build_key,
        compiler_fingerprint=compiler_fingerprint,
        verification_policy_version=VERIFICATION_POLICY_VERSION,
        status="RUNNING",
        created_by=actor_uid,
        started_at=now,
    )
    db.add(build)
    wiki.status = "BUILDING"
    await db.flush()
    try:
        async with db.begin_nested():
            metrics = await _compile_build(db, wiki=wiki, build=build, files=files)
    except Exception as exc:
        build.status = "FAILED"
        build.error_code = "WIKI_COMPILATION_FAILED"
        build.error_detail = str(exc)[:4000]
        build.completed_at = utc_now()
        wiki.status = "BUILD_FAILED"
        _audit(
            db,
            wiki=wiki,
            event_type="WIKI_BUILD_FAILED",
            actor_uid=actor_uid,
            payload={"build_id": build.build_id, "error": build.error_detail},
        )
        await db.flush()
        return {**_build_dict(build), "reused": False}
    build.status = "COMPLETED"
    build.metrics_json = metrics
    build.completed_at = utc_now()
    wiki.status = "READY_TO_PUBLISH"
    wiki.last_content_snapshot_hash = snapshot.content_snapshot_hash
    wiki.last_retrieval_snapshot_hash = snapshot.retrieval_snapshot_hash
    _audit(
        db,
        wiki=wiki,
        event_type="WIKI_BUILD_COMPLETED",
        actor_uid=actor_uid,
        payload={"build_id": build.build_id, "metrics": metrics},
    )
    _outbox(db, wiki=wiki, event_type="WIKI_BUILD_COMPLETED", payload={"build_id": build.build_id})
    await db.flush()
    result = {**_build_dict(build), "reused": False}
    if bool((wiki.policy_json or {}).get("auto_publish")):
        result["publication"] = await publish_wiki(
            db,
            wiki_id=wiki_id,
            tenant_id=tenant_id,
            actor_uid=actor_uid,
            build_id=build.build_id,
        )
    return result


def _scheduled_build_due(*, now: Any, last_completed_at: Any | None, interval_seconds: int) -> bool:
    """Pure scheduling predicate shared by the worker and unit tests."""
    if last_completed_at is None:
        return True
    return now >= last_completed_at + timedelta(seconds=max(30, int(interval_seconds or 300)))


async def _wiki_auto_build_decision(
    db: AsyncSession,
    *,
    wiki: KnowledgeWiki,
    now: Any,
) -> tuple[bool, str]:
    """Return whether a dynamic Wiki is due and a stable queue fingerprint."""
    _, _, content_manifest, retrieval_manifest = await _current_source_manifests(db, wiki=wiki)
    content_hash = _digest(content_manifest)
    retrieval_hash = _digest(retrieval_manifest)
    fingerprint = _digest(
        {
            "wiki_id": wiki.wiki_id,
            "content": content_hash,
            "retrieval": retrieval_hash,
            "compiler": COMPILER_VERSION,
        }
    )
    if wiki.update_mode == "ON_SOURCE_CHANGE":
        changed = content_hash != wiki.last_content_snapshot_hash or retrieval_hash != wiki.last_retrieval_snapshot_hash
        if not changed:
            return False, fingerprint
        pending_at = (
            await db.execute(
                select(func.min(WikiOutboxEvent.available_at)).where(
                    WikiOutboxEvent.wiki_id == wiki.wiki_id,
                    WikiOutboxEvent.event_type == "WIKI_SOURCE_CHANGED",
                    WikiOutboxEvent.status == "PENDING",
                )
            )
        ).scalar_one_or_none()
        return pending_at is None or pending_at <= now, fingerprint
    if wiki.update_mode == "SCHEDULED":
        last_completed_at = (
            await db.execute(
                select(func.max(WikiBuildRun.completed_at)).where(
                    WikiBuildRun.wiki_id == wiki.wiki_id,
                    WikiBuildRun.status == "COMPLETED",
                )
            )
        ).scalar_one_or_none()
        return (
            _scheduled_build_due(
                now=now,
                last_completed_at=last_completed_at,
                interval_seconds=wiki.debounce_seconds,
            ),
            fingerprint,
        )
    return False, fingerprint


async def process_dynamic_wiki_build(
    ctx: dict[str, Any],
    wiki_id: str,
    tenant_id: int,
    actor_uid: str,
    force: bool = False,
) -> dict[str, Any]:
    """ARQ entry point for a durable, tenant-scoped Wiki build."""
    del ctx
    try:
        from yuxi.storage.postgres.manager import pg_manager

        async with pg_manager.get_async_session_context() as db:
            result = await build_wiki(
                db,
                wiki_id=wiki_id,
                tenant_id=int(tenant_id),
                actor_uid=actor_uid,
                force=force,
            )
            if result.get("status") == "COMPLETED":
                now = utc_now()
                await db.execute(
                    update(WikiOutboxEvent)
                    .where(
                        WikiOutboxEvent.wiki_id == wiki_id,
                        WikiOutboxEvent.event_type == "WIKI_SOURCE_CHANGED",
                        WikiOutboxEvent.status.in_(("PENDING", "PROCESSING")),
                    )
                    .values(status="PROCESSED", processed_at=now, lease_owner=None, lease_expires_at=None)
                )
        if result.get("status") != "COMPLETED":
            raise WikiServiceError(result.get("error_detail") or "Wiki 自动构建失败")
        return result
    except Exception as exc:
        from yuxi.storage.postgres.manager import pg_manager

        logger.exception("Dynamic Wiki build failed: wiki_id=%s", wiki_id)
        async with pg_manager.get_async_session_context() as db:
            await db.execute(
                update(WikiOutboxEvent)
                .where(
                    WikiOutboxEvent.wiki_id == wiki_id,
                    WikiOutboxEvent.event_type == "WIKI_SOURCE_CHANGED",
                    WikiOutboxEvent.status.in_(("PENDING", "PROCESSING")),
                )
                .values(
                    status="PENDING",
                    attempts=WikiOutboxEvent.attempts + 1,
                    available_at=utc_now() + timedelta(minutes=5),
                    lease_owner=None,
                    lease_expires_at=None,
                    last_error=str(exc)[:4000],
                )
            )
        raise


async def reconcile_dynamic_wikis(ctx: dict[str, Any] | None = None) -> int:
    """Discover due source-change/scheduled builds and enqueue them idempotently."""
    del ctx
    from yuxi.services.run_queue_service import get_arq_pool
    from yuxi.storage.postgres.manager import pg_manager

    now = utc_now()
    due: list[tuple[str, int, str, str]] = []
    async with pg_manager.get_async_session_context() as db:
        wikis = list(
            (
                await db.execute(
                    select(KnowledgeWiki)
                    .where(
                        KnowledgeWiki.deleted_at.is_(None),
                        KnowledgeWiki.update_mode.in_(("ON_SOURCE_CHANGE", "SCHEDULED")),
                        KnowledgeWiki.status.notin_(("BUILDING", "DELETED")),
                    )
                    .order_by(KnowledgeWiki.updated_at)
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )
        for wiki in wikis:
            try:
                is_due, fingerprint = await _wiki_auto_build_decision(db, wiki=wiki, now=now)
            except Exception as exc:
                logger.warning("Dynamic Wiki reconciliation skipped wiki_id=%s: %s", wiki.wiki_id, exc)
                continue
            if not is_due:
                continue
            wiki.status = "UPDATE_QUEUED"
            due.append((wiki.wiki_id, int(wiki.tenant_id), wiki.created_by, fingerprint))

    if not due:
        return 0
    queue = await get_arq_pool()
    enqueued = 0
    for wiki_id, tenant_id, actor_uid, fingerprint in due:
        job = await queue.enqueue_job(
            "process_dynamic_wiki_build",
            wiki_id,
            tenant_id,
            actor_uid or "system:wiki-reconciler",
            False,
            _job_id=f"dynamic-wiki:{wiki_id}:{fingerprint[:24]}",
        )
        if job is not None:
            enqueued += 1
    return enqueued


async def _publication_navigation_entries(
    db: AsyncSession,
    *,
    wiki_id: str,
    build_id: str,
    source_ids: list[str],
    document_terms: dict[str, list[str]],
) -> list[dict[str, Any]]:
    """Capture navigation while compiling, before mutable page/source rows can change."""
    aliases = list(
        (
            await db.execute(select(KnowledgeGraphEntityAlias).where(KnowledgeGraphEntityAlias.kb_id.in_(source_ids)))
        ).scalars()
    )
    aliases_by_entity: dict[str, list[str]] = defaultdict(list)
    for alias in aliases:
        aliases_by_entity[alias.entity_id].append(alias.alias)
    pages = (
        await db.execute(
            select(WikiPage, WikiPageRevision)
            .join(WikiPageRevision, WikiPageRevision.page_id == WikiPage.page_id)
            .where(WikiPage.wiki_id == wiki_id, WikiPageRevision.build_id == build_id)
            .order_by(WikiPage.title)
        )
    ).all()
    entries = []
    for page, revision in pages:
        entity_id = page.page_key.split(":", 1)[1] if page.page_key.startswith("entity:") else ""
        entry_source_ids: list[str] = []
        expansion_terms = [page.title]
        if entity_id:
            entity = (
                await db.execute(select(KnowledgeGraphEntity).where(KnowledgeGraphEntity.entity_id == entity_id))
            ).scalar_one_or_none()
            if entity:
                entry_source_ids = [entity.kb_id]
                expansion_terms.extend(aliases_by_entity.get(entity_id, []))
        elif page.page_key.startswith("document:"):
            file_id = page.page_key.split(":", 1)[1]
            file = (
                await db.execute(select(KnowledgeFile).where(KnowledgeFile.file_id == file_id))
            ).scalar_one_or_none()
            if file:
                entry_source_ids = [file.kb_id]
                expansion_terms = [re.sub(r"\.[A-Za-z0-9]{1,8}$", "", page.title)]
                expansion_terms.extend(str(item) for item in document_terms.get(file_id) or [])
        entries.append(
            {
                "page_revision_id": revision.page_revision_id,
                "page_key": page.page_key,
                "title": page.title,
                "entity_id": entity_id,
                "aliases": sorted(set(aliases_by_entity.get(entity_id, [])))[:40],
                "expansion_terms": list(dict.fromkeys(expansion_terms))[:40],
                "source_kb_ids": entry_source_ids,
            }
        )
    return entries


async def publish_wiki(
    db: AsyncSession,
    *,
    wiki_id: str,
    tenant_id: int,
    actor_uid: str,
    build_id: str,
) -> dict[str, Any]:
    wiki = await _get_wiki(db, wiki_id, tenant_id, for_update=True)
    build = (
        await db.execute(
            select(WikiBuildRun).where(
                WikiBuildRun.build_id == build_id,
                WikiBuildRun.wiki_id == wiki_id,
                WikiBuildRun.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if build is None or build.status != "COMPLETED" or not build.snapshot_id:
        raise WikiServiceError("只有已完成且拥有不可变快照的构建可以发布")
    revisions = list(
        (await db.execute(select(WikiClaimRevision).where(WikiClaimRevision.build_id == build_id))).scalars()
    )
    revision_ids = [item.claim_revision_id for item in revisions]
    evidence_counts: dict[str, int] = {}
    if revision_ids:
        evidence_counts = {
            claim_revision_id: int(count)
            for claim_revision_id, count in (
                await db.execute(
                    select(WikiClaimEvidence.claim_revision_id, func.count(WikiClaimEvidence.id))
                    .where(WikiClaimEvidence.claim_revision_id.in_(revision_ids))
                    .group_by(WikiClaimEvidence.claim_revision_id)
                )
            ).all()
        }
    invalid = [
        item.claim_revision_id
        for item in revisions
        if item.verification_status != "VERIFIED"
        or item.security_status != "ACTIVE"
        or evidence_counts.get(item.claim_revision_id, 0) < 1
    ]
    if invalid:
        raise WikiServiceError("发布门禁失败：存在未验证、已撤权或没有权威 Evidence 的 Claim")

    snapshot = (
        await db.execute(
            select(WikiBuildSnapshot).where(
                WikiBuildSnapshot.snapshot_id == build.snapshot_id,
                WikiBuildSnapshot.wiki_id == wiki_id,
                WikiBuildSnapshot.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if snapshot is None:
        raise WikiServiceError("构建输入快照不存在，拒绝发布不可复现的产物")
    source_ids = [str(item) for item in (snapshot.manifest_json or {}).get("sources") or []]
    current_sources = await _source_rows(db, source_kb_ids=source_ids, tenant_id=tenant_id)
    current_domain, _ = _require_one_security_domain(current_sources, tenant_id)
    if current_domain != wiki.security_domain:
        raise WikiServiceError("权威知识源权限已变化，必须重新构建后才能发布")
    artifact = (
        await db.execute(
            select(WikiBuildArtifact).where(
                WikiBuildArtifact.build_id == build_id,
                WikiBuildArtifact.kind == "POSTGRES_CANONICAL_MANIFEST",
            )
        )
    ).scalar_one_or_none()
    if artifact is None:
        raise WikiServiceError("构建缺少编译清单，拒绝发布不可追溯的产物")
    _, _, live_manifest, _ = await _current_source_manifests(db, wiki=wiki)
    if _digest(live_manifest) != artifact.metadata_json.get("content_snapshot_digest"):
        raise WikiServiceError("权威知识源在构建后已变化（别名/证据/实体/文件指纹不一致），必须重新构建后才能发布")
    evidence_ref_rows = (
        await db.execute(
            select(WikiClaimEvidence.evidence_ref_id).where(
                WikiClaimEvidence.claim_revision_id.in_([item.claim_revision_id for item in revisions])
            )
        )
    ).scalars()
    live_evidence_ids = set(
        (
            await db.execute(
                select(KnowledgeGraphRelationEvidence.evidence_id).where(
                    KnowledgeGraphRelationEvidence.kb_id.in_(source_ids),
                    KnowledgeGraphRelationEvidence.claim_eligible.is_(True),
                    KnowledgeGraphRelationEvidence.evidence_alignment_status == "ALIGNED",
                    func.lower(KnowledgeGraphRelationEvidence.assertion_status) != "rejected",
                )
            )
        ).scalars()
    )
    missing = {str(item) for item in evidence_ref_rows} - live_evidence_ids
    if missing:
        raise WikiServiceError("发布门禁失败：构建所绑定的权威 Evidence 已被撤下或不再合格")

    entries = artifact.metadata_json.get("navigation_entries")
    frozen_claim_ids = artifact.metadata_json.get("claim_revision_ids")
    page_ids = set(
        (
            await db.execute(select(WikiPageRevision.page_revision_id).where(WikiPageRevision.build_id == build_id))
        ).scalars()
    )
    if (
        entries is None
        or frozen_claim_ids != sorted(revision_ids)
        or len(entries) != len(page_ids)
        or {entry["page_revision_id"] for entry in entries} != page_ids
        or not artifact.metadata_json.get("dependencies_complete")
    ):
        raise WikiServiceError("编译清单的页面或 Claim 集合不完整，必须重新构建后才能发布")
    duplicate = (
        await db.execute(
            select(WikiPublication).where(
                WikiPublication.wiki_id == wiki_id,
                WikiPublication.build_id == build_id,
                WikiPublication.status == "ACTIVE",
            )
        )
    ).scalar_one_or_none()
    if duplicate:
        return {**_publication_dict(duplicate), "reused": True}
    manifest = {
        "schema_version": "wiki-publication/1.0",
        "wiki_id": wiki_id,
        "build_id": build_id,
        "snapshot_id": build.snapshot_id,
        "source_kb_ids": source_ids,
        "security_domain": wiki.security_domain,
        "compiler_fingerprint": build.compiler_fingerprint,
        "verification_policy_version": build.verification_policy_version,
        "verified_claim_count": len(revisions),
        "claim_revision_ids": sorted(revision.claim_revision_id for revision in revisions),
        "content_snapshot_digest": artifact.metadata_json.get("content_snapshot_digest"),
        "navigation_entries": entries,
    }
    now = utc_now()
    publication = WikiPublication(
        publication_id=_new_id("wpub"),
        wiki_id=wiki_id,
        tenant_id=tenant_id,
        snapshot_id=build.snapshot_id,
        build_id=build_id,
        status="ACTIVE",
        previous_publication_id=wiki.current_publication_id,
        manifest_hash=_digest(manifest),
        manifest_json=manifest,
        published_at=now,
    )
    if wiki.current_publication_id:
        current = (
            await db.execute(
                select(WikiPublication).where(WikiPublication.publication_id == wiki.current_publication_id)
            )
        ).scalar_one_or_none()
        if current:
            current.status = "SUPERSEDED"
    db.add(publication)
    await db.flush()
    page_revision_ids = [entry["page_revision_id"] for entry in entries]
    for page_revision_id in page_revision_ids:
        db.add(WikiPublicationPage(publication_id=publication.publication_id, page_revision_id=page_revision_id))
    db.add(
        WikiPublicationIndex(
            publication_id=publication.publication_id,
            index_kind="LEXICAL_NAVIGATION_V1",
            index_ref=f"postgresql://wiki_publications/{publication.publication_id}/manifest",
            status="READY",
            metadata_json={"entry_count": len(entries)},
        )
    )
    for revision in revisions:
        revision.publication_status = "PUBLISHED"
    if page_revision_ids:
        page_revisions = list(
            (
                await db.execute(
                    select(WikiPageRevision).where(WikiPageRevision.page_revision_id.in_(page_revision_ids))
                )
            ).scalars()
        )
        for revision in page_revisions:
            revision.status = "PUBLISHED"
    wiki.current_publication_id = publication.publication_id
    wiki.status = "PUBLISHED"
    _audit(
        db,
        wiki=wiki,
        event_type="WIKI_PUBLISHED",
        actor_uid=actor_uid,
        payload={"publication_id": publication.publication_id, "build_id": build_id},
    )
    _outbox(
        db,
        wiki=wiki,
        event_type="WIKI_PUBLISHED",
        payload={"publication_id": publication.publication_id},
    )
    await db.flush()
    return {**_publication_dict(publication), "reused": False}


async def rollback_wiki(
    db: AsyncSession,
    *,
    wiki_id: str,
    tenant_id: int,
    actor_uid: str,
    publication_id: str,
) -> dict[str, Any]:
    wiki = await _get_wiki(db, wiki_id, tenant_id, for_update=True)
    target = (
        await db.execute(
            select(WikiPublication).where(
                WikiPublication.publication_id == publication_id,
                WikiPublication.wiki_id == wiki_id,
                WikiPublication.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if target is None:
        raise WikiServiceError("目标发布版本不存在")
    if wiki.current_publication_id == target.publication_id:
        return {**_publication_dict(target), "reused": True}
    previous_id = wiki.current_publication_id
    if previous_id:
        current = (
            await db.execute(select(WikiPublication).where(WikiPublication.publication_id == previous_id))
        ).scalar_one_or_none()
        if current:
            current.status = "SUPERSEDED"
    target.status = "ACTIVE"
    wiki.current_publication_id = target.publication_id
    wiki.status = "PUBLISHED"
    _audit(
        db,
        wiki=wiki,
        event_type="WIKI_ROLLED_BACK",
        actor_uid=actor_uid,
        payload={"from": previous_id, "to": target.publication_id},
    )
    _outbox(
        db,
        wiki=wiki,
        event_type="WIKI_ROLLED_BACK",
        payload={"publication_id": target.publication_id},
    )
    await db.flush()
    return {**_publication_dict(target), "reused": False}


def _query_terms(text: str) -> list[str]:
    return list(dict.fromkeys(match.group(0).casefold() for match in _WORD_RE.finditer(str(text or ""))))[:64]


def _entry_score(question: str, entry: dict[str, Any]) -> float:
    query = str(question or "").casefold()
    terms = _query_terms(question)
    values = [
        str(entry.get("title") or ""),
        *[str(item) for item in entry.get("aliases") or []],
        *[str(item) for item in entry.get("expansion_terms") or []],
    ]
    haystack = " ".join(values).casefold()
    exact = any(value.casefold() in query for value in values if len(value.strip()) >= 2)
    matches = sum(1 for term in terms if term in haystack)
    return min(1.0, (0.55 if exact else 0.0) + (matches / max(1, len(terms))) * 0.45)


async def preflight_wiki(
    db: AsyncSession,
    *,
    tenant_id: int,
    source_kb_ids: list[str],
    accessible_source_ids: set[str],
) -> dict[str, Any]:
    """Read-only creation preflight: source ACL, readiness and expected shape.

    Reports per-source blocking reasons without creating anything. PDF-only
    sources are warned about: the deterministic compiler never derives claims
    from PDF text, so a PDF-only Wiki would have navigation pages with zero
    VERIFIED claims — this is an evidence-safety property, not an error.
    """
    checked: list[dict[str, Any]] = []
    blocking: list[str] = []
    for kb_id in list(dict.fromkeys(str(item).strip() for item in source_kb_ids if str(item).strip())):
        if kb_id not in accessible_source_ids:
            checked.append({"kb_id": kb_id, "accessible": False, "blocking_reasons": ["SOURCE_NOT_ACCESSIBLE"]})
            blocking.append(f"SOURCE_NOT_ACCESSIBLE:{kb_id}")
            continue
        row = (
            await db.execute(
                select(KnowledgeBase).where(KnowledgeBase.kb_id == kb_id, KnowledgeBase.tenant_id == tenant_id)
            )
        ).scalar_one_or_none()
        if row is None:
            checked.append({"kb_id": kb_id, "accessible": False, "blocking_reasons": ["SOURCE_NOT_FOUND"]})
            blocking.append(f"SOURCE_NOT_FOUND:{kb_id}")
            continue
        reasons: list[str] = []
        warnings: list[str] = []
        if is_derived_product(row.kb_type):
            reasons.append("DERIVED_PRODUCT_FORBIDDEN")
        triple_count = int(
            (
                await db.execute(select(func.count(KnowledgeGraphTriple.id)).where(KnowledgeGraphTriple.kb_id == kb_id))
            ).scalar_one()
            or 0
        )
        if triple_count == 0:
            warnings.append("PDF_NAVIGATION_ONLY_NO_CLAIMS")
        checked.append({"kb_id": kb_id, "accessible": True, "blocking_reasons": reasons, "warnings": warnings})
        blocking.extend(f"{reason}:{kb_id}" for reason in reasons)
    return {"can_create": not blocking, "blocking_reasons": blocking, "sources": checked}


async def get_build_diff(
    db: AsyncSession,
    *,
    wiki_id: str,
    tenant_id: int,
    build_id: str,
) -> dict[str, Any]:
    """Differential audit between a completed build and the live authority plane."""
    build = (
        await db.execute(
            select(WikiBuildRun).where(
                WikiBuildRun.build_id == build_id,
                WikiBuildRun.wiki_id == wiki_id,
                WikiBuildRun.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if build is None:
        raise WikiServiceError("动态 Wiki 构建不存在")
    wiki = await _get_wiki(db, wiki_id, tenant_id)
    artifact = (
        await db.execute(
            select(WikiBuildArtifact).where(
                WikiBuildArtifact.build_id == build_id,
                WikiBuildArtifact.kind == "POSTGRES_CANONICAL_MANIFEST",
            )
        )
    ).scalar_one_or_none()
    frozen_digest = (artifact.metadata_json or {}).get("content_snapshot_digest") if artifact else None
    live_digest = _digest((await _current_source_manifests(db, wiki=wiki))[2])
    drift = frozen_digest != live_digest
    changed_sources = await _changed_source_kinds(db, wiki=wiki)
    return {
        "build_id": build_id,
        "status": build.status,
        "input_consistent": not drift,
        "frozen_content_snapshot_digest": frozen_digest,
        "live_content_snapshot_digest": live_digest,
        "changed_sources": changed_sources,
    }


async def _changed_source_kinds(db: AsyncSession, *, wiki: KnowledgeWiki) -> dict[str, list[str]]:
    source_ids = await _enabled_source_ids(db, wiki.wiki_id)
    if not source_ids:
        return {}
    current_files = {
        (item.kb_id, item.file_id, item.content_hash, item.active_parse_revision_id)
        for item in (
            (
                await db.execute(
                    select(KnowledgeFile).where(KnowledgeFile.kb_id.in_(source_ids), KnowledgeFile.is_folder.is_(False))
                )
            )
            .scalars()
            .all()
        )
    }
    entity_rows = (
        await db.execute(
            select(KnowledgeGraphEntity.kb_id, KnowledgeGraphEntity.entity_id).where(
                KnowledgeGraphEntity.kb_id.in_(source_ids)
            )
        )
    ).all()
    alias_rows = (
        await db.execute(
            select(KnowledgeGraphEntityAlias.kb_id, KnowledgeGraphEntityAlias.normalized_alias).where(
                KnowledgeGraphEntityAlias.kb_id.in_(source_ids)
            )
        )
    ).all()
    evidence_rows = (
        await db.execute(
            select(KnowledgeGraphRelationEvidence.kb_id, KnowledgeGraphRelationEvidence.evidence_id).where(
                KnowledgeGraphRelationEvidence.kb_id.in_(source_ids),
                KnowledgeGraphRelationEvidence.claim_eligible.is_(True),
            )
        )
    ).all()
    snapshot_items = list(
        (await db.execute(select(WikiBuildSnapshotItem).where(WikiBuildSnapshotItem.snapshot_id.is_(None)))).scalars()
    )
    del snapshot_items  # live-vs-frozen drift is reported via digest comparison
    return {
        "entities": sorted({kb_id for kb_id, _ in entity_rows}),
        "aliases": sorted({kb_id for kb_id, _ in alias_rows}),
        "evidence": sorted({kb_id for kb_id, _ in evidence_rows}),
        "files": sorted({kb_id for kb_id, _, _, _ in current_files}),
    }


async def list_wiki_audit(
    db: AsyncSession,
    *,
    wiki_id: str,
    tenant_id: int,
    limit: int = 100,
) -> list[dict[str, Any]]:
    await _get_wiki(db, wiki_id, tenant_id)
    rows = (
        await db.execute(
            select(WikiAuditEvent)
            .where(WikiAuditEvent.wiki_id == wiki_id, WikiAuditEvent.tenant_id == tenant_id)
            .order_by(WikiAuditEvent.created_at.desc(), WikiAuditEvent.id.desc())
            .limit(max(1, min(int(limit or 100), 500)))
        )
    ).scalars()
    return [
        {
            "event_id": item.event_id,
            "event_type": item.event_type,
            "actor_uid": item.actor_uid,
            "payload": item.payload_json or {},
            "created_at": _iso(item.created_at),
        }
        for item in rows
    ]


async def navigate_wiki(
    db: AsyncSession,
    *,
    wiki_id: str,
    tenant_id: int,
    question: str,
    permitted_source_kb_ids: set[str],
    limit: int = 8,
) -> list[WikiNavigationHit]:
    wiki = await assert_wiki_source_access(
        db,
        wiki_id=wiki_id,
        tenant_id=tenant_id,
        permitted_source_kb_ids=permitted_source_kb_ids,
        use_current_publication=True,
    )
    if not wiki.current_publication_id:
        return []
    publication = (
        await db.execute(
            select(WikiPublication).where(
                WikiPublication.publication_id == wiki.current_publication_id,
                WikiPublication.status == "ACTIVE",
            )
        )
    ).scalar_one_or_none()
    if publication is None:
        return []
    manifest = publication.manifest_json or {}
    if manifest.get("security_domain") != wiki.security_domain:
        return []
    publication_source_ids = {str(item) for item in manifest.get("source_kb_ids") or []}
    enabled_source_ids = set(await _enabled_source_ids(db, wiki_id))
    if not publication_source_ids or not publication_source_ids.issubset(enabled_source_ids):
        return []
    source_rows = list(
        (
            await db.execute(
                select(KnowledgeBase).where(
                    KnowledgeBase.kb_id.in_(publication_source_ids),
                    KnowledgeBase.tenant_id == tenant_id,
                )
            )
        ).scalars()
    )
    if len(source_rows) != len(publication_source_ids):
        return []
    if any(_security_domain(tenant_id, row.share_config) != wiki.security_domain for row in source_rows):
        return []
    scored: list[tuple[float, dict[str, Any]]] = []
    for entry in manifest.get("navigation_entries") or []:
        if not isinstance(entry, dict):
            continue
        source_ids = {str(item) for item in entry.get("source_kb_ids") or []}
        if not source_ids or not source_ids.issubset(permitted_source_kb_ids):
            continue
        score = _entry_score(question, entry)
        if score > 0:
            scored.append((score, entry))
    scored.sort(key=lambda item: (-item[0], str(item[1].get("page_key") or "")))
    hits: list[WikiNavigationHit] = []
    for score, entry in scored[: max(1, min(int(limit or 8), 20))]:
        aliases = tuple(str(item) for item in entry.get("aliases") or [])
        expansions = tuple(str(item) for item in entry.get("expansion_terms") or [])
        hits.append(
            WikiNavigationHit(
                wiki_id=wiki_id,
                publication_id=publication.publication_id,
                channel="ENTITY" if entry.get("entity_id") else "PAGE_PATH",
                entity_id=str(entry.get("entity_id") or ""),
                entity_name=str(entry.get("title") or ""),
                aliases=aliases,
                expansion_terms=expansions,
                page_path=(str(entry.get("title") or ""),),
                score=score,
            )
        )
    return hits


async def navigate_scope_wikis(
    db: AsyncSession,
    *,
    tenant_id: int,
    question: str,
    wiki_members: list[dict[str, Any]],
    raw_members: list[dict[str, Any]],
    limit_per_wiki: int = 6,
) -> tuple[list[WikiNavigationHit], list[dict[str, Any]]]:
    permitted_source_ids = {str(item.get("kb_id")) for item in raw_members if item.get("kb_id")}
    hits: list[WikiNavigationHit] = []
    statuses: list[dict[str, Any]] = []
    for member in wiki_members:
        kb_id = str(member.get("kb_id") or "")
        wiki = (
            await db.execute(
                select(KnowledgeWiki).where(
                    KnowledgeWiki.kb_id == kb_id,
                    KnowledgeWiki.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if wiki is None:
            statuses.append(
                {
                    "kb_id": kb_id,
                    "kb_name": member.get("kb_name") or kb_id,
                    "source": "WIKI_NAVIGATION",
                    "capability_status": "UNAVAILABLE",
                    "query_status": "NOT_QUERIED",
                    "hit_count": 0,
                }
            )
            continue
        try:
            wiki_hits = await navigate_wiki(
                db,
                wiki_id=wiki.wiki_id,
                tenant_id=tenant_id,
                question=question,
                permitted_source_kb_ids=permitted_source_ids,
                limit=limit_per_wiki,
            )
        except WikiServiceError:
            # 来源撤权或范围缩窄只关闭该 Wiki，不阻断已授权的原始检索。
            statuses.append(
                {
                    "kb_id": kb_id,
                    "kb_name": member.get("kb_name") or kb_id,
                    "source": "WIKI_NAVIGATION",
                    "capability_status": "UNAVAILABLE",
                    "query_status": "NOT_QUERIED",
                    "hit_count": 0,
                    "error_code": "WIKI_NAVIGATION_UNAVAILABLE",
                }
            )
            continue
        hits.extend(wiki_hits)
        statuses.append(
            {
                "kb_id": kb_id,
                "kb_name": member.get("kb_name") or kb_id,
                "source": "WIKI_NAVIGATION",
                "capability_status": "AVAILABLE" if wiki.current_publication_id else "UNAVAILABLE",
                "query_status": "SUCCESS" if wiki.current_publication_id else "NOT_QUERIED",
                "hit_count": len(wiki_hits),
                "publication_id": wiki.current_publication_id,
            }
        )
    hits.sort(key=lambda item: (-item.score, item.wiki_id, item.entity_name))
    return hits[:20], statuses
