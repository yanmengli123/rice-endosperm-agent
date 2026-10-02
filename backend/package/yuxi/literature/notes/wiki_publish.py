"""Publish compiled literature notes into the existing Dynamic Wiki plane."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from sqlalchemy import select

from yuxi.literature.reading.service import RENDER_BUCKET
from yuxi.literature.provenance import current_job
from yuxi.services.wiki_service import activate_derived_navigation_publication
from yuxi.storage.minio import get_minio_client
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeBase,
    KnowledgeFile,
    KnowledgeParseRevision,
    KnowledgeWiki,
    LiteratureCompileJob,
    LiteraturePaper,
    WikiBuildArtifact,
    WikiBuildRun,
    WikiBuildSnapshot,
    WikiPage,
    WikiPageRevision,
    WikiPublication,
    WikiSourceBinding,
)
from yuxi.utils.datetime_utils import utc_now

COMPILER_FINGERPRINT = "literature-notes-wiki-v1"


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[: min(48, 63 - len(prefix))]}"


def _digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _title_from_markdown(content: str, fallback: str) -> str:
    match = re.search(r"^#\s+(.+)$", content, flags=re.MULTILINE)
    return match.group(1).strip() if match else fallback


async def publish_literature_notes(
    tenant_id: int, *, actor_uid: str, source_kb_id: str | None = None
) -> dict[str, Any]:
    """Rebuild and atomically activate the tenant's claim-free note wiki."""
    if source_kb_id is None:
        async with pg_manager.get_async_session_context() as session:
            source_ids = list(
                (
                    await session.execute(
                        select(KnowledgeParseRevision.kb_id)
                        .join(LiteraturePaper, LiteraturePaper.source_revision_id == KnowledgeParseRevision.revision_id)
                        .join(LiteratureCompileJob, LiteratureCompileJob.paper_id == LiteraturePaper.id)
                        .where(
                            LiteratureCompileJob.tenant_id == tenant_id,
                            current_job(LiteratureCompileJob),
                            LiteratureCompileJob.status == "SUCCEEDED",
                        )
                        .distinct()
                    )
                ).scalars()
            )
        results = [
            await publish_literature_notes(tenant_id, actor_uid=actor_uid, source_kb_id=source_id)
            for source_id in source_ids
        ]
        if len(results) == 1:
            return results[0]
        return {
            "published": any(r.get("published") for r in results),
            "reused": all(r.get("reused") for r in results),
            "publications": results,
        }
    async with pg_manager.get_async_session_context() as session:
        jobs = list(
            (
                await session.execute(
                    select(LiteratureCompileJob, LiteraturePaper)
                    .join(LiteraturePaper, LiteraturePaper.id == LiteratureCompileJob.paper_id)
                    .join(
                        KnowledgeParseRevision, KnowledgeParseRevision.revision_id == LiteraturePaper.source_revision_id
                    )
                    .join(KnowledgeFile, KnowledgeFile.active_parse_revision_id == KnowledgeParseRevision.revision_id)
                    .where(
                        LiteratureCompileJob.tenant_id == tenant_id,
                        LiteratureCompileJob.status == "SUCCEEDED",
                        LiteratureCompileJob.note_uri.is_not(None),
                        current_job(LiteratureCompileJob),
                        KnowledgeParseRevision.kb_id == source_kb_id,
                    )
                    .order_by(
                        LiteraturePaper.rid,
                        LiteratureCompileJob.level,
                        LiteratureCompileJob.updated_at.desc(),
                        LiteratureCompileJob.id.desc(),
                    )
                )
            ).all()
        )
    if not jobs:
        return {"published": False, "reason": "no_compiled_notes"}
    # Current generated and imported notes can coexist; publish one page per level.
    latest = {}
    for job, paper in jobs:
        latest.setdefault((paper.rid, job.level), (job, paper))
    jobs = list(latest.values())

    minio = get_minio_client()
    pages: list[dict[str, Any]] = []
    source_revision_ids = sorted({str(paper.source_revision_id) for _, paper in jobs if paper.source_revision_id})
    concepts_by_rid: dict[str, list[str]] = {}
    for _job, paper in jobs:
        if paper.rid in concepts_by_rid:
            continue
        try:
            raw = await minio.adownload_file(
                RENDER_BUCKET,
                str(_job.note_uri).removeprefix("kbasset://").rsplit("/", 1)[0] + "/L1_data.json",
            )
            data = json.loads(raw.decode("utf-8"))
            concepts_by_rid[paper.rid] = [
                str(item.get("name") if isinstance(item, dict) else item).strip()
                for item in [*(data.get("concepts") or []), *(data.get("tags") or [])]
                if str(item.get("name") if isinstance(item, dict) else item).strip()
            ]
        except Exception:  # noqa: BLE001 - historical imported notes have no L1_data sidecar
            concepts_by_rid[paper.rid] = []
    for job, paper in jobs:
        content = (await minio.adownload_file(RENDER_BUCKET, str(job.note_uri).removeprefix("kbasset://"))).decode(
            "utf-8"
        )
        pages.append(
            {
                "page_key": f"literature:{paper.rid}:{job.level}",
                "title": _title_from_markdown(content, f"{paper.title or paper.rid} · {job.level}"),
                "content": content,
                "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "rid": paper.rid,
                "level": job.level,
                "source_revision_id": paper.source_revision_id,
                "terms": list(
                    dict.fromkeys(
                        [paper.title or "", paper.rid, paper.doi or "", job.level, *concepts_by_rid[paper.rid]]
                    )
                )[:40],
            }
        )
    frozen = [
        {"rid": p.rid, "source_revision_id": p.source_revision_id, "reading_version": p.reading_version}
        for _, p in jobs
    ]

    source_kb_ids: list[str] = []
    async with pg_manager.get_async_session_context() as session:
        current = (
            (
                await session.execute(
                    select(LiteraturePaper).where(LiteraturePaper.id.in_([p.id for _, p in jobs])).with_for_update()
                )
            )
            .scalars()
            .all()
        )
        if {(p.rid, p.source_revision_id, p.reading_version) for p in current} != {
            (v["rid"], v["source_revision_id"], v["reading_version"]) for v in frozen
        }:
            return {"published": False, "reason": "source_changed"}
        active_files = list(
            (
                await session.execute(
                    select(KnowledgeFile)
                    .where(KnowledgeFile.active_parse_revision_id.in_(source_revision_ids))
                    .with_for_update()
                )
            ).scalars()
        )
        if {file.active_parse_revision_id for file in active_files} != set(source_revision_ids):
            return {"published": False, "reason": "source_changed"}
        if source_revision_ids:
            source_kb_ids = sorted(
                set(
                    (
                        await session.execute(
                            select(KnowledgeParseRevision.kb_id).where(
                                KnowledgeParseRevision.revision_id.in_(source_revision_ids)
                            )
                        )
                    ).scalars()
                )
            )
        source = (
            await session.execute(
                select(KnowledgeBase).where(KnowledgeBase.kb_id == source_kb_id, KnowledgeBase.tenant_id == tenant_id)
            )
        ).scalar_one()
        if source.governance_status in {"ARCHIVED", "DELETED"}:
            return {"published": False, "reason": "source_inactive"}
        from yuxi.services.wiki_service import _security_domain

        security_domain = _security_domain(tenant_id, source.share_config)
        content_digest = _digest(
            {
                "pages": [{"page_key": p["page_key"], "sha256": p["content_sha256"]} for p in pages],
                "inputs": frozen,
                "security_domain": security_domain,
            }
        )
        suffix = hashlib.sha256(source_kb_id.encode()).hexdigest()[:12]
        kb_id = f"literature-notes-{tenant_id}-{suffix}"
        wiki_id = f"literature-wiki-{tenant_id}-{suffix}"
        kb = (await session.execute(select(KnowledgeBase).where(KnowledgeBase.kb_id == kb_id))).scalar_one_or_none()
        if kb is None:
            kb = KnowledgeBase(
                tenant_id=tenant_id,
                kb_id=kb_id,
                name="文献编译笔记",
                description="L1/L2/L3 编译派生产品；仅用于导航，不进入证据通道。",
                kb_type="literature_notes",
                governance_status="ACTIVE",
                normalized_name=f"literature-notes-{tenant_id}-{suffix}",
                created_by=source.created_by,
                share_config=source.share_config,
            )
            session.add(kb)
        wiki = (
            await session.execute(select(KnowledgeWiki).where(KnowledgeWiki.wiki_id == wiki_id))
        ).scalar_one_or_none()
        if wiki is None:
            wiki = KnowledgeWiki(
                wiki_id=wiki_id,
                kb_id=kb_id,
                tenant_id=tenant_id,
                security_domain=security_domain,
                trust_class="DERIVED",
                authority_class="NAVIGATION_ONLY",
                status="DRAFT",
                update_mode="MANUAL",
                policy_json={"product_type": "literature_notes", "claim_free": True},
                created_by=actor_uid,
            )
            session.add(wiki)
            await session.flush()
        else:
            wiki.security_domain = security_domain
        kb.share_config = source.share_config
        kb.created_by = source.created_by
        for source_kb_id in source_kb_ids:
            existing_binding = (
                await session.execute(
                    select(WikiSourceBinding).where(
                        WikiSourceBinding.wiki_id == wiki_id,
                        WikiSourceBinding.source_kb_id == source_kb_id,
                    )
                )
            ).scalar_one_or_none()
            if existing_binding is None:
                session.add(
                    WikiSourceBinding(
                        binding_id=_stable_id("wbind", wiki_id, source_kb_id),
                        wiki_id=wiki_id,
                        source_kb_id=source_kb_id,
                        security_domain=wiki.security_domain,
                        selector_json={"product_type": "literature_notes"},
                        created_by=actor_uid,
                    )
                )
            else:
                existing_binding.security_domain = security_domain

        build_id = _stable_id("wbuild", wiki_id, content_digest)
        existing_build = (
            await session.execute(select(WikiBuildRun).where(WikiBuildRun.build_id == build_id))
        ).scalar_one_or_none()
        if existing_build is not None:
            current = None
            if wiki.current_publication_id:
                current = (
                    await session.execute(
                        select(WikiPublication).where(WikiPublication.publication_id == wiki.current_publication_id)
                    )
                ).scalar_one_or_none()
            if current is not None and current.build_id == build_id and current.status == "ACTIVE":
                return {
                    "published": True,
                    "reused": True,
                    "wiki_id": wiki_id,
                    "build_id": build_id,
                    "publication_id": current.publication_id,
                }
            snapshot = (
                await session.execute(
                    select(WikiBuildSnapshot).where(WikiBuildSnapshot.snapshot_id == existing_build.snapshot_id)
                )
            ).scalar_one()
            artifact = (
                await session.execute(
                    select(WikiBuildArtifact).where(
                        WikiBuildArtifact.build_id == build_id,
                        WikiBuildArtifact.kind == "POSTGRES_CANONICAL_MANIFEST",
                    )
                )
            ).scalar_one()
            entries = list((artifact.metadata_json or {}).get("navigation_entries") or [])
            for entry in entries:
                page = (
                    await session.execute(
                        select(WikiPage)
                        .join(WikiPageRevision, WikiPageRevision.page_id == WikiPage.page_id)
                        .where(WikiPageRevision.page_revision_id == entry["page_revision_id"])
                    )
                ).scalar_one()
                page.current_revision_id = entry["page_revision_id"]
            publication = await activate_derived_navigation_publication(
                session,
                wiki=wiki,
                build=existing_build,
                snapshot=snapshot,
                actor_uid=actor_uid,
                source_ids=source_kb_ids,
                entries=entries,
                claim_revisions=[],
                content_snapshot_digest=content_digest,
            )
            await session.commit()
            return {
                "published": True,
                "reused": True,
                "wiki_id": wiki_id,
                "build_id": build_id,
                "publication_id": publication["publication_id"],
                "page_count": len(entries),
            }
        snapshot_id = _stable_id("wsnap", wiki_id, content_digest)
        manifest = {
            "product_type": "literature_notes",
            "sources": source_kb_ids,
            "source_revision_ids": source_revision_ids,
            "source_versions": frozen,
            "pages": [{"page_key": page["page_key"], "sha256": page["content_sha256"]} for page in pages],
        }
        snapshot = WikiBuildSnapshot(
            snapshot_id=snapshot_id,
            wiki_id=wiki_id,
            tenant_id=tenant_id,
            content_snapshot_hash=content_digest,
            retrieval_snapshot_hash=_digest(source_revision_ids),
            manifest_json=manifest,
        )
        build = WikiBuildRun(
            build_id=build_id,
            wiki_id=wiki_id,
            tenant_id=tenant_id,
            snapshot_id=snapshot_id,
            build_key=f"{wiki_id}:{content_digest}",
            compiler_fingerprint=COMPILER_FINGERPRINT,
            verification_policy_version="claim-free-derived-v1",
            status="COMPLETED",
            metrics_json={"page_count": len(pages), "claim_count": 0},
            created_by=actor_uid,
            started_at=utc_now(),
            completed_at=utc_now(),
        )
        session.add_all([snapshot, build])
        await session.flush()

        entries: list[dict[str, Any]] = []
        for page_data in pages:
            page_id = _stable_id("wpage", wiki_id, page_data["page_key"])
            page = (await session.execute(select(WikiPage).where(WikiPage.page_id == page_id))).scalar_one_or_none()
            if page is None:
                page = WikiPage(
                    page_id=page_id,
                    wiki_id=wiki_id,
                    page_key=page_data["page_key"],
                    title=page_data["title"],
                )
                session.add(page)
            else:
                page.title = page_data["title"]
            revision_id = _stable_id("wprev", build_id, page_data["page_key"], page_data["content_sha256"])
            revision = WikiPageRevision(
                page_revision_id=revision_id,
                page_id=page_id,
                build_id=build_id,
                content_markdown=page_data["content"],
                content_sha256=page_data["content_sha256"],
                status="DRAFT",
            )
            session.add(revision)
            page.current_revision_id = revision_id
            entries.append(
                {
                    "page_revision_id": revision_id,
                    "page_key": page_data["page_key"],
                    "title": page_data["title"],
                    "entity_id": "",
                    "aliases": [],
                    "expansion_terms": [term for term in page_data["terms"] if term],
                    "source_kb_ids": source_kb_ids,
                }
            )
        session.add(
            WikiBuildArtifact(
                artifact_id=_stable_id("wart", build_id, "manifest"),
                build_id=build_id,
                kind="POSTGRES_CANONICAL_MANIFEST",
                object_uri=f"postgresql://wiki_builds/{build_id}/manifest",
                sha256=_digest(manifest),
                metadata_json={
                    "product_type": "literature_notes",
                    "content_snapshot_digest": content_digest,
                    "navigation_entries": entries,
                    "claim_revision_ids": [],
                    "dependencies_complete": True,
                },
            )
        )
        publication = await activate_derived_navigation_publication(
            session,
            wiki=wiki,
            build=build,
            snapshot=snapshot,
            actor_uid=actor_uid,
            source_ids=source_kb_ids,
            entries=entries,
            claim_revisions=[],
            content_snapshot_digest=content_digest,
        )
        await session.commit()
        return {
            "published": True,
            "wiki_id": wiki_id,
            "build_id": build_id,
            "publication_id": publication["publication_id"],
            "page_count": len(entries),
            "reused": publication.get("reused", False),
        }


async def reconcile_literature_note_wikis(ctx: dict | None = None) -> int:
    """Cron reconciliation for compile/import paths that could not publish inline."""
    del ctx
    async with pg_manager.get_async_session_context() as session:
        tenants = list(
            (
                await session.execute(
                    select(LiteratureCompileJob.tenant_id).where(LiteratureCompileJob.status == "SUCCEEDED").distinct()
                )
            ).scalars()
        )
    published = 0
    for tenant_id in tenants:
        result = await publish_literature_notes(int(tenant_id), actor_uid="system:literature-wiki-reconciler")
        published += int(bool(result.get("published") and not result.get("reused")))
    return published
